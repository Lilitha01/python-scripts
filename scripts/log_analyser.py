#!/usr/bin/env python3
"""
log_analyser.py — Parse and summarise a log file.

Usage:
    python3 scripts/log_analyser.py logs/sample.log
    python3 scripts/log_analyser.py logs/sample.log --level ERROR
    python3 scripts/log_analyser.py logs/sample.log --top 3
"""

import argparse
import sys
from collections import defaultdict


def parse_args():
    parser = argparse.ArgumentParser(description="Analyse a log file")
    parser.add_argument("filepath", help="Path to the log file")
    parser.add_argument("--level", help="Filter to this log level", default=None)
    parser.add_argument("--top", type=int, default=5, help="Top N errors to show")
    return parser.parse_args()


def read_log(filepath):
    try:
        with open(filepath, "r") as f:
            return f.readlines()
    except FileNotFoundError:
        print(f"[ERROR] File not found: {filepath}")
        sys.exit(1)


def count_by_level(lines):
    counts = defaultdict(int)
    for line in lines:
        parts = line.split()
        if len(parts) >= 3:
            counts[parts[2]] += 1
    return counts


def top_errors(lines, n=5):
    errors = defaultdict(int)
    for line in lines:
        if "ERROR" in line:
            parts = line.split(None, 3)
            if len(parts) >= 4:
                errors[parts[3].strip()] += 1
    return sorted(errors.items(), key=lambda x: x[1], reverse=True)[:n]


def filter_by_level(lines, level):
    return [line for line in lines if level.upper() in line]


def main():
    args = parse_args()
    lines = read_log(args.filepath)

    if args.level:
        lines = filter_by_level(lines, args.level)
        print(f"Filtered to level: {args.level.upper()}\n")

    print(f"Total lines: {len(lines)}")

    print("\nLines by level:")
    for level, count in sorted(count_by_level(lines).items()):
        print(f"  {level:<8} {count}")

    print(f"\nTop {args.top} errors:")
    for message, count in top_errors(lines, args.top):
        print(f"  {count}x  {message}")


if __name__ == "__main__":
    main()