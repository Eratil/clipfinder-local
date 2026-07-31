from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    clipfinder_data_dir: Path = Path("data")
    whisper_model: str = "large-v3"
    whisper_device: str = "cuda"
    whisper_compute_type: str = "float16"
    # Short, focused candidates are much easier to review.  Longer pieces are
    # still created when one spoken sentence or a clear punchline needs them.
    segment_min_seconds: int = 10
    segment_max_seconds: int = 36
    worker_max_concurrency: int = 1
    update_repository: str = "Eratil/clipfinder-local"

    @property
    def db_path(self) -> Path:
        return self.clipfinder_data_dir / "clipfinder.sqlite3"

    @property
    def incoming_dir(self) -> Path:
        return self.clipfinder_data_dir / "incoming"

    @property
    def exports_dir(self) -> Path:
        return self.clipfinder_data_dir / "exports"

    @property
    def previews_dir(self) -> Path:
        return self.clipfinder_data_dir / "previews"

    @property
    def work_dir(self) -> Path:
        return self.clipfinder_data_dir / "work"

    def ensure_directories(self) -> None:
        for directory in (self.clipfinder_data_dir, self.incoming_dir, self.exports_dir, self.previews_dir, self.work_dir):
            directory.mkdir(parents=True, exist_ok=True)


settings = Settings()
