"""Regression guard for measurement field alignment in server/main.py.

Run: python3 test-data/test_measurement_field_alignment.py   (no DB / FastAPI needed)
 or: python3 -m pytest test-data/test_measurement_field_alignment.py

`measurement_row_to_dict` reads the `MEASUREMENT_SELECT_COLUMNS` result by
hard-coded positional indices (`r[0]`, `r[1]`, ... `r[136]`). That coupling is
fragile: if a column is added to the SELECT but not mapped in the dict (or vice
versa), every index below the change silently shifts and the API returns other
columns' values under the wrong names.

This exact bug shipped once already. The SELECT surfaces the GATT
`hiveheart_{1,2}_temp_c` / `hiveheart_{1,2}_humidity_percent` fields from
`raw_json`, but an earlier `measurement_row_to_dict` omitted those four keys.
That shifted the HiveScale block by four positions, so `/measurements/latest`
returned, for a payload reporting weight=0.92 / raw=3466 / pressure=1000:

    hiveheart_1_temp_c            -> (absent)
    hiveheart_1_humidity_percent -> (absent)
    hivescale_1_weight_kg        -> null
    hivescale_1_raw_weight       -> null
    hivescale_1_temp_c           -> null
    hivescale_1_humidity_percent -> null
    hivescale_1_pressure_hpa     -> 0.92   (the weight value)
    hivescale_1_battery_v        -> 3466   (the raw-weight value)

These tests parse main.py (no import, so no DB/FastAPI needed) and assert that
the SELECT column order and the dict's positional indices stay in lockstep, then
replay that real-world payload through the mapping to confirm every HiveHeart /
HiveScale field comes back under its own name.
"""

import json
import os
import re

MAIN_PY = os.path.join(os.path.dirname(__file__), "..", "server", "main.py")

# The exact payload from the field bug report (firmware 0.16.11). Every value
# must round-trip back under its own key.
REPORTED_PAYLOAD = json.loads(
    '{"hiveheart_1_temp_c":24.6,"hiveheart_1_humidity_percent":44.31372,'
    '"hiveheart_1_frequency_hz":64.8,"hiveheart_1_energy":104,'
    '"hiveheart_1_peak":7,"hiveheart_1_battery_v":2.875,'
    '"hivescale_1_weight_kg":0.92,"hivescale_1_raw_weight":3466,'
    '"hivescale_1_temp_c":21.9,"hivescale_1_humidity_percent":47.45098,'
    '"hivescale_1_pressure_hpa":1000,"hivescale_1_battery_v":4.078125}'
)

_failures = 0


def check(name, condition):
    global _failures
    status = "ok" if condition else "FAIL"
    if not condition:
        _failures += 1
    print(f"[{status}] {name}")


def _split_top_level(select_body):
    """Split a SELECT column list on commas that are not inside parentheses."""
    cols, depth, cur = [], 0, ""
    for ch in select_body:
        if ch == "(":
            depth += 1
            cur += ch
        elif ch == ")":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            cols.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        cols.append(cur)
    return cols


def _alias(col):
    """Return the output name of a SELECT column (its `AS alias`, else the bare column)."""
    col = "\n".join(l for l in col.splitlines() if not l.strip().startswith("#")).strip()
    if not col:
        return None
    m = re.search(r"\sAS\s+([A-Za-z0-9_]+)\s*$", col, re.S)
    return m.group(1) if m else col.split()[-1].strip()


def load_select_aliases(src):
    body = re.search(r'MEASUREMENT_SELECT_COLUMNS\s*=\s*"""(.*?)"""', src, re.S).group(1)
    return [a for a in (_alias(c) for c in _split_top_level(body)) if a]


def load_dict_mapping(src):
    body = re.search(
        r"def measurement_row_to_dict\(r\):\s*return \{(.*?)\n    \}", src, re.S
    ).group(1)
    pairs = re.findall(r'"([^"]+)"\s*:\s*r\[(\d+)\]', body)
    return {key: int(idx) for key, idx in pairs}


src = open(MAIN_PY).read()
aliases = load_select_aliases(src)
key_to_idx = load_dict_mapping(src)
n = len(aliases)

# 1. Every SELECT column is read back under its own name at its own position.
#    This is the invariant whose violation caused the shipped bug.
misaligned = [(i, a, key_to_idx.get(a)) for i, a in enumerate(aliases) if key_to_idx.get(a) != i]
check("every SELECT column maps to its same-named key at the correct index", misaligned == [])
for i, a, got in misaligned:
    print(f"        SELECT[{i}] '{a}' is read as r[{got}] (expected r[{i}])")

# 2. No positional index reads past the end of the SELECT result.
overflow = sorted({i for i in key_to_idx.values() if i >= n})
check("no dict index reads past the SELECT column count", overflow == [])
if overflow:
    print(f"        out-of-range indices: {overflow} (SELECT has {n} columns)")

# 3. No SELECT column is left unmapped (every position 0..n-1 is consumed).
mapped = set(key_to_idx.values())
gaps = [i for i in range(n) if i not in mapped]
check("every SELECT column position is mapped (no gaps)", gaps == [])
if gaps:
    print(f"        unmapped SELECT positions: {gaps} ({[aliases[i] for i in gaps]})")

# 4. Replay the real bug-report payload through the mapping. The SELECT exposes
#    each of these fields via COALESCE(column, raw_json->>'field'), so even when
#    the typed columns are NULL the raw_json fallback carries the value; we model
#    that worst case here. Every field must come back under its own name.
row = [REPORTED_PAYLOAD.get(a) for a in aliases]
result = {key: row[idx] for key, idx in key_to_idx.items()}
for field, expected in REPORTED_PAYLOAD.items():
    check(f"{field} round-trips to {expected!r}", result.get(field) == expected)

# 5. Guard the specific cross-contamination from the original report: the weight
#    and raw-weight values must not leak into pressure / battery.
check(
    "weight value does not leak into hivescale_1_pressure_hpa",
    result.get("hivescale_1_pressure_hpa") != REPORTED_PAYLOAD["hivescale_1_weight_kg"],
)
check(
    "raw-weight value does not leak into hivescale_1_battery_v",
    result.get("hivescale_1_battery_v") != REPORTED_PAYLOAD["hivescale_1_raw_weight"],
)


if _failures:
    raise SystemExit(f"{_failures} check(s) failed")
print(f"\nAll measurement field-alignment checks passed ({n} SELECT columns).")
