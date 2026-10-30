"""Rules for turning HiveTraffic lifetime totals into per-interval counts.

A HiveTraffic counter reports monotonic lifetime ``total_in``/``total_out`` and
nothing per interval; every consumer differences consecutive totals. The read
APIs (measurements.difference_bee_counter_intervals) and the insight engine
(insights._extract_counter_series) each walk their own rows, but the question
of whether the counter restarted between two samples has one answer, and it
lives here so the chart and the insight card cannot disagree about it.

Pure logic with no database or FastAPI dependency: the mock server ships a
verbatim copy next to its copy of insights.py (CI diffs the two).
"""

from __future__ import annotations

from typing import Optional

# How far the counter's own uptime may fall short of the wall-clock time between
# two samples before that is read as a restart. Both readings are stamped by the
# hub at read time, but counters are read one after another within a wake (and
# the hub clock can be corrected by NTP between wakes), so the two clocks are
# allowed to disagree by this much.
RESET_SLACK_S = 90


def counter_restarted(
    prev_total: Optional[float],
    cur_total: Optional[float],
    prev_uptime_s: Optional[float] = None,
    cur_uptime_s: Optional[float] = None,
    elapsed_s: Optional[float] = None,
) -> bool:
    """Did the counter restart (or wrap) between two samples of the same unit?

    The totals live in the counter's RAM and restart from zero on every reset,
    so a decrease is the obvious signal. It is not a sufficient one: a counter
    that rebooted and then counted past its old total before the next poll
    reports an INCREASE, and differencing that as an ordinary interval silently
    drops everything counted since the restart. The counter's ``uptime_s``
    catches that case:

    * uptime went backwards — it restarted since the previous sample;
    * uptime is shorter than the time between the two samples (beyond
      RESET_SLACK_S) — it restarted after the previous sample, even when that
      sample's own uptime was shorter still.

    Rows without uptime (wired-era history, fixtures) fall back to the totals.
    """
    if prev_total is None or cur_total is None:
        return False
    if cur_total < prev_total:
        return True
    if prev_uptime_s is not None and cur_uptime_s is not None:
        if cur_uptime_s < prev_uptime_s:
            return True
        if elapsed_s is not None and cur_uptime_s + RESET_SLACK_S < elapsed_s:
            return True
    return False


def interval_between(prev_total, cur_total, restarted: bool):
    """Crossings between two totals: everything since the restart when the
    counter restarted (what happened before it, after the previous sample, is
    unrecoverable), the plain difference otherwise."""
    return cur_total if restarted else cur_total - prev_total
