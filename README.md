# Reachy Office Bouncer

FastAPI-based office access controller for Reachy Mini Lite.

The app is designed to run mostly locally, fail closed, and only unlock when all of the following are true:

- a nearby person says `Hey Reachy`
- exactly one usable face is present
- the face matches an enrolled local embedding set above threshold
- `mlai-backend` reports a `booked` coworking booking for `today`
- SALTO unlock succeeds

Voice is only a wake signal. It is never used as an authentication factor.

## What Is Included

- `app/`: main application package
- `app/static/`: local browser admin UI
- `app/tests/`: unit and integration tests
- `app/scripts/`: enrollment, diagnostics, calibration, and mock services
- `docs/`: setup, architecture, privacy, runbook, and troubleshooting
- `Dockerfile` and `docker-compose.yml`: simulation-first local stack

## Quickstart

1. Create a Python 3.11 environment:

```bash
uv venv --python 3.11
source .venv/bin/activate
```

2. Install the project:

```bash
uv pip install -e ".[dev]"
```

3. Copy the environment template:

```bash
cp .env.example .env
```

4. Start the app:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

5. Open the local admin UI at `http://localhost:8080`.

## Docker Simulation Stack

```bash
docker compose up --build
```

This starts:

- the office-bouncer app on `:8080`
- Postgres on `:5432`
- a mock `mlai-backend` on `:8091`
- a mock SALTO service on `:8092`

## Tests

```bash
pytest
```

## Core Environment Variables

- `DB_URL`
- `REACHY_ENABLED`
- `REACHY_SIMULATION`
- `MLAI_BACKEND_URL`
- `MLAI_BACKEND_API_KEY`
- `SALTO_CLIENT_ID`
- `SALTO_CLIENT_SECRET`
- `SALTO_AUTH_BASE_URL`
- `SALTO_API_BASE_URL`
- `SALTO_SITE_ID`
- `SALTO_LOCK_ID`
- `FACE_MATCH_THRESHOLD`
- `WAKEWORD_THRESHOLD`
- `VAD_THRESHOLD`

See [.env.example](/Users/samdonegan/Documents/Code/reachymate/.env.example) for the full list.

## Admin Endpoints

- `GET /healthz/live`
- `GET /healthz/ready`
- `GET /camera/snapshot`
- `GET /ws/telemetry`
- `POST /api/admin/enroll`
- `GET /api/admin/events`
- `GET /api/admin/diagnostics/audio`
- `GET /api/admin/diagnostics/camera`
- `GET /api/admin/users/{user_id}/booking-check`
- `POST /api/admin/test-speech`
- `POST /api/admin/simulate/attempt`
- `GET /api/admin/salto/oauth/start`
- `GET /api/admin/salto/oauth/callback`

## Enrollment

Enrollment can be done through the browser UI or the CLI:

```bash
python -m app.scripts.enroll_user \
  --full-name "Jane Doe" \
  --slack-user-id U12345 \
  --slack-display-name jane \
  sample_front.jpg sample_left.jpg sample_right.jpg
```

The enrollment flow stores face embeddings only. It rejects low-quality samples.

## Related Docs

- [Architecture](/Users/samdonegan/Documents/Code/reachymate/docs/architecture.md)
- [Setup](/Users/samdonegan/Documents/Code/reachymate/docs/setup.md)
- [Privacy](/Users/samdonegan/Documents/Code/reachymate/docs/privacy.md)
- [Runbook](/Users/samdonegan/Documents/Code/reachymate/docs/runbook.md)
- [Troubleshooting](/Users/samdonegan/Documents/Code/reachymate/docs/troubleshooting.md)
