"""Shared test fixtures."""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from app.config import Settings
from app.models.domain import FaceMatchResult, FaceQuality
from app.models.enums import FaceQualityFlag


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        app_env="test",
        log_level="INFO",
        db_url=f"sqlite:///{tmp_path / 'test.db'}",
        reachy_simulation=True,
        reachy_enabled=False,
        mlai_backend_url="http://testserver",
        mlai_backend_api_key="test-key",
        allow_dev_endpoints=True,
        dashboard_auto_greeter=False,
        event_greeter_greeting="Welcome to the demo!",
        cooldown_seconds=0,
        access_mode="enforce",  # legacy simulated actuator tests; hardware default remains shadow
    )


@pytest.fixture
def known_face_result() -> FaceMatchResult:
    return FaceMatchResult(
        matched_user_id="user-1",
        similarity=0.91,
        quality=FaceQuality(
            usable=True,
            flags=[FaceQualityFlag.OK],
            face_count=1,
            face_size=180,
            blur_score=140.0,
            brightness=120.0,
        ),
        bbox=(100, 100, 180, 180),
        stable_frames=3,
        embedding=[0.1] * 128,
    )


def encode_test_image(color: str = "#8899aa") -> str:
    image = Image.new("RGB", (128, 128), color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")
