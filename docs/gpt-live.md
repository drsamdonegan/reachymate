# GPT-Live voice transport

The USB test launcher selects OpenAI GPT-Live, using `gpt-live-1` for full-duplex speech and `gpt-6-luna` for Responses delegation. Local wake detection opens a concierge session, where the existing face/audio gate admits visitor speech and closed gates supply exact silence. No microphone audio is sent before wake or an explicit dashboard start. Acoustic echo cancellation uses Reachy's actual speaker output as its reference.

The staging `--demo` launcher also enables **Start chat** on the existing dashboard. This explicit attended mode streams the echo-cancelled Reachy microphone continuously until **Stop chat**, cancellation or ten minutes. It tolerates camera loss and head movements without muting input or ending the session. The dashboard discloses the live microphone and remaining time. Normal wake-word sessions retain speaker gating and departure/silence timeouts. Attended mode is rejected outside the staging, shadow-only GPT-Live profile; local access/consent grammar remains face-gated in both modes.

Double-click `Start Reachy.command` for the complete local stack: it starts/reuses the no-media daemon and loopback mocks, launches `--demo --auto-chat`, connects voice and opens the same dashboard. In this opt-in profile, reopening the dashboard starts one attended session if stopped. Stop remains effective until another page opening or explicit Start; polling never continuously restarts a stopped session. Closing the tab does not end chat. The ordinary manual `--demo` command does not enable auto chat. `DASHBOARD_AUTO_CHAT` requires attended chat and therefore the staging, shadow-only GPT-Live constraints.

## Configuration

```dotenv
REALTIME_ENABLED=true
VOICE_API=gpt_live
TTS_ENGINE=live
OPENAI_LIVE_URL=wss://api.openai.com/v1/live/sessions
OPENAI_LIVE_MODEL=gpt-live-1
OPENAI_LIVE_BACKEND_MODEL=gpt-6-luna
LIVE_CLOSE_TIMEOUT_SECONDS=5
LIVE_MAX_OUTPUT_BUFFER_SECONDS=1.0
LIVE_PLAYBACK_RESERVE_SECONDS=0.2
ATTENDED_CHAT_ENABLED=false
ATTENDED_CHAT_MAX_SECONDS=600
CAMERA_VISION_ENABLED=false
```

Keep `OPENAI_API_KEY` and `REALTIME_SAFETY_IDENTIFIER_KEY` in local secrets. The existing `REALTIME_*` enable switch, voice, safety identifier, queue and timeout settings are retained for compatibility. `VOICE_API=realtime` explicitly selects the previous transport; GPT-Live failures do not replay speech or switch to Realtime.

## Audio and tool flow

The connection sends `session.start`, waits for `session.started`, and continuously sends 16 kHz mono PCM16 in `session.input_audio.append`. Playback consumes `session.output_audio.delta`, with a 200 ms speech-onset reserve for network jitter and a one-second backlog limit. Silence inside buffered speech preserves timeline pauses; idle silence does not keep the speaker busy or extend concierge engagements. Input stays enabled during speech and the initial greeting. GPT-Live handles interruption and backchannels; this transport does not commit VAD turns, cancel Realtime responses or truncate conversation items.

The voice model delegates task work to Responses. The backend receives the seven concierge functions and one typed `robot_motion` function. Completed function items arrive inside `response.event`; the client executes them through the bounded local registry, returns `response.item.create` results, and sends `response.create` to continue. Backend work does not block audio reception. Late results are discarded after a session closes. No model tool identifies a person or controls a door.

`robot_motion` exposes capability discovery, looks, all head/body/antenna pose coordinates, gestures, installed dances/emotions and stop. Motions replace an existing performance, run on the single SDK motion thread, and obey local reachability and speed limits. Parking or capture freeze cancels them; model calls cannot enable motor torque. Stop remains available after the ordinary tool budget is exhausted. Provision the pinned Apache-2.0 Pollen recordings with `python -m app.scripts.provision_movements`; playback does not download data or play sidecar sounds.

The fixed biometric notice uses a local recording. Its consent window mutes cloud input and discards model output; server mute does not itself silence the model. Normal audio resumes after the local controller completes this flow and the server acknowledges unmute. Controller announcements use Live commentary; an acknowledgment confirms acceptance, not that a visitor heard the announcement.

## Camera questions

The staging `--demo` launcher enables `CAMERA_VISION_ENABLED` for attended chat. Ask **What can you see?**, **What am I holding?** or **What color is this?** The voice model delegates to a ninth typed tool, `inspect_camera`, which reads the existing Reachy camera owner and prepares a fresh JPEG in memory. It waits briefly for a requested movement to finish, limits the image to 1280 pixels and 512 KiB, and accepts no file paths or external image URLs. Capture is unavailable outside active attended chat, during local consent, or with a non-Reachy camera.

The application returns all pending function results, queues the image as a Responses `input_image` using `response.item.create`, then continues the backend with `response.create`. `gpt-6-luna` interprets the image and GPT-Live speaks its findings. The audio frontend receives no direct images. This follows [OpenAI's visual context guidance](https://developers.openai.com/api/docs/guides/live-delegation#add-images-and-visual-context).

Snapshots go to OpenAI only on visual requests; there is no continuous video upload or local image archive. Ordinary microphone streaming continues while capture and image analysis run. Failed, late or consent-suspended captures do not transmit images or terminate voice. The tool shares the session's bounded call budget and allows at most one capture attempt per two seconds. Pixels are untrusted input, never identity evidence or access authority.

The dashboard separately shows the number and age of model snapshots and the backend's latest camera observation. This one bounded observation is kept in memory only and cleared on stop; telemetry records counts and dimensions, not pixels, questions or observation text. The local preview alone does not indicate model vision is connected.

During Luma event hosting, the native camera remains closed. Open the dashboard in the same Chrome
profile as Luma, start the host, then click **Enable camera sharing**. Chrome opens a verified Reachy
camera track in the dashboard alongside its scanner tab. For each `inspect_camera` request, the app
issues a short-lived request ID; the dashboard supplies one fresh bounded JPEG to the local app,
which passes it through the same Responses image path. No periodic images are uploaded. The microphone
continues streaming during this work. Camera permission or sharing failures leave voice and Luma alone.

Sharing uses a per-tab in-memory token, same-origin checks and a 15-second lease. Wrong devices,
unsolicited/oversized frames, expired request IDs and late uploads after Stop are rejected. Frames are
decoded and re-encoded without metadata off the audio loop. Closing/reloading the dashboard, camera
disconnect, stopping the host, app restart or **Stop camera sharing** ends sharing. Chrome may briefly
open its selected camera to obtain permission when device labels are hidden; a non-Reachy track is
immediately closed before preview or upload. Choose Reachy in Chrome's camera settings if that happens.

Ask **What can you see?** and check that the model image count increases and a fresh observation appears.
Then hold up a different object and ask again. Check Luma's preview and Express Mode remain active.
**Look around for a person** combines bounded movement with a still after it settles; this is not
continuous face tracking, identity matching or access control. Ticket results remain authoritative in Luma.

On shutdown the client immediately clears local playback, discards further model audio, sends `session.close`, waits up to five seconds for `session.closed` and final cumulative usage, then releases playback and the socket. Attended mode pauses only the ordinary idle watchdog; its ten-minute deadline and the transport's maximum session age still apply. A connection failure closes the engagement through the existing local fallback and bounded circuit breaker. Start chat can retry a failed cloud session immediately.

## Dashboard and verification

`/api/admin/diagnostics/audio` exposes `voice` (also aliased as `realtime` for existing clients):

- `api`, `model`, `backend_model`, `full_duplex` and current connection/input state.
- Input/output frame counts and `simultaneous_input_output_frames`, which counts admitted input while hardware playback is active.
- Bounded queue/drop counts, sanitized error codes, final usage and `finalization_confirmed`.
- `playback_buffer_ms`, `last_stop_reason`, and `conversation.mode`/`remaining_seconds` distinguish buffering, an intentional timeout and a cloud failure.

Transcript telemetry contains character counts and timing only. Diagnostics verify transport overlap, while an attended interruption test verifies what the person hears. Follow [the functionality checklist](testing.md); measured speaker isolation, enrollment, anti-spoofing and live front-desk delivery remain separate acceptance gates.

Dashboard reconnections replay only the recent events that fit the subscriber queue. Disconnected clients release their subscription even during shutdown, and the browser retains at most 200 displayed events.

Protocol references: [Live WebSockets](https://developers.openai.com/api/docs/guides/voice-websockets?api=live), [Responses delegation and tools](https://developers.openai.com/api/docs/guides/live-delegation?delegation-mode=responses), [playback controls](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live#control-playback-when-needed) and [migration](https://developers.openai.com/api/docs/guides/live-migration).
