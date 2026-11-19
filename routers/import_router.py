import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from database import get_db
from models.enums import MediaTypeEnum
from models.library_sync import LibraryConnection
from schemas.import_job import (
    ImportJobCommitRequest,
    ImportJobItemResponse,
    ImportJobItemsResponse,
    ImportJobPatch,
    ImportJobResponse,
)
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
from services.import_jobs.serializers import job_response
from services.import_jobs.service import service as import_job_service
from services.import_jobs.storage import save_import_file
from services.library_sync.providers.base import LibraryProvider
from domain_errors import DomainError

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
            raise DomainError(
                "Arquivo de importação muito grande. Máximo: "
                f"{MAX_IMPORT_FILE_SIZE // (1024 * 1024)}MB."
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/providers", response_model=list[ImportProviderInfo])
def get_providers():
    """Lista os provedores de importação disponíveis e o tipo de entrada de cada um."""
    return [provider.info() for provider in list_providers()]


@router.post("/jobs", response_model=ImportJobResponse, status_code=201)
async def create_import_job(
    user_id: int = Form(...),
    provider_id: str = Form(...),
    media_type: MediaTypeEnum | None = Form(None),
    username: str | None = Form(None),
    source_url: str | None = Form(None),
    file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    """Cria uma importação em background e devolve o job na fila."""
    provider = get_provider(provider_id)
    input_type = provider.input_type
    selected_media_type = media_type
    if selected_media_type is None and input_type != "connection":
        selected_media_type = (
            provider.media_types[0] if provider.media_types else None
        )

    file_path = None
    file_name = None

    try:
        if input_type == "file":
            if file is None:
                raise DomainError("Envie o arquivo da importação.")
            raw = await _read_limited(file)
            file_path, file_name = save_import_file(file.filename, raw)
        elif input_type == "url":
            if not source_url or not source_url.strip():
                raise DomainError("Informe a URL do servidor.")
        elif input_type == "connection":
            connection = db.execute(
                select(LibraryConnection).where(
                    LibraryConnection.user_id == user_id,
                    LibraryConnection.provider == provider_id,
                )
            ).scalar_one_or_none()
            auth = LibraryProvider.auth(connection) if connection else {}
            if not (
                str(auth.get("username") or "").strip()
                or str(auth.get("token") or "").strip()
                or str(auth.get("access_token") or "").strip()
            ):
                raise DomainError(
                    f"Conecte sua conta do {provider.label} em Configurações > Integrações."
                )
        else:
            if not username or not username.strip():
                raise DomainError("Informe o username.")

        job = import_job_service.create(
            db,
            user_id=user_id,
            provider=provider_id,
            input_type=input_type,
            media_type=selected_media_type,
            source_url=source_url.strip() if source_url else None,
            source_username=username.strip() if username else None,
            file_path=file_path,
            file_name=file_name,
        )
        return job_response(job)
    except Exception:
        if file_path:
            try:
                Path(file_path).unlink(missing_ok=True)
            except OSError:
                pass
        raise


@router.get("/jobs", response_model=list[ImportJobResponse])
def list_import_jobs(
    user_id: int,
    status: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Lista as importações recentes do usuário (opcionalmente por status)."""
    return import_job_service.list(db, user_id, status=status, limit=limit)


@router.get("/jobs/{job_id}", response_model=ImportJobResponse)
def get_import_job(job_id: int, user_id: int, db: Session = Depends(get_db)):
    """Detalhe de uma importação (status e progresso)."""
    return import_job_service.get_response(db, job_id, user_id)


@router.get("/jobs/{job_id}/items", response_model=ImportJobItemsResponse)
def get_import_job_items(
    job_id: int,
    user_id: int,
    filter_name: str = Query("all", alias="filter"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Itens da importação, paginados e filtrados, com match e seleção."""
    return import_job_service.items(
        db, job_id, user_id, filter_name=filter_name, offset=offset, limit=limit
    )


@router.patch(
    "/jobs/{job_id}/items/{item_id}", response_model=ImportJobItemResponse
)
def patch_import_job_item(
    job_id: int,
    item_id: int,
    user_id: int,
    data: ImportJobPatch,
    db: Session = Depends(get_db),
):
    """Seleciona um candidato ou ignora um item durante a revisão."""
    return import_job_service.patch_item(db, job_id, item_id, user_id, data)


@router.post("/jobs/{job_id}/commit", response_model=ImportCommitResponse)
def commit_import_job(
    job_id: int,
    user_id: int,
    data: ImportJobCommitRequest,
    db: Session = Depends(get_db),
):
    """Grava os itens selecionados do job e conclui a importação."""
    return import_job_service.commit(db, job_id, user_id, data)


@router.post("/jobs/{job_id}/cancel", response_model=ImportJobResponse)
def cancel_import_job(job_id: int, user_id: int, db: Session = Depends(get_db)):
    """Cancela uma importação na fila, em andamento ou pronta para revisão."""
    return import_job_service.cancel(db, job_id, user_id)


@router.delete("/jobs/{job_id}", status_code=204)
def delete_import_job(job_id: int, user_id: int, db: Session = Depends(get_db)):
    """Remove uma importação já finalizada/cancelada do histórico."""
    import_job_service.delete(db, job_id, user_id)


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
            raise DomainError("Informe a URL do servidor Suwayomi.")
        return await provider.parse_url(source_url, media_type, db, user_id)
    if provider.input_type == "file":
        if file is None:
            raise DomainError("Envie o arquivo da importação.")
        raw = await _read_limited(file)
        return await run_in_threadpool(
            _parse_file_sync, provider, file.filename or "", raw
        )
    if provider.input_type == "connection":
        return await provider.parse_connection(db, user_id, media_type)
    if not username:
        raise DomainError("Informe o username.")
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
    tmdb_key = resolve_tmdb_key(db, data.user_id)
    igdb_credentials = resolve_igdb_credentials(db, data.user_id)
    return await matcher.search(data.media_type, data.query, tmdb_key, igdb_credentials)


@router.post("/commit", response_model=ImportCommitResponse)
def commit_import(data: ImportCommitRequest, db: Session = Depends(get_db)):
    """Grava as mídias, logs e itens de backlog confirmados na revisão."""
    return committer.commit(db, data)
