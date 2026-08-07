from fastapi import APIRouter

from config import settings
from schemas.igdb import IgdbGame, IgdbSearchItem, IgdbSearchRequest
from services.igdb_service import IgdbService

router = APIRouter(prefix="/api/igdb", tags=["IGDB"])

service = IgdbService()


@router.get("/config")
def get_igdb_config() -> dict[str, bool]:
    """Expose whether IGDB credentials are configured in .env."""
    return {
        "configured": bool(settings.igdb_client_id and settings.igdb_client_secret)
    }


@router.post("/games/search", response_model=list[IgdbSearchItem])
async def search_games(payload: IgdbSearchRequest):
    """Search IGDB for games by title."""
    return await service.search_games(payload.query, payload.limit)


@router.get("/games/{game_id}", response_model=IgdbGame)
async def get_game(game_id: int):
    """Get full game details from IGDB."""
    return await service.get_game(game_id)
