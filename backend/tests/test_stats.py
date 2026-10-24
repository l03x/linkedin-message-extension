from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import stats, store
from app.models import Generation, Profile

UTC = ZoneInfo("UTC")
NOW = datetime(2026, 10, 22, 15, 0)  # a Thursday


@pytest.fixture
def seeded(db):
    """Two prompt versions, a mix of modes and outcomes, plus a draft that must never count."""
    v1 = store.active_prompt(db)
    v2 = store.create_prompt(db, "v2", "second", activate=False)
    p = Profile(linkedin_url="https://www.linkedin.com/in/x/", name="X")
    db.add(p)
    db.flush()

    def add(mode, version, status, sent_days_ago=None, reply_after_h=None):
        sent_at = NOW - timedelta(days=sent_days_ago) if sent_days_ago is not None else None
        db.add(
            Generation(
                profile_id=p.id,
                mode=mode,
                language="en",
                prompt_version_id=version.id,
                draft_text="d",
                final_text="f" if sent_at else None,
                status=status,
                generated_at=sent_at or NOW,
                sent_at=sent_at,
                replied_at=sent_at + timedelta(hours=reply_after_h) if reply_after_h is not None else None,
            )
        )

    add("connection_note", v1, "replied", 0, reply_after_h=2)
    add("connection_note", v1, "no_reply", 1)
    add("connection_note", v1, "sent", 3)
    add("message", v1, "replied", 10, reply_after_h=50)
    add("message", v2, "replied", 20, reply_after_h=200)
    add("message", v2, "sent", 30)
    add("inmail", v2, "no_reply", 2)
    add("inmail", v2, "drafted")  # never sent: excluded everywhere except the draft count
    db.commit()
    return v1, v2


def test_totals_and_7_day_rate(db, seeded):
    rows = stats.sent_rows(db)
    assert len(rows) == 7
    t = stats.totals(db, rows)
    assert t == {"drafted": 1, "sent": 7, "awaiting": 2, "replied": 3, "no_reply": 2, "reply_rate": 3 / 7}
    # Sent 0, 1, 2 and 3 days ago fall within the last 7 days.
    assert stats.last_7_days(rows, NOW) == {"count": 4, "per_day": round(4 / 7, 2)}


def test_daily_series_is_zero_filled_and_split_by_mode(db, seeded):
    d = stats.daily_series(stats.sent_rows(db), UTC, NOW, days=14)
    assert len(d["labels"]) == 14 and d["labels"][-1] == "2026-10-22" and d["labels"][0] == "2026-10-09"
    note = d["series"]["connection_note"]
    assert note[-1] == 1 and note[-2] == 1 and note[-4] == 1 and sum(note) == 3
    assert d["series"]["inmail"][-3] == 1 and sum(d["series"]["inmail"]) == 1
    assert d["series"]["message"][-11] == 1 and sum(d["series"]["message"]) == 1  # 20/30 days ago fall outside


def test_weekly_series_starts_on_monday(db, seeded):
    w = stats.weekly_series(stats.sent_rows(db), UTC, NOW, weeks=6)
    assert w["labels"][-1] == "2026-10-19"  # Monday of NOW's week
    assert all(datetime.fromisoformat(label).weekday() == 0 for label in w["labels"])
    # This week (Mon 19 – Thu 22): sent 0, 1, 2 and 3 days ago.
    assert w["series"]["connection_note"][-1] == 3 and w["series"]["inmail"][-1] == 1
    total = sum(sum(s) for s in w["series"].values())
    assert total == 7  # 30 days ago is still inside a 6-week window


def test_timezone_moves_day_buckets():
    row = stats.SentRow(1, "message", 1, "v1", "sent", datetime(2026, 10, 21, 23, 30), None)
    utc = stats.daily_series([row], UTC, NOW, days=3)
    tokyo = stats.daily_series([row], ZoneInfo("Asia/Tokyo"), NOW, days=3)
    assert utc["series"]["message"] == [0, 1, 0]
    assert tokyo["labels"][-1] == "2026-10-23" and tokyo["series"]["message"] == [0, 1, 0]


def test_reply_rate_by_prompt_and_mode(db, seeded):
    v1, v2 = seeded
    rows = stats.sent_rows(db)
    by_prompt = {r["prompt_version_id"]: r for r in stats.reply_rate_by_prompt(rows)}
    assert by_prompt[v1.id]["n"] == 4 and by_prompt[v1.id]["replied"] == 2 and by_prompt[v1.id]["rate"] == 0.5
    assert by_prompt[v1.id]["awaiting"] == 1 and by_prompt[v1.id]["no_reply"] == 1
    assert by_prompt[v2.id]["n"] == 3 and by_prompt[v2.id]["replied"] == 1
    assert by_prompt[v2.id]["label"] == "v2" and by_prompt[v2.id]["small_sample"] is True

    by_mode = {r["mode"]: r for r in stats.reply_rate_by_mode(rows)}
    assert list(by_mode) == ["connection_note", "message", "inmail"]
    assert (by_mode["connection_note"]["n"], by_mode["connection_note"]["replied"]) == (3, 1)
    assert (by_mode["message"]["n"], by_mode["message"]["replied"]) == (3, 2)
    assert (by_mode["inmail"]["n"], by_mode["inmail"]["rate"]) == (1, 0.0)  # the draft isn't counted


def test_wilson_interval():
    assert stats.wilson(0, 0) == (0.0, 0.0)
    low, high = stats.wilson(1, 2)
    assert 0.09 < low < 0.1 and 0.9 < high < 0.91
    low, high = stats.wilson(50, 100)
    assert 0.40 < low < 0.41 and 0.59 < high < 0.60
    # Small samples give wide intervals, large ones narrow.
    assert stats.wilson(2, 4)[1] - stats.wilson(2, 4)[0] > stats.wilson(200, 400)[1] - stats.wilson(200, 400)[0]


def test_time_to_reply(db, seeded):
    ttr = stats.time_to_reply(stats.sent_rows(db))
    assert ttr["n"] == 3 and ttr["enough"] is False
    assert ttr["counts"] == [1, 1, 0, 1, 0]  # 2h, 50h, 200h
    assert ttr["median_hours"] == 50


def test_time_to_reply_buckets_with_enough_data():
    base = datetime(2026, 1, 1)
    hours = [1, 30, 100, 300, 400, 1000]
    rows = [stats.SentRow(i, "message", 1, "v1", "replied", base, base + timedelta(hours=h)) for i, h in enumerate(hours)]
    rows.append(stats.SentRow(99, "message", 1, "v1", "sent", base, None))
    ttr = stats.time_to_reply(rows)
    assert ttr["enough"] is True and ttr["n"] == 6
    assert ttr["counts"] == [1, 1, 1, 1, 2]
    assert ttr["median_hours"] == 200


def test_compute_and_stats_page(dash, db, seeded):
    data = stats.compute(db, UTC, NOW)
    assert set(data) >= {"totals", "last_7_days", "daily", "weekly", "by_prompt", "by_mode", "time_to_reply"}
    page = dash.get("/stats")
    assert page.status_code == 200
    assert "Reply rate by prompt version" in page.text and "small n" in page.text
    assert "Not enough data yet (n=3" in page.text


def test_stats_page_empty(dash):
    page = dash.get("/stats")
    assert page.status_code == 200 and "No sent messages yet" in page.text
