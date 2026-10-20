#!/usr/bin/env python3
"""Measure clean DuckDB handoffs on disposable TiDB + RustFS, with real S3 I/O.

No existing service, bucket, database or credential is used. Client-cold means
a new process; storage/OS caches are deliberately not purged. Every query scans
all eight uncompressed columns and validates count plus column checksums.
"""
import argparse
import csv
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import secrets
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

import boto3
from botocore.config import Config
import pymysql

from test_native_live import port, wait

ROOT = Path(__file__).resolve().parents[1]
SCAN = 'SELECT count(*), sum(id), ' + ', '.join(f'sum(v{i})' for i in range(1, 8)) + ' FROM samples'


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def projection(start, rows):
    columns = ['i::BIGINT AS id'] + [f'hash(i + {j * 100000003})::UBIGINT AS v{j}' for j in range(1, 8)]
    return 'SELECT ' + ', '.join(columns) + f' FROM range({start}, {start + rows}) t(i)'


def aggregate(rows):
    # Store 128-bit sums as decimal strings in JSON, avoiding float conversion.
    parsed = list(csv.reader(rows))
    if len(parsed) != 1 or len(parsed[0]) != 9:
        raise AssertionError('Unexpected aggregate output: ' + repr(rows))
    return [int(value) for value in parsed[0]]


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


class Worker:
    def __init__(self, args, endpoint, token, logs, name):
        self.token = token
        self.log_path = logs / (name + '.stderr.log')
        self.log = self.log_path.open('w')
        self.lines = queue.Queue()
        started = time.perf_counter()
        self.process = subprocess.Popen([args.duckdb, '-unsigned', '-bail', '-csv', '-noheader'],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.log, text=True, bufsize=1,
                                        env=dict(os.environ, TZ='UTC'))

        def receive():
            for line in self.process.stdout:
                self.lines.put(line.rstrip('\n'))
            self.lines.put(None)

        self.reader = threading.Thread(target=receive, daemon=True)
        self.reader.start()
        try:
            self.execute(f"LOAD {quote(args.extension)}; "
                         f"CREATE SECRET fs (TYPE tidbfs, ENDPOINT {quote(endpoint)}, TOKEN {quote(token)}); "
                         f"SET threads={args.threads}; SET memory_limit={quote(args.memory_limit)}; "
                         "SET force_compression='uncompressed'; SET checkpoint_threshold='1TB'", timeout=120)
        except BaseException:
            self.stop()
            raise
        self.bootstrap_seconds = time.perf_counter() - started

    def execute(self, statement, timeout=1800):
        marker = 'benchmark_' + uuid.uuid4().hex
        started = time.perf_counter()
        self.process.stdin.write(statement.rstrip('; \n') + f'; SELECT {quote(marker)};\n')
        self.process.stdin.flush()
        result = []
        deadline = started + timeout
        while True:
            try:
                line = self.lines.get(timeout=max(0.01, deadline - time.perf_counter()))
            except queue.Empty:
                raise TimeoutError('Worker SQL timeout; see private build logs') from None
            if line is None:
                self.log.flush()
                diagnostic = self.log_path.read_text().replace(self.token, '<redacted>')
                raise RuntimeError('DuckDB exited: ' + diagnostic[-3500:])
            if line == marker:
                return time.perf_counter() - started, [line for line in result if line]
            result.append(line)

    def close(self):
        started = time.perf_counter()
        if self.process.poll() is None:
            self.process.stdin.close()
            self.process.wait(timeout=120)
        self.reader.join(timeout=5)
        self.process.stdout.close()
        self.log.close()
        if self.process.returncode:
            raise RuntimeError('DuckDB worker failed during shutdown')
        return time.perf_counter() - started

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdin, self.process.stdout, self.log):
            if stream and not stream.closed:
                stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tidb-server', required=True)
    parser.add_argument('--fs-server', required=True)
    parser.add_argument('--rustfs-server', required=True)
    parser.add_argument('--duckdb', default=shutil.which('duckdb'))
    parser.add_argument('--extension', default=str(ROOT / 'build/tidbfs.duckdb_extension'))
    parser.add_argument('--rows', type=int, default=50331648, help='8 x 8 bytes per row: 3 GiB before block headers')
    parser.add_argument('--dirty-rows', type=int, default=131072, help='8 MiB of appended column data before each dirty handoff')
    parser.add_argument('--trials', type=int, default=5)
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--memory-limit', default='6GB')
    parser.add_argument('--log-dir', required=True)
    args = parser.parse_args()
    for name in ('tidb_server', 'fs_server', 'rustfs_server', 'duckdb', 'extension'):
        setattr(args, name, str(Path(getattr(args, name)).resolve()))
    logs = Path(args.log_dir).resolve()
    logs.mkdir(parents=True, exist_ok=True)
    processes, handles, workers = [], [], []
    report = {
        'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'platform': platform.platform(), 'machine': platform.machine(),
        'cpu': subprocess.check_output(['sysctl', '-n', 'machdep.cpu.brand_string'], text=True).strip(),
        'memory_bytes': int(subprocess.check_output(['sysctl', '-n', 'hw.memsize'], text=True)),
        'duckdb_version': subprocess.check_output([args.duckdb, '-version'], text=True).strip(),
        'plugin_source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'binary_sha256': {name: digest(getattr(args, name)) for name in ('duckdb', 'extension', 'fs_server', 'tidb_server', 'rustfs_server')},
        'backend': 'RustFS S3, TiDB unistore, all services on the same host over loopback',
        'threads': args.threads, 'duckdb_memory_limit': args.memory_limit,
        'extent_cache_bytes': 64 << 20, 'row_payload_bytes': 64, 'initial_rows': args.rows,
        'dirty_append_rows_per_trial': args.dirty_rows,
        'cache_definition': 'client-cold = fresh DuckDB process and in-memory extent cache; warm = repeat the identical scan in that process. Object-service and OS caches are not purged.',
        'scan_sql': SCAN, 'trials': [],
    }

    def save():
        (logs / 'results.json').write_text(json.dumps(report, indent=2) + '\n')

    def launch(command, name, env):
        handle = (logs / (name + '.log')).open('w')
        handles.append(handle)
        child = subprocess.Popen(command, env=env, stdout=handle, stderr=handle)
        processes.append(child)
        return child

    def new_worker(name):
        worker = Worker(args, endpoint, token, logs, name)
        workers.append(worker)
        return worker

    with tempfile.TemporaryDirectory(prefix='runtime-', dir=logs) as tmp:
        try:
            clean_env = {k: v for k, v in os.environ.items() if not k.startswith(('DRIVE9_', 'RUSTFS_', 'MINIO_', 'AWS_', 'OTEL_'))}
            clean_env['TZ'] = 'UTC'
            sql_port, status_port, fs_port, s3_port = port(), port(), port(), port()
            launch([args.tidb_server, '--store=unistore', '--path=' + tmp + '/tidb',
                    '--host=127.0.0.1', '-P=' + str(sql_port), '--status=' + str(status_port),
                    '--status-host=127.0.0.1', '-L=error'], 'tidb', clean_env)
            connection = wait(lambda: pymysql.connect(host='127.0.0.1', port=sql_port, user='root'))
            with connection.cursor() as cursor:
                cursor.execute('CREATE DATABASE handoff_benchmark')
            connection.close()
            access, secret = secrets.token_hex(12).upper(), secrets.token_hex(24)
            rust_env = dict(clean_env, RUSTFS_ACCESS_KEY=access, RUSTFS_SECRET_KEY=secret,
                            RUSTFS_CONSOLE_ENABLE='false', RUSTFS_REGION='us-east-1')
            Path(tmp, 'objects').mkdir()
            launch([args.rustfs_server, 'server', tmp + '/objects', '--address', f'127.0.0.1:{s3_port}'], 'rustfs', rust_env)
            object_endpoint = f'http://127.0.0.1:{s3_port}'
            s3 = boto3.client('s3', endpoint_url=object_endpoint, region_name='us-east-1',
                              aws_access_key_id=access, aws_secret_access_key=secret,
                              config=Config(connect_timeout=2, read_timeout=10,
                                            retries={'max_attempts': 0}, s3={'addressing_style': 'path'}))
            wait(s3.list_buckets)
            s3.create_bucket(Bucket='handoff-benchmark')
            fs_env = dict(clean_env, DRIVE9_TENANT_PROVIDER='local',
                          DRIVE9_TENANT_MAINTENANCE_INTERVAL='1s',
                          DRIVE9_LISTEN_ADDR=f'127.0.0.1:{fs_port}',
                          DRIVE9_LOCAL_DSN=f'root@tcp(127.0.0.1:{sql_port})/handoff_benchmark?parseTime=true',
                          DRIVE9_TOKEN_SIGNING_KEY=secrets.token_hex(32), DRIVE9_MASTER_KEY=secrets.token_hex(32),
                          DRIVE9_S3_BUCKET='handoff-benchmark', DRIVE9_S3_ENDPOINT=object_endpoint,
                          DRIVE9_S3_REGION='us-east-1', DRIVE9_S3_FORCE_PATH_STYLE='true',
                          DRIVE9_S3_ACCESS_KEY_ID=access, DRIVE9_S3_SECRET_ACCESS_KEY=secret,
                          DRIVE9_EXTENT_ALLOW_STATIC_CREDENTIALS='1')
            launch([args.fs_server], 'fs', fs_env)
            endpoint = f'http://127.0.0.1:{fs_port}'

            def health():
                with urllib.request.urlopen(endpoint + '/healthz', timeout=2) as response:
                    return response.read()

            wait(health)
            with urllib.request.urlopen(urllib.request.Request(endpoint + '/v1/provision', method='POST'), timeout=90) as response:
                token = json.load(response)['api_key']

            def tenant_ready():
                request = urllib.request.Request(endpoint + '/v1/fs/', method='HEAD',
                                                 headers={'Authorization': 'Bearer ' + token})
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.read()

            wait(tenant_ready)

            def file_size(path):
                request = urllib.request.Request(endpoint + '/v1/fs' + path, method='HEAD', headers={'Authorization': 'Bearer ' + token})
                try:
                    with urllib.request.urlopen(request, timeout=15) as response:
                        return int(response.headers['Content-Length'])
                except urllib.error.HTTPError as error:
                    if error.code == 404:
                        return 0
                    raise

            uri_path = '/handoff-' + uuid.uuid4().hex + '.duckdb'
            uri = 'tidbfs+db://' + uri_path
            attach = f'ATTACH {quote(uri)} AS remote; USE remote'
            print('Disposable TiDB + RustFS ready; creating uncompressed database', flush=True)
            prepare = new_worker('prepare')
            prepare.execute(attach)
            report['prepare_create_seconds'], _ = prepare.execute('CREATE TABLE samples AS ' + projection(0, args.rows), timeout=3600)
            report['prepare_checkpoint_seconds'], _ = prepare.execute('CHECKPOINT remote', timeout=1800)
            _, output = prepare.execute(SCAN)
            expected = aggregate(output)
            report['initial_expected'] = [str(v) for v in expected]
            report['initial_database_bytes'] = file_size(uri_path)
            prepare.execute('USE memory; DETACH remote')
            prepare.close()
            save()
            print(f"Prepared {report['initial_database_bytes'] / (1 << 30):.3f} GiB in {report['prepare_create_seconds']:.2f}s", flush=True)
            cursor = args.rows
            for mode in ('checkpointed', 'dirty_wal'):
                for trial in range(1, args.trials + 1):
                    label = f'{mode}-{trial}'
                    print('Starting ' + label, flush=True)
                    old = new_worker(label + '-old')
                    old.execute(attach)
                    row = {'mode': mode, 'trial': trial}
                    if mode == 'dirty_wal':
                        delta_sql = projection(cursor, args.dirty_rows)
                        _, output = old.execute(SCAN.replace('FROM samples', 'FROM (' + delta_sql + ') delta'))
                        delta = aggregate(output)
                        row['old_append_commit_seconds'], _ = old.execute('INSERT INTO samples ' + delta_sql)
                        expected = [a + b for a, b in zip(expected, delta)]
                        cursor += args.dirty_rows
                    row['wal_bytes_before_checkpoint'] = file_size(uri_path + '.wal')
                    row['checkpoint_seconds'], _ = old.execute('CHECKPOINT remote')
                    row['detach_seconds'], _ = old.execute('USE memory; DETACH remote')
                    row['old_process_shutdown_seconds'] = old.close()
                    row['flush_close_seconds'] = sum(row[k] for k in ('checkpoint_seconds', 'detach_seconds', 'old_process_shutdown_seconds'))
                    row['database_bytes'] = file_size(uri_path)
                    fresh = new_worker(label + '-new')
                    row['new_process_bootstrap_seconds'] = fresh.bootstrap_seconds
                    row['reopen_seconds'], _ = fresh.execute(attach)
                    row['client_cold_scan_seconds'], output = fresh.execute(SCAN)
                    if aggregate(output) != expected:
                        raise AssertionError('Fresh worker checksum mismatch')
                    row['warm_scan_seconds'] = []
                    for _ in range(2):
                        seconds, output = fresh.execute(SCAN)
                        if aggregate(output) != expected:
                            raise AssertionError('Warm worker checksum mismatch')
                        row['warm_scan_seconds'].append(seconds)
                    row['expected'] = [str(v) for v in expected]
                    row['flush_to_first_result_seconds'] = sum(row[k] for k in ('flush_close_seconds', 'new_process_bootstrap_seconds', 'reopen_seconds', 'client_cold_scan_seconds'))
                    row['validated'] = True
                    fresh.execute('USE memory; DETACH remote')
                    fresh.close()
                    report['trials'].append(row)
                    save()
                    print(f"{label}: flush/close={row['flush_close_seconds']:.4f}s reopen={row['reopen_seconds']:.4f}s "
                          f"cold={row['client_cold_scan_seconds']:.4f}s warm={statistics.median(row['warm_scan_seconds']):.4f}s", flush=True)
            report['completed_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            report['complete'] = True
            save()
        finally:
            for worker in reversed(workers):
                worker.stop()
            for child in reversed(processes):
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
            for handle in handles:
                handle.close()


if __name__ == '__main__':
    main()
