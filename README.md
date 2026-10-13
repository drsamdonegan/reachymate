# Reachy concierge

FastAPI concierge for Reachy Mini Lite. The canonical runtime is `app/`. Local wake detection starts an engagement; a detection-only face tracker and an audio-visual gate select speech for a bounded OpenAI Realtime conversation. The model has seven typed tools and no identity or door actuator capability.

Face checks require a fixed local notice followed by the visitor's own locally recognised “check me in”. Enrollment requires recorded voluntary consent and encrypted templates. Routed visitors cannot start a face check during that engagement.

**The active runtime is shadow-only and never composes a physical actuator.** PAD, measured calibration, a door sensor, second factor, compliance evidence and signed approval remain deployment gates. Recognition thresholds are provisional.

## Setup

```sh
uv sync --frozen --python 3.11.14 --extra audio --extra vision --extra reachy --extra skills --extra dev
cp .env.example .env
uv run python -m app.scripts.provision_models
```

Generate a 32-byte URL-safe base64 `DATA_ENCRYPTION_KEY` and a separate random `REALTIME_SAFETY_IDENTIFIER_KEY`; keep them in private local secrets. A real robot run or enrollment requires encryption. Never commit keys, images, audio corpora or database files.

Loopback simulation:

```sh
uv run uvicorn app.main:app --host 127.0.0.1 --port 8080
uv run python -m app.scripts.mock_frontdesk
```

Admin routes are disabled unless `ALLOW_DEV_ENDPOINTS=true` and are never registered in production. Real hardware additionally requires camera/microphone permissions, measured calibration and an available local daemon:

```sh
.venv/bin/reachy-mini-daemon --no-media --no-wake-up-on-start --goto-sleep-on-stop --fastapi-host 127.0.0.1 --dataset-update-interval 0 --no-preload-datasets
.venv/bin/python -m app.scripts.run_reachy_live_test --demo
```

The demo profile uses loopback front-desk/bookings mocks and forces shadow mode. It does not grant consent or approve any hardware deployment. Camera calibration and direction surveys must be completed before live conversation acceptance.

## Verification

```sh
uv run pytest app/tests -q
uv run ruff check app
uv build
```

Hardware measurements are separate from unit tests. See [hardware probes](docs/hardware-probes.md), [speaker focus validation](docs/speaker-focus-validation.md), [front-desk contract](docs/contracts/frontdesk-skill.md) and [concierge overview](docs/concierge.md). Optional pilot features remain disabled until their assets and held-out measurements are validated.

The legacy conversation prototype is retained until physical migration acceptance; it is excluded from the canonical package and test command.
