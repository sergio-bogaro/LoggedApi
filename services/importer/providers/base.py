"""Contrato de um provedor de importação.

Cada provedor sabe ler a sua fonte (arquivo ou perfil público) e devolver
`ImportEntry` já normalizados (nota 0–10, status do Logged). A resolução do
`external_id` do app fica a cargo do matcher.
"""

from models.enums import MediaTypeEnum
from schemas.import_schemas import ImportPreviewResponse, ImportProviderInfo


class ImportProvider:
    id: str = ""
    label: str = ""
    media_types: list[MediaTypeEnum] = []
    # "file" (upload) ou "username" (perfil público)
    input_type: str = "file"
    accepts: str | None = None

    def info(self) -> ImportProviderInfo:
        return ImportProviderInfo(
            id=self.id,
            label=self.label,
            media_types=self.media_types,
            input_type=self.input_type,
            accepts=self.accepts,
        )

    async def parse_file(self, filename: str, raw: bytes) -> ImportPreviewResponse:
        raise ValueError("Este provedor não aceita arquivo.")

    async def parse_username(
        self, username: str, media_type: MediaTypeEnum | None
    ) -> ImportPreviewResponse:
        raise ValueError("Este provedor não aceita username.")
