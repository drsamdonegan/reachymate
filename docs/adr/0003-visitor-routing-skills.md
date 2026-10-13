# ADR 0003: backend-owned visitor routing

The concierge calls one fixed backend origin over HTTP or MCP 1.x. The backend owns host-directory lookup, Slack notifications and authorized replies. Kiosk tools have enum-limited inputs; controller code adds device/session/track context and idempotency.

No inbound callback or Slack credential is exposed on the kiosk. Replies are polled and reduced to fixed status speech. Known and unknown hosts receive equivalent public responses. Visitor-route records contain no names and physical door control remains outside the model and active runtime.

The local mock proves transport behavior only; production backend ownership, authorization, rate limits and deployment require a separate review.
