"""Generate one local JSON profile per imported business."""

import argparse
import json
import math
import sys
from pathlib import Path

import duckdb

from .profiling import build_profile, write_profile


def _seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('must be finite and positive')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=Path(__file__).resolve().parent.parent / 'tmp' / 'businesses.duckdb')
    parser.add_argument('--business', help='Profile one loaded business ID (default: all loaded businesses)')
    parser.add_argument('--output-dir', type=Path, help='JSON directory (default: <database parent>/business_profiles)')
    parser.add_argument('--timeout', type=_seconds, default=60, help='Query deadline per business in seconds (default: 60)')
    args = parser.parse_args(argv)
    failed = 0
    try:
        if not args.db.is_file():
            raise ValueError(f'database not found: {args.db}')
        output_dir = args.output_dir or args.db.resolve().parent / 'business_profiles'
        with duckdb.connect(str(args.db), read_only=True) as conn:
            businesses = [args.business] if args.business is not None else [row[0] for row in conn.execute("SELECT business_id FROM _catalog.businesses WHERE import_status='loaded' ORDER BY business_id").fetchall()]
            if not businesses:
                raise ValueError('no loaded businesses found')
            for business in businesses:
                try:
                    profile = build_profile(conn, business, timeout=args.timeout)
                    path = write_profile(profile, output_dir)
                    print(json.dumps({'status': 'ok', 'business_id': business, 'output': str(path), 'tables': len(profile['tables'])}, ensure_ascii=False))
                except Exception as exc:
                    failed += 1
                    print(json.dumps({'status': 'error', 'business_id': business, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
    except (OSError, ValueError, duckdb.Error) as exc:
        print(f'business_profile: {exc}', file=sys.stderr)
        return 1
    return int(failed > 0)


if __name__ == '__main__':
    raise SystemExit(main())
