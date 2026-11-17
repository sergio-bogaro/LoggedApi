"""Registro dos provedores de sincronização."""

from services.library_sync.providers.anilist import AniListProvider
from services.library_sync.providers.base import LibraryProvider
from services.library_sync.providers.hardcover import HardcoverProvider
from services.library_sync.providers.kitsu import KitsuProvider
from services.library_sync.providers.letterboxd import LetterboxdProvider
from services.library_sync.providers.mal import MalProvider
from services.library_sync.providers.openlibrary import OpenLibraryProvider
from services.library_sync.providers.simkl import SimklProvider
from services.library_sync.providers.trakt import TraktProvider

_PROVIDERS: list[LibraryProvider] = [
    AniListProvider(),
    MalProvider(),
    KitsuProvider(),
    TraktProvider(),
    SimklProvider(),
    LetterboxdProvider(),
    HardcoverProvider(),
    OpenLibraryProvider(),
]
_PROVIDER_MAP: dict[str, LibraryProvider] = {
    provider.id: provider for provider in _PROVIDERS
}


def list_providers() -> list[LibraryProvider]:
    return list(_PROVIDERS)


def get_provider(provider_id: str) -> LibraryProvider:
    provider = _PROVIDER_MAP.get(provider_id)
    if provider is None:
        raise ValueError(f"Provedor de sincronização desconhecido: '{provider_id}'.")
    return provider
