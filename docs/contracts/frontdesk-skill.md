# Backend front-desk contract

The concierge is an outbound caller. The backend owns Slack, opted-in host lookup, member validation and host authorization; no inbound port or Slack credential is required on the kiosk.

## HTTP

`POST /api/v1/front-door/visitor-requests`, Bearer service credential and `Idempotency-Key: session_ref:call_id`. The body includes a server-generated UUID `request_ref`, `device_id`, `session_ref`, opaque `track_ref`, optional `access_event_id`, typed intent and sanitized names. `skill_tool` is selected by deterministic code: `notify_host` or `notify_channel`. Notes are omitted unless a tour contact disclosure was explicitly authorized locally.

Return `{request_ref,status,expires_at}`. `GET /api/v1/front-door/visitor-requests/{request_ref}` returns the same opaque reference and one of `pending`, `host_coming`, `declined`, `expired`, `approved_entry`. Bind reads to the originating device/service principal. Idempotency must survive backend restarts. Unknown hosts go to the front desk and produce the same external response as known hosts. Never return directory membership, contact details or arbitrary speech.

Poll every three seconds for at most 120 seconds. An approval is status data; it cannot actuate a door. Stage-D3 host authorization separately requires an allowlisted signer, device/session binding and expiry within five minutes.

`GET /api/v1/concierge/members/?slack_user_id=...`, organization-scoped `X-API-Key`, returns a matching active member with `slack_user_id`, `full_name`, `active`. Enrollment fails closed on unavailable or mismatched records.

## MCP

MCP 1.x streamable HTTP at `/mcp`: `notify_host(payload,idempotency_key)`, `notify_channel(payload,idempotency_key)`, `get_request_status(request_ref)`, backend-only `lookup_host` and `event_lookup`. The model never supplies a tool endpoint. Operations have three-second deadlines, size limits and a circuit breaker. Transport scopes are closed by the calling task.

The local mock has both bindings and sends no Slack notifications. Backend production implementation and review are external prerequisites.

## Kiosk key custody

A configured HTTPS key endpoint accepts a device credential and returns `{device_id,key_id,kek}`. The KEK is a 32-byte base64 value held in memory, and unwraps a local AES-GCM envelope `{key_id,nonce,ciphertext}` with AAD `kiosk_data_key|device_id|key_id`. Credentials must be revocable. A failed fetch disables biometric access, never conversation or a manual check-in path. The backend must establish its authentication, authorization, audit and rotation contract before commissioning.
