"""Per-worker TCP disconnects for disposable loopback tests (no fault API).

Both HTTP relays share one switch. Cutting it shuts down active sockets and
rejects new ones without returning an HTTP error or pausing the worker. The
metadata relay rewrites only the test S3 endpoint in data-credential responses,
so object traffic from this worker goes through the same isolation boundary.
"""
import http.client
import http.server
import json
import socket
import threading
import time
import urllib.parse


class Partition:
    def __init__(self):
        self.mu = threading.RLock()
        self.online = True
        self.sockets = set()
        self.events = []

    def record(self, **fields):
        with self.mu:
            self.events.append(dict(monotonic=time.monotonic(), **fields))

    def admit(self, sock):
        with self.mu:
            if not self.online:
                return False
            self.sockets.add(sock)
            return True

    def forget(self, sock):
        with self.mu:
            self.sockets.discard(sock)

    @staticmethod
    def shutdown(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def cut(self):
        with self.mu:
            self.online = False
            started = time.monotonic()
            for sock in list(self.sockets):
                self.shutdown(sock)
            self.record(kind='network_cut')
            return started

    def restore(self):
        with self.mu:
            self.online = True
            self.record(kind='network_restored')
            return time.monotonic()

    def snapshot(self):
        with self.mu:
            return [dict(event) for event in self.events]


class PartitionProxy:
    def __init__(self, upstream, partition, *, lane, object_endpoint=None):
        target = urllib.parse.urlsplit(upstream)
        if target.scheme != 'http' or target.hostname != '127.0.0.1':
            raise ValueError('This test relay only accepts loopback HTTP services')

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def handle(self):
                if not partition.admit(self.connection):
                    partition.record(kind='connection_rejected', lane=lane)
                    partition.shutdown(self.connection)
                    return
                try:
                    super().handle()
                except (OSError, http.client.HTTPException):
                    pass
                finally:
                    partition.forget(self.connection)

            def body(self):
                if self.headers.get('Transfer-Encoding', '').lower() != 'chunked':
                    return self.rfile.read(int(self.headers.get('Content-Length', '0')))
                chunks = []
                while True:
                    size = int(self.rfile.readline().split(b';')[0], 16)
                    if not size:
                        while self.rfile.readline().strip():
                            pass
                        return b''.join(chunks)
                    chunks.append(self.rfile.read(size))
                    self.rfile.read(2)

            def request(self):
                op = self.headers.get('X-Drive9-Extent-Op', '')
                data = self.body()
                conn = http.client.HTTPConnection(target.hostname, target.port, timeout=30)
                sock = None
                try:
                    conn.connect()
                    sock = conn.sock
                    if not partition.admit(sock):
                        partition.record(kind='request_rejected', lane=lane, op=op)
                        partition.shutdown(self.connection)
                        return
                    headers = {k: v for k, v in self.headers.items()
                               if k.lower() not in ('connection', 'transfer-encoding', 'content-length')}
                    headers['Content-Length'] = str(len(data))
                    # Keep Host: it is covered by the client's S3 signature.
                    conn.request(self.command, self.path, body=data, headers=headers)
                    response = conn.getresponse()
                    payload = response.read()
                    fields = dict(kind='response', lane=lane, op=op, status=response.status)
                    if lane == 'metadata' and self.path == '/v1/data-credential' and response.status == 200:
                        credential = json.loads(payload)
                        credential['endpoint'] = object_endpoint
                        payload = json.dumps(credential).encode()
                        fields['credential_endpoint_rewritten'] = True
                    if lane == 'metadata' and op in ('new_session', 'refresh_session'):
                        body, result = json.loads(data), json.loads(payload)
                        fields.update(sid=body.get('sid'), expire=body.get('expire'),
                                      errno=result.get('errno'), session_guard=result.get('session_guard'))
                    partition.record(**fields)
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in ('connection', 'transfer-encoding', 'content-length'):
                            self.send_header(key, value)
                    length = response.getheader('Content-Length', '0') if self.command == 'HEAD' else str(len(payload))
                    self.send_header('Content-Length', length)
                    self.end_headers()
                    if self.command != 'HEAD':
                        self.wfile.write(payload)
                except (OSError, http.client.HTTPException):
                    partition.record(kind='socket_error', lane=lane, op=op)
                    self.close_connection = True
                finally:
                    if sock:
                        partition.forget(sock)
                    conn.close()

            do_GET = do_POST = do_PUT = do_HEAD = do_DELETE = request

        self.partition = partition
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
