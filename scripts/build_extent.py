#!/usr/bin/env python3
"""Build the pinned, in-process drive9 extent adapter; no FUSE daemon required."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
# Match drive9's supported HTTP metadata + file/S3/GCS build. Go implements
# GCS directly; JuiceFS's other metadata engines and cloud SDKs are unused.
TAGS = ('nogspt,noredis,nosqlite,nomysql,nopg,notikv,nobadger,noetcd,nocos,nobos,'
        'nohdfs,noibmcos,noobs,nooss,noqingstor,nosftp,noswift,noazure,nogs,noufile,'
        'nob2,nonfs,nodragonfly,nocifs,nostorj,noqiniu,notos,noks3,nowebdav,nogateway')


def build(output):
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, CGO_ENABLED='1')
    subprocess.run(['go', 'build', '-mod=readonly', '-tags', TAGS, '-buildmode=c-archive',
                    '-o', str(output), '.'], cwd=ROOT / 'bridge', env=env, check=True)


if __name__ == '__main__':
    build(sys.argv[1] if len(sys.argv) > 1 else ROOT / 'build/libtidbfs_extent.a')
