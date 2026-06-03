# Setup

## Requirements

- Python 3.11+
- `uv`
- optional vendor packages for audio, vision, and Reachy hardware

## Local Development

```bash
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

## Optional Extras

```bash
uv pip install -e ".[audio,vision]"
uv pip install -e ".[audio,vision,reachy]"
```

## Database

SQLite is the default local database:

```text
sqlite:///./data/office_bouncer.db
```

For Postgres, set:

```text
DB_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME
```

## SALTO Setup

- configure a confidential OAuth client
- set `SALTO_CLIENT_ID`
- set `SALTO_CLIENT_SECRET`
- set `SALTO_AUTH_BASE_URL`
- set `SALTO_API_BASE_URL`
- set `SALTO_REDIRECT_URI`
- set `SALTO_SITE_ID`
- set `SALTO_LOCK_ID`

Then open `/api/admin/salto/oauth/start` to complete the initial token bootstrap.

## `mlai-backend` Setup

Configure:

- `MLAI_BACKEND_URL`
- `MLAI_BACKEND_API_KEY`

The app expects:

- `GET /api/v1/points/coworking/my-bookings/?slack_user_id=...`
- `POST /api/v1/points/coworking/booking-help/`
