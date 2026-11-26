import asyncio
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import httpx
from fastapi import UploadFile

from config import settings
from domain_errors import DomainError

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_MAX_REDIRECTS = 3


def _max_file_size() -> int:
    return settings.max_upload_mb * 1024 * 1024


def _is_public_ip(value: str) -> bool:
    """Rejeita loopback, rede privada, link-local, multicast e reservados."""
    raw = value.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


async def _validate_remote_url(url: str) -> None:
    """Valida esquema e destino antes de qualquer requisição HTTP."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise DomainError("Informe uma URL http(s) válida para a imagem.")
    if settings.allow_private_image_urls:
        return

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            parsed.hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        raise DomainError("Não foi possível resolver o host da imagem.")

    if not infos:
        raise DomainError("Não foi possível resolver o host da imagem.")
    for info in infos:
        if not _is_public_ip(str(info[4][0])):
            raise DomainError(
                "A URL da imagem aponta para um endereço privado ou reservado."
            )


def _detect_image_extension(content: bytes) -> str:
    """Identifica o formato real pelos magic bytes, ignorando a extensão da URL."""
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    raise DomainError("O conteúdo baixado não é uma imagem suportada.")


async def _download_image(url: str) -> bytes:
    """Baixa a imagem com validação por redirect e limite de tamanho."""
    limit = _max_file_size()
    current = url

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                await _validate_remote_url(current)
                async with client.stream("GET", current) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise DomainError("A URL da imagem redirecionou sem destino.")
                        current = urljoin(str(response.url), location)
                        continue

                    if response.status_code >= 400:
                        raise DomainError(
                            f"Falha ao baixar a imagem ({response.status_code})."
                        )

                    raw_length = response.headers.get("content-length")
                    if raw_length:
                        try:
                            if int(raw_length) > limit:
                                raise DomainError(
                                    f"Arquivo muito grande. Máximo: {settings.max_upload_mb}MB"
                                )
                        except ValueError:
                            pass

                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > limit:
                            raise DomainError(
                                f"Arquivo muito grande. Máximo: {settings.max_upload_mb}MB"
                            )
                        chunks.append(chunk)
                    return b"".join(chunks)

            raise DomainError("A URL da imagem redirecionou demais.")
    except httpx.HTTPError:
        # Não propague a URL/headers: podem conter tokens de acesso.
        raise DomainError("Falha ao baixar a imagem.")


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
            raise DomainError(
                f"Extensão não permitida: {ext}. Use: {', '.join(ALLOWED_EXTENSIONS)}"
            )

        content = await file.read()
        limit = _max_file_size()
        if len(content) > limit:
            raise DomainError(f"Arquivo muito grande. Máximo: {settings.max_upload_mb}MB")

        real_ext = _detect_image_extension(content)
        if real_ext == ".jpeg":
            real_ext = ".jpg"
        if ext in (".jpg", ".jpeg"):
            ext = ".jpg"
        if real_ext != ext:
            raise DomainError("O conteúdo do arquivo não corresponde à extensão informada.")

        filename = f"{uuid4()}{real_ext}"
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
        content = await _download_image(url)
        ext = _detect_image_extension(content)

        filename = f"{uuid4()}{ext}"
        destination = self.upload_dir / filename
        with open(destination, "wb") as f:
            f.write(content)

        return filename
