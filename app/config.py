"""Application configuration."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """Runtime settings loaded from env vars."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"
    db_url: str = f"sqlite:///{BASE_DIR / 'data' / 'office_bouncer.db'}"
    office_timezone: str = "Australia/Melbourne"
    host: str = "0.0.0.0"
    port: int = 8080

    reachy_enabled: bool = False
    reachy_simulation: bool = True
    simulation_use_webcam: bool = False
    simulation_use_microphone: bool = False

    wakeword_model_path: str = str(BASE_DIR / "data" / "models" / "hey_reachy.onnx")
    vosk_model_path: str = str(BASE_DIR / "data" / "models" / "vosk-en")
    tts_engine: str = "simulation"

    mlai_backend_url: str = "http://localhost:8091"
    mlai_backend_api_key: str = "local-dev-key"

    slack_signing_secret: str = ""
    slack_bot_token: str = ""
    slack_app_token: str = ""

    salto_client_id: str = ""
    salto_client_secret: str = ""
    salto_username: str = ""
    salto_password: str = ""
    salto_auth_base_url: str = "https://api.saltoks.com"
    salto_api_base_url: str = "https://api.saltoks.com"
    salto_redirect_uri: str = "http://localhost:8080/api/admin/salto/oauth/callback"
    salto_site_id: str = ""
    salto_lock_id: str = ""

    face_match_threshold: float = 0.36
    wakeword_threshold: float = 0.5
    vad_threshold: float = 0.5
    booking_grace_minutes: int = 0
    engagement_timeout_seconds: int = 8
    cooldown_seconds: int = 4
    unlock_idempotency_seconds: int = 20
    face_stability_frames: int = 3
    min_face_size: int = 96
    min_blur_score: float = 80.0
    min_brightness: float = 40.0
    allow_dev_endpoints: bool = True

    wakeword_chunk_ms: int = 80
    sample_rate_hz: int = 16_000
    microphone_device: str | None = None
    camera_device_index: int = 0

    data_dir: Path = Field(default_factory=lambda: BASE_DIR / "data")
    static_dir: Path = Field(default_factory=lambda: BASE_DIR / "app" / "static")

    @property
    def is_sqlite(self) -> bool:
        return self.db_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings."""

    return Settings()
