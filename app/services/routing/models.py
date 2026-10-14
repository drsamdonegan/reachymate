"""Validated model input and trusted controller context."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class VisitorIntent(str, Enum):
    GUEST = "guest_of_member"
    DELIVERY = "delivery"
    TOUR = "tour_or_membership"
    EVENT = "event_attendee"
    MEMBER_UNKNOWN = "member_not_recognised"
    OTHER = "other"


class StaffReason(str, Enum):
    HELP = "visitor_needs_help"
    ACCESSIBILITY = "accessibility_assistance"
    MEMBER = "member_not_recognised"
    SAFETY = "safety_or_distress"
    ROBOT = "robot_problem"


class RouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: VisitorIntent
    visitor_first_name: str | None = Field(default=None, max_length=40)
    host_name: str | None = Field(default=None, max_length=60)
    company: str | None = Field(default=None, max_length=60)
    event_name: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=200)

    @field_validator("visitor_first_name", "host_name", "company", "event_name")
    @classmethod
    def safe_name(cls, value):
        if value is None:
            return value
        if not value.strip() or not all(c.isalpha() or c in " '-" for c in value):
            raise ValueError("Names may contain letters, spaces, apostrophes and hyphens only")
        return value.strip()

    @field_validator("note")
    @classmethod
    def safe_note(cls, value):
        if value is None:
            return value
        value = re.sub(
            r"[a-z][a-z0-9+.-]*://\S+|(?:mailto|data|javascript):\S+|www\.\S+|"
            r"\S*@\S+|\b\S+\.(?:com|net|org|test|io)\S*",
            "",
            value,
            flags=re.IGNORECASE,
        )
        return "".join(c for c in value if c.isprintable()).strip()


@dataclass(frozen=True)
class RoutingContext:
    device_id: str
    session_ref: str
    track_ref: str
    access_event_id: str | None = None
    contact_consent: bool = False
