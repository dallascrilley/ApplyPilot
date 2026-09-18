"""Per-surface apply pacing — make the *rhythm* of applications human.

Fingerprint hygiene (see chrome.py) covers what a page can observe about the
browser. This module covers what a server can observe about the *account*:
how many applications land per hour/day, how evenly spaced they are, and
whether they arrive at 03:40 local time. Those signals are cheap for a site
to compute and impossible to spoof from the client — the only defence is to
actually behave like a person.

Configured in ``~/.applypilot/searches.yaml``::

    pacing:
      linkedin:
        max_per_hour: 5
        max_per_day: 12
        min_gap_seconds: 90
        max_gap_seconds: 240      # jitter ceiling (gap is uniform in [min, max])
        active_hours: [8, 22]     # local time, half-open [start, end)
        active_days: [0, 1, 2, 3, 4, 5, 6]   # Mon=0 .. Sun=6
      default:                    # applies to any ATS without its own block
        max_per_hour: 12
        max_per_day: 40
        min_gap_seconds: 20
        max_gap_seconds: 75
        active_hours: [7, 23]

A surface key is the ``detect_ats`` slug ("linkedin", "greenhouse", ...);
jobs with no detectable ATS use ``default``.

The counters are derived from the ``jobs`` table (``applied_at`` /
``last_attempted_at``) so they survive restarts and are shared by every
worker. State for "next allowed time" lives in a small JSON file so the
randomized gap is not re-rolled on every poll.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from applypilot import config

log = logging.getLogger(__name__)

_STATE_FILE = config.APP_DIR / "apply_pacing_state.json"

# Conservative built-ins. LinkedIn's own guidance to job seekers is "quality
# over quantity"; accounts that submit dozens of Easy Applies an hour get
# rate-limited or soft-flagged. These numbers are what an engaged but
# unhurried human does in a focused session.
_BUILTIN: dict[str, dict] = {
    "linkedin": {
        "max_per_hour": 5,
        "max_per_day": 12,
        "min_gap_seconds": 90,
        "max_gap_seconds": 240,
        "active_hours": [8, 22],
        "active_days": [0, 1, 2, 3, 4, 5, 6],
    },
    "default": {
        "max_per_hour": 12,
        "max_per_day": 40,
        "min_gap_seconds": 20,
        "max_gap_seconds": 75,
        "active_hours": [7, 23],
        "active_days": [0, 1, 2, 3, 4, 5, 6],
    },
}


@dataclass
class PacingRule:
    surface: str
    max_per_hour: int
    max_per_day: int
    min_gap_seconds: int
    max_gap_seconds: int
    active_hours: tuple[int, int]
    active_days: tuple[int, ...] = field(default=(0, 1, 2, 3, 4, 5, 6))

    def roll_gap(self) -> float:
        lo, hi = self.min_gap_seconds, max(self.min_gap_seconds, self.max_gap_seconds)
        return random.uniform(lo, hi)


@dataclass
class PacingDecision:
    allowed: bool
    reason: str = ""
    retry_after_s: float = 0.0


def _load_rules() -> dict[str, PacingRule]:
    cfg = {}
    try:
        cfg = (config.load_search_config() or {}).get("pacing") or {}
    except Exception:
        log.debug("pacing: search config unavailable, using built-ins", exc_info=True)
    rules: dict[str, PacingRule] = {}
    for surface, base in _BUILTIN.items():
        merged = {**base, **(cfg.get(surface) or {})}
        rules[surface] = _rule_from(surface, merged)
    for surface, user in cfg.items():
        if surface not in rules and isinstance(user, dict):
            rules[surface] = _rule_from(surface, {**_BUILTIN["default"], **user})
    return rules


def _rule_from(surface: str, d: dict) -> PacingRule:
    hours = d.get("active_hours") or [0, 24]
    days = d.get("active_days") or [0, 1, 2, 3, 4, 5, 6]
    return PacingRule(
        surface=surface,
        max_per_hour=int(d.get("max_per_hour", 0) or 0),
        max_per_day=int(d.get("max_per_day", 0) or 0),
        min_gap_seconds=int(d.get("min_gap_seconds", 0) or 0),
        max_gap_seconds=int(d.get("max_gap_seconds", 0) or 0),
        active_hours=(int(hours[0]), int(hours[1])),
        active_days=tuple(int(x) for x in days),
    )


def rule_for(surface: str | None) -> PacingRule:
    rules = _load_rules()
    return rules.get(surface or "default") or rules["default"]


# ── persisted "not before" timestamps ────────────────────────────────────

def _read_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text())
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(_STATE_FILE)
    except Exception:
        log.debug("pacing: could not persist state", exc_info=True)


def note_attempt(surface: str | None) -> None:
    """Record that an apply attempt on ``surface`` just finished.

    Rolls the next randomized gap and persists it. Call after every
    attempt (success or failure) — a human who hit an error on one form
    still doesn't instantly open the next one.
    """
    rule = rule_for(surface)
    key = rule.surface
    state = _read_state()
    state[key] = {
        "last_attempt": time.time(),
        "next_allowed": time.time() + rule.roll_gap(),
    }
    _write_state(state)


# ── the decision ────────────────────────────────────────────────────────

def _surface_sql(surface: str) -> tuple[str, list]:
    """SQL predicate selecting jobs that belong to ``surface``."""
    if surface == "linkedin":
        return ("(site = 'linkedin' AND (application_url IS NULL OR application_url = '')"
                " OR application_url LIKE '%linkedin.com/%')", [])
    if surface == "default":
        return ("1=1", [])
    # Other ATS slugs: match hostname substring via detect_ats' table.
    from applypilot.apply.chrome import ATS_DOMAINS
    hosts = [h for h, s in ATS_DOMAINS.items() if s == surface]
    if not hosts:
        return ("1=0", [])
    clauses = " OR ".join("application_url LIKE ?" for _ in hosts)
    return (f"({clauses})", [f"%{h}%" for h in hosts])


def _count_recent(conn: sqlite3.Connection, surface: str, since: datetime) -> int:
    pred, params = _surface_sql(surface)
    # Count *attempts*, not just successes — the server sees both.
    row = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE {pred} "
        f"AND COALESCE(applied_at, last_attempted_at) >= ?",
        (*params, since.isoformat()),
    ).fetchone()
    return int(row[0] if row else 0)


def check(conn: sqlite3.Connection, surface: str | None,
          now: datetime | None = None) -> PacingDecision:
    """Is an apply on ``surface`` allowed right now?

    Pure read; does not mutate state. ``now`` is injectable for tests and
    is interpreted in local time for the active-hours window.
    """
    rule = rule_for(surface)
    now_local = now or datetime.now().astimezone()
    now_utc = now_local.astimezone(timezone.utc)

    # 1. Active window (local clock)
    start, end = rule.active_hours
    if now_local.weekday() not in rule.active_days:
        return PacingDecision(False, f"{rule.surface}: outside active days",
                              retry_after_s=_seconds_until_next_window(now_local, rule))
    if not (start <= now_local.hour < end):
        return PacingDecision(False, f"{rule.surface}: outside active hours {start:02d}-{end:02d}",
                              retry_after_s=_seconds_until_next_window(now_local, rule))

    # 2. Randomized minimum gap since last attempt
    st = _read_state().get(rule.surface) or {}
    nxt = float(st.get("next_allowed", 0) or 0)
    if nxt > time.time():
        return PacingDecision(False, f"{rule.surface}: min gap", retry_after_s=nxt - time.time())

    # 3. Rolling hourly / daily caps
    if rule.max_per_hour > 0:
        n = _count_recent(conn, rule.surface, now_utc - timedelta(hours=1))
        if n >= rule.max_per_hour:
            return PacingDecision(False, f"{rule.surface}: {n}/{rule.max_per_hour} in last hour",
                                  retry_after_s=300)
    if rule.max_per_day > 0:
        n = _count_recent(conn, rule.surface, now_utc - timedelta(hours=24))
        if n >= rule.max_per_day:
            return PacingDecision(False, f"{rule.surface}: {n}/{rule.max_per_day} in last 24h",
                                  retry_after_s=1800)

    return PacingDecision(True)


def _seconds_until_next_window(now_local: datetime, rule: PacingRule) -> float:
    start, _ = rule.active_hours
    for d in range(0, 8):
        cand = (now_local + timedelta(days=d)).replace(hour=start, minute=0, second=0, microsecond=0)
        if cand <= now_local:
            continue
        if cand.weekday() in rule.active_days:
            return (cand - now_local).total_seconds()
    return 3600.0
