"""Production entrypoint composing all runtime extensions."""

# Apply Codex model and normal-text SSE compatibility first.
import runtime_entrypoint as _runtime  # noqa: F401

# Then register live request-log routes and middleware behavior.
import live_logs_entrypoint as _live

app = _live.app
