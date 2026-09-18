"""Per-surface apply pacing (apply/pacing.py) + EXTERNAL_APPLY_URL parsing."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from applypilot.apply import pacing


@pytest.fixture
def isolated_pacing(tmp_path, monkeypatch):
    """Point the state file at tmp and pin the rules to known values."""
    monkeypatch.setattr(pacing, "_STATE_FILE", tmp_path / "pacing.json")
    monkeypatch.setattr(pacing.config, "load_search_config", lambda: {
        "pacing": {
            "linkedin": {"max_per_hour": 2, "max_per_day": 3,
                         "min_gap_seconds": 10, "max_gap_seconds": 10,
                         "active_hours": [8, 22]},
        }
    })
    return tmp_path


def _conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, site TEXT, application_url TEXT, "
              "applied_at TEXT, last_attempted_at TEXT)")
    return c


NOON = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)


def _seed(c, n, site="linkedin", app=None, minutes_ago=5, now=NOON, applied=True):
    """Insert n attempt rows ``minutes_ago`` before the injected ``now``."""
    ts = (now.astimezone(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    for i in range(n):
        c.execute("INSERT INTO jobs VALUES (?,?,?,?,?)",
                  (f"https://www.linkedin.com/jobs/view/{site}-{i}-{minutes_ago}", site, app,
                   ts if applied else None, ts))


def _at(hour: int) -> datetime:
    return NOON.replace(hour=hour)


def test_allowed_in_window_with_no_history(isolated_pacing):
    assert pacing.check(_conn(), "linkedin", now=_at(12)).allowed


def test_blocked_outside_active_hours(isolated_pacing):
    d = pacing.check(_conn(), "linkedin", now=_at(3))
    assert not d.allowed and "active hours" in d.reason
    assert d.retry_after_s > 0


def test_hourly_cap_counts_attempts_not_just_successes(isolated_pacing):
    c = _conn()
    # 2 attempts in the last hour, neither marked applied_at
    _seed(c, 2, minutes_ago=10, applied=False)
    d = pacing.check(c, "linkedin", now=_at(12))
    assert not d.allowed and "in last hour" in d.reason


def test_daily_cap(isolated_pacing):
    c = _conn()
    _seed(c, 1, minutes_ago=5)
    _seed(c, 2, minutes_ago=300)   # 5h ago: outside hourly, inside daily
    d = pacing.check(c, "linkedin", now=_at(12))
    assert not d.allowed and "in last 24h" in d.reason


def test_min_gap_after_note_attempt(isolated_pacing):
    c = _conn()
    pacing.note_attempt("linkedin")
    d = pacing.check(c, "linkedin", now=_at(12))
    assert not d.allowed and "min gap" in d.reason
    assert 0 < d.retry_after_s <= 10.5


def test_surfaces_are_independent(isolated_pacing):
    c = _conn()
    pacing.note_attempt("linkedin")
    assert not pacing.check(c, "linkedin", now=_at(12)).allowed
    assert pacing.check(c, "greenhouse", now=_at(12)).allowed
    assert pacing.check(c, None, now=_at(12)).allowed


def test_linkedin_surface_only_counts_linkedin_rows(isolated_pacing):
    c = _conn()
    # Two Indeed applies should not eat the LinkedIn hourly budget.
    _seed(c, 2, site="indeed", app="https://x.com/a", minutes_ago=10)
    assert pacing.check(c, "linkedin", now=_at(12)).allowed


def test_user_config_overrides_builtin(isolated_pacing, monkeypatch):
    monkeypatch.setattr(pacing.config, "load_search_config", lambda: {
        "pacing": {"linkedin": {"active_hours": [0, 24]}}})
    r = pacing.rule_for("linkedin")
    assert r.active_hours == (0, 24)
    assert r.max_per_day == 12  # built-in default retained


# ── EXTERNAL_APPLY_URL ────────────────────────────────────────────────────

def test_parse_external_apply_url_writes_back(tmp_db, seed_job, monkeypatch):
    from applypilot.apply.result_handlers import _parse_external_apply_url
    conn = tmp_db()
    seed_job(conn, url_suffix="li", url="https://www.linkedin.com/jobs/view/1",
             site="linkedin", application_url=None)
    out = ("... clicked Apply\nEXTERNAL_APPLY_URL: https://boards.greenhouse.io/acme/jobs/42 \n"
           "RESULT:APPLIED")
    got = _parse_external_apply_url(out, "https://www.linkedin.com/jobs/view/1")
    assert got and "greenhouse.io/acme/jobs/42" in got
    row = conn.execute("SELECT application_url FROM jobs WHERE url = ?",
                       ("https://www.linkedin.com/jobs/view/1",)).fetchone()
    assert "greenhouse.io/acme/jobs/42" in row[0]


def test_parse_external_apply_url_ignores_aggregator_and_garbage(tmp_db, seed_job):
    from applypilot.apply.result_handlers import _parse_external_apply_url
    conn = tmp_db()
    seed_job(conn, url_suffix="li2", url="https://www.linkedin.com/jobs/view/2",
             site="linkedin", application_url=None)
    out = ("EXTERNAL_APPLY_URL: https://www.linkedin.com/jobs/view/2/apply\n"
           "EXTERNAL_APPLY_URL: javascript:void(0)\n"
           "EXTERNAL_APPLY_URL: not a url")
    assert _parse_external_apply_url(out, "https://www.linkedin.com/jobs/view/2") is None
    row = conn.execute("SELECT application_url FROM jobs WHERE url = ?",
                       ("https://www.linkedin.com/jobs/view/2",)).fetchone()
    assert row[0] in (None, "")


def test_parse_external_apply_url_does_not_overwrite_existing(tmp_db, seed_job):
    from applypilot.apply.result_handlers import _parse_external_apply_url
    conn = tmp_db()
    seed_job(conn, url_suffix="li3", url="https://www.linkedin.com/jobs/view/3",
             site="linkedin", application_url="https://jobs.lever.co/acme/1")
    _parse_external_apply_url("EXTERNAL_APPLY_URL: https://other.example/apply",
                              "https://www.linkedin.com/jobs/view/3")
    row = conn.execute("SELECT application_url FROM jobs WHERE url = ?",
                       ("https://www.linkedin.com/jobs/view/3",)).fetchone()
    assert row[0] == "https://jobs.lever.co/acme/1"


# ── is_linkedin_listing ─────────────────────────────────────────────────

@pytest.mark.parametrize("job,expected", [
    ({"url": "https://www.linkedin.com/jobs/view/1", "site": "linkedin", "application_url": None}, True),
    ({"url": "https://www.linkedin.com/jobs/view/1", "site": "linkedin", "application_url": ""}, True),
    ({"url": "https://www.linkedin.com/jobs/view/1", "site": "linkedin", "application_url": "https://x.com"}, False),
    ({"url": "https://www.linkedin.com/jobs/search/", "site": "linkedin", "application_url": None}, False),
    ({"url": "https://www.indeed.com/viewjob?jk=1", "site": "indeed", "application_url": None}, False),
    ({"url": "https://www.linkedin.com/jobs/view/1", "site": "jobspy", "application_url": None}, False),
])
def test_is_linkedin_listing(job, expected):
    from applypilot.apply.chrome import is_linkedin_listing
    assert is_linkedin_listing(job) is expected
