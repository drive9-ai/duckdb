#!/usr/bin/env python3
"""Download the pinned RustFS test binary and verify its release checksum."""
import argparse
import hashlib
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import zipfile

VERSION = '1.0.3'
# Digests published on the corresponding rustfs/rustfs GitHub release assets.
RELEASES = {
    ('Darwin', 'arm64'): ('macos-aarch64', '9ec6f2ad89df88db9b8889f59e37bb27f6b67c61ee8a97968dd90aaa2ac4dd30'),
    ('Linux', 'x86_64'): ('linux-x86_64-gnu', '0cb0924df93734d14f708004eff4b0e48a147fbdc074756ca0d6b8ec919629b8'),
    ('Linux', 'aarch64'): ('linux-aarch64-gnu', '1c667c8b138fae0ba41786709af49c0b99107f037428d36c8a43ba0627b2408b'),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('build/rustfs'))
    args = parser.parse_args()
    release = RELEASES.get((platform.system(), platform.machine()))
    if release is None:
        parser.error('No pinned binary for this platform; supply your own --rustfs-server to the live harness')
    target, checksum = release
    url = f'https://github.com/rustfs/rustfs/releases/download/{VERSION}/rustfs-{target}-v{VERSION}.zip'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='tidbfs-rustfs-') as tmp:
        archive = Path(tmp) / 'release.zip'
        subprocess.run(['curl', '-fsSL', '--retry', '3', url, '-o', str(archive)], check=True)
        digest = hashlib.sha256()
        with archive.open('rb') as source:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != checksum:
            raise RuntimeError('RustFS release checksum mismatch')
        with zipfile.ZipFile(archive) as source, source.open('rustfs') as binary, args.output.open('wb') as output:
            shutil.copyfileobj(binary, output)
    args.output.chmod(0o755)
    subprocess.run([str(args.output.resolve()), '--version'], check=True)


if __name__ == '__main__':
    main()
