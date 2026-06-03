# Runbook

## Start

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

or:

```bash
docker compose up --build
```

## Verify

- `GET /healthz/live`
- `GET /healthz/ready`
- open the admin UI at `http://localhost:8080`
- confirm camera snapshots update
- confirm telemetry websocket receives events

## Daily Checks

- review the last 50 access events
- confirm booking checks against `mlai-backend`
- confirm SALTO tokens are still valid
- confirm no component is in `ERROR`
