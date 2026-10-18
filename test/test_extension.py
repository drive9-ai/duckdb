"""Exercise the compiled extension through the real DuckDB CLI, over HTTP."""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DUCKDB = os.environ.get("DUCKDB", "duckdb")
EXTENSION = ROOT / "build/tidbfs.duckdb_extension"


def sql_string(value):
    return "'" + str(value).replace("'", "''") + "'"


class Fixture:
    def __init__(self, files):
        self.files = files
        self.requests = []
        self.bytes = 0
        self.fault = None
        self.object_expired = False
        self.object_reads = 0
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def handle(self):
                try:
                    super().handle()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # bounded-body rejection intentionally closes the connection

            def do_HEAD(self):
                self.serve(True)

            def do_GET(self):
                self.serve(False)

            def reply(self, status, headers=None, body=b"", head=False):
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, str(value))
                if "Content-Length" not in (headers or {}):
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if not head:
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            def serve(self, head):
                target = urlsplit(self.path)
                object_read = target.path.startswith("/objects/")
                path = unquote(target.path[len("/objects") if object_read else len("/v1/fs"):])
                fixture.requests.append({"method": self.command, "path": path, "object": object_read,
                                         "authorization": self.headers.get("Authorization"),
                                         "if_match": self.headers.get("If-Match"), "range": self.headers.get("Range"),
                                         "list": bool(target.query)})
                if object_read and self.headers.get("Authorization"):
                    return self.reply(403)
                if not object_read and self.headers.get("Authorization") != "Bearer fixture-token":
                    return self.reply(403)
                if target.query == "list=1":
                    entries = {}
                    for name, body in fixture.files.items():
                        if name.startswith(path):
                            child = name[len(path):].split("/")[0]
                            if child:
                                entries[child] = {"name": child, "size": len(body),
                                                  "isDir": "/" in name[len(path):]}
                    return self.reply(200, body=json.dumps({"entries": list(entries.values())}).encode())
                if path not in fixture.files:
                    return self.reply(404)
                body = fixture.files[path]
                etag = '"object-v1"' if object_read else '"inode-v1"'
                headers = {"ETag": etag, "Accept-Ranges": "bytes", "X-Dat9-IsDir": "false",
                           "X-Dat9-Content-Layout": "single", "X-Dat9-Mtime": "1700000000"}
                if head:
                    headers["Content-Length"] = len(body)
                    if fixture.fault == "legacy":
                        del headers["ETag"]
                    if fixture.fault == "extent":
                        headers["X-Dat9-Content-Layout"] = "extent"
                    return self.reply(200, headers, head=True)
                if fixture.fault == "changed" or (self.headers.get("If-Match") and self.headers["If-Match"] != etag):
                    return self.reply(412)
                if path.startswith("/redirect/") and not object_read:
                    headers["Location"] = "/objects" + target.path[len("/v1/fs"):]
                    if fixture.fault == "unsafe_redirect":
                        headers["Location"] = "http://example.invalid/secret?credential=must-not-leak"
                    return self.reply(302, headers)
                if object_read:
                    fixture.object_reads += 1
                    if fixture.fault == "always_expired" or (fixture.fault == "expire_once" and self.headers.get("If-Match") == '"object-v1"' and not fixture.object_expired):
                        fixture.object_expired = True
                        return self.reply(403)
                match = re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
                if not match:
                    return self.reply(400)
                start, end = map(int, match.groups())
                data = body[start:end + 1]
                fixture.bytes += len(data)
                headers["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
                if fixture.fault == "wrong_range":
                    headers["Content-Range"] = f"bytes {start + 1}-{end + 1}/{len(body)}"
                if fixture.fault == "short":
                    data = data[:-1]
                if fixture.fault == "encoded":
                    headers["Content-Encoding"] = "gzip"
                if fixture.fault == "ignore_range":
                    return self.reply(200, headers, body)
                return self.reply(206, headers, data)

        class Server(ThreadingHTTPServer):
            request_queue_size = 128

        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class ExtensionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not EXTENSION.is_file():
            raise RuntimeError("Build the tidbfs extension before running tests")
        cls.temp = tempfile.TemporaryDirectory()
        file = Path(cls.temp.name) / "orders.parquet"
        sql = (f"COPY (SELECT i::BIGINT AS id, (i % 17)::INTEGER AS bucket, "
               f"md5(i::VARCHAR) || md5((i+1)::VARCHAR) || md5((i+2)::VARCHAR) AS payload "
               f"FROM range(100000) t(i)) TO {sql_string(file)} (FORMAT PARQUET, ROW_GROUP_SIZE 10000);")
        subprocess.run([DUCKDB, "-c", sql], check=True, capture_output=True)
        cls.parquet = file.read_bytes()
        footer = Path(cls.temp.name) / "footer.parquet"
        columns = ",".join(f"i::BIGINT AS c{n}" for n in range(32))
        subprocess.run([DUCKDB, "-c", f"COPY (SELECT {columns} FROM range(32768) t(i)) TO {sql_string(footer)} (FORMAT PARQUET, ROW_GROUP_SIZE 2048);"], check=True, capture_output=True)
        cls.footer = footer.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.fixture = Fixture({"/data/orders.parquet": self.parquet,
                                "/data/footer.parquet": self.footer,
                                "/data/items.csv": b"id,name\n1,first\n2,second\n",
                                "/data/items.json": b'{"id":1}\n{"id":2}\n',
                                "/redirect/orders.parquet": self.parquet,
                                "/partition/day=1/orders.parquet": self.parquet,
                                "/partition/day=2/orders.parquet": self.parquet,
                                "/data/中文 % #.parquet": self.parquet})
        self.addCleanup(self.fixture.close)

    def query(self, sql, ok=True, prefetch=65536):
        script = (f"LOAD {sql_string(EXTENSION)}; "
                  f"CREATE SECRET fs (TYPE tidbfs, ENDPOINT {sql_string(self.fixture.endpoint)}, TOKEN 'fixture-token'); "
                  f"SET tidbfs_read_ahead={prefetch}; SET threads=4; " + sql)
        # SQL, including credentials, travels on stdin, never process arguments.
        result = subprocess.run([DUCKDB, "-unsigned", "-csv", "-noheader", "-bail"],
                                input=script, text=True, capture_output=True, timeout=45)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result.stdout.removeprefix("true\n").strip(), result.stderr

    def test_parquet_projection_and_row_group_pruning(self):
        out, _ = self.query("SELECT sum(id) FROM read_parquet('tidbfs:///data/orders.parquet') WHERE id < 2048;")
        self.assertEqual(out, str(2047 * 2048 // 2))
        narrow = self.fixture.bytes
        self.assertLess(narrow, len(self.parquet) // 5)
        self.fixture.bytes = 0
        out, _ = self.query("SELECT sum(length(payload)) FROM read_parquet('tidbfs:///data/orders.parquet');")
        self.assertEqual(out, str(100000 * 96))
        self.assertLess(narrow, self.fixture.bytes // 5)
        print(f"\nParquet bytes: narrow={narrow}, wide={self.fixture.bytes}, file={len(self.parquet)}")

    def test_partition_glob(self):
        out, _ = self.query("SELECT sum(id) FROM read_parquet('tidbfs:///partition/day=*/*.parquet', hive_partitioning=true) WHERE day=2;")
        self.assertEqual(out, str(99999 * 100000 // 2))
        content = [r for r in self.fixture.requests if r["method"] == "GET" and not r["list"]]
        day1 = sum(int(r["range"].split("-")[1]) - int(r["range"].split("=")[1].split("-")[0]) + 1
                   for r in content if "day=1" in r["path"])
        self.assertLessEqual(day1, 2 * 65536)  # schema/footer probes only

    def test_recursive_glob_and_encoded_paths(self):
        out, _ = self.query("SELECT count(*) FROM read_parquet('tidbfs:///partition/**/*.parquet');")
        self.assertEqual(out, "200000")
        out, _ = self.query("SELECT count(*) FROM read_parquet('tidbfs:///data/中文 % #.parquet');")
        self.assertEqual(out, "100000")

    def test_object_redirect_reuse_strips_credentials(self):
        out, _ = self.query("SELECT sum(length(payload)) FROM read_parquet('tidbfs:///redirect/orders.parquet');")
        self.assertEqual(out, str(100000 * 96))
        objects = [r for r in self.fixture.requests if r["object"]]
        redirects = [r for r in self.fixture.requests if not r["object"] and r["method"] == "GET"]
        self.assertGreater(len(objects), len(redirects))
        self.assertTrue(all(r["authorization"] is None for r in objects))
        self.assertTrue(all(r["if_match"] in (None, '"object-v1"') for r in objects))
        self.assertTrue(all(r["if_match"] == '"inode-v1"' for r in redirects))
        self.assertEqual(len([r for r in self.fixture.requests if r["method"] == "HEAD"]), 1)

    def test_query_metadata_cache_expires(self):
        sql = "SELECT count(*) FROM read_parquet('tidbfs:///data/orders.parquet');"
        out, _ = self.query(sql + sql)
        self.assertEqual(out, "100000\n100000")
        self.assertEqual(len([r for r in self.fixture.requests if r["method"] == "HEAD"]), 2)

    def test_parallel_row_groups_and_other_formats(self):
        out, _ = self.query("SET threads=16; SELECT sum(length(payload)) FROM read_parquet('tidbfs:///redirect/orders.parquet');")
        self.assertEqual(out, "9600000")
        self.assertEqual(len([r for r in self.fixture.requests if r["method"] == "HEAD"]), 1)
        out, _ = self.query("SELECT sum(id) FROM read_csv('tidbfs:///data/items.csv');")
        self.assertEqual(out, "3")
        out, _ = self.query("SELECT sum(id) FROM read_json('tidbfs:///data/items.json');")
        self.assertEqual(out, "3")

    def test_read_only_and_invalid_paths(self):
        _, error = self.query("COPY (SELECT 1) TO 'tidbfs:///data/out.parquet' (FORMAT PARQUET);", ok=False)
        self.assertIn("read-only", error)
        for path in ("tidbfs://host/file.parquet", "tidbfs:///data/../file.parquet"):
            with self.subTest(path=path):
                self.fixture.requests.clear()
                self.query(f"SELECT * FROM read_parquet({sql_string(path)});", ok=False)
                self.assertEqual(self.fixture.requests, [])

    def test_expired_object_url_refresh(self):
        # Expire after the first range succeeds, so this exercises reuse/refresh.
        self.fixture.fault = "expire_once"
        out, _ = self.query("SELECT sum(length(payload)) FROM read_parquet('tidbfs:///redirect/orders.parquet');")
        self.assertEqual(out, "9600000")
        self.assertTrue(self.fixture.object_expired)
        self.fixture.fault = "always_expired"
        _, error = self.query("SELECT count(*) FROM read_parquet('tidbfs:///redirect/orders.parquet');", ok=False)
        self.assertIn("HTTP 403", error)  # newly issued invalid URLs are not retried forever

    def test_protocol_failures(self):
        for fault, expected in [("changed", "changed"), ("wrong_range", "byte-range"), ("short", "byte-range"),
                                ("encoded", "byte-range"), ("ignore_range", "transport"),
                                ("legacy", "Upgrade fs"), ("extent", "extent")]:
            with self.subTest(fault=fault):
                self.fixture.fault = fault
                _, error = self.query("SELECT sum(id) FROM read_parquet('tidbfs:///data/orders.parquet');", ok=False)
                self.assertIn(expected, error)

    def test_unsafe_redirect_and_token_redaction(self):
        self.fixture.fault = "unsafe_redirect"
        _, error = self.query("SELECT count(*) FROM read_parquet('tidbfs:///redirect/orders.parquet');", ok=False)
        self.assertNotIn("must-not-leak", error)
        self.assertIn("Unsafe tidbfs object redirect", error)
        out, _ = self.query("SELECT secret_string FROM duckdb_secrets() WHERE type='tidbfs';")
        self.assertNotIn("fixture-token", out)

    def test_tail_prefetch_reduces_requests(self):
        sql = "SELECT count(*) FROM read_parquet('tidbfs:///data/footer.parquet');"
        self.query(sql, prefetch=0)
        without = len([r for r in self.fixture.requests if r["range"]])
        self.fixture.requests.clear()
        self.query(sql)
        with_prefetch = len([r for r in self.fixture.requests if r["range"]])
        self.assertLess(with_prefetch, without)
        print(f"\nFooter requests: no prefetch={without}, prefetch={with_prefetch}")


if __name__ == "__main__":
    unittest.main()
