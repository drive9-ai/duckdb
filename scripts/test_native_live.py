#!/usr/bin/env python3
"""Run native database tests against disposable TiDB and an updated fs binary.

Requires pymysql, DuckDB 1.4.2, tidb-server and the fs
server. No existing services, databases or filesystem credentials are used.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.request
import uuid

import pymysql
from build_extent import TAGS
from fault_proxy import FaultProxy

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def wait(check, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return check()
        except Exception:
            time.sleep(0.25)
    raise RuntimeError('Disposable service did not become ready; inspect build/native-live logs')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tidb-server', required=True)
    parser.add_argument('--fs-server', required=True)
    parser.add_argument('--fs-source', help='Also run relevant Go tests in this fs checkout')
    parser.add_argument('--skip-restart', action='store_true')
    parser.add_argument('--log-dir', default=str(ROOT / 'build/native-live'))
    parser.add_argument('--minio-server', help='Use a disposable MinIO S3 backend and enable object fault tests (requires boto3)')
    parser.add_argument('tests', nargs='*', help='Optional unittest names from native_smoke.py')
    args = parser.parse_args()
    processes, logs, proxies = [], [], []
    log_dir = Path(args.log_dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    def launch(command, name, env=None):
        log = open(log_dir / (name + '.log'), 'w')
        logs.append(log)
        child = subprocess.Popen(command, env=env, stdout=log, stderr=log)
        processes.append(child)
        return child

    with tempfile.TemporaryDirectory(prefix='tidbfs-live-') as tmp:
        sql_port, status_port, fs_port = port(), port(), port()
        try:
            launch([str(Path(args.tidb_server).resolve()), '--store=unistore', '--path=' + tmp + '/tidb',
                    '--host=127.0.0.1', '-P=' + str(sql_port), '--status=' + str(status_port),
                    '--status-host=127.0.0.1', '-L=error'], 'tidb', dict(os.environ, TZ='UTC'))
            db = wait(lambda: pymysql.connect(host='127.0.0.1', port=sql_port, user='root'))
            with db.cursor() as cursor:
                cursor.execute('CREATE DATABASE tidbfs_live')
                cursor.execute('CREATE DATABASE tidbfs_tests')
            db.close()
            print('Disposable TiDB ready', flush=True)
            # Never inherit a production DRIVE9 configuration into this harness.
            env = {k: v for k, v in os.environ.items() if not k.startswith('DRIVE9_')}
            env.update(TZ='UTC', DRIVE9_TENANT_PROVIDER='local',
                       DRIVE9_TENANT_MAINTENANCE_INTERVAL='1s',
                       DRIVE9_LISTEN_ADDR='127.0.0.1:' + str(fs_port),
                       DRIVE9_LOCAL_DSN=f'root@tcp(127.0.0.1:{sql_port})/tidbfs_live?parseTime=true',
                       DRIVE9_S3_DIR=tmp + '/objects', DRIVE9_TOKEN_SIGNING_KEY=secrets.token_hex(32),
                       DRIVE9_MASTER_KEY=secrets.token_hex(32))
            if args.minio_server:
                import boto3
                minio_port = port()
                access, secret = secrets.token_hex(12), secrets.token_hex(24)
                minio_env = dict(env, MINIO_ROOT_USER=access, MINIO_ROOT_PASSWORD=secret,
                                 MINIO_BROWSER='off')
                launch([str(Path(args.minio_server).resolve()), 'server', tmp + '/minio',
                        '--address', f'127.0.0.1:{minio_port}'], 'minio', minio_env)
                minio_url = f'http://127.0.0.1:{minio_port}'
                def minio_ready():
                    with urllib.request.urlopen(minio_url + '/minio/health/live', timeout=2) as response:
                        return response.read()
                wait(minio_ready)
                s3 = boto3.client('s3', endpoint_url=minio_url, region_name='us-east-1',
                                  aws_access_key_id=access, aws_secret_access_key=secret)
                s3.create_bucket(Bucket='tidbfs-test')
                proxy = FaultProxy(minio_url, objects=True)
                proxies.append(proxy)
                env.update(DRIVE9_S3_BUCKET='tidbfs-test', DRIVE9_S3_ENDPOINT=proxy.url,
                           DRIVE9_S3_REGION='us-east-1', DRIVE9_S3_FORCE_PATH_STYLE='true',
                           DRIVE9_S3_ACCESS_KEY_ID=access, DRIVE9_S3_SECRET_ACCESS_KEY=secret,
                           DRIVE9_EXTENT_ALLOW_STATIC_CREDENTIALS='1',
                           TIDBFS_FAULT_PROXY=proxy.url, TIDBFS_FAULT_TOKEN=proxy.token)
                print('Disposable MinIO S3 backend ready', flush=True)
            server_command = [str(Path(args.fs_server).resolve())]
            server = launch(server_command, 'server', env)
            endpoint = f'http://127.0.0.1:{fs_port}'

            def health():
                with urllib.request.urlopen(endpoint + '/healthz', timeout=2) as response:
                    return response.read()

            wait(health)
            with urllib.request.urlopen(urllib.request.Request(endpoint + '/v1/provision', method='POST'), timeout=90) as response:
                token = json.load(response)['api_key']

            def ready():
                request = urllib.request.Request(endpoint + '/v1/fs/', method='HEAD',
                                                  headers={'Authorization': 'Bearer ' + token})
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.read()

            wait(ready)
            if args.fs_source:
                test_env = env.copy()
                test_env['DRIVE9_TEST_TIDB_DSN'] = f'root@tcp(127.0.0.1:{sql_port})/tidbfs_tests?parseTime=true'
                with open(log_dir / 'go-tests.log', 'w') as output:
                    subprocess.run(['go', 'test', './pkg/datastore', './pkg/backend', './pkg/server', './pkg/tenant/schema',
                                    './cmd/drive9-server', '-p=1', '-count=1', '-run',
                                    'TestExtent|TestFileRead|TestStatForRead|TestReadPlan|TestAppendLog|Test.*IfRevision|Test.*RevisionCAS|TestSharedTiDBSchemaStatementsContainsAllTables|TestCurrentSharedTiDBSchemaVersionIsDerivedFromSharedStatements|Test.*DumpInitSQL'],
                                   cwd=args.fs_source, env=test_env, stdout=output, stderr=output, check=True)
                print('Go storage, isolation, protocol, range and schema regressions passed', flush=True)
            env.update(DRIVE9_BASE=endpoint, DRIVE9_API_KEY=token)
            subprocess.run(['go', 'test', '-mod=readonly', '-tags', TAGS, '-count=1', '-v', '.'],
                           cwd=ROOT / 'bridge', env=dict(env, CGO_ENABLED='1'), check=True)
            subprocess.run(['python3', 'scripts/native_smoke.py'] + args.tests, cwd=ROOT, env=env, check=True)
            if not args.skip_restart:
                # Acknowledge a transaction without checkpointing, kill both the
                # writer and fs server, then restart fs over the same TiDB state.
                def quote(value):
                    return "'" + str(value).replace("'", "''") + "'"
                init = (f"LOAD {quote(ROOT / 'build/tidbfs.duckdb_extension')}; "
                        f"CREATE SECRET fs (TYPE tidbfs, ENDPOINT {quote(endpoint)}, TOKEN {quote(token)}); ")
                uri = 'tidbfs+db:///restart-' + uuid.uuid4().hex + '.duckdb'
                attach = f'ATTACH {quote(uri)} AS remote; USE remote; '
                marker = Path(tmp) / 'committed.csv'
                child = subprocess.Popen([os.environ.get('DUCKDB', 'duckdb'), '-unsigned', '-bail'],
                                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                processes.append(child)
                child.stdin.write(init + attach + "SET checkpoint_threshold='1GB'; CREATE TABLE durable AS SELECT 123456 n; "
                                  + f"COPY (SELECT 1) TO {quote(marker)};\n")
                child.stdin.flush()

                def committed():
                    if child.poll() is not None:
                        raise RuntimeError('Writer exited before its durable marker')
                    return marker.read_bytes()

                wait(committed)
                child.kill()
                child.wait(timeout=15)
                child.stdin.close()
                child.stderr.close()
                server.kill()
                server.wait(timeout=15)
                server = launch(server_command, 'server-restarted', env)
                wait(health)
                wait(ready)

                def recover():
                    result = subprocess.run([os.environ.get('DUCKDB', 'duckdb'), '-unsigned', '-bail', '-csv', '-noheader'],
                        input=init + attach + 'SELECT n FROM durable; CHECKPOINT;', text=True, capture_output=True, timeout=60)
                    if result.returncode:
                        raise RuntimeError('Waiting for crashed extent session to expire')
                    if not result.stdout.strip().endswith('123456'):
                        raise AssertionError('Committed value did not survive server restart')
                    return True

                wait(recover, timeout=150)
                print('Client + fs SIGKILL, server restart and WAL recovery passed', flush=True)
        finally:
            for child in reversed(processes):
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
            for log in logs:
                log.close()
            for proxy in proxies:
                proxy.close()


if __name__ == '__main__':
    main()
