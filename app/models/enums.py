"""Enums used throughout the app."""

from __future__ import annotations

from enum import Enum


class RuntimeMode(str, Enum):
    TEST = "test"
    SIMULATION = "simulation"
    STAGING = "staging"
    PRODUCTION = "production"


class AppState(str, Enum):
    BOOT = "BOOT"
    IDLE = "IDLE"
    LISTENING_LOW_POWER = "LISTENING_LOW_POWER"
    DORMANT = "LISTENING_LOW_POWER"
    WAKING = "WAKING"
    CONSENT_PROMPT = "CONSENT_PROMPT"
    AWAITING_HOST = "AWAITING_HOST"
    SAFE_HOLD = "SAFE_HOLD"
    ENGAGED = "ENGAGED"
    IDENTIFYING_FACE = "IDENTIFYING_FACE"
    CHECKING_BOOKING = "CHECKING_BOOKING"
    UNLOCKING = "UNLOCKING"
    DENIED = "DENIED"
    ERROR = "ERROR"
    COOLDOWN = "COOLDOWN"


class Decision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    ERROR = "ERROR"


class ReasonCode(str, Enum):
    ALLOW_ACTIVE_BOOKING = "ALLOW_ACTIVE_BOOKING"
    DENY_NO_FACE = "DENY_NO_FACE"
    DENY_MULTIPLE_FACES = "DENY_MULTIPLE_FACES"
    DENY_FACE_TOO_SMALL = "DENY_FACE_TOO_SMALL"
    DENY_LOW_FACE_QUALITY = "DENY_LOW_FACE_QUALITY"
    DENY_UNKNOWN_FACE = "DENY_UNKNOWN_FACE"
    DENY_LOW_MATCH_CONFIDENCE = "DENY_LOW_MATCH_CONFIDENCE"
    DENY_USER_INACTIVE = "DENY_USER_INACTIVE"
    DENY_CONSENT_REQUIRED = "DENY_CONSENT_REQUIRED"
    DENY_ACCESS_REVOKED = "DENY_ACCESS_REVOKED"
    DENY_NO_ACTIVE_BOOKING = "DENY_NO_ACTIVE_BOOKING"
    DENY_BOOKING_LOOKUP_FAILED = "DENY_BOOKING_LOOKUP_FAILED"
    DENY_DOOR_ACTION_ERROR = "DENY_DOOR_ACTION_ERROR"
    # Retained only for historical audit rows created before the placeholder boundary.
    DENY_SALTO_ERROR = "DENY_SALTO_ERROR"
    ERROR_INTERNAL = "ERROR_INTERNAL"
    ERROR_ACCESS_NOT_READY = "ERROR_ACCESS_NOT_READY"
    SHADOW_WOULD_ALLOW = "SHADOW_WOULD_ALLOW"


class BookingStatus(str, Enum):
    ACTIVE = "ACTIVE"
    NONE = "NONE"
    LOOKUP_FAILED = "LOOKUP_FAILED"
    USER_NOT_LINKED = "USER_NOT_LINKED"


class HealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"


class CommandIntent(str, Enum):
    OPEN_DOOR = "open door"
    HELP = "help"
    BOOK_NOW = "book now"
    REPEAT = "repeat"
    CANCEL = "cancel"
    CONSENT_CONFIRM = "check me in"
    CONSENT_DECLINE = "no thanks"


class FaceQualityFlag(str, Enum):
    NO_FACE = "no_face"
    MULTIPLE_FACES = "multiple_faces"
    FACE_TOO_SMALL = "face_too_small"
    LOW_BRIGHTNESS = "low_brightness"
    HIGH_BRIGHTNESS = "high_brightness"
    POSE = "pose"
    BLURRY = "blurry"
    UNSTABLE = "unstable"
    OK = "ok"
