#!/usr/bin/env python3
"""Build against an installed DuckDB C++ SDK and CLI of the same version."""

import argparse
import hashlib
import os
from pathlib import Path
import platform
import shlex
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
JSON_SHA256 = "9bea4c8066ef4a1c206b2be5a36302f8926f7fdc6087af5d20b417d0cf103ea6"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb", default=os.environ.get("DUCKDB", "duckdb"))
    parser.add_argument("--include", default=os.environ.get("DUCKDB_INCLUDE", "/opt/homebrew/opt/duckdb/include"))
    parser.add_argument("--static-library", default=os.environ.get("DUCKDB_STATIC_LIBRARY"),
                        help="matching libduckdb_static.a (required on Linux)")
    args = parser.parse_args()
    includes = Path(args.include).resolve()
    if not (includes / "duckdb/common/file_system.hpp").is_file():
        parser.error("--include must point to the matching DuckDB C++ SDK (or DuckDB src/include)")
    system = platform.system()
    if system == "Linux" and (not args.static_library or not Path(args.static_library).is_file()):
        parser.error("Linux requires --static-library /path/to/matching/libduckdb_static.a")
    if args.static_library and system != "Linux":
        parser.error("--static-library is currently supported only on Linux")
    version = subprocess.check_output([args.duckdb, "-csv", "-noheader", "-c", "SELECT version()"], text=True).strip()
    target = subprocess.check_output([args.duckdb, "-csv", "-noheader", "-c", "PRAGMA platform"], text=True).strip()
    build = ROOT / "build"
    deps = build / "deps"
    deps.mkdir(parents=True, exist_ok=True)
    header = deps / "json.hpp"
    if not header.exists():
        with urllib.request.urlopen("https://raw.githubusercontent.com/nlohmann/json/v3.11.3/single_include/nlohmann/json.hpp", timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != JSON_SHA256:
            raise RuntimeError("JSON dependency checksum mismatch")
        header.write_bytes(data)
    if hashlib.sha256(header.read_bytes()).hexdigest() != JSON_SHA256:
        raise RuntimeError("JSON dependency checksum mismatch")
    output = build / "tidbfs.duckdb_extension"
    command = [os.environ.get("CXX", "c++"), "-std=c++17", "-O2", "-DNDEBUG", "-fPIC", "-Wall", "-Wextra",
               "-Wno-unused-parameter", "-Wno-deprecated-declarations", "-I" + str(includes), "-I" + str(deps)]
    if system == "Darwin":
        command += ["-dynamiclib", "-undefined", "dynamic_lookup"]
    elif system == "Linux":
        # Match DuckDB's EXTENSION_STATIC_BUILD: official Linux CLIs do not
        # export all C++ filesystem symbols. Hide the linked DuckDB copy.
        command += ["-shared", "-fvisibility=hidden", "-ffunction-sections", "-fdata-sections"]
        if target.endswith("_gcc4"):
            command += ["-D_GLIBCXX_USE_CXX11_ABI=0"]
    else:
        parser.error("This build script supports macOS and Linux")
    command += shlex.split(os.environ.get("CXXFLAGS", ""))
    command += [str(ROOT / "src/tidbfs_extension.cpp"), "-o", str(output)]
    if system == "Linux":
        command += [str(Path(args.static_library).resolve()), "-Wl,--gc-sections", "-Wl,--exclude-libs,ALL", "-pthread", "-ldl"]
    command += ["-lcurl"]
    subprocess.run(command, check=True)
    # DuckDB v1.4.x scripts/append_metadata.cmake: 8 reversed 32-byte fields,
    # then 256 bytes reserved for a signature. The local build is unsigned.
    fields = ["4", target, version, "0.1.0", "CPP", "", "", ""]
    if any(len(field.encode()) > 32 for field in fields):
        raise RuntimeError("DuckDB metadata field exceeds 32 bytes")
    with output.open("ab") as handle:
        handle.write(b"\x00\x93\x04\x10duckdb_signature\x80\x04")
        for field in reversed(fields):
            handle.write(field.encode().ljust(32, b"\x00"))
        handle.write(bytes(256))
    subprocess.run([args.duckdb, "-unsigned", "-c", "LOAD '" + str(output).replace("'", "''") + "';"], check=True)
    print(f"Built and loaded {output} ({version}, {target})")


if __name__ == "__main__":
    main()
