"""Ensure attribution never records credentials/URLs and keeps HTTP sockets alive."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from attach_probe import AttachProbe


class ProbeTests(unittest.TestCase):
    def test_reuse_redaction_and_no_write_replay(self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            calls = 0
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                Handler.calls += 1
                if self.path.startswith('/drop'):
                    self.close_connection = True
                    return
                self.send_response(200)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'{}')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        probe = AttachProbe(f'http://127.0.0.1:{server.server_port}')
        connection = http.client.HTTPConnection('127.0.0.1', probe.server.server_port, timeout=2)
        try:
            for _ in range(2):
                connection.request('POST', '/private?signature=secret', b'private content',
                                   {'Authorization': 'Bearer private-token', 'X-Drive9-Extent-Op': 'lookup'})
                self.assertEqual(connection.getresponse().read(), b'{}')
            self.assertEqual(probe.connections, 1)
            self.assertEqual([e['op'] for e in probe.snapshot()], ['lookup', 'lookup'])
            self.assertEqual([e['upstream_connects'] for e in probe.snapshot()], [1, 0])
            self.assertNotIn('private', json.dumps(probe.snapshot()))
            self.assertNotIn('secret', json.dumps(probe.snapshot()))
            connection.request('POST', '/drop', b'write')
            with self.assertRaises(http.client.RemoteDisconnected):
                connection.getresponse()
            self.assertEqual(Handler.calls, 3)
        finally:
            connection.close()
            probe.close()
            server.shutdown()
            server.server_close()
            thread.join()
