from collections.abc import Generator

from config import settings
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},  # Necessário para SQLite
    echo=False,
)


if engine.url.get_backend_name() == "sqlite":

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


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
# Nota: bancos antigos podem ter media_log.progress/progress_total. As colunas
# foram removidas do modelo em uma versão anterior e são mantidas no banco para
# não destruir dados históricos; o app atual não as utiliza.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "users": {
        "track_music": "BOOLEAN NOT NULL DEFAULT 1",
        "track_series": "BOOLEAN NOT NULL DEFAULT 1",
        "tmdb_api_key": "TEXT",
        "igdb_client_id": "TEXT",
        "igdb_client_secret": "TEXT",
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
                    conn.execute(
                        text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}')
                    )
                    print(f"Migration: added column {table}.{name}")


# Tabelas de funcionalidades descontinuadas. São derrubadas no startup de forma
# idempotente para não ficarem órfãs em bancos já existentes.
_OBSOLETE_TABLES: list[str] = ["custom_views"]


def drop_obsolete_tables() -> None:
    """Remove tabelas de funcionalidades descontinuadas (idempotente)."""
    if not _OBSOLETE_TABLES:
        return

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table in _OBSOLETE_TABLES:
            if table in existing_tables:
                conn.execute(text(f'DROP TABLE IF EXISTS "{table}"'))
                print(f"Migration: dropped obsolete table {table}")


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ──────────────────────────────────────────────
# Integridade e índices (idempotente)
# ──────────────────────────────────────────────
_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "users": {
        "username",
        "password",
        "rating_mode",
        "view_mode",
        "track_movies",
        "track_anime",
        "track_manga",
        "track_games",
        "track_books",
        "track_music",
        "track_series",
    },
    "media": {
        "user_id",
        "external_id",
        "title",
        "type",
        "status",
        "rating",
        "review",
        "release_date",
        "image_path",
    },
    "media_log": {
        "user_id",
        "media_id",
        "date",
        "status",
        "rating",
        "review",
        "start_date",
        "end_date",
    },
    "media_list_items": {"user_id", "media_type", "media_id", "list_type", "date_log"},
    "suwayomi_accounts": {
        "user_id",
        "base_url",
        "enabled",
        "updates_per_day",
        "last_sync_at",
        "last_sync_status",
        "last_sync_summary",
        "next_sync_at",
    },
    "suwayomi_category_mappings": {
        "user_id",
        "category_id",
        "category_name",
        "category_order",
        "action",
        "status",
    },
    "suwayomi_manga_links": {
        "user_id",
        "suwayomi_manga_id",
        "media_id",
        "title",
        "last_action",
        "last_status",
        "backlog_owned",
        "last_seen_at",
        "match_error",
    },
    "tags": {"name"},
    "media_tags": {"media_id", "tag_id"},
}


def validate_schema() -> None:
    """Falha cedo em bancos muito antigos, em vez de quebrar em uma query."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table, required in _REQUIRED_COLUMNS.items():
        if table not in existing_tables:
            raise RuntimeError(
                f"Tabela obrigatória ausente: {table}. "
                "Apague/renomeie o banco e suba a aplicação novamente."
            )
        current = {column["name"] for column in inspector.get_columns(table)}
        missing = required - current
        if missing:
            raise RuntimeError(
                f"Banco antigo: a tabela '{table}' não tem as colunas "
                f"{', '.join(sorted(missing))}. "
                "Faça backup, apague/renomeie o banco e suba a aplicação novamente."
            )


def ensure_indexes() -> None:
    """Cria índices únicos e consolida tags duplicadas por caixa.

    Bancos antigos podem ter tags "Ação" e "ação"; antes do índice único,
    os registros são fundidos na tag de menor id.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    if not {"tags", "media_tags"} <= existing_tables:
        return

    with engine.begin() as conn:
        rows = conn.execute(text("SELECT id, name FROM tags ORDER BY id")).all()
        groups: dict[str, list[int]] = {}
        for tag_id, name in rows:
            groups.setdefault((name or "").strip().lower(), []).append(tag_id)

        for ids in groups.values():
            if len(ids) < 2:
                continue
            keep = ids[0]
            for duplicate in ids[1:]:
                conn.execute(
                    text(
                        "UPDATE OR IGNORE media_tags SET tag_id = :keep "
                        "WHERE tag_id = :duplicate"
                    ),
                    {"keep": keep, "duplicate": duplicate},
                )
                conn.execute(
                    text("DELETE FROM media_tags WHERE tag_id = :duplicate"),
                    {"duplicate": duplicate},
                )
                conn.execute(
                    text("DELETE FROM tags WHERE id = :duplicate"),
                    {"duplicate": duplicate},
                )
                print(f"Migration: merged duplicate tag {duplicate} into {keep}")

        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_tags_name_lower "
                "ON tags (lower(name))"
            )
        )

    with engine.begin() as conn:
        media_duplicates = conn.execute(
            text(
                "SELECT 1 FROM media GROUP BY user_id, type, external_id "
                "HAVING count(*) > 1 LIMIT 1"
            )
        ).first()
        if media_duplicates:
            print(
                "Warning: existem mídias duplicadas; "
                "o índice único uq_media_user_type_external não foi criado."
            )
        else:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_media_user_type_external "
                    "ON media (user_id, type, external_id)"
                )
            )

        list_duplicates = conn.execute(
            text(
                "SELECT 1 FROM media_list_items GROUP BY user_id, media_id, list_type "
                "HAVING count(*) > 1 LIMIT 1"
            )
        ).first()
        if list_duplicates:
            print(
                "Warning: existem itens duplicados em listas; "
                "o índice único uq_media_list_user_media_type não foi criado."
            )
        else:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_media_list_user_media_type "
                    "ON media_list_items (user_id, media_id, list_type)"
                )
            )
