import json
from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:///./logged.db"
    upload_dir: str = "./uploads"
    cors_origins: str = '["http://localhost:5173","http://localhost:3000"]'

    # IGDB / Twitch OAuth
    igdb_client_id: str | None = None
    igdb_client_secret: str | None = None
    igdb_base_url: str = "https://api.igdb.com/v4"
    igdb_oauth_url: str = "https://id.twitch.tv/oauth2/token"
    igdb_request_timeout: float = 15.0

    @property
    def cors_origins_list(self) -> list[str]:
        return json.loads(self.cors_origins)

    @property
    def upload_path(self) -> Path:
        path = Path(self.upload_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
