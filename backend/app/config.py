from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    notes_dir: Path = Path("../notes")
    openrouter_api_key: str = ""
    openrouter_model: str = "deepseek/deepseek-v4-flash"
    embeddings_enabled: bool = True
    colbert_model: str = "lightonai/Agent-ModernColBERT"
    reindex_interval_s: float = 5.0
    vision_enabled: bool = True  # OCR (fast); makes image text searchable
    # VLM captioning is ~3 min/image on CPU and low value — off by default, enable on GPU.
    vlm_caption_enabled: bool = False
    vlm_model: str = "HuggingFaceTB/SmolVLM-256M-Instruct"
    frontend_origin: str = "http://localhost:5173"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
