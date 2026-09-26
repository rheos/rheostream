"""The flagship backup script's retention and failure handling (issue #163).

`deploy/backup/rheostream-pgdumpall.sh` runs against a temp dir of fake dump
files. No Postgres and no Docker: `run` gets a stub `docker` on PATH, so the
dump-failure path (pipefail) is exercised for real.
"""

from __future__ import annotations

import gzip
import os
import shutil
import stat
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "backup" / "rheostream-pgdumpall.sh"
# No skip when bash is missing: a skipped test would pass CI vacuously.
BASH = shutil.which("bash") or "/bin/bash"
MARKER = "-- PostgreSQL database cluster dump complete"


def dump_name(day: date, hms: str = "031500") -> str:
    return f"pgdumpall-{day:%Y%m%d}T{hms}Z.sql.gz"


def stamp(path: Path) -> None:
    """Give a fake dump the mtime its name claims, as a nightly run would."""
    n = path.name
    when = datetime.strptime(n[10:25], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    os.utime(path, (when.timestamp(), when.timestamp()))


def write_good(path: Path) -> None:
    body = "--\n-- fictional cluster dump\n--\nCREATE DATABASE example;\n--\n"
    path.write_bytes(gzip.compress(f"{body}{MARKER}\n--\n\n".encode()))
    stamp(path)


def write_truncated(path: Path) -> None:
    """A valid gzip stream whose dump never reached the completion marker."""
    path.write_bytes(gzip.compress(b"--\nCREATE DATABASE example;\n"))
    stamp(path)


def env_for(backup_dir: Path, **extra: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "RHEO_BACKUP_DIR": str(backup_dir),
        "RHEO_BACKUP_LOG": "stderr",
        "RHEO_BACKUP_MIN_BYTES": "1",
    }
    env.update(extra)
    return env


def run_script(
    backup_dir: Path, *args: str, **extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(SCRIPT), *args],
        env=env_for(backup_dir, **extra),
        capture_output=True,
        text=True,
        check=False,
    )


def names(backup_dir: Path) -> set[str]:
    # .lock is the flock file `run` creates where flock exists (Linux).
    return {p.name for p in backup_dir.iterdir() if p.name != ".lock"}


def expected_keep(
    days: list[date], keep_daily: int = 14, keep_weekly: int = 8
) -> set[str]:
    newest_first = sorted(days, reverse=True)
    daily = newest_first[:keep_daily]
    sundays = [d for d in newest_first if d.weekday() == 6][:keep_weekly]
    return {dump_name(d) for d in set(daily) | set(sundays)}


@pytest.fixture
def backup_dir(tmp_path: Path) -> Path:
    d = tmp_path / "backups"
    d.mkdir()
    return d


def test_keeps_14_daily_and_8_sundays(backup_dir: Path) -> None:
    last = date(2026, 3, 31)
    days = [last - timedelta(days=i) for i in range(120)]
    for d in days:
        write_good(backup_dir / dump_name(d))

    result = run_script(backup_dir, "prune")

    assert result.returncode == 0, result.stderr
    kept = names(backup_dir)
    assert kept == expected_keep(days)
    # 14 daily, 8 Sundays, 2 of those Sundays inside the daily window.
    assert len(kept) == 20
    assert all(
        date(int(n[10:14]), int(n[14:16]), int(n[16:18])).weekday() == 6
        for n in kept - {dump_name(d) for d in days[:14]}
    )


def test_only_newest_dump_of_a_day_counts(backup_dir: Path) -> None:
    day = date(2026, 3, 4)  # a Wednesday
    for hms in ("031500", "120000", "183315"):
        write_good(backup_dir / dump_name(day, hms))

    result = run_script(backup_dir, "prune")

    assert result.returncode == 0, result.stderr
    assert names(backup_dir) == {dump_name(day, "183315")}


def test_dry_run_deletes_nothing(backup_dir: Path) -> None:
    days = [date(2026, 3, 31) - timedelta(days=i) for i in range(40)]
    for d in days:
        write_good(backup_dir / dump_name(d))

    result = run_script(backup_dir, "prune", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert names(backup_dir) == {dump_name(d) for d in days}
    assert "would delete" in result.stderr


def test_unrelated_files_are_never_touched(backup_dir: Path) -> None:
    days = [date(2026, 3, 31) - timedelta(days=i) for i in range(30)]
    for d in days:
        write_good(backup_dir / dump_name(d))
    others = {
        "notes.txt",
        ".pgdumpall-20260101T031500Z.sql.gz.partial",
        "pgdumpall-20260101T031500Z.sql.gz.bak",
        "pgdumpall-2026-01-01.sql.gz",
    }
    for other in others:
        (backup_dir / other).write_text("fictional\n")

    result = run_script(backup_dir, "prune")

    assert result.returncode == 0, result.stderr
    assert others <= names(backup_dir)


def test_newest_good_dump_survives_when_newer_ones_are_bad(backup_dir: Path) -> None:
    days = [date(2026, 3, 31) - timedelta(days=i) for i in range(40)]
    for i, d in enumerate(days):
        path = backup_dir / dump_name(d)
        # The 20 newest are broken: 10 truncated dumps, 10 not even gzip.
        if i < 10:
            write_truncated(path)
        elif i < 20:
            path.write_bytes(b"not a gzip stream")
            stamp(path)
        else:
            write_good(path)

    result = run_script(backup_dir, "prune")

    assert result.returncode == 0, result.stderr
    kept = names(backup_dir)
    newest_good = dump_name(days[20])
    assert newest_good in kept
    assert newest_good not in expected_keep(days)  # kept only by the guard


def test_no_good_dump_means_nothing_is_deleted(backup_dir: Path) -> None:
    days = [date(2026, 3, 31) - timedelta(days=i) for i in range(30)]
    for d in days:
        write_truncated(backup_dir / dump_name(d))

    result = run_script(backup_dir, "prune")

    assert result.returncode == 3
    assert names(backup_dir) == {dump_name(d) for d in days}
    assert "deleting nothing" in result.stderr


def test_wrong_clock_past_date_keep_flag_and_mtime(backup_dir: Path) -> None:
    """A dump written while the clock read 2001 sorts oldest by name."""
    days = [date(2026, 3, 31) - timedelta(days=i) for i in range(30)]
    for d in days:
        path = backup_dir / dump_name(d)
        write_good(path)
    misdated = backup_dir / dump_name(date(2001, 1, 2))
    write_good(misdated)
    os.utime(misdated, (1_800_000_000, 1_800_000_000))  # written last (2027)

    # The most recently written good dump survives even without --keep.
    result = run_script(backup_dir, "prune")
    assert result.returncode == 0, result.stderr
    assert misdated.name in names(backup_dir)

    # --keep protects a file whatever its name or mtime says.
    os.utime(misdated, (1_600_000_000, 1_600_000_000))
    result = run_script(backup_dir, "prune", "--keep", str(misdated))
    assert result.returncode == 0, result.stderr
    assert misdated.name in names(backup_dir)

    # With neither guard it is just the oldest dump, and goes.
    result = run_script(backup_dir, "prune")
    assert result.returncode == 0, result.stderr
    assert misdated.name not in names(backup_dir)


def test_wrong_clock_future_date_does_not_wipe_real_dumps(backup_dir: Path) -> None:
    days = [date(2026, 3, 31) - timedelta(days=i) for i in range(30)]
    for d in days:
        write_good(backup_dir / dump_name(d))
    future = dump_name(date(2099, 6, 1))
    write_good(backup_dir / future)

    result = run_script(backup_dir, "prune")

    assert result.returncode == 0, result.stderr
    kept = names(backup_dir)
    assert future in kept
    # The future dump takes one daily slot; 13 real days remain in the window.
    assert {dump_name(d) for d in days[:13]} <= kept


def test_empty_dir_is_fine(backup_dir: Path) -> None:
    result = run_script(backup_dir, "prune")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("bad", ["0", "x", "-1", ""])
def test_bad_keep_daily_is_refused(backup_dir: Path, bad: str) -> None:
    write_good(backup_dir / dump_name(date(2026, 3, 1)))
    result = run_script(backup_dir, "prune", RHEO_BACKUP_KEEP_DAILY=bad)
    if bad == "":
        # Empty means unset: the default of 14 applies.
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
    assert (backup_dir / dump_name(date(2026, 3, 1))).exists()


# --- run: a stub docker on PATH -------------------------------------------------


def stub_docker(tmp_path: Path, exec_body: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  ps) echo 0123456789ab ;;\n"
        f"  exec) {exec_body} ;;\n"
        "  *) exit 64 ;;\n"
        "esac\n"
    )
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def run_with_docker(
    backup_dir: Path, bin_dir: Path
) -> subprocess.CompletedProcess[str]:
    return run_script(
        backup_dir,
        "run",
        PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        RHEO_BACKUP_COMPOSE_PROJECT="example-project",
    )


def seed_old_dumps(backup_dir: Path) -> set[str]:
    days = [date(2026, 3, 31) - timedelta(days=i) for i in range(30)]
    for d in days:
        write_good(backup_dir / dump_name(d))
    return {dump_name(d) for d in days}


def test_failing_pg_dumpall_deletes_nothing(tmp_path: Path, backup_dir: Path) -> None:
    before = seed_old_dumps(backup_dir)
    # pg_dumpall writes a complete-looking dump and then exits non-zero. gzip
    # succeeds and the file would verify, so only pipefail catches this.
    bin_dir = stub_docker(
        tmp_path,
        f"printf -- '--\\nCREATE DATABASE example;\\n{MARKER}\\n--\\n'; exit 1",
    )

    result = run_with_docker(backup_dir, bin_dir)

    assert result.returncode != 0
    assert names(backup_dir) == before  # no new file, no partial, nothing pruned
    assert "failed" in result.stderr


def test_truncated_dump_is_rejected(tmp_path: Path, backup_dir: Path) -> None:
    before = seed_old_dumps(backup_dir)
    bin_dir = stub_docker(tmp_path, "echo 'CREATE DATABASE example;'")

    result = run_with_docker(backup_dir, bin_dir)

    assert result.returncode != 0
    assert names(backup_dir) == before
    assert "did not verify" in result.stderr


def test_good_run_writes_a_dump_then_prunes(tmp_path: Path, backup_dir: Path) -> None:
    before = seed_old_dumps(backup_dir)
    bin_dir = stub_docker(
        tmp_path, f"printf -- '--\\nCREATE DATABASE example;\\n{MARKER}\\n--\\n'"
    )

    result = run_with_docker(backup_dir, bin_dir)

    assert result.returncode == 0, result.stderr
    after = names(backup_dir)
    new = after - before
    assert len(new) == 1
    (new_name,) = new
    assert new_name.startswith("pgdumpall-") and new_name.endswith("Z.sql.gz")
    assert MARKER in gzip.decompress((backup_dir / new_name).read_bytes()).decode()
    assert (backup_dir / new_name).stat().st_mode & 0o077 == 0  # root-only file
    # The new dump took one daily slot; retention ran and trimmed the rest.
    assert len(after & before) < len(before)
    assert not any(n.endswith(".partial") for n in after)


def test_run_refuses_zero_or_two_containers(tmp_path: Path, backup_dir: Path) -> None:
    before = seed_old_dumps(backup_dir)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text("#!/bin/sh\n[ \"$1\" = ps ] && printf 'aaa\\nbbb\\n'\nexit 0\n")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)

    result = run_with_docker(backup_dir, bin_dir)

    assert result.returncode != 0
    assert "found 2" in result.stderr
    assert names(backup_dir) == before
