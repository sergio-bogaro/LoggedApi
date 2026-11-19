from collections.abc import Generator

from config import settings
from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# ──────────────────────────────────────────────
# Engine
# ──────────────────────────────────────────────
# O projeto usa SQLite como banco único. As opções de conexão são específicas
# do dialeto; passar `check_same_thread` para outro driver quebraria o startup.
_is_sqlite = settings.database_url.startswith("sqlite")

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    echo=False,
)

if not _is_sqlite:
    raise RuntimeError(
        "Este projeto usa SQLite. Ajuste DATABASE_URL para sqlite:///... "
        "ou implemente as migrações específicas do novo dialeto antes de trocar "
        "de banco (ver migrations/)."
    )


if engine.url.get_backend_name() == "sqlite":

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        # Syncs longos e o worker de importação podem disputar o write lock.
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Nomes determinísticos de constraints são pré-requisito do batch mode do
# Alembic no SQLite (recriar tabela em ALTER/DROP exige identificar constraints).
class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
