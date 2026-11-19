"""Política de retenção dos backups (GFS) — sem dependências do app.

Separado de `backup.py` para poder ser testado sem importar `config`/banco.

Semântica: cada tier é uma *janela de períodos*, não uma cota de arquivos.
O conjunto final é a união de:
- DAILY: todos os backups dos últimos N dias;
- WEEKLY: o backup mais recente de cada uma das N últimas semanas ISO;
- MONTHLY: o backup mais recente de cada um dos N últimos meses.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

DB_BACKUP_RE = re.compile(r"^logged-(\d{4}-\d{2}-\d{2})\.db\.gz$")
UPLOADS_BACKUP_RE = re.compile(r"^uploads-(\d{4}-\d{2}-\d{2})\.tar\.gz$")


def parse_backup_date(name: str, pattern: re.Pattern[str]) -> date | None:
    """Extrai a data de um nome de backup; None se não casar ou for inválida."""
    match = pattern.match(name)
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def list_backups(directory: Path, pattern: re.Pattern[str]) -> dict[date, Path]:
    """Mapa data → caminho, ignorando arquivos fora do padrão."""
    found: dict[date, Path] = {}
    if not directory.exists():
        return found
    for path in directory.iterdir():
        if not path.is_file():
            continue
        parsed = parse_backup_date(path.name, pattern)
        if parsed is not None:
            found[parsed] = path
    return found


def keep_set(
    dates: list[date] | set[date],
    today: date,
    daily: int,
    weekly: int,
    monthly: int,
) -> set[date]:
    """Datas que devem sobreviver à poda."""
    unique = sorted(set(dates), reverse=True)
    if not unique:
        return set()

    keep: set[date] = set()

    if daily > 0:
        cutoff = today - timedelta(days=daily - 1)
        keep.update(day for day in unique if cutoff <= day <= today)

    if weekly > 0:
        weeks: list[tuple[int, int]] = []
        cursor = today
        while len(weeks) < weekly:
            week_key = cursor.isocalendar()[:2]
            if week_key not in weeks:  # hoje e o mesmo dia da semana anterior
                weeks.append(week_key)
            cursor -= timedelta(days=7)
        kept_weeks: set[tuple[int, int]] = set()
        for day in unique:
            week_key = day.isocalendar()[:2]
            if week_key in weeks and week_key not in kept_weeks:
                keep.add(day)
                kept_weeks.add(week_key)

    if monthly > 0:
        months: list[tuple[int, int]] = []
        cursor = today.replace(day=1)
        for _ in range(monthly):
            months.append((cursor.year, cursor.month))
            cursor = (cursor - timedelta(days=1)).replace(day=1)
        kept_months: set[tuple[int, int]] = set()
        for day in unique:
            month_key = (day.year, day.month)
            if month_key in months and month_key not in kept_months:
                keep.add(day)
                kept_months.add(month_key)

    # Salvaguardas: nunca apagar o mais recente nem o backup de hoje.
    keep.add(unique[0])
    keep.add(today)
    return keep
