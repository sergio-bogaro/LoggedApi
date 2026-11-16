"""Regras de mapeamento entre categorias do Suwayomi e o Logged."""

from models.enums import MediaStatusEnum

ACTION_IGNORE = "ignore"
ACTION_BACKLOG = "backlog"
ACTION_STATUS = "status"

# Ações aceitas pela API (as de status usam o próprio valor do enum).
VALID_ACTIONS = {
    ACTION_IGNORE,
    ACTION_BACKLOG,
    MediaStatusEnum.IN_PROGRESS.value,
    MediaStatusEnum.FOLLOWING.value,
    MediaStatusEnum.ON_HOLD.value,
    MediaStatusEnum.FINISHED.value,
    MediaStatusEnum.DROPPED.value,
}

_STATUS_BY_VALUE = {status.value: status for status in MediaStatusEnum}

# Sugestões usadas ao conectar: o usuário pode trocar tudo na tela.
_DEFAULT_ACTIONS_BY_NAME = {
    "reading": MediaStatusEnum.IN_PROGRESS.value,
    "lendo": MediaStatusEnum.IN_PROGRESS.value,
    "plan to read": ACTION_BACKLOG,
    "planejo ler": ACTION_BACKLOG,
    "quero ler": ACTION_BACKLOG,
    "on hold": MediaStatusEnum.ON_HOLD.value,
    "em espera": MediaStatusEnum.ON_HOLD.value,
    "completed": MediaStatusEnum.FINISHED.value,
    "complete": MediaStatusEnum.FINISHED.value,
    "concluido": MediaStatusEnum.FINISHED.value,
    "concluído": MediaStatusEnum.FINISHED.value,
    "dropped": MediaStatusEnum.DROPPED.value,
    "abandonado": MediaStatusEnum.DROPPED.value,
    "waiting chapters": MediaStatusEnum.FOLLOWING.value,
    "waiting": MediaStatusEnum.FOLLOWING.value,
    "aguardando capitulos": MediaStatusEnum.FOLLOWING.value,
    "aguardando capítulos": MediaStatusEnum.FOLLOWING.value,
}


def suggest_action(name: str, is_default: bool = False) -> str:
    """Sugere uma ação a partir do nome da categoria."""
    if is_default:
        return ACTION_IGNORE
    return _DEFAULT_ACTIONS_BY_NAME.get((name or "").strip().lower(), ACTION_IGNORE)


def is_valid_action(action: str) -> bool:
    return action in VALID_ACTIONS


def action_to_status(action: str) -> MediaStatusEnum | None:
    """Converte uma ação de status para o enum; backlog/ignore viram None."""
    return _STATUS_BY_VALUE.get(action)


def action_from_status(status: MediaStatusEnum | None, add_to_backlog: bool) -> str:
    """Ação equivalente ao que foi aplicado (usada no vínculo e no commit)."""
    if add_to_backlog:
        return ACTION_BACKLOG
    if status is not None:
        return ACTION_STATUS
    return ACTION_IGNORE


def mapping_to_action(mapping) -> str:
    """Ação exposta pela API a partir de uma linha do banco."""
    if mapping is None:
        return ACTION_IGNORE
    if mapping.action == ACTION_STATUS and mapping.status is not None:
        return mapping.status.value
    if mapping.action == ACTION_BACKLOG:
        return ACTION_BACKLOG
    return ACTION_IGNORE


def resolve_action(
    manga_categories: list[dict],
    mappings: dict[int, object],
    category_meta: dict[int, dict],
) -> tuple[str | None, MediaStatusEnum | None]:
    """Resolve a ação efetiva de um mangá.

    Se estiver em várias categorias mapeadas, vale a primeira na ordem do
    Suwayomi (e, em empate, o menor id). Categorias não mapeadas ou "ignore"
    não participam.
    """
    categories = list(manga_categories or [])
    if not categories:
        # Mangás sem categoria são tratados como "Default".
        categories = [{"id": 0, "name": "Default"}]

    candidates: list[tuple[int, int, object]] = []
    for category in categories:
        try:
            category_id = int(category.get("id"))
        except (TypeError, ValueError):
            continue
        mapping = mappings.get(category_id)
        if mapping is None or mapping.action == ACTION_IGNORE:
            continue
        meta = category_meta.get(category_id) or {}
        order = meta.get("order")
        if order is None:
            order = mapping.category_order or 0
        candidates.append((int(order), category_id, mapping))

    if not candidates:
        return None, None

    candidates.sort(key=lambda item: (item[0], item[1]))
    mapping = candidates[0][2]
    if mapping.action == ACTION_BACKLOG:
        return ACTION_BACKLOG, None
    if mapping.action == ACTION_STATUS and mapping.status is not None:
        return ACTION_STATUS, mapping.status
    return None, None
