#!/usr/bin/env python3
"""Independent native writer/readers against disposable TiDB and RustFS."""
import csv
import os
from pathlib import Path
import signal
import time
import unittest
import uuid

import native_smoke as native
from native_smoke import ENDPOINT, TOKEN, quote, sql
from fault_proxy import FaultProxy


class SnapshotTests(unittest.TestCase):
    setUp = native.NativeDatabaseTests.setUp
    stop_children = native.NativeDatabaseTests.stop_children
    hold = native.NativeDatabaseTests.hold
    finish = native.NativeDatabaseTests.finish
    recover = native.NativeDatabaseTests.recover
    object_fault = native.NativeDatabaseTests.object_fault

    @property
    def snapshot(self):
        return f"ATTACH {quote(self.uri.replace('tidbfs+db:', 'tidbfs+snapshot:'))} AS remote (READ_ONLY); USE remote; "

    def execute(self, child, statement):
        marker = Path(self.tmp.name) / (uuid.uuid4().hex + '.csv')
        child.stdin.write(statement + f"; COPY (SELECT 1) TO {quote(marker)};\n")
        child.stdin.flush()
        deadline = time.monotonic() + 120
        while not marker.exists():
            if child.poll() is not None:
                self.fail(child.stderr.read().replace(TOKEN, '<redacted>'))
            if time.monotonic() > deadline:
                self.fail('Native process did not finish its statement')
            time.sleep(0.02)

    def rows(self, child, query):
        output = Path(self.tmp.name) / (uuid.uuid4().hex + '.csv')
        self.execute(child, f'COPY ({query}) TO {quote(output)} (HEADER false)')
        with output.open() as stream:
            return list(csv.reader(stream))

    def test_pinned_catalog_pages_and_explicit_refresh(self):
        writer = self.hold(self.attach + """
            CREATE SCHEMA app;
            CREATE TABLE app.t(id BIGINT PRIMARY KEY, value VARCHAR);
            INSERT INTO app.t SELECT i, md5(i::VARCHAR) FROM range(10000) t(i);
            CREATE INDEX value_idx ON app.t(value);
            CREATE VIEW app.summary AS SELECT count(*) n, sum(id) total FROM app.t;
            CALL tidbfs_publish('remote');
        """)
        readers = [self.hold(self.snapshot + 'BEGIN; SELECT n FROM app.summary;') for _ in range(3)]
        before = [['10000', '49995000']]
        self.execute(writer, """
            BEGIN;
            DELETE FROM app.t WHERE id < 100;
            UPDATE app.t SET value='changed' WHERE id < 200;
            INSERT INTO app.t VALUES (10000, 'new');
            ALTER TABLE app.t ADD COLUMN generation INTEGER DEFAULT 2;
            CREATE TABLE second_catalog AS SELECT 2 version;
            COMMIT;
        """)
        # Even new readers get the previous publication until an explicit publish.
        self.assertTrue(sql(self.snapshot + 'SELECT n,total FROM app.summary;').endswith('10000,49995000'))
        self.execute(writer, "CALL tidbfs_publish('remote')")
        self.assertTrue(sql(self.snapshot + 'SELECT n,total FROM app.summary;').endswith('9901,50000050'))
        self.assertTrue(sql(self.snapshot + 'SELECT min(generation), count(*) FILTER (WHERE value=\'changed\') FROM app.t;').endswith('2,100'))
        for reader in readers:
            self.assertEqual(self.rows(reader, 'SELECT n,total FROM app.summary'), before)
            self.assertEqual(self.rows(reader, "SELECT count(*) FROM app.t WHERE value=md5('42')"), [['1']])
            self.assertEqual(self.rows(reader, "SELECT count(*) FROM information_schema.columns WHERE table_schema='app' AND table_name='t'"), [['2']])
            self.execute(reader, 'COMMIT')
            # Ending a query or transaction does not silently refresh an attachment.
            self.assertEqual(self.rows(reader, 'SELECT n,total FROM app.summary'), before)
        self.execute(readers[0], 'USE memory; DETACH remote; ' + self.snapshot)
        self.assertEqual(self.rows(readers[0], 'SELECT n,total FROM app.summary'), [['9901', '50000050']])
        # Writer retains the ordinary exclusive live database lock.
        self.assertIn('locked', sql(self.attach + 'SELECT 1;', fail=True))
        self.assertIn('locked', sql(self.readonly + 'SELECT 1;', fail=True))
        for reader in readers:
            self.finish(reader)
        self.finish(writer)

    def test_no_publication_no_wal_and_no_uncommitted_rows(self):
        writer = self.hold(self.attach + "CREATE TABLE t AS SELECT 1 n;")
        self.assertIn('publish', sql(self.snapshot + 'SELECT * FROM t;', fail=True).lower())
        self.execute(writer, "CALL tidbfs_publish('remote'); SET checkpoint_threshold='1GB'; INSERT INTO t VALUES (2)")
        self.assertTrue(sql(self.snapshot + 'SELECT sum(n) FROM t;').endswith('1'))
        self.finish(writer)
        error = sql(self.attach + "BEGIN; INSERT INTO t VALUES (3); CALL tidbfs_publish('remote');", fail=True)
        self.assertIn('transaction-local', error)
        self.assertTrue(sql(self.snapshot + 'SELECT sum(n) FROM t;').endswith('1'))
        self.assertIn('READ_ONLY', sql(self.snapshot.replace(' (READ_ONLY)', '') + 'SELECT 1;', fail=True))
        self.assertIn('read-only', sql(self.snapshot + 'INSERT INTO t VALUES (4);', fail=True).lower())

    def test_publication_ack_loss_is_atomic_and_not_replayed(self):
        sql(self.attach + "CREATE TABLE t AS SELECT 1 n; CALL tidbfs_publish('remote');")
        reader = self.hold(self.snapshot + 'SELECT * FROM t;')
        proxy = FaultProxy(ENDPOINT)
        self.addCleanup(proxy.close)
        with proxy.mu:
            proxy.rule = {'method': 'POST', 'op': 'clone', 'mode': 'drop_ack', 'count': 1}
        error = sql(self.attach + "INSERT INTO t VALUES (2); CALL tidbfs_publish('remote');", endpoint=proxy.url, fail=True)
        self.assertIn('publish', error.lower())
        with proxy.mu:
            clones = [e for e in proxy.events if e['op'] == 'clone']
        self.assertEqual(len(clones), 1, 'An ambiguous publication must not be replayed')
        self.assertEqual(clones[0]['status'], 200)
        self.assertEqual(self.rows(reader, 'SELECT sum(n) FROM t'), [['1']])
        self.assertTrue(sql(self.snapshot + 'SELECT sum(n) FROM t;').endswith('3'))
        self.finish(reader)

    def test_failed_publication_preserves_previous_generation(self):
        sql(self.attach + "CREATE TABLE t AS SELECT 1 n; CALL tidbfs_publish('remote');")
        proxy = FaultProxy(ENDPOINT)
        self.addCleanup(proxy.close)
        with proxy.mu:
            proxy.rule = {'method': 'POST', 'op': 'clone', 'mode': 'unavailable', 'count': -1}
        sql(self.attach + "INSERT INTO t VALUES (2); CALL tidbfs_publish('remote');", endpoint=proxy.url, fail=True)
        self.assertTrue(sql(self.snapshot + 'SELECT sum(n) FROM t;').endswith('1'))
        with proxy.mu:
            self.assertEqual(len([e for e in proxy.events if e['op'] == 'clone']), 1)

    def test_writer_crash_takeover_keeps_old_readers(self):
        writer = self.hold("SET GLOBAL tidbfs_heartbeat_ms=1000; " + self.attach + """
            CREATE TABLE t AS SELECT 1 n;
            CALL tidbfs_publish('remote');
            SET checkpoint_threshold='1GB';
            INSERT INTO t VALUES (2);
            BEGIN; INSERT INTO t VALUES (999);
        """)
        reader = self.hold(self.snapshot + 'SELECT * FROM t;')
        writer.kill()
        writer.wait(timeout=15)
        self.assertEqual(self.rows(reader, 'SELECT sum(n) FROM t'), [['1']])
        result = self.recover(self.attach + "CALL tidbfs_publish('remote'); SELECT sum(n) FROM t;")
        self.assertTrue(result.endswith('3'), result)
        self.assertTrue(sql(self.snapshot + 'SELECT sum(n) FROM t;').endswith('3'))
        self.assertEqual(self.rows(reader, 'SELECT sum(n) FROM t'), [['1']])
        self.finish(reader)

    def test_expired_reader_fails_instead_of_refreshing(self):
        writer = self.hold(self.attach + "CREATE TABLE t AS SELECT i, md5(i::VARCHAR) v FROM range(50000) t(i); CALL tidbfs_publish('remote');")
        reader = self.hold('SET GLOBAL tidbfs_heartbeat_ms=1000; ' + self.snapshot)
        os.kill(reader.pid, signal.SIGSTOP)
        try:
            self.execute(writer, "UPDATE t SET v='new'; CALL tidbfs_publish('remote')")
            time.sleep(7)  # actual lease expiry, not a metadata edit
        finally:
            os.kill(reader.pid, signal.SIGCONT)
        reader.stdin.write('SELECT sum(length(v)) FROM t;\n')
        reader.stdin.flush()
        reader.stdin.close()
        self.assertNotEqual(reader.wait(timeout=30), 0)
        self.assertIn('session failed or expired', reader.stderr.read())
        self.assertTrue(sql(self.snapshot + 'SELECT count(DISTINCT v) FROM t;').endswith('1'))
        self.finish(writer)

    def test_checkpoint_upload_crash_and_old_reader_object_recovery(self):
        self.object_fault({})
        self.addCleanup(self.object_fault, {})
        writer = self.hold('SET GLOBAL tidbfs_heartbeat_ms=1000; ' + self.attach + """
            SET checkpoint_threshold='1GB';
            CREATE TABLE t AS SELECT i, md5(i::VARCHAR) payload FROM range(50000) t(i);
            CALL tidbfs_publish('remote');
            INSERT INTO t SELECT i, md5(i::VARCHAR) FROM range(50000,60000) t(i);
        """)
        reader = self.hold(self.snapshot)  # data blocks have not been scanned
        self.object_fault({'method': 'PUT', 'mode': 'hold_ack', 'count': 1})
        writer.stdin.write("CALL tidbfs_publish('remote');\n")
        writer.stdin.flush()
        deadline = time.monotonic() + 60
        while not any(e['fault'] == 'hold_ack' and e.get('status') == 200 for e in self.object_fault()):
            if writer.poll() is not None or time.monotonic() > deadline:
                self.fail('Publish did not reach the checkpoint upload barrier')
            time.sleep(0.02)
        writer.kill()
        writer.wait(timeout=15)
        self.object_fault({})
        self.assertTrue(sql(self.snapshot + 'SELECT count(*) FROM t;').endswith('50000'))
        self.recover(self.attach + "CALL tidbfs_publish('remote');")
        self.assertTrue(sql(self.snapshot + 'SELECT count(*) FROM t;').endswith('60000'))
        self.object_fault({'method': 'GET', 'mode': 'short_read', 'count': 1})
        self.assertEqual(self.rows(reader, "SELECT count(*),sum(i),sum(length(payload)),count(*) FILTER(WHERE payload<>md5(i::VARCHAR)) FROM t"),
                         [['50000', '1249975000', '1600000', '0']])
        self.assertEqual(sum(e['fault'] == 'short_read' for e in self.object_fault()), 1)
        self.finish(reader)


if __name__ == '__main__':
    unittest.main()
