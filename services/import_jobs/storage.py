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


def delete_job_file(job: ImportJob) -> None:
    if not job.file_path:
        return
    try:
        Path(job.file_path).unlink(missing_ok=True)
    except OSError:
        pass
    job.file_path = None
