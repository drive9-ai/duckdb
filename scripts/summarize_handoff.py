#!/usr/bin/env python3
"""Print phase medians and ranges from a completed handoff benchmark JSON."""
import argparse
import json
from pathlib import Path
import statistics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results')
    args = parser.parse_args()
    report = json.loads(Path(args.results).read_text())
    if not report.get('complete') or not all(t['validated'] for t in report['trials']):
        raise SystemExit('Refusing to summarize an incomplete or unvalidated run')
    groups = {mode: [t for t in report['trials'] if t['mode'] == mode]
              for mode in ('checkpointed', 'dirty_wal')}
    phases = [
        ('Old worker: CHECKPOINT', lambda t: [t['checkpoint_seconds']], 'ms'),
        ('Old worker: DETACH + process exit', lambda t: [t['detach_seconds'] + t['old_process_shutdown_seconds']], 'ms'),
        ('Old worker: total flush/close', lambda t: [t['flush_close_seconds']], 'ms'),
        ('New worker: process + plugin bootstrap', lambda t: [t['new_process_bootstrap_seconds']], 'ms'),
        ('New worker: ATTACH/reopen', lambda t: [t['reopen_seconds']], 'ms'),
        ('First full scan, client-cold', lambda t: [t['client_cold_scan_seconds']], 's'),
        ('Repeated full scan, client-warm', lambda t: t['warm_scan_seconds'], 's'),
    ]
    print('| Phase | Already checkpointed | Committed append before checkpoint |')
    print('| --- | ---: | ---: |')
    for label, values, unit in phases:
        cells = []
        for trials in groups.values():
            sample = [v * (1000 if unit == 'ms' else 1) for t in trials for v in values(t)]
            cells.append(f'{statistics.median(sample):.3f} {unit} ({min(sample):.3f}–{max(sample):.3f})')
        print('| ' + label + ' | ' + ' | '.join(cells) + ' |')
    print('\nCells show median (minimum–maximum); warm scan has two samples per trial.')
    print(f"Initial file bytes: {report['initial_database_bytes']:,}; "
          f"initial rows: {report['initial_rows']:,}.")
    for mode, trials in groups.items():
        wal = [t['wal_bytes_before_checkpoint'] for t in trials]
        print(f'{mode}: {len(trials)} fresh-process handoffs; WAL bytes before checkpoint {min(wal):,}–{max(wal):,}.')


if __name__ == '__main__':
    main()
