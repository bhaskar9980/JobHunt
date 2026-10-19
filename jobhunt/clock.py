"""Time helpers. Everything stored in the DB is ISO-8601 UTC with second precision."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

ISO_FORMAT_TIMESPEC = "seconds"


def now() -> datetime:
    return datetime.now(UTC)


def now_iso() -> str:
    return to_iso(now())


def to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec=ISO_FORMAT_TIMESPEC)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def iso_days_ago(days: int, reference: datetime | None = None) -> str:
    base = reference or now()
    return to_iso(base - timedelta(days=days))


def from_epoch_ms(value: int | float | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)
