"""Credential-free loopback measurement proxy; never logs payloads or URLs.

One upstream keep-alive connection per downstream socket. No retries, redirects,
or body rewriting. Direct object-store requests are outside this observation.
"""
import http.client
import http.server
import socket
import threading
import time
from urllib.parse import urlsplit

OPS = frozenset(('init load incr_counter new_session refresh_session delete_session '
                 'lookup getattr open_ref close_ref setlk getlk read write_slice '
                 'read_slices mknod unlink readdir truncate write rename delete_sustained find_stale_sessions clean_stale_session').split())


class AttachProbe:
    def __init__(self, upstream, delay_ms=0):
        target = urlsplit(upstream)
        if target.scheme not in ('http', 'https') or target.username or target.password or target.query or target.fragment or target.path not in ('', '/'):
            raise ValueError('Expected a plain HTTP(S) endpoint')
        self.events = []
        self.mu = threading.Lock()
        self.connections = 0
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with owner.mu:
                    owner.connections += 1
                    self.connection_number = owner.connections
                cls = http.client.HTTPSConnection if target.scheme == 'https' else http.client.HTTPConnection
                class Connection(cls):
                    connects = 0
                    connect_ms = 0
                    def connect(conn):
                        start = time.perf_counter()
                        try:
                            super().connect()
                        finally:
                            conn.connects += 1
                            conn.connect_ms += (time.perf_counter() - start) * 1000
                self.upstream = Connection(target.hostname, target.port, timeout=30)

            def finish(self):
                self.upstream.close()
                super().finish()

            def handle(self):
                try:
                    super().handle()
                except (OSError, http.client.HTTPException):
                    self.close_connection = True

            def request(self):
                # SDK metadata is length-framed. Fail closed for unsupported framing.
                if self.headers.get('Transfer-Encoding'):
                    self.send_error(400)
                    return
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                op = self.headers.get('X-Drive9-Extent-Op', '')
                kind = op if op in OPS else ('credential' if self.path == '/v1/data-credential' else 'other')
                event = {'op': kind, 'connection': self.connection_number, 'started': time.perf_counter()}
                with owner.mu:
                    owner.events.append(event)
                try:
                    time.sleep(delay_ms / 1000)
                    headers = {k: v for k, v in self.headers.items() if k.lower() not in ('host', 'connection')}
                    connects, connect_ms = self.upstream.connects, self.upstream.connect_ms
                    self.upstream.request(self.command, self.path, body, headers)
                    event['upstream_connects'] = self.upstream.connects - connects
                    event['upstream_connect_ms'] = self.upstream.connect_ms - connect_ms
                    response = self.upstream.getresponse()
                    payload = response.read()
                    event['status'] = response.status
                    self.send_response(response.status)
                    for k, v in response.getheaders():
                        if k.lower() not in ('transfer-encoding', 'connection', 'content-length'):
                            self.send_header(k, v)
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    if self.command != 'HEAD':
                        self.wfile.write(payload)
                    self.wfile.flush()
                finally:
                    with owner.mu:
                        event['ms'] = (time.perf_counter() - event['started']) * 1000

            do_POST = do_GET = do_HEAD = do_PUT = do_DELETE = request

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 128

        self.server = Server(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def snapshot(self):
        with self.mu:
            return [dict(e) for e in self.events]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
