#!/usr/bin/env python3
"""Live conformance tests; requires the generic leased file-volume API and TiDB.

Uses fresh database names. Never runs against or modifies an existing database.
The crash/heartbeat checks deliberately span the 30-second server lease.
"""
import os
import json
import http.server
import socket
import threading
import urllib.request
import urllib.error
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = os.environ['DRIVE9_BASE']
TOKEN = os.environ['DRIVE9_API_KEY']
DUCKDB = os.environ.get('DUCKDB', 'duckdb')


def quote(s):
    return "'" + str(s).replace("'", "''") + "'"


INIT = (f"LOAD {quote(ROOT / 'build/tidbfs.duckdb_extension')}; "
        f"CREATE SECRET fs (TYPE tidbfs, ENDPOINT {quote(ENDPOINT)}, TOKEN {quote(TOKEN)}); ")


def sql(statement, fail=False, endpoint=ENDPOINT):
    result = subprocess.run([DUCKDB, '-unsigned', '-bail', '-csv', '-noheader'],
                            input=INIT.replace(quote(ENDPOINT), quote(endpoint)) + statement, text=True, capture_output=True, timeout=180)
    error = result.stderr.replace(TOKEN, '<redacted>')
    if fail:
        if not result.returncode:
            raise AssertionError('Statement unexpectedly succeeded')
        return error
    if result.returncode:
        raise RuntimeError(error)
    return result.stdout.strip()


class NativeDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.uri = 'tidbfs+db:///conformance-' + uuid.uuid4().hex + '.duckdb'
        self.attach = f'ATTACH {quote(self.uri)} AS remote; USE remote; '
        self.readonly = f'ATTACH {quote(self.uri)} AS remote (READ_ONLY); USE remote; '
        self.tmp = tempfile.TemporaryDirectory(prefix='tidbfs-native-test-')
        self.addCleanup(self.tmp.cleanup)
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=15)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream:
                    stream.close()

    def hold(self, statement):
        marker = Path(self.tmp.name) / (uuid.uuid4().hex + '.csv')
        child = subprocess.Popen([DUCKDB, '-unsigned', '-bail'], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        child.stdin.write(INIT + statement + f" COPY (SELECT 1) TO {quote(marker)};\n")
        child.stdin.flush()
        until = time.monotonic() + 120
        while not marker.exists():
            if child.poll() is not None:
                raise RuntimeError(child.stderr.read().replace(TOKEN, '<redacted>'))
            if time.monotonic() > until:
                raise RuntimeError('Database process did not reach its durable marker')
            time.sleep(0.05)
        return child

    def finish(self, child):
        child.stdin.close()
        self.assertEqual(child.wait(timeout=120), 0,
                         child.stderr.read().replace(TOKEN, '<redacted>'))

    def test_catalog_transactions_checkpoint_reopen(self):
        sql(self.attach + """
            CREATE SCHEMA app;
            CREATE SEQUENCE app.ids START 10001;
            CREATE TABLE app.items(id INTEGER PRIMARY KEY, value VARCHAR CHECK(length(value)>0));
            INSERT INTO app.items SELECT i, md5(i::VARCHAR) FROM range(10000) t(i);
            CREATE INDEX value_idx ON app.items(value);
            CREATE VIEW app.summary AS SELECT count(*) n, sum(id) total FROM app.items;
            CREATE MACRO app.twice(x) AS x*2;
            ALTER TABLE app.items ADD COLUMN active BOOLEAN DEFAULT true;
            BEGIN; UPDATE app.items SET id=id+20000; DELETE FROM app.items WHERE id%2=0; ROLLBACK;
            BEGIN; INSERT INTO app.items VALUES (nextval('app.ids'),'committed',true); COMMIT;
            ANALYZE; CHECKPOINT; VACUUM;
        """)
        self.assertTrue(sql(self.readonly + "SELECT n,total,app.twice(21) FROM app.summary;").endswith('10001,50005001,42'))
        sql(self.attach + "BEGIN; DELETE FROM app.items WHERE id=0; UPDATE app.items SET value='updated' WHERE id=1; COMMIT; CHECKPOINT;")
        self.assertTrue(sql(self.readonly + "SELECT count(*),sum(id),count(*) FILTER(WHERE value='updated') FROM app.items;").endswith('10000,50005001,1'))

    def test_wal_recovers_committed_not_uncommitted_after_sigkill(self):
        child = self.hold(self.attach + """
            SET checkpoint_threshold='1GB';
            CREATE TABLE recovered(id INTEGER);
            BEGIN; INSERT INTO recovered SELECT i FROM range(10000) t(i); COMMIT;
            BEGIN; INSERT INTO recovered VALUES (999999);
        """)
        child.kill()
        child.wait(timeout=15)
        # A crashed process cannot release its lease. Let the actual server
        # clock expire it, without touching any metadata directly.
        deadline = time.monotonic() + 45
        while True:
            try:
                result = sql(self.attach + 'SELECT count(*),sum(id) FROM recovered; CHECKPOINT;')
                break
            except RuntimeError as error:
                if 'locked' not in str(error) or time.monotonic() >= deadline:
                    raise
                time.sleep(1)
        self.assertTrue(result.endswith('10000,49995000'), result)
        self.assertTrue(sql(self.readonly + 'SELECT max(id) FROM recovered;').endswith('9999'))

    def test_writer_exclusion_renewal_and_shared_readers(self):
        writer = self.hold(self.attach + 'CREATE TABLE t AS SELECT 42 AS answer; CHECKPOINT;')
        self.assertIn('locked', sql(self.attach + 'SELECT * FROM t;', fail=True))
        self.assertIn('locked', sql(self.readonly + 'SELECT * FROM t;', fail=True))
        # More than one full lease: the heartbeat must keep the writer alive.
        time.sleep(32)
        self.assertIn('locked', sql(self.attach + 'SELECT * FROM t;', fail=True))
        self.finish(writer)
        reader1 = self.hold(self.readonly + 'SELECT * FROM t;')
        reader2 = self.hold(self.readonly + 'SELECT * FROM t;')
        self.assertIn('locked', sql(self.attach + 'SELECT * FROM t;', fail=True))
        self.finish(reader1)
        self.finish(reader2)
        self.assertTrue(sql(self.attach + 'SELECT * FROM t;').endswith('42'))

    def test_lost_write_ack_is_not_retried_and_database_recovers(self):
        sql(self.attach + 'CREATE TABLE uncertain(id INTEGER); CHECKPOINT;')
        writes = []

        class DropAck(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                op = json.loads(body).get('op')
                request = urllib.request.Request(ENDPOINT + self.path, data=body, method='POST',
                    headers={'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json'})
                try:
                    response = urllib.request.urlopen(request, timeout=120)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    data, code = response.read(), response.code
                if op == 'write':
                    writes.append(code)
                    if len(writes) == 1:
                        # The server committed and replied; the client never
                        # receives the acknowledgement. Never replay this PUT.
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        proxy = http.server.ThreadingHTTPServer(('127.0.0.1', 0), DropAck)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        try:
            error = sql(self.attach + 'INSERT INTO uncertain VALUES (7);', fail=True,
                        endpoint='http://127.0.0.1:' + str(proxy.server_port))
            self.assertIn('Error', error)
            self.assertEqual(writes, [200], 'Ambiguous write was replayed')
        finally:
            proxy.shutdown()
            proxy.server_close()
            thread.join()
        # Either transaction outcome is legal after lost acknowledgement;
        # duplication or corruption is not. The application must reconcile.
        result = sql(self.attach + 'SELECT count(*) FROM uncertain; CHECKPOINT;')
        self.assertIn(result.splitlines()[-1], ('0', '1'))

    def test_external_sort_with_remote_temporary_files(self):
        result = sql(self.attach + f"""
            SET threads=1; SET preserve_insertion_order=false; SET memory_limit='64MB';
            SET temp_directory={quote(self.uri + '.tmp')};
            SELECT sum(length(payload)) FROM (
                SELECT md5(i::VARCHAR) payload FROM range(2000000) t(i) ORDER BY payload
            );
        """)
        self.assertTrue(result.endswith('64000000'), result)
        # Temporary storage can outlive DETACH; reattach must still work.
        result = sql(self.attach + f"SET temp_directory={quote(self.uri + '.tmp')}; "
                     "CREATE TABLE t AS SELECT 42 n; USE memory; DETACH remote; "
                     + self.attach + "SELECT n FROM t;")
        self.assertTrue(result.endswith('42'), result)

    def test_out_of_memory_cleans_up_without_abort(self):
        error = sql(self.attach + f"SET threads=2; SET memory_limit='24MB'; "
                    f"SET temp_directory={quote(self.uri + '.tmp')}; "
                    "SELECT sum(length(payload)) FROM (SELECT md5(i::VARCHAR) payload "
                    "FROM range(1000000) t(i) ORDER BY payload);", fail=True)
        self.assertIn('Out of Memory', error)
        self.assertNotIn('terminating', error)
        self.assertTrue(sql(self.attach + 'SELECT 42;').endswith('42'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
