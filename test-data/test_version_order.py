#!/usr/bin/env python3
"""Firmware versions order the way SemVer orders them (audit finding B9).

parse_version used to strip non-digits per component, so "1.2.3-rc1" read as
1.2.31 and outranked 1.2.4: a device on a release candidate was never offered
the final release, and the dashboard showed the update in the wrong direction.

Run: PYTHONPATH=server python3 test-data/test_version_order.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
os.environ.setdefault("API_KEY", "test-api-key-0123456789abcdef")

from firmware import parse_version  # noqa: E402

# Each pair: (older, newer).
ORDERED = [
    ("0.9.9", "0.10.0"),
    ("0.4", "0.4.1"),
    ("1.2.3-rc1", "1.2.3"),
    ("1.2.3-rc1", "1.2.4"),
    ("1.2.3-alpha", "1.2.3-alpha.1"),
    ("1.2.3-alpha.1", "1.2.3-alpha.beta"),
    ("1.2.3-alpha.beta", "1.2.3-beta"),
    ("1.2.3-beta.2", "1.2.3-beta.11"),
    ("1.2.3-rc.1", "1.2.3"),
    ("0.31.0", "0.31.1+b7"),
    ("garbage", "0.0.1"),
]
EQUAL = [("1.2", "1.2.0"), ("1.2.3+b7", "1.2.3"), ("v1.2.3", "1.2.3")]

failures = 0
for older, newer in ORDERED:
    ok = parse_version(older) < parse_version(newer)
    print(f"  [{'PASS' if ok else 'FAIL'}] {older} < {newer}")
    failures += not ok
for a, b in EQUAL:
    ok = parse_version(a) == parse_version(b)
    print(f"  [{'PASS' if ok else 'FAIL'}] {a} == {b}")
    failures += not ok

if failures:
    print(f"\n{failures} check(s) failed")
    sys.exit(1)
print("\nAll version ordering checks passed.")
