import shutil
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import UploadFile

from config import settings

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


class ImageStorageService:
    def __init__(self) -> None:
        self.upload_dir = settings.upload_path

    async def store(self, file: UploadFile) -> str:
        """Salva uma imagem no disco e retorna o nome do arquivo."""
        if not file.filename:
            raise ValueError("Arquivo sem nome")

        ext = Path(file.filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError(f"Extensão não permitida: {ext}. Use: {', '.join(ALLOWED_EXTENSIONS)}")

        content = await file.read()
        if len(content) > MAX_FILE_SIZE:
            raise ValueError(f"Arquivo muito grande. Máximo: {MAX_FILE_SIZE // (1024 * 1024)}MB")

        filename = f"{uuid4()}{ext}"
        destination = self.upload_dir / filename

        with open(destination, "wb") as f:
            f.write(content)

        return filename

    def delete(self, filename: str) -> bool:
        """Remove uma imagem do disco."""
        filepath = self.upload_dir / filename
        if filepath.exists():
            filepath.unlink()
            return True
        return False

    def get_path(self, filename: str) -> Path | None:
        """Retorna o caminho completo de uma imagem, se existir."""
        filepath = self.upload_dir / filename
        return filepath if filepath.exists() else None

    async def store_from_url(self, url: str) -> str:
        """Baixa uma imagem de uma URL e salva no disco, retornando o nome do arquivo."""
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            res = await client.get(url)
            res.raise_for_status()

        content = res.content
        if len(content) > MAX_FILE_SIZE:
            raise ValueError(f"Arquivo muito grande. Máximo: {MAX_FILE_SIZE // (1024 * 1024)}MB")

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
