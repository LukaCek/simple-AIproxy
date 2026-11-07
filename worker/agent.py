"""SimpleAI CPU worker: outbound WSS, bounded local llama.cpp process."""
import asyncio
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import time
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect
from compute_catalog import CATALOG
from compute_worker_storage import (disk_limit_bytes, used_bytes, install_model, remove_model, verify_model, target_path, prune_to_limit)

DATA = Path(os.getenv("WORKER_DATA_DIR", "/data"))
MODELS = Path(os.getenv("WORKER_MODEL_DIR", "/models"))
IDENTITY = DATA / "identity.json"
PORT = 18228
PROXY = os.environ["SIMPLEAI_PROXY_URL"].rstrip("/")
TOKEN = os.getenv("WORKER_ENROLLMENT_TOKEN", "")
NAME = os.getenv("WORKER_NAME", platform.node())[:80]
# Qwen3.5 small models can spend a short response entirely on reasoning.
# Default to reasoning ON; the proxy can override individual requests.
REASONING_MODE = os.getenv("WORKER_REASONING", "on").strip().lower()
if REASONING_MODE not in {"on", "off", "auto"}:
    raise ValueError("WORKER_REASONING must be on, off, or auto")
DISK_LIMIT_BYTES = disk_limit_bytes(os.getenv("WORKER_DISK_LIMIT_GB", "8"))


def memory_limit():
    values = []
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            value = Path(path).read_text().strip()
            if value != "max" and 0 < int(value) < (1 << 62):
                values.append(int(value))
        except (ValueError, OSError):
            pass
    values.append(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    return min(values)


def _reclaimable_file_cache(stat_path: str, key: str) -> int:
    """Return the inactive file-page cache the kernel may reclaim on pressure."""
    try:
        for line in Path(stat_path).read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == key:
                return max(0, int(parts[1]))
    except (OSError, ValueError):
        pass
    return 0


def memory_available():
    host = memory_limit()
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                host = int(line.split()[1]) * 1024
                break
    except (OSError, IndexError, ValueError):
        pass

    # cgroup memory.current includes reclaimable page cache from model GGUF
    # download and SHA256 verification. If counted as non-reclaimable, loading
    # Qwen 0.8B after a 2B download is falsely denied even with ample headroom.
    for usage_path, stat_path, inactive_key in (
        ("/sys/fs/cgroup/memory.current",
         "/sys/fs/cgroup/memory.stat", "inactive_file"),
        ("/sys/fs/cgroup/memory/memory.usage_in_bytes",
         "/sys/fs/cgroup/memory/memory.stat", "total_inactive_file"),
    ):
        try:
            used = int(Path(usage_path).read_text().strip())
        except (OSError, ValueError):
            continue
        if used < 0:
            continue
        inactive_file = _reclaimable_file_cache(stat_path, inactive_key)
        effective_used = max(0, used - min(used, inactive_file))
        return max(0, min(host, memory_limit() - effective_used))
    return max(0, min(host, memory_limit()))


def model_ram_requirement(model_bytes: int) -> int:
    """Conservative RAM prediction, plus runtime/context overhead."""
    limit = memory_limit()
    reserve = min(2 * 1024**3, max(512 * 1024**2, limit // 8))
    return int(model_bytes * 1.25) + reserve


def memory_safety_margin() -> int:
    """Keep hard cgroup headroom even when a model is close to the limit."""
    return max(128 * 1024**2, min(256 * 1024**2, memory_limit() // 8))


def cpu_count():
    cpus = os.cpu_count() or 1
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            cpus = min(cpus, max(1, int(quota) // int(period)))
    except (OSError, ValueError):
        pass
    return max(1, cpus)


def hardware():
    disk = shutil.disk_usage(MODELS)
    return {
        "arch": platform.machine(), "os": platform.system(),
        "cpus": cpu_count(), "memory_limit_bytes": memory_limit(),
        "memory_available_bytes": memory_available(),
        "disk_free_bytes": disk.free, "disk_total_bytes": disk.total,
        "version": "0.1.0", "backend": "llama.cpp-cpu",
    }


def benchmark_metrics(data, elapsed):
    """Read actual llama.cpp timing counters. Do not invent token throughput."""
    timings = data.get("timings", {}) if isinstance(data, dict) else {}
    if not isinstance(timings, dict):
        timings = {}
    result = {"wall_seconds": round(max(0.0, min(float(elapsed), 3600.0)), 3)}
    for key, count_key, ms_key in (
        ("prompt_tps", "prompt_n", "prompt_ms"),
        ("generation_tps", "predicted_n", "predicted_ms"),
    ):
        rate_key = "prompt_per_second" if key == "prompt_tps" else "predicted_per_second"
        value = timings.get(rate_key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            n, ms = timings.get(count_key), timings.get(ms_key)
            value = n * 1000.0 / ms if (isinstance(n, (int, float)) and
                isinstance(ms, (int, float)) and not isinstance(n, bool) and
                not isinstance(ms, bool) and math.isfinite(n) and math.isfinite(ms) and n > 0 and ms > 0) else None
        if isinstance(value, (int, float)) and math.isfinite(value) and 0 < value < 1_000_000:
            result[key] = round(float(value), 3)
    return result


def model_file(mid):
    return target_path(MODELS, mid)


def verify(path, checksum):
    verify_model(path, checksum)


def download(mid, last_used=None, protected=None):
    return install_model(MODELS, mid, DISK_LIMIT_BYTES, last_used or {}, protected)


class Worker:
    def __init__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        MODELS.mkdir(parents=True, exist_ok=True)
        self.process = None
        self.active = None
        self.last_load_seconds = None
        self.lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()
        self.tasks = {}
        self.policy = None
        self.policy_revision = 0
        self.policy_generation = 0
        self.reconcile_task = None
        self.last_used = {}
        self.capabilities = {}
        self.state_path = DATA / "model-test-results.json"
        self.fingerprint = hashlib.sha256(
            (str(DISK_LIMIT_BYTES) + ":" + str(memory_limit()) + ":" +
             str(cpu_count()) + ":" + json.dumps(CATALOG, sort_keys=True) +
             ":autotest-v3-timing-sample").encode()
        ).hexdigest()
        self._read_state()
        # No download is running before the worker main loop starts. Clean
        # orphaned partial downloads so a crash cannot strand the disk quota.
        for mid in CATALOG:
            part = model_file(mid).with_suffix(".part")
            if part.is_file() or part.is_symlink():
                part.unlink()
        # If an operator lowers the Compose disk budget, automatically reclaim
        # oldest cached model files before accepting more downloads.
        prune_to_limit(MODELS, DISK_LIMIT_BYTES, self.last_used)

    def _read_state(self):
        try:
            state = json.loads(self.state_path.read_text())
            if state.get("fingerprint") != self.fingerprint:
                return
            capabilities = state.get("capabilities", {})
            last_used = state.get("last_used", {})
            if isinstance(capabilities, dict):
                self.capabilities = {k: dict(v) for k, v in capabilities.items()
                                     if k in CATALOG and isinstance(v, dict)
                                     and v.get("status") in ("supported", "unsupported", "error", "testing")}
                for result in self.capabilities.values():
                    if result.get("status") == "testing":
                        result.update(status="error",
                                      reason="Worker stopped during model test (possible resource exhaustion)")
            if isinstance(last_used, dict):
                self.last_used = {k: float(v) for k, v in last_used.items()
                                  if k in CATALOG and isinstance(v, (float, int))}
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _save_state(self):
        data = json.dumps({"fingerprint": self.fingerprint,
                           "capabilities": self.capabilities,
                           "last_used": self.last_used})
        tmp = self.state_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(data)
        os.replace(tmp, self.state_path)

    def status(self):
        ready = bool(self.process and self.process.returncode is None and self.active)
        return {"installed": [k for k in CATALOG if model_file(k).exists()],
                "active": self.active if ready else None, "ready": ready,
                "capabilities": self.capabilities,
                "busy": self.lock.locked() or bool(self.tasks),
                "running_tasks": 1 if self.lock.locked() else 0,
                "disk_used_bytes": used_bytes(MODELS),
                "disk_limit_bytes": DISK_LIMIT_BYTES,
                "policy_revision": self.policy_revision}

    async def credentials(self):
        if IDENTITY.exists():
            identity = json.loads(IDENTITY.read_text())
            if identity.get("node_id") and identity.get("node_secret"):
                return identity
        if not TOKEN:
            raise RuntimeError("Missing one-time enrollment token")
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.post(PROXY + "/compute/enroll", json={"token": TOKEN, "name": NAME})
            res.raise_for_status()
            identity = res.json()
        tmp = IDENTITY.with_suffix(".tmp")
        tmp.write_text(json.dumps(identity))
        os.chmod(tmp, 0o600)
        os.replace(tmp, IDENTITY)
        return identity

    async def send(self, ws, msg):
        async with self.write_lock:
            await ws.send(json.dumps(msg, separators=(",", ":")))

    async def heartbeat(self, ws):
        while True:
            await self.send(ws, {"type": "telemetry", "hardware": hardware(), "models": self.status()})
            await asyncio.sleep(15)

    async def telemetry(self, ws):
        try:
            await self.send(ws, {"type": "telemetry", "hardware": hardware(),
                                 "models": self.status()})
        except Exception:
            # Reconciliation is also able to finish when WSS reconnects.
            pass

    def mark_model(self, mid, status, reason="", seconds=0, benchmark=None):
        self.capabilities[mid] = {
            "status": status, "reason": str(reason)[:240],
            "test_seconds": round(float(seconds), 2),
            "tested_at": int(time.time()),
        }
        if isinstance(benchmark, dict):
            self.capabilities[mid]["benchmark"] = benchmark
        self._save_state()

    async def ensure_installed(self, mid):
        evicted = await asyncio.to_thread(download, mid, self.last_used, {self.active} if self.active else set())
        self.last_used[mid] = time.time()
        self._save_state()
        return evicted

    async def benchmark(self, mid):
        """One real non-thinking completion to prove the CPU backend works."""
        async with httpx.AsyncClient(timeout=180) as client:
            started = time.monotonic()
            response = await client.post(
                f"http://127.0.0.1:{PORT}/v1/chat/completions",
                json={"model": mid, "stream": False, "max_tokens": 64,
                      "chat_template_kwargs": {"enable_thinking": False},
                      "messages": [{"role": "user",
                                    "content": "Reply with exactly: READY"}]},
            )
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices", [])
            content = choices[0].get("message", {}).get("content") if choices else None
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("Inference returned no visible answer")
            result = benchmark_metrics(data, time.monotonic() - started)
            # The READY probe may generate only two tokens. A second bounded,
            # non-thinking probe provides a more representative throughput
            # sample without making valid models depend on this optional test.
            try:
                started = time.monotonic()
                perf = await client.post(
                    f"http://127.0.0.1:{PORT}/v1/chat/completions",
                    json={"model": mid, "stream": False, "max_tokens": 64,
                          "chat_template_kwargs": {"enable_thinking": False},
                          "messages": [{"role": "user", "content":
                                        "Count from 1 to 40, comma separated, no explanation."}]},
                )
                perf.raise_for_status()
                measured = benchmark_metrics(perf.json(), time.monotonic() - started)
                for field in ("prompt_tps", "generation_tps", "wall_seconds"):
                    if field in measured:
                        result[field] = measured[field]
            except (httpx.HTTPError, ValueError, KeyError):
                pass  # Never fail model support merely for a speed probe.
            if self.last_load_seconds is not None:
                result["load_seconds"] = round(max(0, float(self.last_load_seconds)), 3)
            return result

    async def receive_policy(self, ws, message):
        selected = message.get("models")
        version = message.get("revision")
        if (type(version) is not int or version < 1 or not isinstance(selected, list)
                or len(selected) > len(CATALOG) or len(set(str(x) for x in selected)) != len(selected)
                or any(not isinstance(x, str) or x not in CATALOG for x in selected)):
            return
        previous = self.policy
        old_version = self.policy_revision
        self.policy = selected
        self.policy_revision = version
        if previous != selected:
            # A node may get a new model assignment at the same global revision.
            self.policy_generation += 1
        self.force_retest = previous == selected and old_version not in (0, version)
        if self.reconcile_task is None or self.reconcile_task.done():
            self.reconcile_task = asyncio.create_task(self.reconcile(ws))

    async def reconcile(self, ws):
        """Test requested models, report capacity, and warm the preferred one.

        All destructive model-store operations are serialized against inference.
        The configured disk cap is enforced even if the policy selects more
        models than can be cached simultaneously.
        """
        while self.policy is not None:
            selected = list(self.policy)
            version = self.policy_revision
            generation = self.policy_generation
            retest = getattr(self, "force_retest", False)
            self.force_retest = False
            # Test from lowest priority first so the preferred model is cached
            # last and remains available on small volumes.
            for mid in reversed(selected):
                if version != self.policy_revision or generation != self.policy_generation:
                    break
                previous = self.capabilities.get(mid, {})
                if not retest and previous.get("status") in ("supported", "unsupported", "error"):
                    continue
                started = time.monotonic()
                self.mark_model(mid, "testing")
                await self.telemetry(ws)
                model = CATALOG[mid]
                max_file = max(model["size"] + 16 * 1024**2, int(model["size"] * 1.15))
                required_ram = model_ram_requirement(int(model["size"]))
                if max_file > DISK_LIMIT_BYTES:
                    self.mark_model(mid, "unsupported", "Exceeds configured model disk limit")
                elif required_ram + memory_safety_margin() > memory_limit():
                    self.mark_model(mid, "unsupported",
                                    "Model plus runtime safety margin exceeds configured cgroup RAM")
                else:
                    try:
                        async with self.lock:
                            await self.unload()
                            await self.ensure_installed(mid)
                            await self.load(mid)
                            measured = await self.benchmark(mid)
                            await self.unload()
                        self.mark_model(mid, "supported", "Actual inference passed",
                                        time.monotonic() - started, benchmark=measured)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        await self.unload()
                        # Failed downloads / inference can be retried by saving
                        # model policy again; don't claim the model is supported.
                        self.mark_model(mid, "error", str(exc),
                                        time.monotonic() - started)
                await self.telemetry(ws)
            if version != self.policy_revision or generation != self.policy_generation:
                continue
            preferred = next((mid for mid in selected
                              if self.capabilities.get(mid, {}).get("status") == "supported"),
                             None)
            try:
                async with self.lock:
                    if preferred is None:
                        await self.unload()
                    elif self.active != preferred or not self.status()["ready"]:
                        await self.unload()
                        await self.ensure_installed(preferred)
                        await self.load(preferred)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if preferred:
                    self.mark_model(preferred, "error", "Warmup: " + str(exc))
            await self.telemetry(ws)
            if version == self.policy_revision and generation == self.policy_generation:
                break

    async def unload(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 15)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process, self.active = None, None

    async def load(self, mid):
        path = model_file(mid)
        if not path.exists():
            raise RuntimeError("Model not installed")
        if self.active == mid and self.status()["ready"]:
            return
        required = model_ram_requirement(path.stat().st_size)
        if required + memory_safety_margin() > memory_limit():
            raise RuntimeError("Model exceeds cgroup RAM limit with required safety margin")
        # Page cache from SHA256/model download is reclaimable. Do not include
        # it as resident used memory when considering model startup.
        available = memory_available()
        if required > available:
            raise RuntimeError(
                f"Insufficient *reclaimable-adjusted* available memory: "
                f"need {required // 1024**2} MiB, have {available // 1024**2} MiB"
            )
        await self.unload()
        started_loading = time.monotonic()
        self.process = await asyncio.create_subprocess_exec(
            "/usr/local/bin/llama-server", "-m", str(path), "--alias", mid,
            "--host", "127.0.0.1", "--port", str(PORT),
            "-t", str(min(int(os.getenv("WORKER_THREADS", "4")), cpu_count())),
            "-c", str(CATALOG[mid]["ctx"]), "-np", "1", "-ngl", "0",
            "--no-webui", "--no-mmproj", "--jinja", "--reasoning", REASONING_MODE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        self.active = mid
        async with httpx.AsyncClient(timeout=10) as client:
            for _ in range(180):
                if self.process.returncode is not None:
                    await self.unload()
                    raise RuntimeError("llama-server crashed during startup")
                try:
                    result = await client.get(f"http://127.0.0.1:{PORT}/health")
                    if result.status_code == 200:
                        self.last_load_seconds = time.monotonic() - started_loading
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(2)
        await self.unload()
        raise RuntimeError("llama-server did not become ready")

    async def command(self, ws, msg):
        cid, mid, action = msg["id"], msg.get("model"), msg.get("action")
        try:
            if mid not in CATALOG:
                raise ValueError("Unapproved model")
            async with self.lock:
                if action == "install":
                    await self.ensure_installed(mid)
                elif action == "load":
                    await self.unload()
                    await self.ensure_installed(mid)
                    await self.load(mid)
                elif action == "unload":
                    await self.unload()
                elif action == "delete":
                    if mid == self.active:
                        await self.unload()
                    remove_model(MODELS, mid)
                    self.last_used.pop(mid, None)
                    self._save_state()
                else:
                    raise ValueError("Invalid action")
            await self.send(ws, {"type": "result", "id": cid, "ok": True})
            await self.send(ws, {"type": "telemetry", "hardware": hardware(), "models": self.status()})
        except Exception as exc:
            await self.send(ws, {"type": "error", "id": cid, "error": str(exc)[:400]})
        finally:
            self.tasks.pop(cid, None)

    async def inference(self, ws, msg):
        cid, mid = msg["id"], msg.get("model")
        try:
            if mid not in CATALOG:
                raise RuntimeError("Model not approved")
            if self.policy is not None and mid not in self.policy:
                raise RuntimeError("Model not enabled in proxy policy")
            payload = msg.get("payload")
            if not isinstance(payload, dict) or len(json.dumps(payload)) > 256 * 1024:
                raise ValueError("Invalid inference payload")
            payload = {**payload, "model": mid}
            async with self.lock:
                if self.active != mid or not self.status()["ready"]:
                    if self.policy is not None and self.capabilities.get(mid, {}).get("status") != "supported":
                        raise RuntimeError("Model has not passed worker self-test")
                    await self.unload()
                    await self.ensure_installed(mid)
                    await self.load(mid)
                    await self.telemetry(ws)
                self.last_used[mid] = time.time()
                self._save_state()
                await self._inference_stream(ws, cid, mid, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.send(ws, {"type": "error", "id": cid, "error": str(exc)[:400]})
        finally:
            self.tasks.pop(cid, None)

    async def _inference_stream(self, ws, cid, mid, payload):
            async with httpx.AsyncClient(timeout=httpx.Timeout(3600, connect=10)) as client:
                async with client.stream(
                    "POST", f"http://127.0.0.1:{PORT}/v1/chat/completions", json=payload
                ) as res:
                    if res.status_code != 200:
                        body = await res.aread()
                        raise RuntimeError(f"llama.cpp HTTP {res.status_code}: {body[:160]!r}")
                    # Do not request a fixed 32 KiB read: HTTPX would wait
                    # for that much output (or EOF), defeating token-by-token SSE.
                    # Bound each WSS message without buffering tiny SSE frames.
                    async for part in res.aiter_bytes():
                        for offset in range(0, len(part), 32768):
                            piece = part[offset:offset + 32768]
                            await self.send(ws, {"type": "chunk", "id": cid,
                                                 "body": base64.b64encode(piece).decode()})
            await self.send(ws, {"type": "end", "id": cid, "status": 200})

    async def run(self):
        parts = urlsplit(PROXY)
        if parts.scheme != "https" or not parts.netloc:
            raise RuntimeError("Proxy address must be HTTPS")
        url = urlunsplit(("wss", parts.netloc, parts.path.rstrip("/") + "/compute/ws", "", ""))
        identity = await self.credentials()
        delay = 2
        while True:
            try:
                async with connect(url, max_size=2 * 1024 * 1024, ping_interval=20) as ws:
                    await self.send(ws, {"type": "auth", **identity, "protocol": 1})
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 20))
                    if msg.get("type") != "auth_ok":
                        raise RuntimeError("Authentication rejected")
                    delay = 2
                    heart = asyncio.create_task(self.heartbeat(ws))
                    try:
                        async for raw in ws:
                            cmd = json.loads(raw)
                            cid, kind = str(cmd.get("id") or ""), cmd.get("type")
                            if kind == "policy":
                                await self.receive_policy(ws, cmd)
                            elif kind == "cancel":
                                task = self.tasks.get(cid)
                                if task:
                                    task.cancel()
                            elif kind in ("command", "run") and cid:
                                if len(self.tasks) >= 1:
                                    await self.send(ws, {"type": "error", "id": cid, "error": "Worker busy"})
                                    continue
                                task = asyncio.create_task(
                                    self.command(ws, cmd) if kind == "command" else self.inference(ws, cmd)
                                )
                                self.tasks[cid] = task
                    finally:
                        heart.cancel()
                        if self.reconcile_task and not self.reconcile_task.done():
                            self.reconcile_task.cancel()
                            await asyncio.gather(self.reconcile_task, return_exceptions=True)
                        self.reconcile_task = None
                        for task in self.tasks.values():
                            task.cancel()
                        await asyncio.gather(heart, *list(self.tasks.values()), return_exceptions=True)
                        self.tasks.clear()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"Disconnected: {exc}", flush=True)
            await asyncio.sleep(delay)
            delay = min(60, delay * 2)


if __name__ == "__main__":
    asyncio.run(Worker().run())
