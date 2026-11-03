#!/usr/bin/env python3
"""Compare native ATTACH in fresh processes and repeated attaches in one instance.

Accepts credentials only from the environment, writes only aggregate telemetry.
Use test_native_live.py --attach-benchmark for disposable local services.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
from urllib.parse import urlsplit
import platform
import queue
import statistics
import sys
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

from attach_probe import AttachProbe

ROOT = Path(__file__).resolve().parents[1]


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


class Session:
    def __init__(self, duckdb):
        self.child = subprocess.Popen([duckdb, '-unsigned', '-bail', '-csv', '-noheader'],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True,
                                      env=dict(os.environ, DUCKDB_AGENT_MODE='0'))
        self.lines = queue.Queue()
        self.error_kinds = set()
        def errors():
            for line in self.child.stderr:
                for word in ('Parser Error', 'Binder Error', 'IO Error', 'HTTP', 'errno', 'Unsupported', 'not found', 'Failed', 'credentials', 'Invalid'):
                    if word in line:
                        self.error_kinds.add(word)
        self.error_reader = threading.Thread(target=errors, daemon=True)
        self.error_reader.start()
        def read():
            for line in self.child.stdout:
                self.lines.put(line.strip())
            self.lines.put(None)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def run(self, sql):
        marker = uuid.uuid4().hex
        self.child.stdin.write(sql + '; SELECT ' + quote(marker) + ';\n')
        self.child.stdin.flush()
        deadline = time.monotonic() + 120
        rows = []
        while True:
            line = self.lines.get(timeout=max(.001, deadline - time.monotonic()))
            if line is None:
                self.error_reader.join(timeout=1)
                raise RuntimeError('Benchmark SQL failed: ' + ', '.join(sorted(self.error_kinds)))
            if line == marker:
                return rows
            rows.append(line)

    def close(self):
        if self.child.poll() is None:
            self.child.stdin.close()
            try:
                self.child.wait(timeout=45)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait()
                raise RuntimeError('Benchmark session failed to close') from None
        self.reader.join(timeout=2)
        self.child.stdout.close()
        self.error_reader.join(timeout=2)
        self.child.stderr.close()
        if self.child.returncode and sys.exc_info()[0] is None:
            raise RuntimeError('Benchmark session exited unsuccessfully')


def distribution(values):
    ordered = sorted(values)
    return {'n': len(values), 'median': statistics.median(values),
            'p95': ordered[math.ceil(.95 * len(values)) - 1], 'min': ordered[0], 'max': ordered[-1]}


def benchmark(args):
    endpoint, token = os.environ['DRIVE9_BASE'], os.environ['DRIVE9_API_KEY']
    if not args.local and not args.allow_remote:
        raise ValueError('Remote runs require --allow-remote; use disposable local harness otherwise')
    if args.local and urlsplit(endpoint).hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('Local benchmark requires a loopback service')
    probe = AttachProbe(endpoint, args.delay_ms)
    uri = 'tidbfs+db:///attach-bench-' + uuid.uuid4().hex + '.duckdb'
    init = (f'LOAD {quote(Path(args.extension).resolve())};',
            f'CREATE SECRET fs (TYPE tidbfs, ENDPOINT {quote(probe.url)}, TOKEN {quote(token)});')
    samples = []
    def stage(session, mode, number, name, sql):
        start = time.perf_counter()
        result = session.run(sql)
        end = time.perf_counter()
        samples.append({'mode': mode, 'sample': number, 'stage': name, 'ms': (end-start)*1000,
                        'events': [e for e in probe.snapshot() if start <= e['started'] <= end]})
        return result
    try:
        setup = Session(args.duckdb)
        try:
            setup.run(''.join(init) + f'ATTACH {quote(uri)} AS remote; CREATE TABLE remote.bench AS SELECT 42 AS n; CHECKPOINT remote; DETACH remote;')
        finally:
            setup.close()
        for mode in ('fresh_process', 'same_instance', 'new_instance_same_process'):
            session = None
            try:
                for number in range(args.samples):
                    if session is not None and mode == 'new_instance_same_process':
                        session.child.stdin.write('.open :memory:\n')
                        session.child.stdin.flush()
                        stage(session, mode, number, 'instance_reset', 'SELECT 1')
                        stage(session, mode, number, 'extension_load', init[0])
                        stage(session, mode, number, 'secret_setup', init[1])
                    if session is None:
                        start = time.perf_counter()
                        session = Session(args.duckdb)
                        stage(session, mode, number, 'process_ready', 'SELECT 1')
                        samples[-1]['ms'] = (time.perf_counter()-start)*1000
                        stage(session, mode, number, 'extension_load', init[0])
                        stage(session, mode, number, 'secret_setup', init[1])
                    observed_mode = 'same_instance_cold' if mode == 'same_instance' and number == 0 else mode
                    stage(session, observed_mode, number, 'attach', f'ATTACH {quote(uri)} AS remote (READ_ONLY)')
                    stage(session, observed_mode, number, 'catalog', "SELECT table_name FROM information_schema.tables WHERE table_catalog='remote'")
                    rows = stage(session, observed_mode, number, 'first_query', 'SELECT sum(n) FROM remote.bench')
                    if rows != ['42']:
                        raise RuntimeError('Benchmark query returned unexpected data')
                    stage(session, observed_mode, number, 'detach', 'DETACH remote')
                    if mode == 'fresh_process':
                        start = time.perf_counter()
                        session.close()
                        samples.append({'mode': mode, 'sample': number, 'stage': 'process_close',
                                        'ms': (time.perf_counter()-start)*1000,
                                        'events': [e for e in probe.snapshot() if e['started'] >= start]})
                        session = None
            finally:
                if session:
                    session.close()
    finally:
        # Delete exactly this invocation's database and WAL, even after failure.
        try:
            for suffix in ('', '.wal'):
                req = urllib.request.Request(endpoint + '/v1/fs/' + uri.split(':///')[1] + suffix,
                                             method='DELETE', headers={'Authorization': 'Bearer ' + token})
                try:
                    with urllib.request.urlopen(req, timeout=30):
                        pass
                except urllib.error.HTTPError as error:
                    if error.code != 404:
                        raise RuntimeError('Benchmark resource cleanup failed') from None
        finally:
            probe.close()
    for sample in samples:
        if sample['stage'] == 'attach':
            ops = Counter(e['op'] for e in sample['events'])
            expected = 0 if sample['mode'] == 'same_instance' else 1
            for op in ('init', 'load', 'new_session', 'credential'):
                if ops[op] != expected:
                    raise RuntimeError('Unexpected extent startup/session reuse count')
    summary = []
    for mode, name in sorted({(s['mode'], s['stage']) for s in samples}):
        group = [s for s in samples if s['mode'] == mode and s['stage'] == name]
        counts = Counter(e['op'] for s in group for e in s['events'])
        summary.append({'mode': mode, 'stage': name, 'latency_ms': distribution([s['ms'] for s in group]),
                        'requests': dict(counts), 'requests_per_sample': distribution([len(s['events']) for s in group]),
                        'operation_ms': {op: distribution([e['ms'] for s in group for e in s['events'] if e['op'] == op and 'ms' in e]) for op in counts if any(e['op'] == op and 'ms' in e for s in group for e in s['events'])},
                        'upstream_connects': sum(e.get('upstream_connects', 0) for s in group for e in s['events']),
                        'upstream_connect_ms': sum(e.get('upstream_connect_ms', 0) for s in group for e in s['events']),
                        'downstream_connections': len({e['connection'] for s in group for e in s['events']})})
    # Strip absolute monotonic times; never serialize endpoint, path, SQL or errors.
    for sample in samples:
        for event in sample['events']:
            event.pop('started', None)
    return {'schema': 1, 'label': args.label, 'plugin_sha': args.plugin_sha,
            'sdk_sha': (ROOT/'cpp-sdk-revision.txt').read_text().strip(),
            'extension_sha256': hashlib.sha256(Path(args.extension).read_bytes()).hexdigest(),
            'duckdb': subprocess.check_output([args.duckdb, '-csv', '-noheader', '-c', 'SELECT version()'], env=dict(os.environ, DUCKDB_AGENT_MODE='0'), text=True).strip(),
            'platform': platform.system() + '/' + platform.machine(), 'region': args.region,
            'delay_per_metadata_request_ms': args.delay_ms, 'samples': args.samples,
            'cache': 'newly created database; server/OS caches warm; fresh process client caches cold; same-instance caches retained',
            'object_transport_observed': False, 'summary': summary, 'observations': samples}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duckdb', default=os.environ.get('DUCKDB', 'duckdb'))
    parser.add_argument('--extension', default=str(ROOT/'build/tidbfs.duckdb_extension'))
    parser.add_argument('--samples', type=int, default=10)
    parser.add_argument('--delay-ms', type=float, default=0)
    parser.add_argument('--allow-remote', action='store_true')
    parser.add_argument('--local', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--region', default='unspecified')
    parser.add_argument('--label', default='candidate')
    parser.add_argument('--plugin-sha', default=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip())
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.samples < 2 or not math.isfinite(args.delay_ms) or args.delay_ms < 0:
        parser.error('Use at least two samples and nonnegative delay')
    try:
        report = benchmark(args)
    except Exception as error:
        print(type(error).__name__, flush=True)
        if isinstance(error, RuntimeError):
            print(str(error), flush=True)
        parser.exit(1, 'Benchmark failed; sensitive diagnostics suppressed. Check disposable service health.\n')
    Path(args.output).write_text(json.dumps(report, indent=2) + '\n')
    print('Credential-free benchmark report written')


if __name__ == '__main__':
    main()
