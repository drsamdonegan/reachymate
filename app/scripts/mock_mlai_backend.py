"""Mock mlai-backend coworking API."""

from __future__ import annotations

from datetime import date

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Mock mlai-backend")
BOOKINGS: dict[str, list[dict[str, str]]] = {
    "UDEMO": [{"id": "booking-demo", "date": date.today().isoformat(), "status": "booked"}],
}
HELP_REQUESTS: list[dict[str, str]] = []


@app.get("/healthz/ready")
async def ready():
    return {"ready": True}


@app.get("/api/v1/points/coworking/my-bookings/")
async def my_bookings(slack_user_id: str):
    return BOOKINGS.get(slack_user_id, [])


class BookingHelpRequest(BaseModel):
    slack_user_id: str
    reason_code: str
    access_event_id: str


@app.post("/api/v1/points/coworking/booking-help/")
async def booking_help(payload: BookingHelpRequest):
    HELP_REQUESTS.append(payload.model_dump())
    return {"queued": True}


@app.post("/seed/{slack_user_id}")
async def seed(slack_user_id: str):
    BOOKINGS[slack_user_id] = [
        {"id": f"booking-{slack_user_id}", "date": date.today().isoformat(), "status": "booked"}
    ]
    return {"seeded": True}
