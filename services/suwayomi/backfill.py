"""Aplica ignores antigos da importação aos vínculos do Suwayomi.

Antes de o campo `ignored` existir, marcar um item como ignorado na revisão da
importação só o excluía do commit. A sincronização seguinte tratava o título
como novo e criava um vínculo pendente. Este script faz o backfill uma única
vez, sem tocar em vínculos que já têm mídia.

Uso:
    python -m services.suwayomi.backfill
"""

import json

from sqlalchemy import select

from database import Base, SessionLocal, engine
from models.import_job import ImportJob, ImportJobItem
from models.suwayomi import SuwayomiMangaLink


def _suwayomi_id(item: ImportJobItem) -> int | None:
    raw = (json.loads(item.external_refs_json or "{}") or {}).get("suwayomiId")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def backfill(db) -> tuple[int, int]:
    """Devolve (vínculos criados, vínculos marcados como ignorados)."""
    rows = db.execute(
        select(ImportJobItem, ImportJob.user_id)
        .join(ImportJob, ImportJob.id == ImportJobItem.job_id)
        .where(
            ImportJobItem.source == "suwayomi",
            ImportJobItem.ignored.is_(True),
            ImportJobItem.match_status != "already_in_library",
        )
    ).all()

    created = ignored = 0
    for item, user_id in rows:
        suwayomi_id = _suwayomi_id(item)
        if suwayomi_id is None:
            continue

        link = db.execute(
            select(SuwayomiMangaLink).where(
                SuwayomiMangaLink.user_id == user_id,
                SuwayomiMangaLink.suwayomi_manga_id == suwayomi_id,
            )
        ).scalar_one_or_none()

        if link is None:
            link = SuwayomiMangaLink(
                user_id=user_id,
                suwayomi_manga_id=suwayomi_id,
                title=item.title,
                ignored=True,
            )
            db.add(link)
            created += 1
            ignored += 1
            continue

        # Só marca vínculos que a sync criou e que ainda estão sem mídia. Se o
        # usuário restaurou manualmente, `match_error` já foi limpo e o script
        # não desfaz a escolha.
        if link.media_id is None and link.match_error is not None:
            link.ignored = True
            link.match_error = None
            link.last_match_attempt_at = None
            link.match_attempts = 0
            ignored += 1

    db.commit()
    return created, ignored


def main() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        created, ignored = backfill(db)
    finally:
        db.close()
    print(f"Vínculos criados: {created}")
    print(f"Vínculos marcados como ignorados: {ignored}")


if __name__ == "__main__":
    main()
