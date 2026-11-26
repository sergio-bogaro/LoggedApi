"""Backup consistente do banco SQLite e dos uploads, com retenção GFS.

Uso:
    python scripts/backup.py                 # backup + poda
    python scripts/backup.py --dry-run       # só mostra o que faria
    python scripts/backup.py --on-startup    # usado pelo entrypoint do container
    python scripts/backup.py --keep-monthly 12

O snapshot usa `VACUUM INTO`, que é consistente mesmo com a API rodando em WAL.
O arquivo resultante passa por `PRAGMA integrity_check` antes de substituir o
backup do dia; se a verificação falhar, nada é apagado.
"""

from __future__ import annotations

import argparse
import gzip
import logging
import os
import shutil
import sqlite3
import sys
import tarfile
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from uuid import uuid4

# Permite `python scripts/backup.py` (sys.path[0] vira a pasta scripts/).
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.engine import make_url

from config import settings
from scripts.backup_retention import (
    DB_BACKUP_RE,
    UPLOADS_BACKUP_RE,
    keep_set,
    list_backups,
)

log = logging.getLogger("logged.backup")

_LOCK_NAME = ".backup.lock"
# Um lock órfão (processo morto) não pode bloquear backups para sempre.
_LOCK_STALE_SECONDS = 6 * 60 * 60


class BackupError(RuntimeError):
    """Falha que deve impedir o startup (ou retornar exit code 1)."""


@contextmanager
def _backup_lock(directory: Path):
    """Serializa execuções concorrentes de backup."""
    lock_path = directory / _LOCK_NAME
    acquired = False
    fd: int | None = None
    try:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0
            if age < _LOCK_STALE_SECONDS:
                raise BackupError("Já existe um backup em andamento.")
            lock_path.unlink(missing_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)

        acquired = True
        os.write(fd, f"pid={os.getpid()} started={time.time():.0f}".encode())
        yield
    finally:
        if fd is not None:
            os.close(fd)
        if acquired:
            lock_path.unlink(missing_ok=True)


def database_path() -> Path:
    """Caminho absoluto do arquivo SQLite configurado em DATABASE_URL."""
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        raise BackupError(
            "O backup automático só suporta SQLite em arquivo. "
            f"Banco configurado: {url.get_backend_name()}"
        )
    if url.database == ":memory:":
        raise BackupError("Banco em memória não tem arquivo para copiar.")
    return Path(url.database).expanduser().resolve()


def backup_dir() -> Path:
    path = Path(settings.backup_dir).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    """Copia o banco de forma consistente (inclui o que está no WAL)."""
    if destination.exists():
        destination.unlink()

    connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        connection.execute("VACUUM INTO ?", (str(destination),))
    finally:
        connection.close()


def _integrity_ok(path: Path) -> tuple[bool, str]:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    message = str(result[0]) if result else "sem resultado"
    return message.lower() == "ok", message


def _gzip_file(source: Path, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + f".{uuid4().hex}.tmp")
    try:
        with source.open("rb") as raw, gzip.open(
            temporary, "wb", compresslevel=9
        ) as gz:
            shutil.copyfileobj(raw, gz)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def create_database_backup(directory: Path, today: date, dry_run: bool) -> Path | None:
    source = database_path()
    if not source.exists():
        log.warning("Banco %s não existe ainda; nada para copiar.", source)
        return None

    destination = directory / f"logged-{today.isoformat()}.db.gz"
    if dry_run:
        log.info("[dry-run] criaria %s a partir de %s", destination, source)
        return destination

    # Nome único por execução: dois backups simultâneos não compartilham
    # temporários nem apagam o arquivo um do outro.
    temporary_snapshot = (
        directory / f".logged-{today.isoformat()}-{uuid4().hex}.db.tmp"
    )
    try:
        _sqlite_snapshot(source, temporary_snapshot)
        ok, message = _integrity_ok(temporary_snapshot)
        if not ok:
            raise BackupError(
                f"integrity_check falhou no snapshot ({message}). "
                "O backup anterior foi preservado."
            )
        _gzip_file(temporary_snapshot, destination)
    finally:
        temporary_snapshot.unlink(missing_ok=True)

    size_mb = destination.stat().st_size / (1024 * 1024)
    log.info("Banco copiado para %s (%.1f MB)", destination, size_mb)
    return destination


def create_uploads_backup(directory: Path, today: date, dry_run: bool) -> Path | None:
    source = Path(settings.upload_path).expanduser().resolve()
    if not settings.backup_include_uploads:
        log.info("Backup de uploads desabilitado (BACKUP_INCLUDE_UPLOADS=false).")
        return None
    if not source.exists():
        log.warning("Diretório de uploads %s não existe; nada para copiar.", source)
        return None

    files = sorted(
        path
        for path in source.iterdir()
        if path.is_file() and path.name != ".gitkeep"
    )
    if not files:
        log.info("Nenhum upload para copiar.")
        return None

    destination = directory / f"uploads-{today.isoformat()}.tar.gz"
    if dry_run:
        log.info("[dry-run] criaria %s com %d arquivo(s)", destination, len(files))
        return destination

    temporary = destination.with_suffix(
        destination.suffix + f".{uuid4().hex}.tmp"
    )
    try:
        with tarfile.open(temporary, "w:gz") as tar:
            for path in files:
                tar.add(path, arcname=f"uploads/{path.name}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)

    size_mb = destination.stat().st_size / (1024 * 1024)
    log.info("Uploads copiados para %s (%.1f MB)", destination, size_mb)
    return destination


def prune(directory: Path, today: date, *, dry_run: bool) -> list[Path]:
    """Remove backups fora do keep-set. Arquivos fora do padrão nunca são tocados."""
    database_backups = list_backups(directory, DB_BACKUP_RE)
    upload_backups = list_backups(directory, UPLOADS_BACKUP_RE)

    keep = keep_set(
        database_backups.keys(),
        today,
        settings.backup_keep_daily,
        settings.backup_keep_weekly,
        settings.backup_keep_monthly,
    )
    if not keep:
        return []

    removed: list[Path] = []
    for pattern, backups in ((DB_BACKUP_RE, database_backups), (UPLOADS_BACKUP_RE, upload_backups)):
        for day, path in sorted(backups.items()):
            if day in keep:
                continue
            removed.append(path)
            if dry_run:
                log.info("[dry-run] removeria %s", path)
            else:
                path.unlink(missing_ok=True)
                log.info("Removido (retenção): %s", path)
    return removed


def run(
    *,
    dry_run: bool = False,
    on_startup: bool = False,
    today: date | None = None,
) -> dict[str, object]:
    """Executa backup + poda e devolve um resumo (usado também pelo painel)."""
    today = today or date.today()
    directory = backup_dir()
    origin = "startup" if on_startup else "manual"
    log.info("Backup iniciado (%s) em %s", origin, directory)

    with _backup_lock(directory):
        database_file = create_database_backup(directory, today, dry_run)
        uploads_file = create_uploads_backup(directory, today, dry_run)
        removed = prune(directory, today, dry_run=dry_run)

    log.info(
        "Backup concluído: banco=%s uploads=%s removidos=%d",
        database_file.name if database_file else "-",
        uploads_file.name if uploads_file else "-",
        len(removed),
    )
    return {
        "database": str(database_file) if database_file else None,
        "uploads": str(uploads_file) if uploads_file else None,
        "removed": [str(path) for path in removed],
        "dry_run": dry_run,
        "origin": origin,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backup do Logged (SQLite + uploads).")
    parser.add_argument("--dry-run", action="store_true", help="Não grava nem apaga nada.")
    parser.add_argument(
        "--on-startup",
        action="store_true",
        help="Marca a execução como originada pelo entrypoint do container.",
    )
    parser.add_argument("--keep-daily", type=int, help="Sobrescreve BACKUP_KEEP_DAILY.")
    parser.add_argument("--keep-weekly", type=int, help="Sobrescreve BACKUP_KEEP_WEEKLY.")
    parser.add_argument("--keep-monthly", type=int, help="Sobrescreve BACKUP_KEEP_MONTHLY.")
    args = parser.parse_args(argv)

    if args.keep_daily is not None:
        settings.backup_keep_daily = args.keep_daily
    if args.keep_weekly is not None:
        settings.backup_keep_weekly = args.keep_weekly
    if args.keep_monthly is not None:
        settings.backup_keep_monthly = args.keep_monthly

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        run(dry_run=args.dry_run, on_startup=args.on_startup)
    except BackupError as exc:
        log.error("Backup falhou: %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001 — o entrypoint precisa do exit code
        log.exception("Backup falhou inesperadamente: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
