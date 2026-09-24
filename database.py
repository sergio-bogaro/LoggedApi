from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config import settings

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},  # Necessário para SQLite
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


# ──────────────────────────────────────────────
# Migração leve de schema (SQLite)
# ──────────────────────────────────────────────
# O projeto não usa Alembic. Tabelas novas são criadas por `Base.metadata.create_all`,
# mas colunas adicionadas a tabelas *existentes* precisam de ALTER TABLE. Declare aqui
# as colunas novas e `ensure_columns()` as adiciona de forma idempotente no startup,
# preservando os dados atuais.
#
# Formato: { "tabela": { "coluna": "DDL SQL (tipo + default/constraints)" } }
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "media_log": {
        "progress": "FLOAT",
        "progress_total": "FLOAT",
    },
    "users": {
        "track_music": "BOOLEAN NOT NULL DEFAULT 1",
    },
}


def ensure_columns() -> None:
    """Adiciona colunas ausentes em tabelas já existentes (migração leve, idempotente)."""
    if not _ADDED_COLUMNS:
        return

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue
            current = {col["name"] for col in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in current:
                    conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}'))
                    print(f"Migration: added column {table}.{name}")


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
