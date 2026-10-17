# Troubleshooting

## Dashboard is unavailable

Double-click Start Reachy.command. Opening a browser URL cannot start a stopped server. The launcher reuses healthy services and starts missing ones. Read its Terminal error and `.codex-runtime-logs/launcher-*.log`. If a different app occupies a required port, close it in its original terminal; the launcher does not kill unrelated processes. An older app on 8080 must be stopped once before the auto-chat launcher can replace it.

## Reachy does not connect

Check USB and external power. Inspect http://127.0.0.1:8000/api/daemon/status and the daemon log. Use one real no-media daemon and one media-owning app. If a motor reports Overload Error or stops following its target, stop movement and the app, switch off external power, check for obstructions without forcing a joint, and power on before restarting. Never bypass motor protection.

## GPT-Live is unavailable or silent

Check the private OpenAI key, network, voice error and audio diagnostics. The launcher explicitly selects `gpt_live`; it does not fall back to another API. Input and output counters should advance, with echo cancellation enabled and no increasing drop counts. Use Start chat to retry a failed session. An attended session ends after ten minutes; Start chat renews it.

Stop chat must remain stopped while its page stays open. Reloading/reopening an auto-chat dashboard starts chat again. Closing a tab does not stop the microphone session; press Stop chat first. Manual `run_reachy_live_test --demo` stays click-to-start unless `--auto-chat` is supplied.

## Camera is off or wrong

The camera is intentionally off at rest. Start chat, then verify the preview says Reachy Mini Camera and cover Reachy’s lens to check the source. Grant Terminal camera/microphone access in System Settings → Privacy & Security. No automatic fallback to the MacBook camera is permitted. Standalone probes require the media-owning app to be stopped first.

For visual questions, attended vision must be enabled. The model receives fresh snapshots on request through its Responses backend, not a continuous video feed. Check the snapshot count and latest observation. A failed or stale capture should leave voice running and report unavailability.

## Health stays unready

Use `/healthz/live` to check service startup. Missing PAD, measured calibration or public knowledge can intentionally keep access readiness false while attended chat works. A healthy generic MLAI backend is not evidence that its public knowledge publication surface exists. Ask its owner for the scoped principal/corpus; do not substitute a general Roo credential.

## Wake word or speaker selection is unreliable

Verify pinned models with `python -m app.scripts.provision_models --verify-only`. Complete the measured camera/DoA and held-out two-person checks in [speaker focus validation](speaker-focus-validation.md). Include trailing silence in recorded wake samples. Do not lower thresholds from anecdotes: collect a consented calibration set and rerun the held-out corpus gate. Attended chat deliberately tolerates face loss; it does not establish speaker-selection acceptance.
