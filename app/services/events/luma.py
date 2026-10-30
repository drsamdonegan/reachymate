"""Documented Luma reads. Attendance writes are performed in Luma's signed-in UI."""

from __future__ import annotations

import logging
from datetime import datetime

import httpx


class LumaError(RuntimeError):
    pass


class LumaClient:
    def __init__(self, api_key=None, *, transport=None):
        key = api_key.get_secret_value() if api_key else ""
        self.configured = bool(key)
        self.http = httpx.AsyncClient(
            base_url="https://public-api.luma.com",
            headers={"x-luma-api-key": key},
            timeout=8,
            follow_redirects=False,
            transport=transport,
        )
        # Ticket keys are bearer-like values in the GET query; never log request URLs.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    async def close(self):
        await self.http.aclose()

    async def get(self, path, params):
        if not self.configured:
            raise LumaError("Set LUMA_API_KEY locally to verify attendance with Luma")
        try:
            response = await self.http.get(path, params=params)
            if response.status_code in (401, 403):
                raise LumaError("Luma access denied: check the calendar API key and Luma Plus access")
            if response.status_code == 404:
                raise LumaError("Ticket or event not found in this Luma calendar")
            if response.status_code == 429:
                raise LumaError("Luma rate limit reached; wait before trying again")
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (httpx.HTTPError, ValueError):
            raise LumaError("Luma is unavailable; attendance has not been verified") from None

    async def guest(self, code):
        data = await self.get("/v1/events/guests/get", {"event_id": code.event_id, "id": code.key})
        if not isinstance(data.get("event_tickets"), list) or not isinstance(data.get("id"), str):
            raise LumaError("Unexpected Luma guest response")
        tickets = []
        for ticket in data["event_tickets"]:
            try:
                checked = ticket["checked_in_at"]
                if checked is not None:
                    timestamp = datetime.fromisoformat(checked.replace("Z", "+00:00"))
                    if timestamp.tzinfo is None:
                        raise ValueError()
                if not isinstance(ticket["id"], str) or type(ticket["is_captured"]) is not bool:
                    raise ValueError()
                tickets.append(
                    {
                        "id": ticket["id"],
                        "name": str(ticket.get("name") or "Ticket")[:100],
                        "checked_in_at": checked,
                        "valid": ticket["is_captured"],
                    }
                )
            except (KeyError, TypeError, ValueError, AttributeError):
                raise LumaError("Unexpected Luma ticket response") from None
        return {
            "guest_id": data["id"],
            "name": str(data.get("user_name") or "Guest")[:100],
            "approved": data.get("approval_status") == "approved",
            "tickets": tickets,
        }

    async def event(self, event_id):
        data = await self.get("/v1/events/get", {"event_id": event_id})
        event = data
        if not isinstance(event, dict) or event.get("id") != event_id:
            raise LumaError("Unexpected Luma event response")
        if event.get("access") != "manage":
            raise LumaError("This key cannot manage check-in for that event")
        if event.get("location_type") != "offline":
            raise LumaError("Select an in-person Luma event for ticket check-in")
        return {"id": event_id, "name": str(event.get("name") or "Luma event")[:150]}
