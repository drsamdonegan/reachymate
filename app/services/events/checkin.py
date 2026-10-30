"""Bounded scan sessions; only provider timestamps can confirm attendance."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from copy import deepcopy
from uuid import uuid4

from app.services.events.luma import LumaError
from app.services.events.qr import EVENT_ID, QRDecoder, parse_ticket


class EventCheckin:
    def __init__(
        self,
        *,
        camera,
        client,
        telemetry,
        camera_name="Camera",
        camera_allowed=lambda: True,
        on_confirmed=None,
        on_scanner_stop=None,
    ):
        self.camera, self.client, self.telemetry = camera, client, telemetry
        self.camera_name = camera_name
        self.camera_allowed, self.on_confirmed = camera_allowed, on_confirmed
        self.on_scanner_stop = on_scanner_stop
        self.decoder = QRDecoder()
        self.scanning = False
        self.event = None
        self.current = None
        self.error = None
        self._code = None
        self._guest_id = None
        self._task = None
        self._lock = asyncio.Lock()
        self._control_lock = asyncio.Lock()
        self._expiry = None
        self._generation = 0
        self._last_lookup = -float("inf")
        self._seen = {}
        self._until = 0.0

    def snapshot(self):
        self._expire()
        return {
            "scanning": self.scanning,
            "camera_source": self.camera_name,
            "event": deepcopy(self.event),
            "current": deepcopy(self.current),
            "error": self.error,
            "verification_available": self.client.configured,
            "write_method": "luma_official_checkin",
            "remaining_seconds": max(0, int(self._until - time.monotonic())) if self.scanning else 0,
        }

    def _expire(self):
        if self.current and time.monotonic() - self.current["seen_at"] > 300:
            self._generation += 1
            self.current = self._code = self._guest_id = None
            self._seen.clear()

    async def select_event(self, event_id):
        if not EVENT_ID.fullmatch(event_id):
            raise ValueError("Use a Luma event ID beginning evt-")
        async with self._lock:
            event = {"id": event_id, "name": "Luma event"}
            if self.client.configured:
                event = await self.client.event(event_id)
            await self.stop_scanning()
            self._generation += 1
            self.current = self._code = self._guest_id = None
            self._seen.clear()
            # Without a key the official signed-in Luma page still handles validation/check-in.
            self.event = event
            self._last_lookup = -float("inf")
            self.error = None
        return self.snapshot()

    async def start_scanning(self):
        async with self._control_lock:
            if not self.event:
                raise ValueError("Select an event first")
            if not self.camera_allowed():
                raise ValueError("Camera scanning is paused during a hardware fault or consent check")
            if not self.scanning:
                if self._task and not self._task.done():
                    await self._task
                self.scanning = True
                self.error = None
                self._until = time.monotonic() + 1800
                self._task = asyncio.create_task(self._scan_loop(), name="event-qr-scanner")
        return self.snapshot()

    async def stop_scanning(self):
        async with self._control_lock:
            self.scanning = False
            self._generation += 1
            task, self._task = self._task, None
            if task and task is not asyncio.current_task():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        return self.snapshot()

    async def close(self):
        try:
            await self.stop_scanning()
        finally:
            if self._expiry:
                self._expiry.cancel()
            self.current = self._code = self._guest_id = None
            self._seen.clear()
            await self.client.close()

    def _read_codes(self):
        frame = self.camera.get_frame()
        if frame is None:
            raise RuntimeError("Camera frame unavailable")
        return self.decoder.decode(frame)

    async def _scan_loop(self):
        try:
            while self.scanning and time.monotonic() < self._until:
                if self.camera_allowed():
                    capture = asyncio.create_task(asyncio.to_thread(self._read_codes))
                    try:
                        codes = await asyncio.shield(capture)
                    except asyncio.CancelledError:
                        # Do not release/reopen the shared camera underneath its worker.
                        await capture
                        raise
                    if not self.scanning:
                        break
                    if len(codes) > 1:
                        self.error = "Show one ticket QR code at a time"
                    elif codes:
                        await self.scan(codes[0])
                await asyncio.sleep(0.4)
        except Exception:
            self.error = "Scanner unavailable; check Reachy's camera and restart scanning"
        finally:
            self.scanning = False
            if self.on_scanner_stop:
                try:
                    await self.on_scanner_stop()
                except Exception:
                    self.error = "Scanner stopped; camera cleanup needs attention"

    async def scan(self, value):
        async with self._lock:
            self._expire()
            if not self.event:
                raise ValueError("Select an event first")
            try:
                code = parse_ticket(value, self.event["id"])
            except ValueError as exc:
                # Do not leave a previous arrival's success beside a new invalid scan.
                if self.current and self.current["status"] in {"checked_in", "not_approved"}:
                    self._generation += 1
                    self.current = self._code = self._guest_id = None
                self.error = str(exc)
                return self.snapshot()
            now = time.monotonic()
            self._seen = {key: seen for key, seen in self._seen.items() if now - seen < 15}
            if code.key in self._seen or now - self._last_lookup < 2:
                return self.snapshot()
            # A pending group/ticket cannot be silently replaced by the next camera frame.
            if self.current and self.current["status"] in {
                "awaiting_luma",
                "unverified",
                "partially_checked_in",
            }:
                return self.snapshot()
            self._seen[code.key] = now
            self._last_lookup = now
            self._code, self._guest_id = code, None
            self.current = {
                "scan_id": str(uuid4()),
                "seen_at": now,
                "status": "unverified",
                "name": "Ticket",
                "tickets": [],
                "checkin_url": code.url,
            }
            if self._expiry:
                self._expiry.cancel()
            self._expiry = asyncio.get_running_loop().call_later(301, self._expire)
            self.error = None
            generation = self._generation
            if self.client.configured:
                await self._refresh(generation)
            if self.current and generation == self._generation:
                self.telemetry.publish("event_ticket_scanned", {"status": self.current["status"]})
            return self.snapshot()

    async def verify(self, scan_id):
        async with self._lock:
            self._expire()
            if not self.current or self.current["scan_id"] != scan_id:
                raise ValueError("This scan expired; scan the ticket again")
            if time.monotonic() - self._last_lookup < 2:
                return self.snapshot()
            self._last_lookup = time.monotonic()
            await self._refresh(self._generation)
            return self.snapshot()

    async def _refresh(self, generation):
        try:
            guest = await self.client.guest(self._code)
        except LumaError as exc:
            self.error = str(exc)
            return
        if generation != self._generation or not self.current:
            return
        if self._guest_id and self._guest_id != guest["guest_id"]:
            self.error = "Luma returned a different guest; scan again"
            return
        self._guest_id = guest["guest_id"]
        previous = {t["id"]: t["checked_in_at"] for t in self.current["tickets"]}
        tickets = guest["tickets"]
        valid = [t for t in tickets if t["valid"]]
        checked = [t for t in valid if t["checked_in_at"]]
        status = "awaiting_luma"
        if not guest["approved"] or not valid:
            status = "not_approved"
        elif len(checked) == len(valid):
            status = "checked_in"
        elif checked:
            status = "partially_checked_in"
        self.current.update(name=guest["name"], tickets=tickets, status=status)
        self.error = None
        # Initial lookup of an old checked-in ticket is not a new arrival.
        newly_checked = any(t["id"] in previous and previous[t["id"]] is None for t in checked)
        if newly_checked and guest["approved"]:
            self.telemetry.publish("event_checkin_confirmed", {"tickets": len(checked)})
            if self.on_confirmed:
                try:
                    await self.on_confirmed()
                except Exception:
                    self.error = "Check-in confirmed; spoken welcome unavailable"

    async def next_guest(self):
        async with self._lock:
            self._generation += 1
            self.current = self._code = self._guest_id = None
            self.error = None
        return self.snapshot()
