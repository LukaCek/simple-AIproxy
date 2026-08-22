"""Production entrypoint composing all runtime extensions."""

# Apply Codex model and normal-text SSE compatibility first.
import runtime_entrypoint as _runtime  # noqa: F401

# Then register live request-log routes and middleware behavior.
import live_logs_entrypoint as _live

# Finally attach the dynamic free-model registry to the shared base application.
import free_model_registry as _free_registry
import main_impl as _impl

_free_registry.install(_impl)
app = _live.app
