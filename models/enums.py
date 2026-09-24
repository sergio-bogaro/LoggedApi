import enum


class MediaTypeEnum(str, enum.Enum):
    MOVIES = "movies"
    MANGA = "manga"
    ANIME = "anime"
    GAME = "game"
    BOOK = "book"
    MUSIC = "music"


class MediaStatusEnum(str, enum.Enum):
    IN_PROGRESS = "in_progress"
    DROPPED = "dropped"
    ON_HOLD = "on_hold"
    FOLLOWING = "following"
    FINISHED = "finished"
