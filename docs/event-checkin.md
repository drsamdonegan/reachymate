# Luma event check-in

Use Reachy's existing USB camera and dashboard to read a guest's Luma ticket. Recording attendance uses
Luma's signed-in check-in page. The dashboard can then verify each ticket's `checked_in_at` timestamp
through Luma's documented API. A scan or approved registration alone never means someone is checked in.

## Setup

1. Install the existing `vision` extra (`uv sync --frozen --extra vision --extra audio --extra reachy --extra skills --extra dev`). QR decoding uses OpenCV; no separate scanner hardware is needed.
2. Set `LUMA_EVENT_ID=evt-...` in the private `.env`, or select the event ID in the dashboard. Find it in the event's permanent/manage URL, or through Luma's entity lookup API. The public event slug and event ID differ. Dashboard selections last for the current app run; `.env` supplies the startup default.
3. Sign in to Luma in the browser with event management or check-in access.
4. To enable attendance verification, obtain a calendar-scoped Luma API key through Luma's calendar settings and store it as `LUMA_API_KEY` in `.env`. API access requires Luma Plus. Restart the Reachy app after changing `.env`; reopening an already-running launcher reuses the old process. Keep the key out of source control, chat, screenshots and logs.
5. Open the existing Reachy launcher/dashboard. Confirm **Reachy's View** is the USB Reachy camera. The scanner itself does not start a cloud voice session or move motors. The launcher's separate auto-chat setting still applies when opening the dashboard. Stop chat if you only want local scanning.

## At the event

1. Confirm the selected event ID. Click **Start QR scanner** and show one phone ticket to Reachy. Adjust distance/brightness until its full QR fits in the camera view. Keep the robot still while scanning.
2. Review the result. Codes for other events, unrelated URLs and malformed keys are rejected. Duplicate frames are suppressed. Guest keys can represent several tickets, so all returned tickets are shown individually.
3. Click **Complete check-in in Luma**. Review the guest and appropriate tickets on Luma's page, then confirm there. This is the step that actually records attendance. Luma's own scan-mode settings still apply.
4. Return to Reachy and click **Verify Luma check-in**. A configured API key allows a fresh lookup; only Luma's returned timestamps produce **Checked in**. A partial group remains marked partial. If Reachy is already in voice chat, a newly observed check-in gets a fixed welcome; an already checked-in ticket does not get another welcome.
5. Click **Next guest** before replacing an unresolved ticket/group. Click **Stop QR scanner** when finished. Scanning expires after 30 minutes, independently of the voice session's ten-minute limit.

Without an API key, decoding and the official Luma link still work. Complete and verify attendance on
Luma's page; Reachy's dashboard deliberately remains **unverified**. It never offers a local “mark checked
in” override, changes approval status, or queues attendance writes while offline.

## Test checklist

Use synthetic data for automated checks. For a live check-in, use a consenting attendee who is present,
or an organiser-controlled test registration; do not test on an absent real guest.

| Check | Expected result |
| --- | --- |
| Local QR decoding | Show a Luma ticket to the USB camera; its official check-in link appears. Pasting the decoded URL is also available for troubleshooting. |
| Wrong event / random QR | Rejected without following the URL or contacting a different host. |
| Repeat the same QR | No duplicate greeting or attendance write; pending tickets stay selected. |
| Unapproved / invalid ticket | With the key configured, rejected and referred to staff. Without a key, Luma must validate it. |
| Confirm in Luma, then Verify | Actual ticket timestamps appear; Luma's own guest list shows the same attendance. |
| Multi-ticket guest | Individual statuses stay visible. Partial attendance never becomes a whole-group confirmation. |
| API unavailable / no key | Explicit unverified/error state; attendance can still be handled in Luma directly. |
| Voice and camera ownership | Chat continues while scanning; model camera requests are unavailable. Stop chat keeps the explicitly started scanner active. Stop scanning releases an otherwise idle camera. |
| Privacy | No ticket keys or guest names in telemetry. Next guest, expiry and shutdown clear the current ticket. |

Run the automated decoder, API contract, cancellation and dashboard endpoint checks with:

```sh
uv run pytest app/tests/unit/test_event_checkin.py app/tests/unit/test_model_vision.py -q
```

## Boundaries and API evidence

Checked against Luma's published documentation and OpenAPI schema on 2026-10-30:

- [External check-in integration](https://help.luma.com/p/external-check-in-integration) documents both guest and ticket QR keys and `GET /v1/events/guests/get?event_id=...&id=...`.
- [Get Guest](https://docs.luma.com/reference/get_v1-events-guests-get) returns individual ticket attendance; [OpenAPI](https://public-api.luma.com/openapi.json) exposes reads but no attendance-write operation.
- [Official check-in workflow](https://help.luma.com/p/check-in) records attendance using Standard or Express mode. The dashboard hands off to that supported UI, rather than reverse-engineering a session-cookie endpoint.
- [API setup](https://docs.luma.com/reference/getting-started-with-your-api) describes calendar API access. Registration approval is a different operation and must not be used as a check-in substitute.

Fully unattended scan-to-check-in remains dependent on a supported Luma attendance-write integration.
Do not claim the current handoff is automatic. Reachy's cloud model has no ticket, guest-list or attendance
write tool, and event check-in has no connection to the door controller.

Ticket URLs are bearer-like secrets. The runtime retains only the current lookup in memory (five-minute
expiry), sends it only to the fixed Luma API origin or the operator-opened Luma page, and uses no-store
responses and no-referrer links. It does not retain frames or guest lists. Luma's browser tab/history can
retain its URL, so use an operator-controlled browser and close ticket tabs after use. While scanning,
model image uploads are blocked, including images waiting for the socket. Normal attended image requests
resume after scanning stops.
