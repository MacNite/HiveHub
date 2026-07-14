"""Shared HiveHeart FFT decoding, frequency metadata and derived features.

This module is the single source of truth for the packed-nibble FFT that
beehivemonitoring.com **HiveHeart** sensors report in payload bytes 12–19.

Wire format
-----------
HiveHeart sends an 8-byte array. Each byte packs two unsigned 4-bit values, the
**high nibble first**::

    bins.append((byte >> 4) & 0x0F)   # high nibble
    bins.append(byte & 0x0F)          # low nibble

So the 8 raw bytes decode to 16 values, each a *relative level* from 0 to 15.
They are **not** dB or dBFS and must never be compared directly with the
microphone dBFS band values.

The raw 8-byte array stays the canonical stored representation everywhere
(``raw_json``); the decoded ``fft_bins`` and the higher-level features below are
derived on read. Keep the raw array — do not replace it.

Example::

    decode_fft([103, 244, 83, 34, 17, 17, 0, 17])
    == [6, 7, 15, 4, 5, 3, 2, 2, 1, 1, 1, 1, 0, 0, 1, 1]
"""

from __future__ import annotations

from typing import Any, Optional

# ── Wire / encoding constants ────────────────────────────────────────────────
FFT_RAW_LEN = 8          # bytes on the wire
FFT_BIN_COUNT = 16       # decoded nibble values
FFT_ENCODING = "packed_u4_hi_lo"
FFT_LEVEL_MAX = 15       # decoded values are relative levels 0–15 (not dB)

# ── Frequency range table (16 bins) ──────────────────────────────────────────
# TODO(hiveheart-freq-table): The supplied HiveHeart range table is irregular.
# Besides a known GAP from 845 to 853 Hz between bins 9 and 10, several steps are
# not the ~93.75 Hz a clean grid would give (e.g. bin 6 is 480–562). These
# boundaries are preserved VERBATIM as delivered by the HiveHeart vendor — do not
# silently "correct" them. When the true bin boundaries are confirmed against
# hardware, fix them HERE in this one place and everything downstream follows.
FFT_RANGES_HZ: list[tuple[int, int]] = [
    (0, 93),       # 1
    (94, 187),     # 2
    (188, 281),    # 3
    (282, 375),    # 4
    (376, 479),    # 5
    (480, 562),    # 6
    (563, 656),    # 7
    (657, 750),    # 8
    (751, 844),    # 9   ── gap 845–853 Hz ──
    (854, 937),    # 10
    (938, 1031),   # 11
    (1032, 1125),  # 12
    (1126, 1218),  # 13
    (1219, 1312),  # 14
    (1313, 1406),  # 15
    (1407, 1500),  # 16
]

# "0–93", "94–187", … — en-dash separated, matches the dashboard x-axis labels.
FFT_RANGE_LABELS: list[str] = [f"{lo}–{hi}" for lo, hi in FFT_RANGES_HZ]


def fft_ranges_hz() -> list[list[int]]:
    """The 16 frequency ranges as JSON-friendly ``[[lo, hi], …]`` pairs."""
    return [[lo, hi] for lo, hi in FFT_RANGES_HZ]


def range_midpoints() -> list[float]:
    """Midpoint (Hz) of each of the 16 frequency ranges."""
    return [(lo + hi) / 2.0 for lo, hi in FFT_RANGES_HZ]


# ── Decoding ─────────────────────────────────────────────────────────────────
def _is_raw_byte(b: Any) -> bool:
    # Reject bools (a bool is an int subclass) and out-of-range / non-int values.
    return isinstance(b, int) and not isinstance(b, bool) and 0 <= b <= 255


def decode_fft(raw: Any) -> Optional[list[int]]:
    """Decode an 8-byte packed-nibble FFT array into 16 relative levels (0–15).

    Returns ``None`` for missing or malformed data (wrong length, non-integer or
    out-of-range byte) so legacy / corrupt rows are safely *omitted* rather than
    raising and blowing up an entire measurement response. The raw array is never
    mutated.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != FFT_RAW_LEN:
        return None
    bins: list[int] = []
    for b in raw:
        if not _is_raw_byte(b):
            return None
        bins.append((b >> 4) & 0x0F)
        bins.append(b & 0x0F)
    return bins


def _valid_bins(bins: Any) -> bool:
    return (
        isinstance(bins, (list, tuple))
        and len(bins) == FFT_BIN_COUNT
        and all(isinstance(v, int) and not isinstance(v, bool) for v in bins)
    )


def _as_bins(value: Any) -> Optional[list[int]]:
    """Accept either an already-decoded 16-value bin list or a raw 8-byte array."""
    if _valid_bins(value):
        return list(value)
    return decode_fft(value)


# ── Derived features ─────────────────────────────────────────────────────────
def total_activity(bins: Any) -> Optional[int]:
    """Sum of all 16 relative bin values (total relative spectrum activity)."""
    b = _as_bins(bins)
    return sum(b) if b is not None else None


def dominant_range(bins: Any) -> Optional[dict]:
    """Index and Hz boundaries of the largest bin.

    Returns ``None`` for malformed input or a zero-energy spectrum (no dominant
    frequency is meaningful when every bin is zero).
    """
    b = _as_bins(bins)
    if b is None or sum(b) == 0:
        return None
    idx = max(range(FFT_BIN_COUNT), key=lambda i: b[i])
    lo, hi = FFT_RANGES_HZ[idx]
    return {
        "index": idx,
        "lower_hz": lo,
        "upper_hz": hi,
        "midpoint_hz": (lo + hi) / 2.0,
    }


def spectral_centroid_hz(bins: Any) -> Optional[float]:
    """Relative-level-weighted spectral centroid, using range midpoints.

    Returns ``None`` when total relative activity is zero (undefined centroid).
    """
    b = _as_bins(bins)
    if b is None:
        return None
    total = sum(b)
    if total == 0:
        return None
    mids = range_midpoints()
    return sum(v * m for v, m in zip(b, mids)) / total


# Conceptual acoustic bands used across HiveHub insights. HiveHeart bins cross
# these boundaries, so aggregation is overlap-weighted rather than assigning a
# whole bin to one band. Sub-bass is widened to 0 Hz (vs the microphone path's
# 50–150 Hz) so the HiveHeart 0–93 Hz bin is captured; see docs/insights.md.
SEMANTIC_BANDS: list[tuple[str, int, int]] = [
    ("sub_bass", 0, 150),
    ("hum", 150, 300),
    ("piping", 300, 550),
    ("stress", 550, 1500),
]


def _overlap(a_lo: float, a_hi: float, b_lo: float, b_hi: float) -> float:
    """Width of the overlap between two [lo, hi] intervals (0 if disjoint)."""
    return max(0.0, min(a_hi, b_hi) - max(a_lo, b_lo))


def semantic_bands(bins: Any) -> Optional[dict]:
    """Aggregate the 16 HiveHeart bins into conceptual acoustic bands.

    Uses proportional overlap weighting so a bin that straddles two semantic
    ranges contributes to each in proportion to the overlap width::

        contribution = bin_value * overlap_width(bin_range, target_range) / bin_width

    Returns a ``{band_name: weighted_level}`` dict, or ``None`` for malformed
    input. A zero-energy spectrum yields all-zero bands (not ``None``) so callers
    can distinguish "silent" from "no data".
    """
    b = _as_bins(bins)
    if b is None:
        return None
    out: dict[str, float] = {name: 0.0 for name, _, _ in SEMANTIC_BANDS}
    for i, (blo, bhi) in enumerate(FFT_RANGES_HZ):
        bin_width = bhi - blo
        if bin_width <= 0:
            continue
        for name, lo, hi in SEMANTIC_BANDS:
            ov = _overlap(blo, bhi, lo, hi)
            if ov > 0:
                out[name] += b[i] * ov / bin_width
    return out


def fft_features(value: Any) -> Optional[dict]:
    """Full feature bundle for a raw 8-byte array *or* decoded 16-bin list.

    Returns ``None`` for missing / malformed data. Otherwise::

        {
          "bins": [...16...],
          "total_activity": int,
          "dominant_range": {index, lower_hz, upper_hz, midpoint_hz} | None,
          "spectral_centroid_hz": float | None,
          "semantic_bands": {sub_bass, hum, piping, stress},
        }
    """
    b = _as_bins(value)
    if b is None:
        return None
    return {
        "bins": b,
        "total_activity": total_activity(b),
        "dominant_range": dominant_range(b),
        "spectral_centroid_hz": spectral_centroid_hz(b),
        "semantic_bands": semantic_bands(b),
    }
