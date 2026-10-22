#!/usr/bin/env python3
"""Repeat real CLI multipart success/abort cases to exercise async scheduling."""
import argparse
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iterations', type=int, default=30)
    parser.add_argument('--async-threads', type=int, help='Constrain DuckDB async I/O threads for stress runs')
    args = parser.parse_args()
    if args.iterations < 1 or (args.async_threads is not None and args.async_threads < 1):
        parser.error('iterations and async-threads must be positive')
    if args.async_threads is not None:
        os.environ['TIDBFS_TEST_ASYNC_THREADS'] = str(args.async_threads)
    sys.path.insert(0, str(ROOT / 'test'))
    from test_extension import ExtensionTest
    names = ('test_failed_multipart_is_aborted', 'test_multipart_copy_and_credential_boundary')
    suite = unittest.TestSuite(ExtensionTest(name) for _ in range(args.iterations) for name in names)
    result = unittest.TextTestRunner(verbosity=1, failfast=True).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)


if __name__ == '__main__':
    main()
