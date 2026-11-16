"""Helpers de texto compartilhados pelos importadores."""

import re
import unicodedata


def normalize_title(text: str) -> str:
    """Compara títulos ignorando caixa, acentos e pontuação."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    without_accents = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    return re.sub(r"[^a-z0-9]+", " ", without_accents.lower()).strip()


def search_title(title: str) -> str:
    """Variação usada para buscar no AniList.

    O Suwayomi costuma trazer títulos como ``Kaiju n°8`` ou com prefixos de
    capítulo; a busca do AniList funciona melhor com ``Kaiju No. 8``.
    """
    text = title or ""
    text = re.sub(r"^\s*#\d+\s*[-:]\s*", "", text)
    text = re.sub(r"\bn\s*[°º]\s*(\d)", r"No. \1", text, flags=re.IGNORECASE)
    text = re.sub(r"\bn\s+(\d)", r"No. \1", text, flags=re.IGNORECASE)
    text = text.replace("°", " ").replace("º", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text or title
