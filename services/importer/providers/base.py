"""Contrato de um provedor de importação.

Cada provedor sabe ler a sua fonte (arquivo, perfil público ou servidor) e
devolver `ImportEntry` já normalizados (nota 0–10, status do Logged). A
resolução do `external_id` do app fica a cargo do matcher.
"""

from models.enums import MediaTypeEnum
from schemas.import_schemas import ImportPreviewResponse, ImportProviderInfo


class ImportProvider:
    id: str = ""
    label: str = ""
    media_types: list[MediaTypeEnum] = []
    # "file" (upload), "username" (perfil público) ou "url" (servidor)
    input_type: str = "file"
    accepts: str | None = None
    # Itens que já existem no Logged são ignorados na revisão.
    skip_existing: bool = False

    def info(self) -> ImportProviderInfo:
        return ImportProviderInfo(
            id=self.id,
            label=self.label,
            media_types=self.media_types,
            input_type=self.input_type,
            accepts=self.accepts,
            skip_existing=self.skip_existing,
        )

    async def parse_file(self, filename: str, raw: bytes) -> ImportPreviewResponse:
        raise ValueError("Este provedor não aceita arquivo.")

    async def parse_username(
        self, username: str, media_type: MediaTypeEnum | None
    ) -> ImportPreviewResponse:
        raise ValueError("Este provedor não aceita username.")

    async def parse_url(
        self, url: str, media_type: MediaTypeEnum | None, db, user_id: int
    ) -> ImportPreviewResponse:
        raise ValueError("Este provedor não aceita URL.")
