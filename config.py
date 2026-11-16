import json
from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:///./logged.db"
    upload_dir: str = "./uploads"
    # Arquivos temporários de importação. Ficam fora do diretório servido
    # estaticamente em /uploads (não devem ser acessíveis pelo navegador).
    # Quando vazio, usa uma pasta "imports" ao lado de upload_dir.
    import_dir: str | None = None
    cors_origins: str = '["http://localhost:5173","http://localhost:3000"]'

    # IGDB / Twitch OAuth (fallback de instância; o usuário pode ter as próprias)
    igdb_client_id: str | None = None
    igdb_client_secret: str | None = None
    igdb_base_url: str = "https://api.igdb.com/v4"
    igdb_oauth_url: str = "https://id.twitch.tv/oauth2/token"
    igdb_request_timeout: float = 15.0

    # TMDB (fallback de instância; o usuário pode ter a própria chave)
    tmdb_api_key: str | None = None
    tmdb_base_url: str = "https://api.themoviedb.org/3"
    tmdb_request_timeout: float = 15.0

    # Suwayomi-Server (conexão configurada por usuário no app)
    suwayomi_request_timeout: float = 15.0

    # Worker de importações em background. Desligado em testes.
    import_job_worker_enabled: bool = True

    @property
    def cors_origins_list(self) -> list[str]:
        return json.loads(self.cors_origins)

    @property
    def upload_path(self) -> Path:
        path = Path(self.upload_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def import_path(self) -> Path:
        path = (
            Path(self.import_dir)
            if self.import_dir
            else self.upload_path.parent / "imports"
        )
        path.mkdir(parents=True, exist_ok=True)
        return path

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
