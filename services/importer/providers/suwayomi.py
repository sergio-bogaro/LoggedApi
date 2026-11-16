"""Provedor Suwayomi (mangás da biblioteca do servidor).

Lê a biblioteca e as categorias do servidor e devolve apenas os títulos que
ainda não existem no Logged. A categoria define o status e/ou o backlog.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.enums import MediaTypeEnum
from models.media import Media
from models.suwayomi import SuwayomiAccount, SuwayomiCategoryMapping
from schemas.import_schemas import (
    ImportEntry,
    ImportPreviewCounts,
    ImportPreviewResponse,
)
from services.importer.providers.base import ImportProvider
from services.importer.text import normalize_title, search_title
from services.suwayomi.categories import ACTION_BACKLOG, ACTION_STATUS, resolve_action
from services.suwayomi.client import SuwayomiClient, normalize_base_url


class SuwayomiProvider(ImportProvider):
    id = "suwayomi"
    label = "Suwayomi"
    media_types = [MediaTypeEnum.MANGA]
    input_type = "url"
    skip_existing = True

    async def parse_url(
        self,
        url: str,
        media_type: MediaTypeEnum | None,
        db: Session,
        user_id: int,
    ) -> ImportPreviewResponse:
        base_url = normalize_base_url(url)

        mappings = {
            row.category_id: row
            for row in db.execute(
                select(SuwayomiCategoryMapping).where(
                    SuwayomiCategoryMapping.user_id == user_id
                )
            )
            .scalars()
            .all()
        }
        if not mappings:
            raise ValueError(
                "Nenhuma categoria do Suwayomi foi configurada. "
                "Abra Configurações → Integrações e defina o que cada categoria vira no Logged."
            )
        if not any(row.action != "ignore" for row in mappings.values()):
            raise ValueError(
                "Todas as categorias do Suwayomi estão como 'Ignorar'. "
                "Configure ao menos uma categoria para importar."
            )

        account = db.execute(
            select(SuwayomiAccount).where(SuwayomiAccount.user_id == user_id)
        ).scalar_one_or_none()
        username = password = None
        if account is not None:
            try:
                if normalize_base_url(account.base_url) == base_url:
                    username = account.username
                    password = account.password
            except ValueError:
                pass

        async with SuwayomiClient(base_url, username=username, password=password) as client:
            categories = await client.get_categories()
            library = await client.get_library()

        category_meta = {category["id"]: category for category in categories}
        existing_titles = {
            normalize_title(title)
            for title in db.execute(
                select(Media.title).where(
                    Media.user_id == user_id, Media.type == MediaTypeEnum.MANGA
                )
            )
            .scalars()
            .all()
        }

        entries: list[ImportEntry] = []
        seen_titles: set[str] = set()

        for manga in library:
            action, status = resolve_action(
                (manga.get("categories") or {}).get("nodes") or [],
                mappings,
                category_meta,
            )
            if action is None:
                continue

            title = (manga.get("title") or "").strip()
            normalized = normalize_title(title)
            if not normalized or normalized in existing_titles or normalized in seen_titles:
                continue
            seen_titles.add(normalized)

            manga_id = manga.get("id")
            if manga_id is None:
                continue

            real_url = manga.get("realUrl") or manga.get("url") or ""
            refs = {
                "suwayomiId": str(manga_id),
                "suwayomiUrl": real_url,
            }
            cleaned = search_title(title)
            if cleaned and normalize_title(cleaned) != normalized:
                refs["searchTitle"] = cleaned

            entries.append(
                ImportEntry(
                    key=f"suwayomi:{manga_id}",
                    media_type=MediaTypeEnum.MANGA,
                    title=title,
                    overview=manga.get("description"),
                    source="suwayomi",
                    external_refs=refs,
                    status=status if action == ACTION_STATUS else None,
                    in_backlog=action == ACTION_BACKLOG,
                )
            )

        if not entries:
            raise ValueError(
                "Nenhum mangá novo encontrado nas categorias mapeadas. "
                "Talvez a biblioteca do Suwayomi já esteja toda no Logged."
            )

        counts = ImportPreviewCounts(
            total=len(entries),
            with_logs=0,
            rated=0,
            reviewed=0,
            backlog=sum(1 for entry in entries if entry.in_backlog),
        )
        return ImportPreviewResponse(
            media_type=MediaTypeEnum.MANGA, entries=entries, counts=counts
        )
