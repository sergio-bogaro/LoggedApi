"""Verifica se o banco existente pode ser migrado pelo Alembic.

Cenário tratado: banco criado antes da adoção do Alembic (tem tabelas, mas não
tem `alembic_version`). Rodar `alembic upgrade head` nele tentaria criar tabelas
que já existem; o correto é marcar a baseline uma vez com `alembic stamp`.

Exit code 0 = pode seguir; 1 = precisa de intervenção manual.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Permite `python scripts/check_schema_state.py` (sys.path[0] vira scripts/).
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.engine import make_url

from config import settings

_BASELINE = "0001_baseline"


def database_path() -> Path | None:
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    return Path(url.database).expanduser().resolve()


def table_names(path: Path) -> set[str]:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()


def main() -> int:
    path = database_path()
    if path is None or not path.exists():
        return 0

    tables = table_names(path) - {"sqlite_sequence"}
    if not tables or "alembic_version" in tables:
        return 0

    print("=" * 70)
    print("Banco existente sem controle de migrations (anterior ao Alembic).")
    print(f"Arquivo: {path}")
    print("Marque a baseline uma única vez e reinicie:")
    print(f"    alembic stamp {_BASELINE}")
    print(f"    alembic upgrade head")
    print("=" * 70)
    return 1


if __name__ == "__main__":
    sys.exit(main())
