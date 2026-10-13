"""Application configuration."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models.enums import RuntimeMode

BASE_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """Runtime settings loaded from env vars."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: RuntimeMode = RuntimeMode.SIMULATION
    log_level: str = "INFO"
    db_url: str = f"sqlite:///{BASE_DIR / 'data' / 'office_bouncer.db'}"
    data_encryption_key: SecretStr | None = None
    office_timezone: str = "Australia/Melbourne"
    host: str = "127.0.0.1"
    port: int = 8080

    reachy_enabled: bool = False
    reachy_simulation: bool = True
    physical_unlock_enabled: bool = False
    reachy_host: str = "127.0.0.1"
    reachy_port: int = 8000
    reachy_connection_mode: str = "auto"
    reachy_connection_timeout_seconds: float = 8.0
    reachy_media_backend: str = "lite_direct"
    reachy_camera_device_index: int = 1
    simulation_use_webcam: bool = False
    simulation_use_microphone: bool = False

    wakeword_model_path: str = str(BASE_DIR / "data" / "models" / "hey_reachy.onnx")
    wakeword_engine: str = "sherpa_onnx"
    wake_phrase: str = "hey reachy"
    sherpa_kws_model_dir: str = str(BASE_DIR / "data" / "models" / "sherpa-kws")
    sherpa_kws_keywords_path: str = str(BASE_DIR / "app" / "assets" / "hey_reachy.keywords")
    sherpa_kws_threshold: float = 0.25
    sherpa_kws_score: float = 1.5
    wakeword_vosk_fallback_enabled: bool = False
    vosk_model_path: str = str(BASE_DIR / "data" / "models" / "vosk-en")
    tts_engine: str = "simulation"

    realtime_enabled: bool = False
    openai_api_key: SecretStr | None = None
    openai_realtime_url: str = "wss://api.openai.com/v1/realtime"
    openai_realtime_model: str = "gpt-realtime-2.1"
    openai_realtime_voice: str = "marin"
    openai_realtime_reasoning_effort: str = "low"
    realtime_idle_timeout_seconds: float = 45.0
    realtime_max_session_seconds: float = 900.0
    realtime_output_queue_chunks: int = 64
    realtime_tool_limit: int = 20
    realtime_safety_identifier_key: SecretStr | None = None
    engagement_silence_seconds: float = 20.0
    consent_timeout_seconds: float = 8.0
    realtime_input_queue_chunks: int = 64
    realtime_open_timeout_seconds: float = 10.0
    realtime_vad_threshold: float = 0.8
    realtime_vad_prefix_padding_ms: int = 300
    realtime_vad_silence_duration_ms: int = 600
    realtime_initial_output_settle_ms: int = 800
    device_id: str = "reachy-lobby"

    mlai_backend_url: str = "http://localhost:8091"
    mlai_backend_api_key: str = "local-dev-key"
    mlai_public_knowledge_token: SecretStr | None = None
    mlai_public_knowledge_timeout_seconds: float = 3.0
    mlai_organization_domain: str = ""

    slack_signing_secret: str = ""
    slack_bot_token: str = ""
    slack_app_token: str = ""

    salto_client_id: str = ""
    salto_client_secret: str = ""
    salto_username: str = ""
    salto_password: str = ""
    salto_auth_base_url: str = "https://identity.eu.my-clay.com"
    salto_api_base_url: str = ""
    salto_redirect_uri: str = "http://localhost:8080/api/admin/salto/oauth/callback"
    salto_site_id: str = ""
    salto_lock_id: str = ""

    access_mode: str = "shadow"
    identification_mode: str = "request_scoped"
    attended_demo: bool = False
    attended_demo_expires_at: float = 0.0
    face_unknown_threshold: float = 0.30
    face_match_margin: float = 0.08
    face_match_threshold: float = 0.48
    wakeword_threshold: float = 0.5
    vad_threshold: float = 0.5
    vad_model_path: str = str(BASE_DIR / "data" / "models" / "silero_vad.onnx")
    booking_grace_minutes: int = 0
    engagement_timeout_seconds: int = 8
    cooldown_seconds: int = 4
    unlock_idempotency_seconds: int = 20
    face_identification_timeout_seconds: float = 5.0
    booking_lookup_timeout_seconds: float = 5.0
    door_action_timeout_seconds: float = 2.0
    face_stability_frames: int = 3
    min_face_size: int = 152
    min_blur_score: float = 60.0
    min_brightness: float = 60.0
    max_brightness: float = 200.0
    allow_dev_endpoints: bool = False
    health_freshness_seconds: int = 30
    health_probe_interval_seconds: float = 10.0

    wakeword_chunk_ms: int = 80
    sample_rate_hz: int = 16_000
    audio_processing_enabled: bool = True
    audio_aec_stream_delay_ms: int = 10
    audio_noise_suppression_level: int = 3
    microphone_device: str | None = None
    reachy_input_channel: str = "mean"
    reachy_camera_name: str = "Reachy Mini Camera"
    reachy_camera_unique_id: str | None = None
    camera_calibration_path: str | None = None
    xvf_profile: str = "factory"
    doa_bearing_validated: bool = False
    av_gate_mode: str = "demo"
    mouth_landmarker_path: str | None = None
    mouth_landmarker_sha256: str | None = None
    gtcrn_model_path: str | None = None
    local_barge_in_enabled: bool = False
    av_zero_tail_enabled: bool = False
    dither_validated: bool = False
    frontdesk_url: str = "http://127.0.0.1:8092"
    frontdesk_token: SecretStr | None = None
    frontdesk_transport: str = "http"
    frontdesk_poll_seconds: float = 3.0
    frontdesk_wait_seconds: float = 120.0
    routing_enabled: bool = True
    door_stage: int = 0
    door_kill_switch_path: str = str(BASE_DIR / "data" / "DOOR_DISABLED")
    kiosk_heartbeat_path: str = str(BASE_DIR / "data" / "heartbeat.json")
    kek_endpoint: str | None = None
    kiosk_device_credential: SecretStr | None = None
    data_key_envelope_path: str | None = None
    camera_device_index: int = 0
    face_detector_model_path: str = str(BASE_DIR / "data" / "models" / "face_detection_yunet.onnx")
    face_embedder_model_path: str = str(BASE_DIR / "data" / "models" / "face_recognition_sface.onnx")

    data_dir: Path = Field(default_factory=lambda: BASE_DIR / "data")
    static_dir: Path = Field(default_factory=lambda: BASE_DIR / "app" / "static")

    @property
    def is_sqlite(self) -> bool:
        return self.db_url.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        return self.app_env == RuntimeMode.PRODUCTION

    @property
    def dev_endpoints_enabled(self) -> bool:
        return self.allow_dev_endpoints and not self.is_production


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings."""

    return Settings()
