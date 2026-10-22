#!/usr/bin/env python3
"""
Summarise public-bot grounding_metrics log lines (no personal data).

Usage:
  python scripts/grounding_report.py path/to/app.log
  cat app.log | python scripts/grounding_report.py -
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def _iter_lines(path: str):
    if path == "-":
        for line in sys.stdin:
            yield line
        return
    with Path(path).open(encoding="utf-8", errors="replace") as f:
        for line in f:
            yield line


def main(argv: list[str]) -> int:
    src = argv[1] if len(argv) > 1 else "-"
    total = 0
    failed = 0
    regen = 0
    det = 0
    claim_types: Counter = Counter()

    for line in _iter_lines(src):
        if "grounding_metrics" not in line:
            continue
        # Prefer JSON payload after the marker
        idx = line.find("{")
        if idx < 0:
            continue
        try:
            payload = json.loads(line[idx:])
        except json.JSONDecodeError:
            continue
        if payload.get("event") != "grounding_metrics":
            continue
        total += 1
        if payload.get("verifier_failed"):
            failed += 1
        if payload.get("regeneration_used"):
            regen += 1
        if payload.get("deterministic_fallback"):
            det += 1
        for ct in payload.get("claim_types") or []:
            claim_types[str(ct)] += 1

    rate = (100.0 * failed / total) if total else 0.0
    print(f"turns_logged: {total}")
    print(f"verifier_failed: {failed} ({rate:.1f}%)")
    print(f"regeneration_used: {regen}")
    print(f"deterministic_fallback: {det}")
    if claim_types:
        print("claim_types:")
        for k, v in claim_types.most_common(20):
            print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
