# Setup

Use the canonical `app/` package on Python 3.11. The connected Mini Lite runs directly on the host Mac; the container configuration is for simulation.

```sh
uv sync --frozen --python 3.11.14 --extra audio --extra vision --extra reachy --extra skills --extra kiosk --extra dev
cp .env.example .env
.venv/bin/python -m app.scripts.provision_models
.venv/bin/python -m app.scripts.provision_movements
```

Set `OPENAI_API_KEY`, a URL-safe base64 `DATA_ENCRYPTION_KEY` containing 32 random bytes, and a separate random `REALTIME_SAFETY_IDENTIFIER_KEY` in private local secrets. Keep `.env` private and do not replace an existing file containing keys. Enrollment and real-hardware use require encryption. Do not commit credentials, camera images, recordings, databases or site evidence.

## USB Reachy on macOS

Connect USB and external power. Grant Camera and Microphone access to Terminal in System Settings → Privacy & Security. Double-click **Start Reachy.command** in Finder. It starts/reuses the no-media SDK daemon, loopback booking/front-desk mocks and GPT-Live app, then connects chat and opens http://127.0.0.1:8080/.

The launcher uses GPT-Live and the actual Reachy camera/audio devices. It permanently forces loopback hosting, stage zero and shadow mode. Reopening the dashboard starts attended chat once; Stop chat stays stopped while the page remains open. Closing the tab or Terminal does not stop services or an active chat. An attended session ends after ten minutes unless explicitly renewed with Start chat. Camera images are sent to OpenAI only on visual questions. No staff messages or door commands are sent by the local mocks/placeholder.

To run without opening a browser:

```sh
.venv/bin/python -m app.scripts.launch_reachy --no-browser
```

Repeated launches reuse the running services. If an old manual app is on 8080, stop that process in its original terminal, then reopen the launcher. Logs live under the ignored `.codex-runtime-logs/launcher-*.log` paths. The launcher never installs login agents or changes macOS power or security settings.

For manual management, start each command in its own terminal:

```sh
.venv/bin/python -m app.scripts.run_reachy_daemon --no-media --no-wake-up-on-start --goto-sleep-on-stop --fastapi-host 127.0.0.1 --dataset-update-interval 0 --no-preload-datasets
.venv/bin/python -m uvicorn app.scripts.mock_mlai_backend:app --host 127.0.0.1 --port 8091
.venv/bin/python -m app.scripts.mock_frontdesk
.venv/bin/python -m app.scripts.run_reachy_live_test --demo
```

Add `--auto-chat` to the last command to enable chat on dashboard opening. Without it, Start chat is explicit. The ordinary wake-word flow retains speaker gating. Unmeasured SDK camera intrinsics support an attended demonstration only: they do not pass calibration, speaker-selection or biometric acceptance gates.

## Simulation and storage

`docker compose up --build` runs a non-actuating simulation and booking mock. Developer/admin routes are disabled in this configuration. For a local development dashboard use the settings in `.env.example`, loopback hosting and explicit `ALLOW_DEV_ENDPOINTS=true`; production never registers those routes.

SQLite is the default (`sqlite:///./data/office_bouncer.db`). Startup runs Alembic migrations. Postgres uses `DB_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME`. Keep encryption keys outside database backups. Legacy sensitive records can be previewed and migrated with `python -m app.scripts.migrate_sensitive_data`, then `--apply`; back up first.

## External integrations

Approved knowledge needs a public-only service principal for `public_knowledge.read` and `surface=public_roo`, plus the backend’s deliberate publication endpoint and corpus. General Roo or Slack credentials are not a substitute. The real front-desk service must implement [its contract](contracts/frontdesk-skill.md); loopback mock success does not establish actual delivery.

The active runtime’s door action is a non-actuating placeholder. Physical deployment requires the separate measured, security, sensor and approval gates in [the concierge overview](concierge.md). See [testing](testing.md) before attempting hardware acceptance.
