"""One-command end-to-end compute smoke test for the isolated Oblak staging proxy.

Run *inside* the staging proxy container:
    python compute_staging_smoke.py --thinking off
    python compute_staging_smoke.py --thinking on --max-tokens 512

Never execute against production. Credentials and API keys are ephemeral and
never printed. The prompt is explicitly public, so untrusted nodes may run it.
"""
import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:8000"
MODEL = "qwen35-0.8b-q4"
WORKER = "oblak-staging-worker"


def admin_api(path, body=None):
    secret = Path("/state/admin-password").read_text().strip()
    basic = base64.b64encode(f"staging-admin:{secret}".encode()).decode()
    request = Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Basic " + basic,
            "Content-Type": "application/json",
            "X-SimpleAI-Admin-Action": "1",
        },
    )
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def worker_state():
    nodes = admin_api("/admin/api/compute/nodes")["nodes"]
    return next(
        (node for node in nodes if node["name"] == WORKER and node["online"]),
        None,
    )


def ensure_loaded():
    node = worker_state()
    if node is None:
        raise RuntimeError("Worker is not online")
    if MODEL not in node["models"].get("installed", []):
        raise RuntimeError("Model is not installed. Run the install operation first.")
    if node["models"].get("ready") and node["models"].get("active") == MODEL:
        print("Model already loaded.", flush=True)
        return
    op = admin_api(
        f"/admin/api/compute/nodes/{node['id']}/operations",
        {"action": "load", "model": MODEL},
    )
    print("Loading existing GGUF model into RAM ...", flush=True)
    until = time.monotonic() + 180
    while time.monotonic() < until:
        result = admin_api("/admin/api/compute/operations/" + op["id"])
        if result["status"] == "succeeded":
            break
        if result["status"] == "failed":
            raise RuntimeError("Model load failed: " + result.get("detail", ""))
        time.sleep(2)
    else:
        raise RuntimeError("Timed out waiting for model load")
    until = time.monotonic() + 30
    while time.monotonic() < until:
        node = worker_state()
        if node and node["models"].get("ready") and node["models"].get("active") == MODEL:
            print("Model ready.", flush=True)
            return
        time.sleep(1)
    raise RuntimeError("Model load succeeded, but ready telemetry has not arrived")


def stream_test(thinking: bool, max_tokens: int):
    db = sqlite3.connect("/app/app.db")
    key = secrets.token_hex(24)
    start = time.monotonic()
    try:
        with db:
            db.execute(
                "INSERT INTO API_Keys(name,key,created_at) VALUES(?,?,?)",
                ("one-time-staging-smoke", key, datetime.now(timezone.utc).isoformat()),
            )
        payload = {
            "model": "local/" + MODEL,
            "aiproxy_visibility": "public",
            "stream": True,
            "max_tokens": max_tokens,
            "think": thinking,
            "messages": [
                {"role": "user", "content": "Koliko je 2 + 2? Odgovori samo s stevilko."}
            ],
        }
        request = Request(
            BASE + "/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )
        content = []
        reasoning = 0
        events = 0
        first_event = None
        done = False
        print("Thinking:", "on" if thinking else "off", flush=True)
        print("Token limit:", max_tokens, flush=True)
        print("Streaming result:", flush=True)
        with urlopen(request, timeout=180) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                events += 1
                if first_event is None:
                    first_event = round(time.monotonic() - start, 2)
                if line[6:] == "[DONE]":
                    done = True
                    break
                event = json.loads(line[6:])
                for choice in event.get("choices", []):
                    delta = choice.get("delta") or {}
                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        print(text, end="", flush=True)
                        content.append(text)
                    if delta.get("reasoning_content"):
                        reasoning += 1
        print("\nFirst SSE event:", first_event, "s")
        print("Elapsed:", round(time.monotonic() - start, 2), "s")
        print("SSE events:", events)
        print("Reasoning chunks:", reasoning)
        print("Content chunks:", len(content))
        answer = "".join(content).strip()
        print("Answer:", repr(answer))
        if answer and "4" not in answer:
            print("WARNING: Math answer is not 4; streaming passed but model accuracy did not.")
        print("Received DONE:", done)
        if not done or not content:
            raise RuntimeError("Stream incomplete or no visible answer")
        print("PASS: streamed content received through proxy and worker")
    finally:
        with db:
            db.execute("DELETE FROM API_Keys WHERE key=?", (key,))
        db.close()
        print("One-time API key deleted.")


def main():
    parser = argparse.ArgumentParser(description="Test local model via staging proxy")
    parser.add_argument("--thinking", choices=("on", "off"), default="off",
                        help="Smoke tests use off by default for fast delivery; production defaults on")
    parser.add_argument("--max-tokens", type=int, default=None)
    args = parser.parse_args()
    limit = args.max_tokens if args.max_tokens is not None else (512 if args.thinking == "on" else 96)
    if not 1 <= limit <= 1024:
        parser.error("--max-tokens must be between 1 and 1024")
    if os.getenv("ADMIN_USERNAME") != "staging-admin":
        raise RuntimeError("Refusing to run outside isolated staging")
    if not Path("/state/admin-password").is_file():
        raise RuntimeError("Missing staging credentials volume")
    ensure_loaded()
    stream_test(args.thinking == "on", limit)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, HTTPError, URLError) as exc:
        if isinstance(exc, HTTPError):
            print("HTTP", exc.code, exc.read(1024).decode("utf-8", "replace"))
        else:
            print("FAIL:", exc)
        sys.exit(1)
