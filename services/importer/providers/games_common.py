"""Helpers compartilhados pelos provedores de jogos (Grouvee, Infinite Backlog…)."""

from models.enums import MediaStatusEnum

# Status/rótulos de tracker de jogos → status do Logged (None = backlog).
GAME_STATUS_MAP: dict[str, MediaStatusEnum | None] = {
    "completed": MediaStatusEnum.FINISHED,
    "complete": MediaStatusEnum.FINISHED,
    "finished": MediaStatusEnum.FINISHED,
    "played": MediaStatusEnum.FINISHED,
    "beaten": MediaStatusEnum.FINISHED,
    "playing": MediaStatusEnum.IN_PROGRESS,
    "in progress": MediaStatusEnum.IN_PROGRESS,
    "in-progress": MediaStatusEnum.IN_PROGRESS,
    "current": MediaStatusEnum.IN_PROGRESS,
    "started": MediaStatusEnum.IN_PROGRESS,
    "backlog": None,
    "wishlist": None,
    "want to play": None,
    "plan to play": None,
    "to play": None,
    "on hold": MediaStatusEnum.ON_HOLD,
    "onhold": MediaStatusEnum.ON_HOLD,
    "on-hold": MediaStatusEnum.ON_HOLD,
    "paused": MediaStatusEnum.ON_HOLD,
    "shelved": MediaStatusEnum.ON_HOLD,
    "dropped": MediaStatusEnum.DROPPED,
    "abandoned": MediaStatusEnum.DROPPED,
    "retired": MediaStatusEnum.DROPPED,
}

BACKLOG_STATUSES = {
    "backlog",
    "wishlist",
    "want to play",
    "plan to play",
    "to play",
}


def map_game_status(value: str | None) -> MediaStatusEnum | None:
    return GAME_STATUS_MAP.get((value or "").strip().lower())


def is_backlog_status(value: str | None) -> bool:
    return (value or "").strip().lower() in BACKLOG_STATUSES
