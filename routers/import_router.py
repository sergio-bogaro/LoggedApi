from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session

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
    file: UploadFile | None = File(None),
):
    """Lê a fonte do provedor (arquivo ou username) e devolve os itens normalizados."""
    provider = get_provider(provider_id)
    if provider.input_type == "file":
        if file is None:
            raise ValueError("Envie o arquivo da importação.")
        return await provider.parse_file(file.filename or "", await file.read())
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
