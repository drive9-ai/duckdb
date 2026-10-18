# Server compatibility

The extension speaks the TiDB Filesystem HTTP API. It does not connect directly
to a TiDB SQL endpoint and does not bundle the filesystem server. Deploy the
server read-path changes before using this extension against an existing service.

## File metadata

`HEAD /v1/fs/<path>` must return:

* `Content-Length`: the full file size in bytes.
* `ETag`: a strong validator that incorporates inode identity, revision, and
  size. Recreating a deleted path must change the validator.
* `Accept-Ranges: bytes`.
* `X-Dat9-IsDir`, `X-Dat9-Content-Layout`, and optionally `X-Dat9-Mtime`
  (Unix seconds).

The extension rejects missing/weak validators and extent-layout files. Metadata
is cached only within the current DuckDB query, with endpoint and credentials
included in the cache key.

## Conditional range reads

The extension sends `GET /v1/fs/<path>` with `Range: bytes=<start>-<end>` and
`If-Match: <filesystem ETag>`. Authorization and version checks must precede
returning bytes or an object URL. A version mismatch returns 412.

Inline and append-log responses return status 206 with the exact bytes,
Content-Length, Content-Range (including total file size), and the filesystem
ETag. Full-body fallbacks, compression, short responses, or changed bounds are
rejected. The inline path should use SQL `SUBSTRING`/`RIGHT` projections so
unneeded blob bytes do not travel from TiDB to the server. Metadata and projected
bytes must come from the same SQL statement/snapshot, preserving tenant scoping.

Single-object files may return 302 or 307 with the filesystem ETag and Location.
The extension requests that URL separately, stripping the filesystem bearer
token and validator. Object responses need exact 206 ranges and a strong object
ETag; subsequent requests use that object ETag with If-Match. The object URL must
identify an immutable generation. One refresh through fs is allowed on an
expired URL, still requiring the original filesystem ETag.

HTTPS is required. Loopback HTTP is allowed for development, including object
redirects only to the same origin. Signed URLs and response bodies are excluded
from transport diagnostics.

## Discovery

`GET /v1/fs/<directory>/?list=1` returns an `entries` array containing `name`
(one path component), `size`, and `isDir`. The extension traverses only directory
prefixes needed by the glob and preserves normal DuckDB partition pruning.
Directory-list authorization is separate from file-read authorization; scoped
credentials must permit both when a query uses globs.

## Writes and namespace operations

Writable outputs use the existing server write protocol:

* `GET /v1/status`: `inline_threshold` and optionally `max_upload_bytes`.
  Nonempty files **at or above** the threshold use multipart uploads.
* HEAD of an existing output must include a positive `X-Dat9-Revision`.
* `PUT /v1/fs/<path>` with `X-Dat9-Expected-Revision`: `0` creates only if
  absent; a positive revision conditionally replaces the current generation.
  Successful inline writes return JSON `{ "revision": <committed revision> }`.
* `POST /v2/uploads/initiate` with `path`, `total_size`, `expected_revision`;
  `/presign` with `part_number`; direct signed part PUTs; `/complete` with
  `parts: [{number, etag}]`; `/abort` on failure. Required checksum contracts
  are rejected until supported. Filesystem tokens and conditional headers are
  never copied to object storage. Presigned headers are restricted to content
  type and object-provider headers.
* `POST /v1/fs/<directory>/?mkdir`, `DELETE ...?kind=file`,
  `DELETE /v1/fs/<directory>/?recursive`, and `POST /v1/fs/<target>?rename`.
  Rename sends `X-Dat9-Rename-Source` with `X-Dat9-Path-Encoding: base64url`.
  Source and destination must resolve to the same endpoint and credentials.

Revision conflicts return 409. Revision-only writes do not fence inode
replacement or provide distributed file locking. Namespace mutations are not
rolled back with a failed SQL transaction. Sync/close publishes the complete
staged output; it is not a database WAL durability protocol. Server upload limits
and credential permissions continue to apply.

## Consistency and scope

Each query pins each opened file's version across its row-group readers. Inline
updates fail instead of mixing revisions. Immutable object generations can
continue serving their old bytes, or fail if garbage collection removes them.
There is no multi-file or transaction-wide snapshot guarantee.

The included `scripts/smoke.py` verifies the actual TiDB-backed inline path using
a disposable local filesystem with `DRIVE9_INLINE_THRESHOLD=16777216`. The
ordinary `make test` suite uses an instrumented HTTP fixture and needs no server
or credentials. Both execute the real DuckDB CLI.
