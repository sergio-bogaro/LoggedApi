from services.importer.providers.anilist import AniListProvider
from services.importer.providers.goodreads import GoodreadsProvider
from services.importer.providers.grouvee import GrouveeProvider
from services.importer.providers.imdb import ImdbProvider
from services.importer.providers.infinite_backlog import InfiniteBacklogProvider
from services.importer.providers.letterboxd import LetterboxdProvider
from services.importer.providers.library_sync import LibrarySyncImportProvider
from services.importer.providers.mal import MalProvider
from services.importer.providers.suwayomi import SuwayomiProvider
from services.importer.providers.trakt import TraktProvider
from services.library_sync.registry import list_providers as list_sync_providers
from domain_errors import DomainError

_ALL_PROVIDERS: list = [
    LetterboxdProvider(),
    AniListProvider(),
    MalProvider(),
    GoodreadsProvider(),
    TraktProvider(),
    ImdbProvider(),
    GrouveeProvider(),
    InfiniteBacklogProvider(),
    SuwayomiProvider(),
    LibrarySyncImportProvider("kitsu"),
    LibrarySyncImportProvider("simkl"),
    LibrarySyncImportProvider("hardcover"),
    LibrarySyncImportProvider("openlibrary"),
]
_PROVIDER_MAP: dict[str, object] = {
    provider.id: provider for provider in _ALL_PROVIDERS
}

# Fontes com sincronização completa não precisam de importação manual: o sync
# lê tudo o que o Logged armazena. O Letterboxd fica de fora porque o sync é
# parcial (RSS recente, sem watchlist) e o ZIP ainda é necessário.
_SYNC_COVERED_PROVIDER_IDS = {
    provider.id
    for provider in list_sync_providers()
    if not provider.sync_partial
} | {"suwayomi"}

# A API pública lista apenas as fontes que ainda exigem importação manual.
_PROVIDERS: list = [
    provider
    for provider in _ALL_PROVIDERS
    if provider.id not in _SYNC_COVERED_PROVIDER_IDS
]


def list_providers() -> list:
    return list(_PROVIDERS)


def get_provider(provider_id: str):
    provider = _PROVIDER_MAP.get(provider_id)
    if provider is None:
        raise DomainError(f"Provedor de importação desconhecido: '{provider_id}'.")
    return provider
