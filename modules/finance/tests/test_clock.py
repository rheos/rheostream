"""The clock and local-time helpers, driven by the ``finance.locale.timezone`` key."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from rheo_finance.clock import (
    FakeClock,
    SystemClock,
    local_date,
    local_to_utc,
    local_today,
    render_local,
)
from rheo_finance.settings import load_settings

# 14:30 UTC on 2037-10-19 is 23:30 the same day at UTC+9 (Etc/GMT-9, no DST).
NEAR_MIDNIGHT = dt.datetime(2037, 10, 19, 14, 30, tzinfo=dt.UTC)
ZONE = 'finance.locale.timezone = "Etc/GMT-9"\n'


def test_a_date_near_midnight_renders_in_the_zone_setting(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text("", encoding="utf-8")
    zone = load_settings(path).timezone
    assert zone.key == "UTC"  # the default until the owner names a zone
    assert render_local(NEAR_MIDNIGHT, zone) == "2037-10-19 14:30 UTC"

    path.write_text(ZONE, encoding="utf-8")
    zone = load_settings(path).timezone
    assert render_local(NEAR_MIDNIGHT, zone) == "2037-10-19 23:30 +09"
    assert local_date(NEAR_MIDNIGHT, zone) == dt.date(2037, 10, 19)

    path.write_text('finance.locale.timezone = "Etc/GMT-10"\n', encoding="utf-8")
    zone = load_settings(path).timezone
    assert render_local(NEAR_MIDNIGHT, zone) == "2037-10-20 00:30 +10"
    assert local_date(NEAR_MIDNIGHT, zone) == dt.date(2037, 10, 20)


def test_fake_clock_stands_still_until_moved(tmp_path: Path) -> None:
    clock = FakeClock(NEAR_MIDNIGHT)
    path = tmp_path / "settings.toml"
    path.write_text(ZONE, encoding="utf-8")
    zone = load_settings(path).timezone
    assert clock.now_utc() == NEAR_MIDNIGHT
    assert local_today(clock, zone) == dt.date(2037, 10, 19)
    clock.advance(dt.timedelta(minutes=31))
    assert local_today(clock, zone) == dt.date(2037, 10, 20)
    clock.set(dt.datetime(2037, 1, 1, 12, tzinfo=dt.timezone(dt.timedelta(hours=5))))
    assert clock.now_utc() == dt.datetime(2037, 1, 1, 7, tzinfo=dt.UTC)
    assert clock.now_utc().tzinfo == dt.UTC


def test_naive_datetimes_are_refused(tmp_path: Path) -> None:
    zone = load_settings(tmp_path / "missing.toml").timezone
    with pytest.raises(ValueError, match="naive"):
        FakeClock(dt.datetime(2037, 1, 1))
    with pytest.raises(ValueError, match="naive"):
        render_local(dt.datetime(2037, 1, 1), zone)


def test_system_clock_is_aware_utc() -> None:
    now = SystemClock().now_utc()
    assert now.utcoffset() == dt.timedelta(0)


def test_a_local_wall_time_converts_to_utc_through_the_zone(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text('finance.locale.timezone = "Europe/Berlin"\n', encoding="utf-8")
    zone = load_settings(path).timezone
    # The same 08:00 is a different UTC hour either side of the DST change.
    summer = local_to_utc(dt.date(2037, 7, 1), dt.time(8, 0), zone)
    winter = local_to_utc(dt.date(2037, 12, 1), dt.time(8, 0), zone)
    assert summer == dt.datetime(2037, 7, 1, 6, tzinfo=dt.UTC)
    assert winter == dt.datetime(2037, 12, 1, 7, tzinfo=dt.UTC)
