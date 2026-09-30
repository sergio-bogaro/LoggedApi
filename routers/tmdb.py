from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import get_db
from services.credentials_service import resolve_tmdb_key, tmdb_source
from services.tmdb_service import service

router = APIRouter(prefix="/api/tmdb", tags=["TMDB"])


@router.get("/config")
def get_tmdb_config(
    user_id: int | None = None, db: Session = Depends(get_db)
) -> dict[str, object]:
    """Informa se há chave do TMDB disponível e de onde ela vem (usuário/instância)."""
    return {"configured": resolve_tmdb_key(db, user_id) is not None, "source": tmdb_source(db, user_id)}


@router.get("/search")
async def search_movies(
    query: str, user_id: int | None = None, db: Session = Depends(get_db)
) -> dict:
    """Busca filmes no TMDB (proxy; a chave nunca chega ao navegador)."""
    api_key = resolve_tmdb_key(db, user_id)
    if not api_key:
        raise ValueError("Nenhuma chave do TMDB configurada.")
    return await service.search_movies(query, api_key)


@router.get("/movie/{movie_id}")
async def get_movie(
    movie_id: int, user_id: int | None = None, db: Session = Depends(get_db)
) -> dict:
    """Detalhes de um filme no TMDB (proxy)."""
    api_key = resolve_tmdb_key(db, user_id)
    if not api_key:
        raise ValueError("Nenhuma chave do TMDB configurada.")
    return await service.get_movie(movie_id, api_key)


@router.get("/search/tv")
async def search_series(
    query: str, user_id: int | None = None, db: Session = Depends(get_db)
) -> dict:
    """Busca séries no TMDB (proxy; a chave nunca chega ao navegador)."""
    api_key = resolve_tmdb_key(db, user_id)
    if not api_key:
        raise ValueError("Nenhuma chave do TMDB configurada.")
    return await service.search_tv(query, api_key)


@router.get("/tv/{tv_id}")
async def get_tv(
    tv_id: int, user_id: int | None = None, db: Session = Depends(get_db)
) -> dict:
    """Detalhes de uma série no TMDB (proxy)."""
    api_key = resolve_tmdb_key(db, user_id)
    if not api_key:
        raise ValueError("Nenhuma chave do TMDB configurada.")
    return await service.get_tv(tv_id, api_key)
