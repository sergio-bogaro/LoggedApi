from services.importer.providers.anilist import AniListProvider
from services.importer.providers.goodreads import GoodreadsProvider
from services.importer.providers.imdb import ImdbProvider
from services.importer.providers.letterboxd import LetterboxdProvider
from services.importer.providers.mal import MalProvider
from services.importer.providers.trakt import TraktProvider

_PROVIDERS: list = [
    LetterboxdProvider(),
    AniListProvider(),
    MalProvider(),
    GoodreadsProvider(),
    TraktProvider(),
    ImdbProvider(),
]
_PROVIDER_MAP: dict[str, object] = {provider.id: provider for provider in _PROVIDERS}


def list_providers() -> list:
    return list(_PROVIDERS)


def get_provider(provider_id: str):
    provider = _PROVIDER_MAP.get(provider_id)
    if provider is None:
        raise ValueError(f"Provedor de importação desconhecido: '{provider_id}'.")
    return provider
