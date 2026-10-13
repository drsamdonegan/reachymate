"""Mock SALTO OAuth and unlock endpoints."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

app = FastAPI(title="Mock SALTO KS")


@app.get("/oauth/authorize")
async def authorize(response_type: str, client_id: str, redirect_uri: str, state: str):
    return RedirectResponse(f"{redirect_uri}?code=mock-auth-code&state={state}")


@app.post("/oauth/token")
async def token():
    return {
        "access_token": "mock-access-token",
        "refresh_token": "mock-refresh-token",
        "token_type": "Bearer",
        "expires_in": 3600,
    }


@app.patch("/v1.2/sites/{site_id}/locks/{lock_id}/locking")
async def unlock(site_id: str, lock_id: str):
    return {"request_id": f"{site_id}:{lock_id}:unlock", "locked_state": "open"}
