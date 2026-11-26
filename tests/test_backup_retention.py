"""Testes da política de retenção GFS (sem banco, sem config)."""

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from scripts.backup_retention import (
    DB_BACKUP_RE,
    UPLOADS_BACKUP_RE,
    keep_set,
    list_backups,
    parse_backup_date,
)


def _daily_until(today: date, days: int) -> list[date]:
    return [today - timedelta(days=offset) for offset in range(days)]


class BackupLockTests(unittest.TestCase):
    def test_blocks_concurrent_runs_and_releases(self) -> None:
        from scripts.backup import BackupError, _backup_lock

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with _backup_lock(directory):
                with self.assertRaises(BackupError):
                    with _backup_lock(directory):
                        pass
            # Depois do release, o lock pode ser adquirido novamente.
            with _backup_lock(directory):
                pass


class ParseBackupDateTests(unittest.TestCase):
    def test_parses_database_and_uploads_names(self) -> None:
        self.assertEqual(
            parse_backup_date("logged-2026-11-19.db.gz", DB_BACKUP_RE),
            date(2026, 11, 19),
        )
        self.assertEqual(
            parse_backup_date("uploads-2026-11-19.tar.gz", UPLOADS_BACKUP_RE),
            date(2026, 11, 19),
        )

    def test_ignores_other_names_and_invalid_dates(self) -> None:
        for name in (
            "manual.db.gz",
            "logged-2026-11-19.db",
            "logged-antes-do-alembic.db.gz",
            "logged-2026-13-40.db.gz",
        ):
            self.assertIsNone(parse_backup_date(name, DB_BACKUP_RE), name)


class KeepSetTests(unittest.TestCase):
    def test_daily_keeps_exactly_the_window(self) -> None:
        today = date(2026, 11, 19)
        dates = _daily_until(today, 20)

        keep = keep_set(dates, today, daily=7, weekly=0, monthly=0)

        expected = {today - timedelta(days=offset) for offset in range(7)}
        self.assertEqual(keep, expected)

    def test_gap_in_days_does_not_break(self) -> None:
        today = date(2026, 11, 19)
        dates = [today, today - timedelta(days=3), today - timedelta(days=30)]

        keep = keep_set(dates, today, daily=7, weekly=0, monthly=0)

        self.assertEqual(keep, {today, today - timedelta(days=3)})

    def test_union_of_daily_weekly_monthly(self) -> None:
        today = date(2026, 11, 19)
        dates = _daily_until(today, 80)

        keep = keep_set(dates, today, daily=7, weekly=4, monthly=3)

        expected = {
            # daily
            *(today - timedelta(days=offset) for offset in range(7)),
            # weekly: mais recente de cada uma das 4 semanas ISO
            date(2026, 11, 8),
            date(2026, 11, 1),
            # monthly: mais recente de cada um dos 3 meses
            date(2026, 10, 31),
            date(2026, 9, 30),
        }
        self.assertEqual(keep, expected)

    def test_iso_week_across_year_boundary(self) -> None:
        today = date(2027, 1, 5)
        dates = [date(2026, 12, 28) + timedelta(days=offset) for offset in range(9)]

        keep = keep_set(dates, today, daily=0, weekly=2, monthly=0)

        # 2026-12-28..2027-01-03 é a semana 53 de 2026; 2027-01-04..05 é a 1 de 2027.
        self.assertIn(date(2027, 1, 3), keep)
        self.assertIn(date(2027, 1, 5), keep)

    def test_zero_tiers_keep_only_newest(self) -> None:
        today = date(2026, 11, 19)
        dates = [today - timedelta(days=offset) for offset in (2, 5, 9)]

        keep = keep_set(dates, today, daily=0, weekly=0, monthly=0)

        existing = {day for day in keep if day in dates}
        self.assertEqual(existing, {today - timedelta(days=2)})

    def test_empty_input(self) -> None:
        self.assertEqual(keep_set([], date(2026, 11, 19), 7, 4, 3), set())


class ListBackupsTests(unittest.TestCase):
    def test_lists_only_valid_names_without_touching_others(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            (directory / "logged-2026-11-19.db.gz").write_bytes(b"a")
            (directory / "logged-2026-11-18.db.gz").write_bytes(b"b")
            (directory / "uploads-2026-11-19.tar.gz").write_bytes(b"c")
            (directory / "logged-manual.db.gz").write_bytes(b"d")

            backups = list_backups(directory, DB_BACKUP_RE)
            uploads = list_backups(directory, UPLOADS_BACKUP_RE)

            self.assertEqual(
                sorted(backups), [date(2026, 11, 18), date(2026, 11, 19)]
            )
            self.assertEqual(sorted(uploads), [date(2026, 11, 19)])
            # A poda nunca considera o arquivo fora do padrão.
            self.assertNotIn("logged-manual.db.gz", {p.name for p in backups.values()})


if __name__ == "__main__":
    unittest.main()
