# Runbook

## Start and stop chat

Connect USB and external power, then double-click **Start Reachy.command**. It starts or reuses the local daemon, mocks and GPT-Live app and opens the existing dashboard with chat connected. Reopening the dashboard starts chat once if stopped. Stop chat immediately closes cloud voice; it stays stopped across status polls. Closing the browser or launcher Terminal leaves background services running and does not end voice. The attended chat cap is ten minutes.

If services are stopped, reopen the launcher. A browser page alone cannot start a stopped local server. Camera/microphone permissions belong to the launching Terminal. Logs are under `.codex-runtime-logs/launcher-{daemon,bookings,frontdesk,app}.log`; do not publish them or private evidence.

For wake-word and resting tests, press Stop chat, then leave the page open. Do not reopen an auto-chat dashboard during those tests. Allow the normal rest timer to park Reachy. Stop the application gracefully before running standalone camera/audio probes; a second media owner will conflict with the app.

## Verify

- `/healthz/live` returns 200 when the app is serving. `/healthz/ready` may remain 503 while PAD or approved knowledge is unavailable; that is not a failed dashboard startup.
- Audio diagnostics show `api=gpt_live`, `full_duplex=true`, increasing input/output counters, healthy echo cancellation and no increasing drop/restart counts.
- The preview names Reachy Mini Camera. Visual questions increase the model snapshot counter and produce a current scene observation.
- Stop chat silences playback and closes the session. Reloading the launcher-enabled dashboard reconnects it once.
- Small look/antenna requests and bounded dances complete without motor errors. Stop movement cancels a performance promptly.
- Door mode stays shadow/stage zero. Mock routing never sends Slack notifications.

Use [the functionality checklist](testing.md) for conversation, interruption, motion, visual and recovery checks. Hardware measurements are separate from software tests.

## Asset and release checks

Verify local models with `python -m app.scripts.provision_models --verify-only`. Use only a consented, labelled doorway corpus for wake/command evaluation; do not retain incidental room audio. Run `python -m app.scripts.evaluate_audio_corpus /path/to/corpus/manifest.json` with a held-out set.

Missing or degraded identity, consent, PAD, booking or hardware evidence denies access. Do not reinterpret a placeholder response, a model’s spoken claim or healthy general voice as permission to unlock. The physical door remains outside this attended run.
