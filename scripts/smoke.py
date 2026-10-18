#!/usr/bin/env python3
"""Real DuckDB -> HTTP -> fs -> TiDB smoke, with observed HTTP body bytes.

Set DRIVE9_BASE and DRIVE9_API_KEY for a disposable local filesystem served with
DRIVE9_INLINE_THRESHOLD=16777216. Creates and removes one unique test directory.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def main():
    endpoint = os.environ["DRIVE9_BASE"].rstrip("/")
    token = os.environ["DRIVE9_API_KEY"]
    duckdb = os.environ.get("DUCKDB", "duckdb")
    remote = "/duckdb-smoke/" + uuid.uuid4().hex
    headers = {"Authorization": "Bearer " + token}
    measurements = []

    def request(method, path, data=None):
        req = urllib.request.Request(endpoint + "/v1/fs" + path, method=method, headers=headers, data=data)
        return urllib.request.urlopen(req, timeout=60)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    upstream = urllib.request.build_opener(NoRedirect)

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_HEAD(self):
            self.forward()

        def do_GET(self):
            self.forward()

        def do_PUT(self):
            self.forward()

        def do_POST(self):
            self.forward()

        def do_DELETE(self):
            self.forward()

        def forward(self):
            length = int(self.headers.get("Content-Length", "0"))
            data = self.rfile.read(length) if length else None
            req = urllib.request.Request(endpoint + self.path, method=self.command, data=data,
                                         headers={k: v for k, v in self.headers.items() if k.lower() != "host"})
            try:
                response = upstream.open(req, timeout=60)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                body = response.read()
                measurements.append({"method": self.command, "status": response.status, "bytes": len(body)})
                self.send_response(response.status)
                for k, v in response.headers.items():
                    if k.lower() not in ("transfer-encoding", "connection", "server", "date"):
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

    class Server(ThreadingHTTPServer):
        request_queue_size = 128
        daemon_threads = True

    proxy = Server(("127.0.0.1", 0), Proxy)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()

    def query(sql):
        setup = (f"LOAD {quote(ROOT / 'build/tidbfs.duckdb_extension')}; "
                 f"CREATE SECRET fs (TYPE tidbfs, ENDPOINT 'http://127.0.0.1:{proxy.server_port}', TOKEN {quote(token)}); "
                 "SET threads=4; ")
        result = subprocess.run([duckdb, "-unsigned", "-csv", "-noheader", "-bail"],
                                input=setup + sql, text=True, capture_output=True, timeout=90)
        if result.returncode:
            # CLI diagnostics can echo the secret-bearing setup SQL.
            error = result.stderr.splitlines()[0] if result.stderr else "no diagnostic"
            error = error.replace(token, "<redacted>")
            raise RuntimeError("DuckDB smoke query failed: " + error)
        return result.stdout.removeprefix("true\n").strip()

    try:
        with tempfile.TemporaryDirectory() as tmp:
            parquet = Path(tmp) / "orders.parquet"
            subprocess.run([duckdb, "-c", f"COPY (SELECT i::BIGINT AS id, md5(i::VARCHAR) || md5((i+1)::VARCHAR) || md5((i+2)::VARCHAR) AS payload FROM range(100000) t(i)) TO {quote(parquet)} (FORMAT PARQUET, ROW_GROUP_SIZE 10000);"], check=True)
            query(f"COPY (SELECT * FROM read_parquet({quote(parquet)})) "
                  f"TO 'tidbfs://{remote}/orders.parquet' (FORMAT PARQUET, ROW_GROUP_SIZE 10000);")
            with request("HEAD", remote + "/orders.parquet") as response:
                if response.headers.get("X-Dat9-Storage-Type") != "db9":
                    raise RuntimeError("This TiDB byte-projection smoke requires DRIVE9_INLINE_THRESHOLD=16777216")
                remote_size = int(response.headers["Content-Length"])
            report = {"file_bytes": remote_size, "queries": {}}
            for name, expression, predicate, expected in [
                ("narrow", "sum(id)", "WHERE id < 2048", "2096128"),
                ("wide", "sum(length(payload))", "", "9600000"),
            ]:
                measurements.clear()
                result = query(f"SELECT {expression} FROM read_parquet('tidbfs://{remote}/orders.parquet') {predicate};")
                if result != expected:
                    raise RuntimeError(f"{name} DuckDB query failed or returned the wrong result")
                report["queries"][name] = {"result": expected, "body_bytes": sum(m["bytes"] for m in measurements),
                                           "head_requests": sum(m["method"] == "HEAD" for m in measurements),
                                           "range_requests": sum(m["status"] == 206 for m in measurements)}
            assert report["queries"]["narrow"]["body_bytes"] < report["queries"]["wide"]["body_bytes"] / 5
            query(f"COPY (SELECT i AS id, i % 3 AS bucket FROM range(100) t(i)) "
                  f"TO 'tidbfs://{remote}/partition' (FORMAT PARQUET, PARTITION_BY(bucket));")
            assert query(f"SELECT sum(id) FROM read_parquet('tidbfs://{remote}/partition/**/*.parquet');") == "4950"
            query(f"COPY (SELECT 1 AS id) TO 'tidbfs://{remote}/overwrite.csv' (FORMAT CSV); "
                  f"COPY (SELECT 9 AS id) TO 'tidbfs://{remote}/overwrite.csv' (FORMAT CSV, USE_TMP_FILE true);")
            assert query(f"SELECT id FROM read_csv('tidbfs://{remote}/overwrite.csv');") == "9"
            query("CREATE TABLE items AS SELECT i AS id FROM range(100) t(i); "
                  "BEGIN; INSERT INTO items VALUES (100); UPDATE items SET id=101 WHERE id=100; "
                  "DELETE FROM items WHERE id=0; COMMIT; "
                  f"EXPORT DATABASE 'tidbfs://{remote}/backup' (FORMAT PARQUET);")
            assert query(f"IMPORT DATABASE 'tidbfs://{remote}/backup'; SELECT count(*), sum(id) FROM items;") == "100,5051"
            report["write_roundtrips"] = ["Parquet COPY", "partitioned COPY", "temporary-file overwrite", "EXPORT/IMPORT with DML"]
            print(json.dumps(report, indent=2))
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join()
        try:
            with request("DELETE", remote + "/?recursive=1") as response:
                response.read()
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise


if __name__ == "__main__":
    main()
