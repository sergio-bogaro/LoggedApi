from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from fastapi import UploadFile

from config import settings
from domain_errors import DomainError

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _max_file_size() -> int:
    return settings.max_upload_mb * 1024 * 1024


class ImageStorageService:
    def __init__(self) -> None:
        self.upload_dir = settings.upload_path

    def _safe_path(self, filename: str) -> Path:
        """Impede que um valor vindo do banco/API escape da pasta de uploads."""
        if not filename or Path(filename).name != filename:
            raise DomainError("Nome de arquivo de imagem inválido.")

        base = self.upload_dir.resolve()
        filepath = (base / filename).resolve()
        if filepath.parent != base:
            raise DomainError("Nome de arquivo de imagem inválido.")
        return filepath

    async def store(self, file: UploadFile) -> str:
        """Salva uma imagem no disco e retorna o nome do arquivo."""
        if not file.filename:
            raise DomainError("Arquivo sem nome")

        ext = Path(file.filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise DomainError(f"Extensão não permitida: {ext}. Use: {', '.join(ALLOWED_EXTENSIONS)}")

        content = await file.read()
        limit = _max_file_size()
        if len(content) > limit:
            raise DomainError(f"Arquivo muito grande. Máximo: {settings.max_upload_mb}MB")

        filename = f"{uuid4()}{ext}"
        destination = self.upload_dir / filename

        with open(destination, "wb") as f:
            f.write(content)

        return filename

    def delete(self, filename: str) -> bool:
        """Remove uma imagem do disco."""
        filepath = self._safe_path(filename)
        if filepath.exists():
            filepath.unlink()
            return True
        return False

    def get_path(self, filename: str) -> Path | None:
        """Retorna o caminho completo de uma imagem, se existir."""
        filepath = self._safe_path(filename)
        return filepath if filepath.exists() else None

    async def store_from_url(self, url: str) -> str:
        """Baixa uma imagem de uma URL e salva no disco, retornando o nome do arquivo."""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise DomainError("Informe uma URL http(s) válida para a imagem.")

        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                res = await client.get(url)
                res.raise_for_status()
        except httpx.HTTPError as exc:
            raise DomainError(f"Falha ao baixar a imagem: {exc}") from exc

        content = res.content
        limit = _max_file_size()
        if len(content) > limit:
            raise DomainError(f"Arquivo muito grande. Máximo: {settings.max_upload_mb}MB")

        # Determine extension from URL or content type
        ext = Path(url.split("?")[0]).suffix.lower()
        content_type = res.headers.get("content-type", "")
        if ext not in ALLOWED_EXTENSIONS:
            if "png" in content_type:
                ext = ".png"
            elif "webp" in content_type:
                ext = ".webp"
            elif "gif" in content_type:
                ext = ".gif"
            else:
                ext = ".jpg"  # fallback

        filename = f"{uuid4()}{ext}"
        destination = self.upload_dir / filename

        with open(destination, "wb") as f:
            f.write(content)

        return filename
