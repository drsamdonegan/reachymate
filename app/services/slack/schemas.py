"""Schemas for mlai-backend booking calls."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class CoworkingBooking(BaseModel):
    id: str
    date: date
    status: str
    points_cost: int | None = None


class BookingHelpRequest(BaseModel):
    slack_user_id: str
    reason_code: str
    access_event_id: str
