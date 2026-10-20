#!/usr/bin/env python3
"""Install the extension and its Linux runtime together, then verify LOAD."""
import argparse
import csv
import io
import os
from pathlib import Path
import platform
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def install(duckdb, directory=None):
    extension = ROOT / 'build/tidbfs.duckdb_extension'
    runtime = ROOT / 'build/libtidbfs_extent.so'
    if not extension.is_file() or (platform.system() == 'Linux' and not runtime.is_file()):
        raise RuntimeError('Build the extension and runtime with make build first')
    settings = f'SET extension_directory={quote(Path(directory).resolve())}; ' if directory else ''
    command = [duckdb, '-unsigned', '-bail', '-csv', '-noheader']
    result = subprocess.run(command, input=settings + f'FORCE INSTALL {quote(extension)}; '
                            "SELECT install_path FROM duckdb_extensions() WHERE extension_name='tidbfs';",
                            text=True, capture_output=True, check=True)
    rows = list(csv.reader(io.StringIO(result.stdout)))
    if len(rows) != 1 or len(rows[0]) != 1 or not rows[0][0]:
        raise RuntimeError('DuckDB did not report an installed extension path')
    installed = Path(rows[0][0])
    if platform.system() == 'Linux':
        shutil.copyfile(runtime, installed.parent / runtime.name)
    subprocess.run(command, input=settings + 'LOAD tidbfs;', text=True, check=True)
    print('Installed and loaded ' + str(installed))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duckdb', default=os.environ.get('DUCKDB', 'duckdb'))
    parser.add_argument('--directory', help='Override DuckDB extension_directory')
    args = parser.parse_args()
    install(args.duckdb, args.directory)
