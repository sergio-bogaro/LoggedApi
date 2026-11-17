import json
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from database import get_db
from models.enums import MediaTypeEnum
from models.media import Media
from models.suwayomi import SuwayomiAccount, SuwayomiCategoryMapping, SuwayomiMangaLink
from schemas.suwayomi import (
    SuwayomiCategoriesResponse,
    SuwayomiCategoriesUpdate,
    SuwayomiCategory,
    SuwayomiConfigPayload,
    SuwayomiConfigResponse,
    SuwayomiLink,
    SuwayomiLinkUpdate,
    SuwayomiSyncResponse,
    SuwayomiTestPayload,
    SuwayomiTestResponse,
)
from services.suwayomi.categories import (
    ACTION_BACKLOG,
    ACTION_IGNORE,
    ACTION_STATUS,
    action_to_status,
    is_valid_action,
    mapping_to_action,
    suggest_action,
)
from services.suwayomi.client import SuwayomiClient, normalize_base_url
from services.suwayomi.sync import service as sync_service, utcnow

router = APIRouter(prefix="/api/suwayomi", tags=["Suwayomi"])


def _get_account(db: Session, user_id: int) -> SuwayomiAccount | None:
    return db.execute(
        select(SuwayomiAccount).where(SuwayomiAccount.user_id == user_id)
    ).scalar_one_or_none()


def _decode_summary(account: SuwayomiAccount) -> dict | None:
    if not account.last_sync_summary:
        return None
    try:
        value = json.loads(account.last_sync_summary)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _config_response(account: SuwayomiAccount | None) -> SuwayomiConfigResponse:
    if account is None:
        return SuwayomiConfigResponse()
    return SuwayomiConfigResponse(
        configured=True,
        base_url=account.base_url,
        username=account.username,
        has_password=bool(account.password),
        enabled=account.enabled,
        updates_per_day=account.updates_per_day,
        last_sync_at=account.last_sync_at,
        last_sync_status=account.last_sync_status,
        last_sync_summary=_decode_summary(account),
        next_sync_at=account.next_sync_at,
    )


def _interval(updates_per_day: int) -> timedelta:
    per_day = min(max(int(updates_per_day or 1), 1), 24)
    return timedelta(hours=24 / per_day)


def _account_client(account: SuwayomiAccount) -> SuwayomiClient:
    return SuwayomiClient(
        account.base_url, username=account.username, password=account.password
    )


def _count_by_category(library: list[dict]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for manga in library:
        nodes = (manga.get("categories") or {}).get("nodes") or []
        if not nodes:
            counts[0] = counts.get(0, 0) + 1
            continue
        for category in nodes:
            try:
                category_id = int(category.get("id"))
            except (TypeError, ValueError):
                continue
            counts[category_id] = counts.get(category_id, 0) + 1
    return counts


def _mapping_rows(db: Session, user_id: int) -> dict[int, SuwayomiCategoryMapping]:
    rows = (
        db.execute(
            select(SuwayomiCategoryMapping).where(
                SuwayomiCategoryMapping.user_id == user_id
            )
        )
        .scalars()
        .all()
    )
    return {row.category_id: row for row in rows}


def _save_mapping(
    db: Session,
    user_id: int,
    category_id: int,
    category_name: str,
    action: str,
    category_order: int = 0,
) -> SuwayomiCategoryMapping:
    if not is_valid_action(action):
        raise ValueError(f"Ação inválida para a categoria '{category_name}': {action}.")

    row = db.execute(
        select(SuwayomiCategoryMapping).where(
            SuwayomiCategoryMapping.user_id == user_id,
            SuwayomiCategoryMapping.category_id == category_id,
        )
    ).scalar_one_or_none()
    if row is None:
        row = SuwayomiCategoryMapping(
            user_id=user_id, category_id=category_id, category_name=category_name
        )
        db.add(row)

    row.category_name = category_name
    if category_order:
        row.category_order = category_order

    if action == ACTION_IGNORE:
        row.action = ACTION_IGNORE
        row.status = None
    elif action == ACTION_BACKLOG:
        row.action = ACTION_BACKLOG
        row.status = None
    else:
        row.action = ACTION_STATUS
        row.status = action_to_status(action)

    return row


def _ensure_suggested_mappings(
    db: Session, user_id: int, categories: list[dict]
) -> None:
    existing = _mapping_rows(db, user_id)
    for category in categories:
        category_id = int(category["id"])
        if category_id in existing:
            continue
        _save_mapping(
            db,
            user_id,
            category_id,
            category.get("name") or "",
            suggest_action(category.get("name") or "", bool(category.get("default"))),
            int(category.get("order") or 0),
        )


def _build_categories_response(
    db: Session,
    user_id: int,
    categories: list[dict],
    counts: dict[int, int] | None,
    stale: bool = False,
    error: str | None = None,
) -> SuwayomiCategoriesResponse:
    mappings = _mapping_rows(db, user_id)
    result: list[SuwayomiCategory] = []
    seen: set[int] = set()

    for category in categories:
        category_id = int(category["id"])
        seen.add(category_id)
        mapping = mappings.get(category_id)
        result.append(
            SuwayomiCategory(
                id=category_id,
                name=category.get("name") or (mapping.category_name if mapping else ""),
                order=int(category.get("order") or 0),
                default=bool(category.get("default")),
                manga_count=None if counts is None else counts.get(category_id, 0),
                action=(
                    mapping_to_action(mapping)
                    if mapping is not None
                    else suggest_action(
                        category.get("name") or "", bool(category.get("default"))
                    )
                ),
                mapped=mapping is not None,
            )
        )

    # Categorias mapeadas que não vieram do servidor (cache/excluídas).
    for category_id, mapping in mappings.items():
        if category_id in seen:
            continue
        result.append(
            SuwayomiCategory(
                id=category_id,
                name=mapping.category_name,
                order=mapping.category_order or 0,
                default=category_id == 0,
                manga_count=None,
                action=mapping_to_action(mapping),
                mapped=True,
            )
        )

    if 0 not in seen and counts is not None and counts.get(0):
        result.append(
            SuwayomiCategory(
                id=0,
                name="Default",
                order=0,
                default=True,
                manga_count=counts.get(0, 0),
                action="ignore",
                mapped=False,
            )
        )

    result.sort(key=lambda item: (item.order, item.id))
    return SuwayomiCategoriesResponse(categories=result, stale=stale, error=error)


def _link_response(link: SuwayomiMangaLink) -> SuwayomiLink:
    return SuwayomiLink(
        id=link.id,
        suwayomi_manga_id=link.suwayomi_manga_id,
        media_id=link.media_id,
        title=link.title,
        ignored=link.ignored,
        match_error=link.match_error,
        match_attempts=link.match_attempts,
        last_match_attempt_at=link.last_match_attempt_at,
        last_seen_at=link.last_seen_at,
    )


@router.get("/links", response_model=list[SuwayomiLink])
def list_links(
    user_id: int,
    status: Literal["pending", "ignored", "all"] = "pending",
    db: Session = Depends(get_db),
):
    """Lista vínculos que precisam de revisão ou que foram ignorados."""
    query = select(SuwayomiMangaLink).where(SuwayomiMangaLink.user_id == user_id)
    if status == "pending":
        query = query.where(
            SuwayomiMangaLink.media_id.is_(None),
            SuwayomiMangaLink.ignored.is_(False),
        )
    elif status == "ignored":
        query = query.where(SuwayomiMangaLink.ignored.is_(True))
    query = query.order_by(SuwayomiMangaLink.title, SuwayomiMangaLink.id)
    return [_link_response(link) for link in db.execute(query).scalars().all()]


@router.patch("/links/{link_id}", response_model=SuwayomiLink)
def update_link(
    link_id: int,
    data: SuwayomiLinkUpdate,
    user_id: int,
    db: Session = Depends(get_db),
):
    """Vincula o título a uma mídia existente ou ignora/restaura o vínculo."""
    link = db.get(SuwayomiMangaLink, link_id)
    if link is None or link.user_id != user_id:
        raise HTTPException(status_code=404, detail="Vínculo do Suwayomi não encontrado.")

    fields = data.model_fields_set
    if "media_id" in fields and data.media_id is not None:
        media = db.get(Media, data.media_id)
        if media is None or media.user_id != user_id:
            raise HTTPException(status_code=404, detail="Mídia não encontrada.")
        if media.type != MediaTypeEnum.MANGA:
            raise ValueError("O Suwayomi só pode ser vinculado a mangás.")
        link.media_id = media.id
        link.ignored = False
        link.match_error = None
        link.last_match_attempt_at = None
        link.match_attempts = 0
        # Força a próxima sincronização a aplicar a categoria atual.
        link.last_action = None
        link.last_status = None

    if "ignored" in fields and data.ignored is not None:
        if data.ignored and link.media_id is not None:
            raise ValueError("Desvincule a mídia antes de ignorar este título.")
        link.ignored = data.ignored
        link.match_error = None
        link.last_match_attempt_at = None
        link.match_attempts = 0

    db.commit()
    db.refresh(link)
    return _link_response(link)


@router.get("/config", response_model=SuwayomiConfigResponse)
def get_config(user_id: int, db: Session = Depends(get_db)):
    """Configuração e último resultado do sync (nunca expõe a senha)."""
    return _config_response(_get_account(db, user_id))


@router.put("/config", response_model=SuwayomiConfigResponse)
async def save_config(data: SuwayomiConfigPayload, db: Session = Depends(get_db)):
    """Cria/atualiza a conexão e, se necessário, sugere o mapeamento de categorias."""
    base_url = normalize_base_url(data.base_url)
    account = _get_account(db, data.user_id)
    url_changed = False

    if account is None:
        account = SuwayomiAccount(
            user_id=data.user_id,
            base_url=base_url,
            enabled=data.enabled,
            updates_per_day=data.updates_per_day,
        )
        db.add(account)
    else:
        if account.base_url != base_url:
            url_changed = True
            # IDs de categoria/mangá e credenciais são específicos do servidor:
            # trocar a URL sem limpar misturaria dados de servidores diferentes.
            account.username = None
            account.password = None
            db.execute(
                delete(SuwayomiMangaLink).where(
                    SuwayomiMangaLink.user_id == data.user_id
                )
            )
            db.execute(
                delete(SuwayomiCategoryMapping).where(
                    SuwayomiCategoryMapping.user_id == data.user_id
                )
            )
        account.base_url = base_url
        account.enabled = data.enabled
        account.updates_per_day = data.updates_per_day

    if data.clear_auth:
        account.username = None
        account.password = None
    elif "username" in data.model_fields_set or "password" in data.model_fields_set:
        username = (data.username or "").strip()
        password = data.password or ""
        if bool(username) != bool(password):
            raise ValueError(
                "Preencha usuário e senha do Suwayomi, ou deixe os dois vazios."
            )
        if username and password:
            account.username = username
            account.password = password

    now = utcnow()
    account.next_sync_at = now + _interval(account.updates_per_day) if account.enabled else None
    db.commit()
    db.refresh(account)

    # Primeira configuração (ou troca de servidor): busca as categorias e deixa
    # sugestões prontas. Se o servidor estiver fora, a config é salva mesmo assim.
    if url_changed or not _mapping_rows(db, data.user_id):
        client = _account_client(account)
        try:
            categories = await client.get_categories()
            _ensure_suggested_mappings(db, data.user_id, categories)
            db.commit()
        except ValueError:
            pass
        finally:
            await client.aclose()

    return _config_response(account)


@router.post("/test", response_model=SuwayomiTestResponse)
async def test_connection(data: SuwayomiTestPayload, db: Session = Depends(get_db)):
    """Testa a conexão com o servidor informado (ou com o já salvo)."""
    account = _get_account(db, data.user_id) if data.user_id is not None else None
    base_url = data.base_url or (account.base_url if account else None)
    if not base_url:
        return SuwayomiTestResponse(ok=False, error="Informe a URL do Suwayomi.")

    username = data.username if data.username is not None else (account.username if account else None)
    password = data.password if data.password is not None else (account.password if account else None)

    client = SuwayomiClient(base_url, username=username, password=password)
    try:
        about = await client.about()
        return SuwayomiTestResponse(
            ok=True,
            server_name=about.get("name") or "Suwayomi",
            version=about.get("version"),
        )
    except ValueError as exc:
        return SuwayomiTestResponse(ok=False, error=str(exc))
    finally:
        await client.aclose()


@router.get("/categories", response_model=SuwayomiCategoriesResponse)
async def list_categories(user_id: int, db: Session = Depends(get_db)):
    """Lista as categorias do servidor com contagem e mapeamento atual."""
    account = _get_account(db, user_id)
    if account is None:
        raise HTTPException(status_code=404, detail="O Suwayomi ainda não foi configurado.")

    client = _account_client(account)
    try:
        categories = await client.get_categories()
        library = await client.get_library()
        counts = _count_by_category(library)

        # Atualiza nome/ordem das categorias já mapeadas.
        mappings = _mapping_rows(db, user_id)
        for category in categories:
            mapping = mappings.get(int(category["id"]))
            if mapping is not None:
                mapping.category_name = category.get("name") or mapping.category_name
                mapping.category_order = int(category.get("order") or 0)
        db.commit()

        return _build_categories_response(db, user_id, categories, counts)
    except ValueError as exc:
        mappings = _mapping_rows(db, user_id)
        cached = [
            {
                "id": mapping.category_id,
                "name": mapping.category_name,
                "order": mapping.category_order,
                "default": mapping.category_id == 0,
            }
            for mapping in mappings.values()
        ]
        return _build_categories_response(
            db, user_id, cached, None, stale=True, error=str(exc)
        )
    finally:
        await client.aclose()


@router.put("/categories", response_model=SuwayomiCategoriesResponse)
async def save_categories(data: SuwayomiCategoriesUpdate, db: Session = Depends(get_db)):
    """Salva o mapeamento de todas as categorias exibidas na tela."""
    account = _get_account(db, data.user_id)
    if account is None:
        raise HTTPException(status_code=404, detail="O Suwayomi ainda não foi configurado.")

    for entry in data.categories:
        _save_mapping(
            db,
            data.user_id,
            entry.category_id,
            entry.category_name,
            entry.action,
            entry.category_order,
        )
    db.commit()

    # Devolve as categorias já salvas (com a ordem que estava no banco). A tela
    # pode recarregar a lista ao vivo depois; aqui basta refletir o que foi salvo.
    rows = _mapping_rows(db, data.user_id)
    categories = [
        {
            "id": row.category_id,
            "name": row.category_name,
            "order": row.category_order,
            "default": row.category_id == 0,
        }
        for row in rows.values()
    ]
    return _build_categories_response(db, data.user_id, categories, None)


@router.post("/sync", response_model=SuwayomiSyncResponse)
async def sync_now(user_id: int, db: Session = Depends(get_db)):
    """Executa o sync imediatamente, mesmo com o automático desligado."""
    account = _get_account(db, user_id)
    if account is None:
        raise HTTPException(status_code=404, detail="O Suwayomi ainda não foi configurado.")

    summary = await run_in_threadpool(sync_service.sync_user, user_id)
    return SuwayomiSyncResponse(
        status=str(summary.get("status") or "ok"), summary=summary
    )
