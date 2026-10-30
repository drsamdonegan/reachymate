# Luma event check-in

The dashboard offers **automatic single-ticket check-in through Luma Express** and the original assisted
scanner. Both use Reachy's existing USB camera. Luma records attendance; a locally decoded QR or approved
registration alone never means someone is checked in.

## Automatic check-in with Luma Express

1. Open the existing Reachy launcher/dashboard and confirm the event ID.
2. Click **Open automatic Luma check-in**. Reachy finishes any current wake, closes cloud voice, cancels
   face checks and stops local microphone/direction workers before releasing the USB camera and opening
   the official Luma scanner in a new browser tab. USB audio recovery is paused too, so it cannot reset the
   device while Luma is scanning.
   His head wakes to the normal position. Local camera preview, model images and movement requests pause;
   emergency Stop movement still works. If the popup is blocked, allow the dashboard to open a tab and retry.
3. Sign in to Luma with check-in access. In Chrome, open **Settings → Privacy and security → Site settings
   → Camera** and select **Reachy Mini Camera** from the device dropdown. Reload Luma and allow camera access.
   Chrome's selected default camera also applies to other sites. Luma's **Flip Camera** can fail on desktop;
   use the named Chrome device selector if it shows the MacBook or reports a media error. Cover Reachy's
   lens briefly to confirm that the preview darkens. See [Chrome's camera selection instructions](https://support.google.com/chrome/answer/2693767).
4. In **Scanner Options**, choose **Express → Confirm**. Check that the page says **Express Mode On**.
   Check this after opening or reloading a scanner: both can revert to Standard mode even in the same browser.
5. Hold one guest's QR code in front of Reachy. Luma automatically records a valid single-ticket arrival
   and displays the result. There is no per-person dashboard click and no API key is needed. Multiple-ticket
   guests still require selecting tickets in Luma; invalid/unapproved tickets go to staff.
6. When finished, close **every** Luma scanner tab, then click **Luma scanner is closed — return camera to
   Reachy** in the dashboard. Click **Start chat** if wanted. Returning the camera does not open a cloud
   conversation automatically.

The dashboard reports that it released the camera; it cannot inspect Luma's cross-origin page to certify
camera permission, device choice, Express Mode or a guest's check-in. Results are authoritative in Luma.
The handoff stays in place until you explicitly return the camera, including across dashboard reloads,
launcher reuse and app restarts. Closing the dashboard, stopping Reachy, or a robot fault cannot close
Luma's independent browser scanner. Close it yourself when stopping admission. A private marker at
`data/luma-browser-handoff.json` preserves that boundary; don't remove it while Luma still uses the camera.

If setup fails partway through, chat stays paused. Retry **Prepare and open Luma again**, or close any
scanner tabs and return the camera. A failed camera handoff never opens the scanner automatically.

If USB is unplugged, Chrome's old stream can stop even after reconnection. Reconnect USB and external
power, restart the Reachy app if it entered a hardware fault, then prepare the handoff again. Reload Luma,
confirm the Reachy camera and re-enable Express Mode. While the handoff is active, hardware-fault recovery
does not restart audio or reset USB underneath Chrome's scanner.

## GPT-Live host alongside Express

After preparing the handoff, enter a welcome in the existing dashboard and click **Start GPT-Live host**.
Reachy greets guests and listens through his USB microphone while Luma keeps the camera. Use **Say welcome
again**, ask him aloud to welcome the next guest, or send a request through **Ask Roo (text)**. A greeting
is an invitation to scan, never a confirmation that a ticket was accepted. No Luma API key is needed.

Ask “look around”, “look left”, “dance”, “wiggle your antennas”, or “stop moving”. The model can use the
full existing motion controller, including installed Hugging Face community recordings, with the same
joint, speed and duration limits as ordinary chat. It searches the catalogue for named moves instead of
guessing identifiers. Reachy's head also moves with his spoken audio, including after a requested move
finishes. Stop movement holds still until the next movement or chat start.

For visual questions, open this dashboard in the same Chrome profile as Luma and click **Enable camera
sharing** after starting the host. Allow camera access and verify the preview shows Reachy's view.
Chrome shares the camera between the dashboard and scanner; the native app keeps its camera closed.
Ask "What can you see?" or "What am I holding?" The dashboard supplies one fresh image only when the
model requests it. The model image count and latest observation confirm that pixels reached the backend.
No image files are saved locally. If Chrome cannot share the device, the dashboard reports the error;
voice and Luma remain independent. A MacBook camera is never accepted for preview or upload.

"Look for a person" can combine looking around and an image after movement settles. This does not
identify anyone, continuously track faces, read ticket credentials, verify attendance or operate a door.
Luma's preview moves with the head; let Reachy settle before scanning. Keep this dashboard open while
sharing. Closing/reloading it, disconnecting USB, stopping the host or clicking **Stop camera sharing**
closes its stream. Luma's separate stream stays open. Re-enable sharing after restarting the host.

The host streams microphone audio to OpenAI even without a face in view, including while speaking.
**Stop host voice** or **Stop chat** stops both the cloud session and local audio without releasing
Luma's camera. The attended chat limit applies (ten minutes by default). Closing the dashboard does not
stop voice. Reloading it or reopening the launcher preserves the current mode; restarting the app leaves
host voice off. A cloud disconnect or audio failure also stops the host, and USB recovery is blocked
while Luma owns the camera. Resolve the audio issue and explicitly restart the host to retry.

Welcome edits apply on the next host start and last for the current app run. Set `LUMA_HOST_GREETING`
in the private `.env` for a persistent default, then restart the app. Keep real event configuration private.

## Optional assisted scanner setup

1. Install the existing `vision` extra (`uv sync --frozen --extra vision --extra audio --extra reachy --extra skills --extra dev`). QR decoding uses OpenCV; no separate scanner hardware is needed.
2. Set `LUMA_EVENT_ID=evt-...` in the private `.env`, or select the event ID in the dashboard. Find it in the event's permanent/manage URL, or through Luma's entity lookup API. The public event slug and event ID differ. Dashboard selections last for the current app run; `.env` supplies the startup default.
3. Sign in to Luma in the browser with event management or check-in access.
4. To enable attendance verification, obtain a calendar-scoped Luma API key through Luma's calendar settings, then double-click **Configure Luma.command**. Its local Terminal prompt hides the key, verifies access to the selected event with a read-only API call, and saves it in `.env` with owner-only permissions. The equivalent command is `.venv/bin/python -m app.scripts.configure_luma`. API access requires Luma Plus. Restart the Reachy app after changing `.env`; reopening an already-running launcher reuses the old process. Keep the key out of source control, chat, screenshots and logs. The key does not add an attendance-write operation to Luma's API.
5. Open the existing Reachy launcher/dashboard. Confirm **Reachy's View** is the USB Reachy camera. The scanner itself does not start a cloud voice session or move motors. The launcher's separate auto-chat setting still applies when opening the dashboard. Stop chat if you only want local scanning.

## Assisted check-in at the event

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
| Express handoff | Start chat, then open automatic check-in. Voice closes, camera preview pauses, and the Luma scanner opens after the device is released. |
| Browser setup | Confirm Reachy Mini Camera physically and **Express Mode On**. Neither is inferred from the dashboard's handoff flag. |
| Automatic single ticket | Present an organiser-controlled ticket or a consenting attendee who is present. Luma records the check-in without a confirmation click; its Checked In list/count updates. |
| Reload and restart | Reload the dashboard or reopen the launcher: the current host mode is preserved. Restart the app: host voice stays off and local camera capture stays blocked. |
| Host voice | Start GPT-Live host, hear the welcome, then interrupt it. Input/output overlap increases in voice diagnostics and Luma still shows Express Mode with a live preview. |
| Shared model vision | Enable camera sharing in the same Chrome profile. Ask about two different objects; each request produces a new image count and matching observation while Luma keeps scanning. Stop sharing and verify further visual requests report it unavailable. |
| Motion | Ask for a look-around, dance and named community move. Motion runs, and speech animation resumes afterwards; no camera image is sent to the model. Stop movement holds still. |
| Stop and faults | Stop host voice, expire its time limit, or disconnect cloud voice. Microphone capture ends, the handoff remains, and no automatic voice/USB restart occurs. |
| Return to chat | Close all Luma scanner tabs and return the camera. Start chat explicitly; confirm normal camera and voice work again. |
| Local QR decoding | Show a Luma ticket to the USB camera; its official check-in link appears. Pasting the decoded URL is also available for troubleshooting. |
| Wrong event / random QR | Rejected without following the URL or contacting a different host. |
| Repeat the same QR | No duplicate greeting or attendance write; pending tickets stay selected. |
| Unapproved / invalid ticket | With the key configured, rejected and referred to staff. Without a key, Luma must validate it. |
| Confirm in Luma, then Verify | Actual ticket timestamps appear; Luma's own guest list shows the same attendance. |
| Multi-ticket guest | Individual statuses stay visible. Partial attendance never becomes a whole-group confirmation. |
| API unavailable / no key | Explicit unverified/error state; attendance can still be handled in Luma directly. |
| Assisted voice and camera ownership | Chat continues while local assisted scanning; model camera requests are unavailable. Stop chat keeps the explicitly started scanner active. Stop scanning releases an otherwise idle camera. Express requires explicit host voice opt-in. |
| Privacy | No ticket keys or guest names in telemetry. Next guest, expiry and shutdown clear the current ticket. |

Run the automated decoder, API contract, cancellation and dashboard endpoint checks with:

```sh
uv run pytest app/tests/unit/test_event_checkin.py app/tests/unit/test_luma_express.py app/tests/unit/test_luma_host.py app/tests/unit/test_configure_luma.py app/tests/unit/test_model_vision.py -q
```

## Boundaries and API evidence

Checked against Luma's published documentation and OpenAPI schema on 2026-10-30:

- [External check-in integration](https://help.luma.com/p/external-check-in-integration) documents both guest and ticket QR keys and `GET /v1/events/guests/get?event_id=...&id=...`.
- [Get Guest](https://docs.luma.com/reference/get_v1-events-guests-get) returns individual ticket attendance; [OpenAPI](https://public-api.luma.com/openapi.json) exposes reads but no attendance-write operation.
- [Official check-in workflow](https://help.luma.com/p/check-in) records attendance using Standard or Express mode. The dashboard hands off to that supported UI, rather than reverse-engineering a session-cookie endpoint.
- [API setup](https://docs.luma.com/reference/getting-started-with-your-api) describes calendar API access. Registration approval is a different operation and must not be used as a check-in substitute.

Automatic single-ticket admission is performed by Luma's Express scanner. The custom local decoder still
has no direct attendance-write API; its assisted mode still needs confirmation in Luma. Group-ticket
selection remains an operator action in either workflow. Reachy's cloud model has no ticket, guest-list
or attendance write tool, and event check-in has no connection to the door controller.

In assisted mode, ticket URLs are bearer-like secrets. The runtime retains only the current lookup in memory (five-minute
expiry), sends it only to the fixed Luma API origin or the operator-opened Luma page, and uses no-store
responses and no-referrer links. It does not retain frames or guest lists. Luma's browser tab/history can
retain its URL, so use an operator-controlled browser and close ticket tabs after use. While scanning,
model image uploads are blocked, including images waiting for the socket. Normal attended image requests
resume after scanning stops.

In Express Mode, Luma's browser page handles tickets under its own policies. The optional dashboard
camera-sharing stream supplies requested stills to OpenAI through the local app, without recording or
archiving them. It does not read Luma's page, guest records or check-in results.
