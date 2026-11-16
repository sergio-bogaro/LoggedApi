import asyncio

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from database import get_db
from models.enums import MediaTypeEnum
from schemas.import_schemas import (
    ImportCandidate,
    ImportCommitRequest,
    ImportCommitResponse,
    ImportMatchRequest,
    ImportMatchResponse,
    ImportPreviewResponse,
    ImportProviderInfo,
    ImportSearchRequest,
)
from services.credentials_service import resolve_igdb_credentials, resolve_tmdb_key
from services.importer.commit import committer
from services.importer.matcher import matcher
from services.importer.registry import get_provider, list_providers

router = APIRouter(prefix="/api/import", tags=["Import"])

MAX_IMPORT_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


def _parse_file_sync(provider, filename: str, raw: bytes):
    """Executa o parser (CPU) fora da event loop."""
    return asyncio.run(provider.parse_file(filename, raw))


async def _read_limited(file: UploadFile) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > MAX_IMPORT_FILE_SIZE:
            raise ValueError(
                "Arquivo de importação muito grande. Máximo: "
                f"{MAX_IMPORT_FILE_SIZE // (1024 * 1024)}MB."
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/providers", response_model=list[ImportProviderInfo])
def get_providers():
    """Lista os provedores de importação disponíveis e o tipo de entrada de cada um."""
    return [provider.info() for provider in list_providers()]


@router.post("/{provider_id}/parse", response_model=ImportPreviewResponse)
async def parse_provider(
    provider_id: str,
    user_id: int = Form(...),
    media_type: MediaTypeEnum | None = Form(None),
    username: str | None = Form(None),
    source_url: str | None = Form(None),
    file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    """Lê a fonte do provedor (arquivo, username ou servidor) e devolve os itens normalizados."""
    provider = get_provider(provider_id)
    if provider.input_type == "url":
        if not source_url:
            raise ValueError("Informe a URL do servidor Suwayomi.")
        return await provider.parse_url(source_url, media_type, db, user_id)
    if provider.input_type == "file":
        if file is None:
            raise ValueError("Envie o arquivo da importação.")
        raw = await _read_limited(file)
        return await run_in_threadpool(
            _parse_file_sync, provider, file.filename or "", raw
        )
    if not username:
        raise ValueError("Informe o username.")
    return await provider.parse_username(username, media_type)


@router.post("/match", response_model=ImportMatchResponse)
async def match_import(data: ImportMatchRequest, db: Session = Depends(get_db)):
    """Casa um lote de itens com o provedor de metadata (TMDB/AniList/OpenLibrary/IGDB)."""
    tmdb_key = resolve_tmdb_key(db, data.user_id)
    igdb_credentials = resolve_igdb_credentials(db, data.user_id)
    return await matcher.match(db, data, tmdb_key, igdb_credentials)


@router.post("/search", response_model=list[ImportCandidate])
async def search_import(
    data: ImportSearchRequest, db: Session = Depends(get_db)
) -> list[ImportCandidate]:
    """Busca manual de candidatos para corrigir um match."""
    tmdb_key = resolve_tmdb_key(db, data.user_id) if data.user_id is not None else None
    igdb_credentials = (
        resolve_igdb_credentials(db, data.user_id) if data.user_id is not None else (None, None)
    )
    return await matcher.search(data.media_type, data.query, tmdb_key, igdb_credentials)


@router.post("/commit", response_model=ImportCommitResponse)
def commit_import(data: ImportCommitRequest, db: Session = Depends(get_db)):
    """Grava as mídias, logs e itens de backlog confirmados na revisão."""
    return committer.commit(db, data)
