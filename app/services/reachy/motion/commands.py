"""Typed robot movement requests, separate from door/access authority."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RobotMotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    action: Literal[
        "capabilities", "list", "look", "pose", "gesture", "dance", "emotion", "recording", "stop"
    ]
    direction: Literal["left", "right", "up", "down", "forward", "around"] = "forward"
    name: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    duration_seconds: float = Field(default=4.0, ge=0.5, le=20.0, strict=True)
    repeat: int = Field(default=1, ge=1, le=3, strict=True)
    query: str = Field(default="", max_length=120)
    offset: int = Field(default=0, ge=0, le=10000, strict=True)
    x_mm: float | None = Field(default=None, ge=-40, le=40, strict=True)
    y_mm: float | None = Field(default=None, ge=-40, le=40, strict=True)
    z_mm: float | None = Field(default=None, ge=-50, le=30, strict=True)
    roll_degrees: float | None = Field(default=None, ge=-40, le=40, strict=True)
    pitch_degrees: float | None = Field(default=None, ge=-40, le=40, strict=True)
    yaw_degrees: float | None = Field(default=None, ge=-180, le=180, strict=True)
    body_yaw_degrees: float | None = Field(default=None, ge=-160, le=160, strict=True)
    right_antenna_degrees: float | None = Field(default=None, ge=-175, le=175, strict=True)
    left_antenna_degrees: float | None = Field(default=None, ge=-175, le=175, strict=True)
