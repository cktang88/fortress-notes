"""Settings, read once per process from the environment and ``backend/.env``.

Everything else asks :func:`get_settings` for the one active :class:`Settings`
(scripts switch them with :func:`activate`, tests patch them). Paths are resolved once,
when the settings are made: a relative ``NOTES_DIR`` (or converter path) is
relative to the backend directory, never to the current directory, so the
backend finds the same notes however it is started.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_FILENAME = ".fortress.sqlite3"
# The ColBERT model and the exact commit of its Hugging Face repository we run: an
# update to the repo can't change the weights, tokenizer or config under us. To move
# to a newer commit, see docs/dependencies.md (the store re-indexes on its own).
COLBERT_MODEL = "lightonai/Agent-ModernColBERT"
COLBERT_REVISION = "10b5ec41b805bc1b64f6b2ee3d97c2b800f679ca"  # 2026-08-17
EMBEDDINGS_FILENAME = ".fortress-embeddings.sqlite3"


class Settings(BaseSettings):
    # Environment variables win over backend/.env (whatever the current directory).
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env", extra="ignore", populate_by_name=True
    )

    notes_dir: Path = Path("../notes")
    openrouter_api_key: str = ""
    # Ask needs answers in well under 3 s. Measured end to end (scripts/bench.py, 18
    # questions, streamed): Mercury 3 p50 0.90 s / p95 1.08 s; Gemini 2.5 Flash Lite
    # 1.03 / 1.74 s; GPT-4.1 nano, Claude Haiku 5.5, Gemini 3.1 Flash Lite 1.3-1.5 s
    # p50; same answers found. (GLM 5.3 Flash always reasons and takes 3-4 s.)
    ask_model: str = "anthropic/claude-haiku-5.5"
    # OpenRouter reasoning effort for ASK_MODEL ("none", "minimal", "low"...; empty
    # leaves the model's default). Reasoning models need "none" to stay fast.
    ask_reasoning_effort: str = "none"
    embeddings_enabled: bool = True
    colbert_model: str = COLBERT_MODEL
    # A commit hash (or tag) of COLBERT_MODEL's repository. Empty: COLBERT_REVISION for
    # the default model; for another model the repository's newest commit, unpinned.
    colbert_revision: str = ""
    # "auto": on Apple Silicon, ColBERT encodes on the GPU with MLX when it is
    # installed (app/colbert_mlx.py), elsewhere on the CPU; any MLX error moves back to
    # the CPU for good. "mlx": the same, with a warning when MLX is unavailable.
    # "cpu": never the GPU.
    embeddings_device: str = "auto"
    # Threads torch may use for one encode. 0 = half the cores, at most 4. Measured on
    # 4 cores (query encode / 16-paragraph batch): 1 thread 141 ms / 2.2 s, 2 threads
    # 77 ms / 1.3 s, 4 threads 69 ms / 1.0 s; with two other busy processes (OCR,
    # requests) 2 threads stay at 78 ms / 1.3 s but 4 threads go to p90 983 ms / 4.4 s.
    embeddings_threads: int = 0
    # Also precompute each paragraph's query encoding (after indexing, in the
    # background) so "related to this paragraph" skips the model entirely.
    precompute_query_embeddings: bool = True
    reindex_interval_s: float = 5.0
    # Read the text in pictures and PDFs (app/ocr.py: local, background, cached by
    # content) so it is searchable. OCR_THREADS 0 = half the cores, at most 4.
    vision_enabled: bool = True
    ocr_threads: int = 0
    # A new workspace (no database yet) starts with the Welcome note (app/welcome.md).
    welcome_note: bool = True
    # Keep a daily SQLite snapshot in NOTES_DIR/.fortress-backups (last 7 kept).
    auto_backup_enabled: bool = True
    # The OneNote converter (tools/fortress-onenote): an explicit binary, else it is
    # looked up (app/onenote_import.converter_path). Seconds before a run is stopped.
    onenote_bin: str = Field("", validation_alias="FORTRESS_ONENOTE_BIN")
    onenote_timeout_s: float = Field(3600.0, validation_alias="FORTRESS_ONENOTE_TIMEOUT_S")
    # The OneNote download from Microsoft (tools/fortress-onenote-graph, run by
    # app/onenote_graph.py): the tool's folder; empty = the one in this repository.
    onenote_graph_dir: str = Field("", validation_alias="FORTRESS_ONENOTE_GRAPH_DIR")

    def model_post_init(self, _context: object) -> None:
        notes = self.notes_dir.expanduser()
        if not notes.is_absolute():
            notes = BACKEND_DIR / notes
        self.notes_dir = notes.resolve()
        if self.onenote_bin.strip():
            binary = Path(self.onenote_bin.strip()).expanduser()
            self.onenote_bin = str(binary if binary.is_absolute() else BACKEND_DIR / binary)
        if self.onenote_graph_dir.strip():
            tool = Path(self.onenote_graph_dir.strip()).expanduser()
            self.onenote_graph_dir = str(tool if tool.is_absolute() else BACKEND_DIR / tool)

    @property
    def colbert_model_revision(self) -> str | None:
        """The revision to load COLBERT_MODEL at: None for a local folder (or an
        unpinned other model), which Hugging Face then takes as it is."""

        if self.colbert_revision.strip():
            return self.colbert_revision.strip()
        if self.colbert_model == COLBERT_MODEL:
            return COLBERT_REVISION
        return None

    # Derived paths (fixed once notes_dir is resolved).
    @property
    def notes_path(self) -> Path:
        return self.notes_dir

    @property
    def assets_path(self) -> Path:
        return self.notes_dir / "assets"

    @property
    def block_db_path(self) -> Path:
        return self.notes_dir / DB_FILENAME

    @property
    def embeddings_path(self) -> Path:
        return self.notes_dir / EMBEDDINGS_FILENAME

    def create_directories(self) -> None:
        self.assets_path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def default_threads(configured: int) -> int:
        """``configured``, or (for 0) half the cores, at most 4."""

        return configured if configured > 0 else max(1, min(4, (os.cpu_count() or 2) // 2))


_active: Settings | None = None


def get_settings() -> Settings:
    """The process's settings (read from the environment on first use)."""

    global _active
    if _active is None:
        settings = Settings()
        settings.create_directories()
        _active = settings
    return _active


def activate(settings: Settings | None) -> None:
    """Make ``settings`` the active ones (scripts); None reads them again on next use."""

    global _active
    if settings is not None:
        settings.create_directories()
    _active = settings
