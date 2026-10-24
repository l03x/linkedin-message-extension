"""Stats for the dashboard. Only messages actually sent (sent / replied / no_reply) count toward rates."""

import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modes import MODES
from app.models import SENT_STATUSES, STATUSES, Generation, PromptVersion, utcnow

MIN_REPLIES_FOR_DISTRIBUTION = 5
SMALL_SAMPLE = 10

REPLY_BUCKETS = [  # (label, upper bound in hours, exclusive)
    ("< 1 day", 24),
    ("1–3 days", 72),
    ("3–7 days", 168),
    ("1–2 weeks", 336),
    ("> 2 weeks", math.inf),
]


@dataclass
class SentRow:
    id: int
    mode: str
    prompt_version_id: int
    prompt_name: str
    status: str
    sent_at: datetime
    replied_at: datetime | None


def sent_rows(db: Session) -> list[SentRow]:
    q = (
        select(
            Generation.id,
            Generation.mode,
            Generation.prompt_version_id,
            PromptVersion.name,
            Generation.status,
            Generation.sent_at,
            Generation.replied_at,
        )
        .join(PromptVersion, PromptVersion.id == Generation.prompt_version_id)
        .where(Generation.status.in_(SENT_STATUSES), Generation.sent_at.is_not(None))
        .order_by(Generation.sent_at)
    )
    return [SentRow(*r) for r in db.execute(q)]


def local_date(dt: datetime, tz: ZoneInfo) -> date:
    return dt.replace(tzinfo=timezone.utc).astimezone(tz).date()


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for k successes out of n; (0, 0) when n == 0."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def totals(db: Session, rows: list[SentRow]) -> dict:
    counts = dict.fromkeys(STATUSES, 0)
    for status, n in db.execute(select(Generation.status, func.count()).group_by(Generation.status)):
        counts[status] = n
    sent_total = len(rows)
    replied = sum(r.status == "replied" for r in rows)
    return {
        "drafted": counts["drafted"],
        "sent": sent_total,
        "awaiting": counts["sent"],
        "replied": replied,
        "no_reply": counts["no_reply"],
        "reply_rate": replied / sent_total if sent_total else None,
    }


def last_7_days(rows: list[SentRow], now: datetime) -> dict:
    since = now - timedelta(days=7)
    count = sum(r.sent_at >= since for r in rows)
    return {"count": count, "per_day": round(count / 7, 2)}


def daily_series(rows: list[SentRow], tz: ZoneInfo, now: datetime, days: int = 60) -> dict:
    end = local_date(now, tz)
    labels = [end - timedelta(days=i) for i in range(days - 1, -1, -1)]
    index = {d: i for i, d in enumerate(labels)}
    series = {m: [0] * days for m in MODES}
    for r in rows:
        i = index.get(local_date(r.sent_at, tz))
        if i is not None:
            series.setdefault(r.mode, [0] * days)[i] += 1
    return {"labels": [d.isoformat() for d in labels], "series": series}


def weekly_series(rows: list[SentRow], tz: ZoneInfo, now: datetime, weeks: int = 16) -> dict:
    today = local_date(now, tz)
    this_monday = today - timedelta(days=today.weekday())
    labels = [this_monday - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    index = {d: i for i, d in enumerate(labels)}
    series = {m: [0] * weeks for m in MODES}
    for r in rows:
        d = local_date(r.sent_at, tz)
        i = index.get(d - timedelta(days=d.weekday()))
        if i is not None:
            series.setdefault(r.mode, [0] * weeks)[i] += 1
    return {"labels": [d.isoformat() for d in labels], "series": series}


def _rate_entry(label: str, group: list[SentRow]) -> dict:
    n = len(group)
    replied = sum(r.status == "replied" for r in group)
    low, high = wilson(replied, n)
    return {
        "label": label,
        "n": n,
        "replied": replied,
        "no_reply": sum(r.status == "no_reply" for r in group),
        "awaiting": sum(r.status == "sent" for r in group),
        "rate": replied / n if n else None,
        "ci_low": low,
        "ci_high": high,
        "small_sample": n < SMALL_SAMPLE,
    }


def reply_rate_by_prompt(rows: list[SentRow]) -> list[dict]:
    groups: dict[int, list[SentRow]] = {}
    names: dict[int, str] = {}
    for r in rows:
        groups.setdefault(r.prompt_version_id, []).append(r)
        names[r.prompt_version_id] = r.prompt_name
    return [{**_rate_entry(names[pid], groups[pid]), "prompt_version_id": pid} for pid in sorted(groups)]


def reply_rate_by_mode(rows: list[SentRow]) -> list[dict]:
    out = []
    for key, mode in MODES.items():
        group = [r for r in rows if r.mode == key]
        if group:
            out.append({**_rate_entry(mode.short_label or mode.label, group), "mode": key})
    return out


def time_to_reply(rows: list[SentRow]) -> dict:
    hours = [
        (r.replied_at - r.sent_at).total_seconds() / 3600
        for r in rows
        if r.status == "replied" and r.replied_at is not None and r.replied_at >= r.sent_at
    ]
    counts = [0] * len(REPLY_BUCKETS)
    for h in hours:
        for i, (_, upper) in enumerate(REPLY_BUCKETS):
            if h < upper:
                counts[i] += 1
                break
    return {
        "n": len(hours),
        "enough": len(hours) >= MIN_REPLIES_FOR_DISTRIBUTION,
        "labels": [label for label, _ in REPLY_BUCKETS],
        "counts": counts,
        "median_hours": statistics.median(hours) if hours else None,
    }


def compute(db: Session, tz: ZoneInfo, now: datetime | None = None) -> dict:
    now = now or utcnow()
    rows = sent_rows(db)
    return {
        "totals": totals(db, rows),
        "last_7_days": last_7_days(rows, now),
        "daily": daily_series(rows, tz, now),
        "weekly": weekly_series(rows, tz, now),
        "by_prompt": reply_rate_by_prompt(rows),
        "by_mode": reply_rate_by_mode(rows),
        "time_to_reply": time_to_reply(rows),
        "mode_labels": {k: m.label for k, m in MODES.items()},
        "mode_order": list(MODES),
    }
