# Reachy concierge

FastAPI concierge for Reachy Mini Lite. The canonical runtime is `app/`. Local wake detection starts an engagement; a detection-only face tracker and an audio-visual gate select speech for a bounded GPT-Live conversation. `gpt-live-1` listens while speaking; its `gpt-6-luna` Responses backend has seven concierge tools plus a bounded robot movement tool, with no identity or door actuator capability.

Face checks require a fixed local notice followed by the visitor's own locally recognised “check me in”. Enrollment requires recorded voluntary consent and encrypted templates. Routed visitors cannot start a face check during that engagement.

**The active runtime is shadow-only and never composes a physical actuator.** PAD, measured calibration, a door sensor, second factor, compliance evidence and signed approval remain deployment gates. Recognition thresholds are provisional.

## Setup

```sh
uv sync --frozen --python 3.11.14 --extra audio --extra vision --extra reachy --extra skills --extra dev
cp .env.example .env
uv run python -m app.scripts.provision_models
uv run python -m app.scripts.provision_movements
```

Generate a 32-byte URL-safe base64 `DATA_ENCRYPTION_KEY` and a separate random `REALTIME_SAFETY_IDENTIFIER_KEY`; keep them in private local secrets. A real robot run or enrollment requires encryption. Never commit keys, images, audio corpora or database files.

Loopback simulation:

```sh
uv run uvicorn app.main:app --host 127.0.0.1 --port 8080
uv run python -m app.scripts.mock_frontdesk
```

Admin routes are disabled unless `ALLOW_DEV_ENDPOINTS=true` and are never registered in production.

## Open Reachy

Connect USB, turn on external power, then double-click **[Start Reachy.command](Start%20Reachy.command)** in Finder. It starts or reuses the local no-media daemon, booking/front-desk mocks and GPT-Live app, connects chat, and opens the existing dashboard at **http://127.0.0.1:8080/**. Reopening that dashboard starts chat once when stopped. **Stop chat** stays stopped while the page remains open. Closing a browser tab or the launcher Terminal leaves services running; chat still has its ten-minute cap.

During a Luma camera handoff, reopening the dashboard or launcher preserves the current mode. Use **Start GPT-Live host** for voice alongside the scanner. An app restart always leaves host voice off until explicitly started again.

Camera and microphone permissions must be granted to Terminal. This launcher uses the existing `.venv`, `.env` and provisioned assets; it never installs packages or changes system login settings. Startup logs are private under `.codex-runtime-logs/launcher-*.log`. Repeated launches reuse healthy services; conflicting ports, missing power or a failed dependency produce a diagnostic instead of starting duplicate hardware owners.

The same launcher is available from a terminal:

```sh
./"Start Reachy.command"
# Or connect without opening a browser:
.venv/bin/python -m app.scripts.launch_reachy --no-browser
```

For manual service management, run the no-media daemon separately, the loopback mocks, then the app:

```sh
.venv/bin/python -m app.scripts.run_reachy_daemon --no-media --no-wake-up-on-start --goto-sleep-on-stop --fastapi-host 127.0.0.1 --dataset-update-interval 0 --no-preload-datasets
.venv/bin/python -m app.scripts.run_reachy_live_test --demo
```

The demo profile uses loopback front-desk/bookings mocks and forces shadow mode. Without measured camera calibration it uses SDK intrinsics for detection-only conversation, reports degraded attention, and disables face-directed motion. Its bench-only joint-settling allowance is 0.18 rad; normal profiles retain 0.10 rad, and every profile still verifies torque off and the sleep head pose. This demo does not grant biometric consent or approve hardware deployment. Complete camera calibration and direction surveys before live conversation acceptance.

The launcher selects `VOICE_API=gpt_live` and `TTS_ENGINE=live`. Cloud voice still uses the existing `REALTIME_ENABLED` enable switch and `REALTIME_SAFETY_IDENTIFIER_KEY` secret. The previous Realtime transport remains explicitly selectable with `VOICE_API=realtime`; connection failures do not switch APIs automatically. See [GPT-Live transport](docs/gpt-live.md) for configuration, lifecycle and diagnostics.

For uninterrupted bench conversation, use the launcher or click **Start chat**. The staging demo streams Reachy's microphone continuously for up to ten minutes, including while he looks away or dances. **Stop chat** ends it immediately. The manual `--demo` command retains click-to-start behavior; add `--auto-chat` for the launcher's dashboard behavior. Wake-word concierge sessions continue to use the face/audio gate; local biometric consent remains gated in both modes. The dashboard shows the active mode and remaining time.

The staging demo also supports camera questions such as **What can you see?** and **What am I holding?** A fresh still from Reachy's camera goes to the existing vision-capable Responses backend, whose findings GPT-Live speaks. The dashboard shows model snapshot counts and its latest observation. Snapshots are on request, never a continuous video stream; model vision is opt-in and limited to attended chat.

The same dashboard includes look, gesture, dance, expression and numeric pose controls. Voice requests use `robot_motion` for all six head coordinates, body yaw and both antennas, plus 19 pinned Pollen dances and 85 expressions. A single motion thread applies reachability, relative-yaw, duration and speed limits. Recordings reduce amplitude when necessary; numeric pose requests are rejected if unreachable. Stop cancels playback; parking, consent/capture freeze and engagement end cancel manual movements. Recorded motion does not play its sound sidecars over the conversation. On macOS the camera selector matches OpenCV's unique-ID ordering and shows the selected Reachy camera name in the preview.

## Verification

The existing dashboard also includes [Luma event check-in](docs/event-checkin.md). **Open automatic Luma
check-in** pauses Reachy's voice/camera features and opens Luma's official Express scanner using his USB
camera. Select **Reachy Mini Camera** in Chrome's camera settings and confirm **Express Mode On** after opening or reloading the scanner; valid single-ticket
guests then check in without per-person clicks or an API key. Group tickets still need selection. Close
all Luma scanner tabs and return the camera before resuming native camera ownership.
**Start GPT-Live host** enables interruptible greetings, talking motion, looks and dances while Luma scans;
**Enable camera sharing** lets the host answer visual questions through Chrome's Reachy camera stream.
Use the same Chrome profile for both tabs and keep the dashboard open. Images are sent only on request;
the model cannot identify people or verify ticket results. The camera handoff survives restarts, while
camera sharing requires a fresh opt-in. The original assisted local scanner and optional API verification remain available. Use
**Configure Luma.command** for masked local key entry if verification is wanted.

To install the current public Hugging Face community movement sets, run
`uv run python -m app.scripts.provision_movements --community`, then click **Reload installed moves**.
This discovers datasets tagged `reachy_mini_community_moves`, pins each revision and validates the
recorded motion JSON. GPT-Live can search every installed move; the existing dashboard's movement
library includes a **Hugging Face community** selector and name/creator search. Playback keeps motor
limits and never downloads code or plays a recording's sidecar audio. See [movement setup](docs/movements.md).

```sh
uv run pytest app/tests -q
uv run ruff check app
uv build
```

Hardware measurements are separate from unit tests. See [hardware probes](docs/hardware-probes.md), [speaker focus validation](docs/speaker-focus-validation.md), [front-desk contract](docs/contracts/frontdesk-skill.md) and [concierge overview](docs/concierge.md). Optional pilot features remain disabled until their assets and held-out measurements are validated.

Follow the [functionality test guide](docs/testing.md) for the attended bench checklist, automated scenarios, failure drills and remaining physical acceptance gates.

The legacy conversation prototype is retained until physical migration acceptance; it is excluded from the canonical package and test command.
