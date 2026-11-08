"""Live GGUF catalog -> WSS policy -> ARM CPU inference acceptance for Oblak.

Run ONLY in the isolated staging proxy after updating its image to PR #46:
    python compute_staging_dynamic_smoke.py

This mutates only the staging catalog/policy, restores the previous policy
and removes the test entry afterward. The worker may retain the downloaded
model cache (normal safe LRU behavior). No private prompts are sent.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sys
import time
from urllib.request import Request, urlopen

import compute_staging_smoke as smoke


SOURCE = "unsloth/Qwen3.5-0.8B-GGUF"
VARIANT = "Qwen3.5-0.8B-Q3_K_M.gguf"
LIMIT_SECONDS = 900


def guard_staging():
    if os.getenv("ADMIN_USERNAME") != "staging-admin":
        raise RuntimeError("Refusing to mutate catalog outside isolated staging")
    if not Path("/state/admin-password").is_file():
        raise RuntimeError("Required isolated staging state volume is missing")


def delete_custom(mid: str):
    secret = Path("/state/admin-password").read_text().strip()
    basic = base64.b64encode(f"staging-admin:{secret}".encode()).decode()
    request = Request(
        smoke.BASE + "/admin/api/compute/catalog/custom/" + mid,
        headers={"Authorization": "Basic " + basic,
                 "X-SimpleAI-Admin-Action": "1"},
        method="DELETE",
    )
    with urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError("Catalog test cleanup failed")


def main():
    guard_staging()
    existing_policy = smoke.admin_api("/admin/api/compute/model-policy")["models"]
    node = smoke.worker_state()
    if node is None:
        raise RuntimeError("Isolated staging ARM worker is not online")

    print("Inspecting live Hugging Face metadata; weights stay on the worker")
    info = smoke.admin_api("/admin/api/compute/catalog/inspect", {"source": SOURCE})
    variants = {item["file"]: item for item in info["files"]}
    if VARIANT not in variants:
        raise RuntimeError("Missing GGUF quantization in real HF metadata: " + VARIANT)
    option = variants[VARIANT]
    if not (option.get("sha256") and option.get("revision") and option.get("size")):
        raise RuntimeError("Hugging Face metadata missing immutable SHA256/size")
    print("Pinned revision:", option["revision"])
    print("Verified metadata size:", option["size"], "bytes")

    mid = None
    try:
        added = smoke.admin_api("/admin/api/compute/catalog/custom", {
            "source": SOURCE, "file": VARIANT, "ctx": 4096, "enable": True,
        })
        mid = added["id"]
        print("Added staging-only custom model:", mid, flush=True)
        deadline = time.monotonic() + LIMIT_SECONDS
        while time.monotonic() < deadline:
            node = smoke.worker_state()
            if node:
                models = node.get("models", {})
                result = models.get("capabilities", {}).get(mid, {})
                status = result.get("status")
                if status in ("unsupported", "error"):
                    raise RuntimeError("Worker self-test: " + status + ": " +
                                       str(result.get("reason", "")))
                if status == "supported" and not models.get("busy"):
                    print("CPU inference self-test passed:", result.get("reason"))
                    old_smoke_model = smoke.MODEL
                    try:
                        smoke.MODEL = mid
                        smoke.stream_test(False, 96)
                    finally:
                        smoke.MODEL = old_smoke_model
                    print("PASS: real custom GGUF imported, provisioned, tested and streamed")
                    return 0
            time.sleep(3)
        raise RuntimeError("Custom GGUF model test exceeded staging deadline")
    finally:
        # Restore the old staging policy even when the test fails. Do not
        # mutate production, remote worker identity, or private access.
        if mid is not None:
            try:
                smoke.admin_api("/admin/api/compute/model-policy", {
                    "models": existing_policy
                })
            finally:
                delete_custom(mid)
            print("Cleaned up staging catalog entry and restored policy")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("FAIL:", str(exc)[:500], file=sys.stderr)
        sys.exit(1)
