"""Deterministic routing, durable reservations and privacy-preserving results."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from uuid import NAMESPACE_URL, uuid5

from .models import RouteRequest, RoutingContext, StaffReason, VisitorIntent

UNAVAILABLE = {"status": "unavailable", "next_step": "see_staff", "say_hint": "Please see the front desk."}
NOTIFIED = {
    "status": "notified",
    "next_step": "wait_here",
    "say_hint": "I've let the front desk know. Please wait here.",
}
SAY_STATUS = {
    "pending": "Please wait here while the front desk checks.",
    "host_coming": "Someone is on their way down to meet you.",
    "declined": "Please see the front desk for help.",
    "expired": "Please see the front desk for help.",
    "approved_entry": "Please wait for a staff member to meet you.",
}


class VisitorRouter:
    def __init__(self, skill, *, record=None, poll_seconds=3, wait_seconds=120):
        self.skill = skill
        self.record = record
        self.poll_seconds = poll_seconds
        self.wait_seconds = wait_seconds
        self._lock = asyncio.Lock()
        self._results = {}
        self._refs = {}
        self._session_counts = defaultdict(int)
        self._hourly = {"route": deque(), "staff": deque()}

    def _reserve_limit(self, session, kind):
        queue = self._hourly[kind]
        now = time.monotonic()
        while queue and queue[0] < now - 3600:
            queue.popleft()
        per_session, per_hour = (2, 20) if kind == "route" else (1, 6)
        if self._session_counts[(session, kind)] >= per_session or len(queue) >= per_hour:
            return False
        self._session_counts[(session, kind)] += 1
        queue.append(now)
        return True

    async def route(self, request: RouteRequest, context: RoutingContext, call_id: str, *, kind="route"):
        key = f"{context.session_ref}:{call_id}"
        async with self._lock:
            if key in self._results:
                return dict(self._results[key])
            if not self._reserve_limit(context.session_ref, kind):
                return dict(UNAVAILABLE)
            ref = str(uuid5(NAMESPACE_URL, f"{context.device_id}:{key}"))
            row = dict(
                id=ref,
                session_ref=context.session_ref,
                intent=request.intent.value,
                skill_tool="notify_host"
                if kind != "staff"
                and request.intent in {VisitorIntent.GUEST, VisitorIntent.DELIVERY}
                and request.host_name
                else "notify_channel",
                status="RESERVED",
                latency_ms=0,
            )
            try:
                if self.record:
                    reserved = self.record(**row)
                    if reserved is False:
                        return dict(UNAVAILABLE)  # previous process already reserved this call
            except Exception:
                return dict(UNAVAILABLE)
            # Suppress replay even if the remote request times out after being sent.
            self._results[key] = dict(UNAVAILABLE)
            payload = request.model_dump(mode="json", exclude_none=True)
            if request.intent != VisitorIntent.TOUR or not context.contact_consent:
                payload.pop("note", None)
            payload.update(
                device_id=context.device_id,
                session_ref=context.session_ref,
                track_ref=context.track_ref,
                access_event_id=context.access_event_id,
                request_ref=ref,
                skill_tool=row["skill_tool"],
            )
            started = time.monotonic()
            try:
                result = await asyncio.wait_for(self.skill.notify(payload, key), 3)
                if not result or result.get("status") not in SAY_STATUS:
                    return dict(UNAVAILABLE)
                row.update(status=result["status"], latency_ms=int((time.monotonic() - started) * 1000))
                if self.record:
                    self.record(**row)
                self._refs[context.session_ref] = result["request_ref"]
                self._results[key] = dict(NOTIFIED)
                return dict(NOTIFIED)
            except Exception:
                return dict(UNAVAILABLE)

    async def notify_staff(self, reason: StaffReason, context: RoutingContext, call_id: str):
        reason = StaffReason(reason)
        # The reason is an enum; no arbitrary message is sent to Slack.
        request = RouteRequest(intent="member_not_recognised" if reason == StaffReason.MEMBER else "other")
        return await self.route(request, context, call_id, kind="staff")

    async def status(self, session_ref):
        ref = self._refs.get(session_ref)
        if not ref:
            return dict(UNAVAILABLE)
        try:
            result = await asyncio.wait_for(self.skill.status(ref), 3)
            if not result or result.get("status") not in SAY_STATUS:
                return dict(UNAVAILABLE)
            status = result["status"]
            return {"status": status, "say_hint": SAY_STATUS[status], "next_step": "wait_here"}
        except Exception:
            return dict(UNAVAILABLE)

    async def poll(self, session_ref, on_status, on_poll=None):
        deadline = time.monotonic() + self.wait_seconds
        previous = "pending"
        while time.monotonic() < deadline:
            await asyncio.sleep(self.poll_seconds)
            result = await self.status(session_ref)
            if on_poll:
                on_poll()
            status = result["status"]
            if status != previous:
                await on_status(result)
                previous = status
            if status != "pending":
                return result
        result = {"status": "expired", "say_hint": SAY_STATUS["expired"]}
        await on_status(result)
        return result

    def forget_session(self, ref):
        self._refs.pop(ref, None)
        for key in [k for k in self._results if k.startswith(ref + ":")]:
            self._results.pop(key, None)
