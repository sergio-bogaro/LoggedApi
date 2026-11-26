"""Armazenamento temporário dos arquivos de importação."""

import uuid
from pathlib import Path

from config import settings
from models.import_job import ImportJob


def imports_dir() -> Path:
    path = settings.import_path
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_import_file(filename: str | None, raw: bytes) -> tuple[str, str]:
    safe_name = Path(filename or "import.bin").name
    path = imports_dir() / f"{uuid.uuid4().hex}_{safe_name}"
    path.write_bytes(raw)
    return str(path), safe_name


def delete_import_path(file_path: str | None) -> bool:
    """Remove um arquivo temporário, mas só dentro da pasta de imports."""
    if not file_path:
        return True
    try:
        candidate = Path(file_path).resolve()
        base = imports_dir().resolve()
        if candidate.parent != base:
            return False
        candidate.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def delete_job_file(job: ImportJob) -> None:
    if not job.file_path:
        return
    if delete_import_path(job.file_path):
        job.file_path = None
