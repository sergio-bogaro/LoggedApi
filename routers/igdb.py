from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import get_db
from schemas.igdb import IgdbGame, IgdbSearchItem, IgdbSearchRequest
from services.credentials_service import igdb_source, resolve_igdb_credentials
from services.igdb_service import IgdbService

router = APIRouter(prefix="/api/igdb", tags=["IGDB"])

service = IgdbService()


def _require_credentials(client_id: str | None, client_secret: str | None) -> tuple[str, str]:
    if not client_id or not client_secret:
        raise ValueError(
            "Nenhuma credencial do IGDB configurada. Informe o Client ID e o Secret."
        )
    return client_id, client_secret


@router.get("/config")
def get_igdb_config(
    user_id: int | None = None, db: Session = Depends(get_db)
) -> dict[str, object]:
    """Informa se há credenciais do IGDB e de onde vêm (usuário/instância)."""
    client_id, client_secret = resolve_igdb_credentials(db, user_id)
    return {
        "configured": bool(client_id and client_secret),
        "source": igdb_source(db, user_id),
    }


@router.post("/games/search", response_model=list[IgdbSearchItem])
async def search_games(
    payload: IgdbSearchRequest,
    user_id: int | None = None,
    db: Session = Depends(get_db),
):
    """Search IGDB for games by title."""
    client_id, client_secret = _require_credentials(
        *resolve_igdb_credentials(db, user_id)
    )
    return await service.search_games(
        payload.query, client_id, client_secret, payload.limit
    )


@router.get("/games/{game_id}", response_model=IgdbGame)
async def get_game(
    game_id: int, user_id: int | None = None, db: Session = Depends(get_db)
):
    """Get full game details from IGDB."""
    client_id, client_secret = _require_credentials(
        *resolve_igdb_credentials(db, user_id)
    )
    return await service.get_game(game_id, client_id, client_secret)
