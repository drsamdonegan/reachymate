# GPT-Live voice transport

The USB test launcher selects OpenAI GPT-Live, using `gpt-live-1` for full-duplex speech and `gpt-6-luna` for Responses delegation. Local wake detection opens a concierge session, where the existing face/audio gate admits visitor speech and closed gates supply exact silence. No microphone audio is sent before wake or an explicit dashboard start. Acoustic echo cancellation uses Reachy's actual speaker output as its reference.

The staging `--demo` launcher also enables **Start chat** on the existing dashboard. This explicit attended mode streams the echo-cancelled Reachy microphone continuously until **Stop chat**, cancellation or ten minutes. It tolerates camera loss and head movements without muting input or ending the session. The dashboard discloses the live microphone and remaining time. Normal wake-word sessions retain speaker gating and departure/silence timeouts. Attended mode is rejected outside the staging, shadow-only GPT-Live profile; local access/consent grammar remains face-gated in both modes.

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
```

Keep `OPENAI_API_KEY` and `REALTIME_SAFETY_IDENTIFIER_KEY` in local secrets. The existing `REALTIME_*` enable switch, voice, safety identifier, queue and timeout settings are retained for compatibility. `VOICE_API=realtime` explicitly selects the previous transport; GPT-Live failures do not replay speech or switch to Realtime.

## Audio and tool flow

The connection sends `session.start`, waits for `session.started`, and continuously sends 16 kHz mono PCM16 in `session.input_audio.append`. Playback consumes `session.output_audio.delta`, with a 200 ms speech-onset reserve for network jitter and a one-second backlog limit. Silence inside buffered speech preserves timeline pauses; idle silence does not keep the speaker busy or extend concierge engagements. Input stays enabled during speech and the initial greeting. GPT-Live handles interruption and backchannels; this transport does not commit VAD turns, cancel Realtime responses or truncate conversation items.

The voice model delegates task work to Responses. The backend receives the seven concierge functions and one typed `robot_motion` function. Completed function items arrive inside `response.event`; the client executes them through the bounded local registry, returns `response.item.create` results, and sends `response.create` to continue. Backend work does not block audio reception. Late results are discarded after a session closes. No model tool identifies a person or controls a door.

`robot_motion` exposes capability discovery, looks, all head/body/antenna pose coordinates, gestures, installed dances/emotions and stop. Motions replace an existing performance, run on the single SDK motion thread, and obey local reachability and speed limits. Parking or capture freeze cancels them; model calls cannot enable motor torque. Stop remains available after the ordinary tool budget is exhausted. Provision the pinned Apache-2.0 Pollen recordings with `python -m app.scripts.provision_movements`; playback does not download data or play sidecar sounds.

The fixed biometric notice uses a local recording. Its consent window mutes cloud input and discards model output; server mute does not itself silence the model. Normal audio resumes after the local controller completes this flow and the server acknowledges unmute. Controller announcements use Live commentary; an acknowledgment confirms acceptance, not that a visitor heard the announcement.

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
