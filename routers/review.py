from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from models.enums import MediaTypeEnum
from schemas.review import (
    ReviewCommitRequest,
    ReviewCommitResponse,
    ReviewCounts,
    ReviewItem,
    ReviewItemsResponse,
    ReviewPatch,
    ReviewStateRequest,
)
from services.review.service import (
    ReviewMatchFilter,
    ReviewSourceFilter,
    ReviewStateFilter,
    service as review_service,
)

router = APIRouter(prefix="/api/review", tags=["Revisão"])


@router.get("/items", response_model=ReviewItemsResponse)
def list_review_items(
    user_id: int,
    state: ReviewStateFilter = "pending",
    match: ReviewMatchFilter = "all",
    source: ReviewSourceFilter = "all",
    provider: str | None = None,
    media_type: MediaTypeEnum | None = None,
    search: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Lista itens de revisão de jobs manuais e do sync com um contrato único."""
    return review_service.list_items(
        db,
        user_id,
        state=state,
        match=match,
        source=source,
        provider=provider,
        media_type=media_type,
        search=search,
        offset=offset,
        limit=limit,
    )


@router.get("/counts", response_model=ReviewCounts)
def review_counts(
    user_id: int,
    provider: str | None = None,
    media_type: MediaTypeEnum | None = None,
    source: ReviewSourceFilter = "all",
    db: Session = Depends(get_db),
):
    """Contadores por tarefa e por item para sidebar, aba e filtros."""
    return review_service.counts(
        db, user_id, provider=provider, media_type=media_type, source=source
    )


@router.patch("/items/{key}", response_model=ReviewItem)
def patch_review_item(
    key: str,
    data: ReviewPatch,
    db: Session = Depends(get_db),
):
    """Seleciona um candidato ou ignora/restaura um item da revisão."""
    return review_service.patch_item(
        db, data.user_id, key, selected=data.selected, ignored=data.ignored
    )


@router.post("/state", status_code=204)
def set_review_state(
    data: ReviewStateRequest,
    db: Session = Depends(get_db),
):
    """Aplica ignorar/restaurar em lote."""
    review_service.set_state(db, data.user_id, data.keys, data.state)


@router.post("/commit", response_model=ReviewCommitResponse)
def commit_review_items(
    data: ReviewCommitRequest,
    db: Session = Depends(get_db),
):
    """Confirma os itens selecionados, delegando para o fluxo de cada origem."""
    return review_service.commit(
        db, data.user_id, data.keys, add_to_backlog=data.add_to_backlog
    )
