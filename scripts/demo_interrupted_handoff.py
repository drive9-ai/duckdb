#!/usr/bin/env python3
"""Disconnect writer A, let B take over, reconnect A and verify stale COMMIT fails.

Real DuckDB/extension, TiDB and RustFS processes. Only A's network routes are
cut by socket relays; A is never paused/killed before its attempted stale write.
No session expiry, lock or database rows are edited to force takeover.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid

import boto3
from botocore.config import Config
import pymysql

from benchmark_handoff import ROOT, Worker, digest, quote
from partition_proxy import Partition, PartitionProxy
from test_native_live import port, wait


def fingerprint(lines):
    return hashlib.sha256(('\n'.join(lines) + '\n').encode()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--tidb-server', required=True)
    p.add_argument('--fs-server', required=True)
    p.add_argument('--fs-source', required=True, help='Checkout used to build the guarded server')
    p.add_argument('--rustfs-server', required=True)
    p.add_argument('--duckdb', default=shutil.which('duckdb'))
    p.add_argument('--extension', default=str(ROOT / 'build/tidbfs.duckdb_extension'))
    p.add_argument('--rows', type=int, default=100000)
    p.add_argument('--maintenance-interval', default='1s')
    p.add_argument('--takeover-timeout', type=float, default=180)
    p.add_argument('--poll-interval', type=float, default=1)
    p.add_argument('--log-dir', required=True)
    args = p.parse_args()
    args.threads, args.memory_limit = 4, '2GB'
    for name in ('tidb_server', 'fs_server', 'fs_source', 'rustfs_server', 'duckdb', 'extension'):
        setattr(args, name, str(Path(getattr(args, name)).resolve()))
    logs = Path(args.log_dir).resolve()
    logs.mkdir(parents=True, exist_ok=True)
    if (logs / 'results.json').exists():
        raise RuntimeError('Choose a fresh log directory to preserve previous evidence')
    processes, handles, workers, proxies = [], [], [], []
    origin = time.monotonic()
    report = dict(started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  platform=platform.platform(), machine=platform.machine(),
                  duckdb_version=subprocess.check_output([args.duckdb, '-version'], text=True).strip(),
                  plugin_source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                  fs_source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=args.fs_source, text=True).strip(),
                  binary_sha256={n: digest(getattr(args, n)) for n in ('duckdb', 'extension', 'fs_server', 'tidb_server', 'rustfs_server')},
                  harness_sha256=digest(__file__), proxy_sha256=digest(ROOT / 'scripts/partition_proxy.py'),
                  backend='Single-host loopback; TiDB unistore + RustFS S3',
                  fault='Socket shutdown and connection rejection on A metadata and S3 routes; B uses independent routes',
                  maintenance_interval=args.maintenance_interval, poll_interval_seconds=args.poll_interval,
                  initial_rows=args.rows, events=[], attempts=[], complete=False)

    def save():
        (logs / 'results.json').write_text(json.dumps(report, indent=2) + '\n')

    def event(name, **fields):
        row = dict(event=name, elapsed_seconds=time.monotonic() - origin, **fields)
        report['events'].append(row)
        save()
        print(json.dumps(row), flush=True)

    def launch(command, name, env):
        out = (logs / (name + '.log')).open('w')
        handles.append(out)
        child = subprocess.Popen(command, stdout=out, stderr=out, env=env)
        processes.append(child)
        return child

    def worker(name, endpoint):
        child = Worker(args, endpoint, token, logs, name)
        workers.append(child)
        return child

    with tempfile.TemporaryDirectory(prefix='runtime-', dir=logs) as tmp:
        try:
            env = {k: v for k, v in os.environ.items() if not k.startswith(('DRIVE9_', 'RUSTFS_', 'MINIO_', 'AWS_', 'OTEL_'))}
            env['TZ'] = 'UTC'
            sql_port, status_port, fs_port, s3_port = port(), port(), port(), port()
            launch([args.tidb_server, '--store=unistore', '--path=' + tmp + '/tidb', '--host=127.0.0.1',
                    '-P=' + str(sql_port), '--status=' + str(status_port), '--status-host=127.0.0.1', '-L=error'], 'tidb', env)
            db = wait(lambda: pymysql.connect(host='127.0.0.1', port=sql_port, user='root'))
            with db.cursor() as cursor:
                cursor.execute('CREATE DATABASE interrupted_handoff')
            db.close()
            access, secret = secrets.token_hex(12).upper(), secrets.token_hex(24)
            Path(tmp, 'objects').mkdir()
            launch([args.rustfs_server, 'server', tmp + '/objects', '--address', f'127.0.0.1:{s3_port}'], 'rustfs',
                   dict(env, RUSTFS_ACCESS_KEY=access, RUSTFS_SECRET_KEY=secret,
                        RUSTFS_CONSOLE_ENABLE='false', RUSTFS_REGION='us-east-1'))
            objects = f'http://127.0.0.1:{s3_port}'

            def s3_client(endpoint):
                return boto3.client('s3', endpoint_url=endpoint, region_name='us-east-1',
                                    aws_access_key_id=access, aws_secret_access_key=secret,
                                    config=Config(connect_timeout=2, read_timeout=3, retries={'max_attempts': 0},
                                                  s3={'addressing_style': 'path'}))

            s3 = s3_client(objects)
            wait(s3.list_buckets)
            s3.create_bucket(Bucket='interrupted-handoff')
            env.update(DRIVE9_TENANT_PROVIDER='local', DRIVE9_TENANT_MAINTENANCE_INTERVAL=args.maintenance_interval,
                       DRIVE9_LISTEN_ADDR=f'127.0.0.1:{fs_port}',
                       DRIVE9_LOCAL_DSN=f'root@tcp(127.0.0.1:{sql_port})/interrupted_handoff?parseTime=true',
                       DRIVE9_TOKEN_SIGNING_KEY=secrets.token_hex(32), DRIVE9_MASTER_KEY=secrets.token_hex(32),
                       DRIVE9_S3_BUCKET='interrupted-handoff', DRIVE9_S3_ENDPOINT=objects,
                       DRIVE9_S3_REGION='us-east-1', DRIVE9_S3_FORCE_PATH_STYLE='true',
                       DRIVE9_S3_ACCESS_KEY_ID=access, DRIVE9_S3_SECRET_ACCESS_KEY=secret,
                       DRIVE9_EXTENT_ALLOW_STATIC_CREDENTIALS='1')
            launch([args.fs_server], 'fs', env)
            endpoint = f'http://127.0.0.1:{fs_port}'

            def get(url, method='GET', auth=False):
                headers = {'Authorization': 'Bearer ' + token} if auth else {}
                with urllib.request.urlopen(urllib.request.Request(url, method=method, headers=headers), timeout=5) as response:
                    return dict(response.headers), response.read()

            wait(lambda: get(endpoint + '/healthz'))
            _, provisioned = get(endpoint + '/v1/provision', 'POST')
            token = json.loads(provisioned)['api_key']
            wait(lambda: get(endpoint + '/v1/fs/', 'HEAD', True))
            partition = Partition()
            obj_proxy = PartitionProxy(objects, partition, lane='objects')
            proxies.append(obj_proxy)
            meta_proxy = PartitionProxy(endpoint, partition, lane='metadata', object_endpoint=obj_proxy.url)
            proxies.append(meta_proxy)
            old_s3 = s3_client(obj_proxy.url)
            uri_path = '/interrupted-' + uuid.uuid4().hex + '.duckdb'
            attach = f'ATTACH {quote("tidbfs+db://" + uri_path)} AS remote; USE remote'
            readonly = f'ATTACH {quote("tidbfs+db://" + uri_path)} AS remote (READ_ONLY); USE remote'
            scan = 'SELECT id, payload FROM ledger ORDER BY id'

            def values(i):
                return f"({i}, md5('{i}'))"

            def expected(count):
                return fingerprint([f'{i},{hashlib.md5(str(i).encode()).hexdigest()}' for i in range(count)])

            def verify(w, count):
                seconds, rows = w.execute(scan)
                result = dict(rows=len(rows), sha256=fingerprint(rows), expected_sha256=expected(count), scan_seconds=seconds)
                if result['rows'] != count or result['sha256'] != result['expected_sha256']:
                    raise AssertionError('Full ordered-row checksum mismatch')
                return result

            a = worker('writer-a', meta_proxy.url)
            a.execute(attach)
            a.execute('CREATE TABLE ledger(id BIGINT PRIMARY KEY, payload VARCHAR); '
                      f'INSERT INTO ledger SELECT i, md5(i::VARCHAR) FROM range({args.rows}) t(i); CHECKPOINT remote')
            a.execute('INSERT INTO ledger VALUES ' + values(args.rows))
            report['before_partition'] = verify(a, args.rows + 1)
            headers, _ = get(endpoint + '/v1/fs' + uri_path + '.wal', 'HEAD', True)
            report['wal_bytes_before_partition'] = int(headers['Content-Length'])
            assert report['wal_bytes_before_partition'] > 0, 'Need a real committed WAL to recover'
            a.execute('BEGIN; INSERT INTO ledger VALUES ' + values(args.rows + 2))
            registered = [e for e in partition.snapshot() if e.get('op') == 'new_session' and e.get('errno') == 0]
            assert registered and all(e.get('session_guard') for e in registered), 'Guarded server required'
            report['session_guard_advertised'] = True
            assert any(e['lane'] == 'objects' and e['status'] == 200 for e in partition.snapshot() if e['kind'] == 'response')
            event('a_ready', committed_rows=args.rows + 1, uncommitted_id=args.rows + 2,
                  wal_bytes=report['wal_bytes_before_partition'])

            cut = partition.cut()
            event('network_cut', writer_a_alive=a.process.poll() is None)
            # Independent probes verify BOTH routes are actually unavailable.
            for lane, probe in [('metadata', lambda: get(meta_proxy.url + '/healthz')),
                                ('objects', lambda: old_s3.head_bucket(Bucket='interrupted-handoff'))]:
                try:
                    probe()
                except Exception as error:
                    event('route_blocked', lane=lane, error_type=type(error).__name__)
                else:
                    raise AssertionError(lane + ' route was not disconnected')
            _, alive = a.execute('SELECT 42')
            assert alive == ['42'], 'A must remain running during partition'

            while True:
                start = time.monotonic()
                b = worker('writer-b-attempt-' + str(len(report['attempts']) + 1), endpoint)
                try:
                    opened, _ = b.execute(attach, timeout=45)
                except RuntimeError as error:
                    b.stop()
                    if 'locked' not in str(error).lower():
                        raise
                    report['attempts'].append(dict(start_seconds=start - cut,
                                                   end_seconds=time.monotonic() - cut, result='locked'))
                    save()
                    if time.monotonic() - cut > args.takeover_timeout:
                        raise TimeoutError('Takeover exceeded configured limit')
                    time.sleep(args.poll_interval)
                else:
                    acquired = time.monotonic()
                    report['attempts'].append(dict(start_seconds=start - cut, end_seconds=acquired - cut, result='acquired'))
                    report['takeover_seconds'] = acquired - cut
                    report['successful_attach_seconds'] = opened
                    event('b_acquired', seconds_since_cut=acquired - cut, attempts=len(report['attempts']))
                    break
            assert any(x['result'] == 'locked' for x in report['attempts']), 'Initial exclusion was not exercised'
            report['b_recovered'] = verify(b, args.rows + 1)
            event('wal_recovered', **report['b_recovered'])
            b.execute('INSERT INTO ledger VALUES ' + values(args.rows + 1))
            report['b_before_reconnect'] = verify(b, args.rows + 2)
            event('b_committed', rows=args.rows + 2)
            assert a.process.poll() is None, 'A exited before reconnection'
            restored = partition.restore()
            # Successful probes prove reachability returned before trying A again.
            get(meta_proxy.url + '/healthz')
            old_s3.head_bucket(Bucket='interrupted-handoff')
            event('network_restored', seconds_since_cut=restored - cut, both_routes_reachable=True)
            started = time.monotonic()
            try:
                a.execute('INSERT INTO ledger VALUES ' + values(args.rows + 3) + '; COMMIT', timeout=45)
            except RuntimeError as error:
                if 'session failed or expired' not in str(error):
                    raise
                report['stale_commit_error'] = 'extent session failed or expired; close and reopen, do not replay writes'
                report['stale_commit_rejection_seconds'] = time.monotonic() - started
                a.process.wait(timeout=10)
                assert a.process.returncode != 0
                event('a_stale_commit_rejected', seconds=report['stale_commit_rejection_seconds'],
                      process_exit_code=a.process.returncode, error=report['stale_commit_error'])
            else:
                raise AssertionError('Stale writer committed after takeover')
            report['b_after_reconnect'] = verify(b, args.rows + 2)
            b.execute('CHECKPOINT remote; USE memory; DETACH remote')
            b.close()
            reader = worker('fresh-reader', endpoint)
            reader.execute(readonly)
            report['fresh_reader'] = verify(reader, args.rows + 2)
            _, forbidden = reader.execute(f'SELECT count(*) FROM ledger WHERE id IN ({args.rows + 2}, {args.rows + 3})')
            assert forbidden == ['0'], 'Uncommitted/stale rows leaked'
            reader.execute('USE memory; DETACH remote')
            reader.close()
            event('fresh_reader_verified', **report['fresh_reader'], forbidden_rows=0)
            report['proxy_events'] = [dict({k: v for k, v in e.items() if k != 'monotonic'},
                                           seconds_since_cut=e['monotonic'] - cut) for e in partition.snapshot()]
            report['complete'] = True
            report['completed_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            event('passed')
        except BaseException as error:
            report['failure_type'] = type(error).__name__
            save()
            raise
        finally:
            for child in reversed(workers):
                child.stop()
            for proxy in reversed(proxies):
                proxy.close()
            for child in reversed(processes):
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
            for out in handles:
                out.close()


if __name__ == '__main__':
    main()
