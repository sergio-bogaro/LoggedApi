"""Importação em massa usando uma conexão de sincronização já configurada.

Serve para provedores que não têm um export CSV/XML próprio (Kitsu, Simkl,
Hardcover, Open Library). O job continua passando pela revisão normal da página
Importar.
"""

from sqlalchemy import select

from models.enums import MediaStatusEnum, MediaTypeEnum
from models.library_sync import LibraryConnection, LibraryStatusMapping
from schemas.import_schemas import (
    ImportEntry,
    ImportPreviewCounts,
    ImportPreviewResponse,
)
from services.importer.providers.base import ImportProvider
from services.library_sync.engine import _action_for_status
from services.library_sync.providers.base import LibraryProvider, SourceSnapshot
from services.library_sync.registry import get_provider as get_sync_provider


class LibrarySyncImportProvider(ImportProvider):
    input_type = "connection"

    def __init__(self, source_id: str) -> None:
        provider = get_sync_provider(source_id)
        self.sync_provider = provider
        self.id = source_id
        self.label = provider.label
        self.media_types = list(provider.media_types)

    async def parse_connection(
        self, db, user_id: int, media_type: MediaTypeEnum | None
    ) -> ImportPreviewResponse:
        connection = db.execute(
            select(LibraryConnection).where(
                LibraryConnection.user_id == user_id,
                LibraryConnection.provider == self.id,
            )
        ).scalar_one_or_none()
        if connection is None:
            raise ValueError(
                f"Configure a conexão com {self.label} em Configurações > Integrações."
            )
        auth = LibraryProvider.auth(connection)
        if not (
            str(auth.get("username") or "").strip()
            or str(auth.get("token") or "").strip()
            or str(auth.get("access_token") or "").strip()
        ):
            raise ValueError(
                f"Conecte sua conta do {self.label} antes de importar a biblioteca."
            )

        mappings = {
            row.source_status: row
            for row in db.execute(
                select(LibraryStatusMapping).where(
                    LibraryStatusMapping.user_id == user_id,
                    LibraryStatusMapping.provider == self.id,
                )
            )
            .scalars()
            .all()
        }
        snapshot: SourceSnapshot = await self.sync_provider.fetch(connection, {})
        entries: list[ImportEntry] = []
        for item in snapshot.items:
            if media_type is not None and item.media_type != media_type:
                continue
            status_def = next(
                (value for value in snapshot.statuses if value.key == item.status),
                None,
            )
            action, target = _action_for_status(
                mappings.get(item.status),
                status_def.default_action if status_def else None,
            )
            if action == "ignore":
                continue
            in_backlog = action == "backlog"
            status = target if not in_backlog else None
            refs = dict(item.external_refs or {})
            if item.external_id:
                if item.media_type in (MediaTypeEnum.ANIME, MediaTypeEnum.MANGA):
                    refs.setdefault("anilistId", item.external_id)
                elif item.media_type in (MediaTypeEnum.MOVIES, MediaTypeEnum.SERIES):
                    refs.setdefault("tmdbId", item.external_id)
                elif item.media_type == MediaTypeEnum.BOOK:
                    refs.setdefault("openlibraryId", item.external_id)
            refs["syncProvider"] = self.id
            refs["syncSourceId"] = item.source_id
            refs["syncStatus"] = item.status
            entries.append(
                ImportEntry(
                    key=f"{self.id}:{item.source_id}",
                    media_type=item.media_type,
                    title=item.title,
                    year=item.year,
                    overview=item.overview,
                    source=self.id,
                    external_refs=refs,
                    status=status if isinstance(status, MediaStatusEnum) else None,
                    rating=item.rating,
                    review=item.review,
                    logs=[],
                    in_backlog=in_backlog,
                    backlog_date=None,
                )
            )

        if not entries:
            raise ValueError(
                f"Nenhum item encontrado na biblioteca do {self.label}."
            )

        counts = ImportPreviewCounts(
            total=len(entries),
            with_logs=0,
            rated=sum(1 for entry in entries if entry.rating is not None),
            reviewed=sum(1 for entry in entries if entry.review),
            backlog=sum(1 for entry in entries if entry.in_backlog),
        )
        return ImportPreviewResponse(
            media_type=entries[0].media_type,
            entries=entries,
            counts=counts,
        )
