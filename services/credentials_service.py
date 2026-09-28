from sqlalchemy.orm import Session

from config import settings
from models.user import User

# Fontes possíveis de uma credencial
SOURCE_USER = "user"
SOURCE_INSTANCE = "instance"
SOURCE_NONE = "none"


def _clean(value: str | None) -> str | None:
    """Normaliza uma credencial: string vazia/só espaços vira None."""
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _get_user(db: Session, user_id: int | None) -> User | None:
    if user_id is None:
        return None
    return db.query(User).filter(User.id == user_id).first()


def resolve_tmdb_key(db: Session, user_id: int | None) -> str | None:
    """Chave do TMDB do usuário, com fallback para a da instância (.env)."""
    user = _get_user(db, user_id)
    user_key = _clean(user.tmdb_api_key) if user else None
    if user_key:
        return user_key
    return _clean(settings.tmdb_api_key)


def tmdb_source(db: Session, user_id: int | None) -> str:
    user = _get_user(db, user_id)
    if user and _clean(user.tmdb_api_key):
        return SOURCE_USER
    if _clean(settings.tmdb_api_key):
        return SOURCE_INSTANCE
    return SOURCE_NONE


def resolve_igdb_credentials(
    db: Session, user_id: int | None
) -> tuple[str | None, str | None]:
    """Credenciais do IGDB do usuário (id + secret), com fallback para a instância."""
    user = _get_user(db, user_id)
    if user:
        client_id = _clean(user.igdb_client_id)
        client_secret = _clean(user.igdb_client_secret)
        if client_id and client_secret:
            return client_id, client_secret
    return _clean(settings.igdb_client_id), _clean(settings.igdb_client_secret)


def igdb_source(db: Session, user_id: int | None) -> str:
    user = _get_user(db, user_id)
    if user and _clean(user.igdb_client_id) and _clean(user.igdb_client_secret):
        return SOURCE_USER
    if _clean(settings.igdb_client_id) and _clean(settings.igdb_client_secret):
        return SOURCE_INSTANCE
    return SOURCE_NONE
