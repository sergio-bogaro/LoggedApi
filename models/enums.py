import enum


class MediaTypeEnum(str, enum.Enum):
    MOVIES = "movies"
    MANGA = "manga"
    ANIME = "anime"
    GAME = "game"
    BOOK = "book"


class MediaStatusEnum(str, enum.Enum):
    BACKLOG = "backlog"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    DROPPED = "dropped"
    ON_HOLD = "on_hold"
