from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    notes_dir: Path = Path("../notes")
    openrouter_api_key: str = ""
    openrouter_model: str = "z-ai/glm-5.3-flash"
    embeddings_enabled: bool = True
    colbert_model: str = "lightonai/Agent-ModernColBERT"
    reindex_interval_s: float = 5.0
    vision_enabled: bool = True  # OCR (fast); makes image text searchable
    # Captions and OCR run locally in the background and reuse cached image text.
    vlm_caption_enabled: bool = True
    vlm_model: str = "HuggingFaceTB/SmolVLM-256M-Instruct"
    frontend_origin: str = "http://localhost:5173"
    block_db_enabled: bool = True
    block_db_import_on_startup: bool = True

    @property
    def notes_path(self) -> Path:
        p = self.notes_dir.expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def assets_path(self) -> Path:
        p = self.notes_path / "assets"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def block_db_path(self) -> Path:
        return self.notes_path / ".fortress.sqlite3"


@lru_cache
def get_settings() -> Settings:
    return Settings()
