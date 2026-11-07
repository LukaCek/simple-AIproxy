"""End-to-end autonomous model provisioning smoke test on *isolated* Oblak.

Inside the staging proxy:
    python compute_staging_auto_smoke.py

Sets the approved model targets in the proxy, waits for the worker's real
per-model benchmarks, checks a dynamically routed inference, and leaves the
policy enabled. No commands are sent to the worker host beyond Compose.
"""
import os
from pathlib import Path
import sys
import time

from compute_catalog import CATALOG
import compute_staging_smoke as smoke


def main():
    if os.getenv("ADMIN_USERNAME") != "staging-admin":
        raise RuntimeError("Refusing to edit model policy outside staging")
    if not Path("/state/admin-password").exists():
        raise RuntimeError("Missing staging secret volume")

    selected = list(CATALOG)
    policy = smoke.admin_api("/admin/api/compute/model-policy",
                             {"models": selected})
    version = policy["revision"]
    print("Enabled self-tests via proxy policy:", ", ".join(selected))
    print("Policy revision:", version, flush=True)
    deadline = time.monotonic() + 360
    finished = {"supported", "unsupported", "error"}
    while time.monotonic() < deadline:
        node = smoke.worker_state()
        if node:
            models = node.get("models", {})
            capabilities = models.get("capabilities", {})
            statuses = {
                mid: capabilities.get(mid, {}).get("status", "untested")
                for mid in selected
            }
            if (models.get("policy_revision") == version
                    and all(status in finished for status in statuses.values())):
                preferred = next((mid for mid in selected
                                  if statuses[mid] == "supported"), None)
                if preferred is None:
                    print("No tested model is currently available on this worker.")
                    hw = node.get("hardware", {})
                    limit = int(hw.get("memory_limit_bytes") or 0)
                    free = int(hw.get("memory_available_bytes") or 0)
                    print("Worker RAM limit:", round(limit / (1024**3), 2), "GiB")
                    print("Worker RAM available:", round(free / (1024**3), 2), "GiB")
                    print("Model cache:", round(int(models.get("disk_used_bytes") or 0) / (1024**3), 2),
                          "/", round(int(models.get("disk_limit_bytes") or 0) / (1024**3), 2), "GiB")
                    for mid in selected:
                        print(mid, statuses[mid],
                              capabilities.get(mid, {}).get("reason", ""))
                    return 1
                # The policy reconciler warms the preferred supported model.
                if models.get("ready") and models.get("active") == preferred:
                    print("Self-test results:")
                    for mid in selected:
                        result = capabilities.get(mid, {})
                        print(mid + ": " + statuses[mid] +
                              " (" + result.get("reason", "") + ")")
                    print("Preferred model ready:", preferred)
                    if preferred == smoke.MODEL:
                        smoke.stream_test(False, 96)
                    else:
                        print("Staging smoke uses only the 0.8B model; primary differs.")
                    return 0
        time.sleep(3)
    print("FAIL: Worker model self-tests did not finish within the check window")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("FAIL:", str(exc)[:600])
        sys.exit(1)
