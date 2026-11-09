import argparse
import math
import sys
from pathlib import Path

from .runner import run


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError('must be positive')
    return number


def positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('must be finite and positive')
    return number


def main():
    parser = argparse.ArgumentParser(description='Generate one SQL query per question and save answers.jsonl-compatible results.')
    parser.add_argument('--questions', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--details', type=Path, help='SQL/results/errors JSONL; defaults to <output stem>.details.jsonl')
    parser.add_argument('--db', type=Path, default=Path(__file__).resolve().parent.parent / 'tmp' / 'businesses.duckdb')
    parser.add_argument('--limit', type=positive_int, help='maximum input lines')
    parser.add_argument('--timeout', type=positive_seconds, default=60, help='worker deadline in seconds (default: 60)')
    parser.add_argument('--max-rows', type=positive_int, default=10000, help='result row limit (default: 10000)')
    args = parser.parse_args()
    try:
        return run(args.questions, args.output, args.db, details_path=args.details, limit=args.limit, timeout=args.timeout, max_rows=args.max_rows)
    except (OSError, ValueError) as exc:
        print(f'question_sql: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
