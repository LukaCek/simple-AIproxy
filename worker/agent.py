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
from compute_catalog import CATALOG, BUILTIN_CATALOG, MAX_CATALOG_MODELS, validate_model_entry
from compute_worker_diagnostics import backend_error_code, error_code, validate as validate_diagnostic
from compute_worker_snapshot import STATUS_FILE, write_snapshot
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
    """Estimate clean file-backed cache reclaimable under cgroup pressure.

    The inactive_file counter alone excludes warm GGUF mmap pages in
    active_file. When the previous llama-server exits these *clean* cached
    pages may be reclaimed for another model. Exclude shmem and dirty /
    writeback pages to avoid treating pending writes as disposable.
    Fall back to the conservative inactive_file counter if needed.
    """
    try:
        counters = {}
        for line in Path(stat_path).read_text().splitlines():
            parts = line.split()
            if len(parts) == 2:
                counters[parts[0]] = max(0, int(parts[1]))
        inactive = counters.get(key, 0)
        # cgroup v2 file includes shmem/tmpfs; v1 total_cache is analogous.
        if "file" in counters:
            file_total = counters["file"]
            shmem = counters.get("shmem", 0)
            dirty = counters.get("file_dirty", 0)
            writeback = counters.get("file_writeback", 0)
        elif "total_cache" in counters:
            file_total = counters["total_cache"]
            shmem = counters.get("total_shmem", counters.get("shmem", 0))
            dirty = counters.get("total_dirty", counters.get("dirty", 0))
            writeback = counters.get("total_writeback", counters.get("writeback", 0))
        else:
            return inactive
        return max(0, file_total - shmem - dirty - writeback)
    except (OSError, ValueError):
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
        "catalog_protocol": 1,
        "build_revision": os.getenv("WORKER_BUILD_REVISION", "local-dev")[:64],
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
        self.backend_log_task = None
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
        self.catalog_path = DATA / "approved-model-catalog.json"
        self._diag_path = DATA / "diagnostics.json"
        self._diag_seq = 0
        self._diag_pending = []
        self._load_diagnostics()
        self._restore_catalog()
        self.fingerprint = self._catalog_fingerprint()
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
        self.connected = False
        self.phase = "starting"
        self.current_model = None
        self.current_kind = None
        self.phase_started_at = int(time.time())
        self.job_started_at = None
        self._publish_snapshot()

    def _catalog_fingerprint(self):
        return hashlib.sha256(
            (str(DISK_LIMIT_BYTES) + ":" + str(memory_limit()) + ":" +
             str(cpu_count()) + ":" + json.dumps(CATALOG, sort_keys=True) +
             ":autotest-v3-timing-sample").encode()
        ).hexdigest()

    def _validated_catalog(self, source):
        if (not isinstance(source, dict) or not 1 <= len(source) <= MAX_CATALOG_MODELS):
            raise ValueError("Invalid remote catalog size")
        clean = {}
        for mid, definition in source.items():
            verified = validate_model_entry(mid, definition)
            if mid in BUILTIN_CATALOG and verified != BUILTIN_CATALOG[mid]:
                raise ValueError("Bundled model identity cannot be overwritten")
            clean[mid] = verified
        if not set(BUILTIN_CATALOG).issubset(clean):
            raise ValueError("Remote catalog must retain bundled models")
        return clean

    def _restore_catalog(self):
        # Retain the central admin catalog across worker container restarts.
        # This avoids re-testing unchanged models at every image update.
        try:
            if self.catalog_path.stat().st_size > 128 * 1024:
                return
            cached = json.loads(self.catalog_path.read_text(encoding="utf-8"))
            clean = self._validated_catalog(cached)
        except (OSError, ValueError, TypeError):
            return
        CATALOG.clear()
        CATALOG.update(clean)

    def _save_catalog(self):
        data = json.dumps(CATALOG, sort_keys=True, separators=(",", ":"))
        if len(data.encode("utf-8")) > 128 * 1024:
            raise ValueError("Catalog too large")
        temp = self.catalog_path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
        os.replace(temp, self.catalog_path)

    def _apply_remote_catalog(self, payload):
        if payload is None:
            return False
        fresh = self._validated_catalog(payload)
        if fresh == CATALOG:
            return False
        changed = {mid for mid in set(CATALOG) | set(fresh)
                   if CATALOG.get(mid) != fresh.get(mid)}
        CATALOG.clear()
        CATALOG.update(fresh)
        # Re-test only changed/new models, retaining verified results for
        # unchanged models (also across image updates).
        for mid in changed:
            self.capabilities.pop(mid, None)
            self.last_used.pop(mid, None)
        self.fingerprint = self._catalog_fingerprint()
        self._save_catalog()
        self._save_state()
        self._publish_snapshot()
        return True

    def _publish_snapshot(self):
        """Local TUI observability only: never serialize credentials or prompts."""
        try:
            state = self.status()
            safe_capabilities = {}
            for mid, result in self.capabilities.items():
                if mid not in CATALOG or not isinstance(result, dict):
                    continue
                metric = result.get("benchmark")
                safe_metric = {}
                if isinstance(metric, dict):
                    for name in ("prompt_tps", "generation_tps", "load_seconds"):
                        value = metric.get(name)
                        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value < 1_000_000:
                            safe_metric[name] = round(float(value), 3)
                safe_capabilities[mid] = {
                    "status": result.get("status") if result.get("status") in
                        ("supported", "unsupported", "testing", "error") else "untested",
                    "benchmark": safe_metric,
                }
            snapshot = {
                "schema": 1, "updated_at": time.time(), "worker_name": NAME,
                "connected": self.connected, "phase": self.phase,
                "phase_started_at": self.phase_started_at,
                "job_started_at": self.job_started_at,
                "current_model": self.current_model, "current_kind": self.current_kind,
                "policy": list(self.policy or []),
                "hardware": hardware(),
                "models": {
                    "installed": state["installed"], "active": state["active"],
                    "ready": state["ready"], "busy": state["busy"],
                    "running_tasks": state["running_tasks"],
                    "disk_used_bytes": state["disk_used_bytes"],
                    "disk_limit_bytes": state["disk_limit_bytes"],
                    "policy_revision": state["policy_revision"],
                    "capabilities": safe_capabilities,
                },
            }
            write_snapshot(DATA / STATUS_FILE, snapshot)
        except (OSError, ValueError, TypeError):
            # Diagnostics must never prevent inference or reconnects.
            pass

    def _load_diagnostics(self):
        """Keep a small private local buffer through WSS disconnects/restarts."""
        try:
            if self._diag_path.stat().st_size > 96 * 1024:
                return
            state = json.loads(self._diag_path.read_text(encoding="utf-8"))
            last = state.get("last_seq", 0)
            if type(last) is int and 0 <= last < 2**53:
                self._diag_seq = last
            entries = state.get("pending", [])
            if isinstance(entries, list):
                for item in entries[-120:]:
                    try:
                        safe = validate_diagnostic(item)
                    except ValueError:
                        continue
                    if safe["seq"] <= self._diag_seq:
                        self._diag_pending.append(safe)
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _persist_diagnostics(self):
        # Only validated enums, numbers and catalog model IDs are written.
        target = self._diag_path.with_suffix(".tmp")
        try:
            data = json.dumps({"last_seq": self._diag_seq,
                               "pending": self._diag_pending}, separators=(",", ":"))
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(data)
            os.replace(target, self._diag_path)
        except OSError:
            # Diagnostic disk I/O must never make a working inference fail.
            pass

    def _diag(self, kind, code, *, level="info", model=None, metrics=None):
        """Append only a strict, privacy-safe diagnostic entry."""
        self._diag_seq += 1
        entry = {"seq": self._diag_seq, "kind": kind, "code": code,
                 "level": level, "model": model if model in CATALOG else "",
                 "occurred_at": int(time.time()), "metrics": metrics or {}}
        try:
            self._diag_pending.append(validate_diagnostic(entry))
        except ValueError:
            return
        self._diag_pending = self._diag_pending[-120:]
        self._persist_diagnostics()

    def _ack_diag(self, seq):
        if type(seq) is not int:
            return
        remaining = [entry for entry in self._diag_pending if entry["seq"] != seq]
        if len(remaining) != len(self._diag_pending):
            self._diag_pending = remaining
            self._persist_diagnostics()

    async def _send_diagnostics(self, ws):
        """Replay pending structured entries after reconnect, with server ack."""
        sent = set()
        while True:
            pending = [item for item in self._diag_pending if item["seq"] not in sent]
            for item in pending[:16]:
                await self.send(ws, {"type": "diagnostic", **item})
                sent.add(item["seq"])
            await asyncio.sleep(2)

    def _resource_metrics(self, mid):
        # Diagnostics are best effort: a broken model mount must not crash
        # the worker a second time while it is reporting the first error.
        mib = 1024 ** 2
        try:
            return {
                "ram_limit_mib": max(0, memory_limit() // mib),
                "ram_available_mib": max(0, memory_available() // mib),
                "disk_limit_mib": max(0, DISK_LIMIT_BYTES // mib),
                "disk_used_mib": max(0, used_bytes(MODELS) // mib),
                "model_size_mib": max(0, int(CATALOG[mid]["size"]) // mib),
            }
        except (OSError, ValueError, KeyError):
            return {}

    def _activity(self, phase, model=None, kind=None):
        now = time.time()
        if phase in ("idle", "starting", "connecting", "disconnected"):
            self.current_model = None
            self.current_kind = None
            self.job_started_at = None
        else:
            if self.job_started_at is None:
                self.job_started_at = now
            self.current_model = model
            if kind is not None:
                self.current_kind = kind
        old_phase = self.phase
        self.phase = phase
        self.phase_started_at = now
        self._publish_snapshot()
        transitions = {
            "starting": ("lifecycle", "starting"),
            "connecting": ("network", "connecting"),
            "disconnected": ("network", "disconnected"),
            "checking disk / downloading": ("model", "download_start"),
            "loading into RAM": ("model", "load_start"),
            "unloading": ("model", "unload_start"),
            "benchmarking": ("model", "benchmark_start"),
            "command": ("operation", "operation_start"),
            "preparing inference": ("inference", "inference_start"),
        }
        if phase != old_phase and phase in transitions:
            kind, code = transitions[phase]
            self._diag(kind, code, model=model)

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
        """15-second network heartbeat, 1-second local diagnostics snapshot.

        Keep local TUI responsive without multiplying WSS telemetry traffic.
        """
        while True:
            await self.send(ws, {"type": "telemetry", "hardware": hardware(), "models": self.status()})
            for _ in range(15):
                self._publish_snapshot()
                await asyncio.sleep(1)

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
        self._publish_snapshot()
        if status == "testing":
            code, level = "test_start", "info"
        elif status == "supported":
            code, level = "test_supported", "info"
        elif status == "unsupported":
            code, level = error_code(reason), "warning"
            if code == "unknown_error":
                code = "test_unsupported"
        else:
            code, level = error_code(reason), "error"
            if code == "unknown_error":
                code = "test_failed"
        self._diag("model_test", code, level=level, model=mid,
                   metrics=self._resource_metrics(mid)
                   if status in ("unsupported", "error") else {}) 

    async def ensure_installed(self, mid):
        self._activity("checking disk / downloading", mid)
        evicted = await asyncio.to_thread(download, mid, self.last_used, {self.active} if self.active else set())
        self.last_used[mid] = time.time()
        self._save_state()
        return evicted

    async def benchmark(self, mid):
        """One real non-thinking completion to prove the CPU backend works."""
        self._activity("benchmarking", mid, "self-test")
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
                or len(set(str(x) for x in selected)) != len(selected)):
            return
        try:
            async with self.lock:
                catalog_changed = self._apply_remote_catalog(message.get("catalog"))
        except (ValueError, OSError, TypeError):
            # Never allow an unpinned or malformed remote download target.
            return
        if (len(selected) > len(CATALOG)
                or any(not isinstance(x, str) or x not in CATALOG for x in selected)):
            return
        previous = self.policy
        old_version = self.policy_revision
        self.policy = selected
        self.policy_revision = version
        self._diag("policy", "policy_received")
        self._publish_snapshot()
        if previous != selected:
            # A node may get a new model assignment at the same global revision.
            self.policy_generation += 1
        # Re-saving an unchanged policy deliberately re-tests; merely
        # inspecting/adding a disabled catalog entry must not re-run expensive
        # benchmarks for every already verified model.
        self.force_retest = (not catalog_changed and previous == selected
                             and old_version not in (0, version))
        if self.reconcile_task is None or self.reconcile_task.done():
            self.reconcile_task = asyncio.create_task(self.reconcile(ws))

    async def test_model(self, mid):
        """Perform actual CPU load and inference, with caller holding self.lock."""
        if mid not in CATALOG:
            raise ValueError("Model not in approved catalog")
        started = time.monotonic()
        self.mark_model(mid, "testing")
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
                self.mark_model(mid, "error", str(exc),
                                time.monotonic() - started)
        return self.capabilities[mid]

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
                async with self.lock:
                    await self.test_model(mid)
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
            self._activity("idle")
            if version == self.policy_revision and generation == self.policy_generation:
                break

    async def _capture_backend_errors(self, proc, mid):
        """Observe only known llama.cpp stderr failure signatures.

        NEVER forward or persist raw stderr; upstream versions may include
        user-provided prompt text and even model responses in their logs.
        """
        seen = set()
        try:
            while proc.stderr is not None:
                line = await proc.stderr.readline()
                if not line:
                    break
                code = backend_error_code(line)
                if code and code not in seen:
                    seen.add(code)
                    self._diag("model", code, level="error", model=mid)
        except (OSError, ValueError, asyncio.LimitOverrunError):
            # Capturing stderr is optional diagnostics, not a failure of the
            # inference service. Ignore malformed or oversized backend lines.
            pass

    async def unload(self):
        if self.process and self.process.returncode is None:
            self._activity("unloading", self.active)
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 15)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.backend_log_task is not None:
            self.backend_log_task.cancel()
            await asyncio.gather(self.backend_log_task, return_exceptions=True)
            self.backend_log_task = None
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
        self._activity("loading into RAM", mid)
        started_loading = time.monotonic()
        self.process = await asyncio.create_subprocess_exec(
            "/usr/local/bin/llama-server", "-m", str(path), "--alias", mid,
            "--host", "127.0.0.1", "--port", str(PORT),
            "-t", str(min(int(os.getenv("WORKER_THREADS", "4")), cpu_count())),
            "-c", str(CATALOG[mid]["ctx"]), "-np", "1", "-ngl", "0",
            "--no-webui", "--no-mmproj", "--jinja", "--reasoning", REASONING_MODE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        self.backend_log_task = asyncio.create_task(
            self._capture_backend_errors(self.process, mid))
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
                        self._publish_snapshot()
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
                self._activity("command", mid, action)
                if action == "install":
                    await self.ensure_installed(mid)
                elif action == "test":
                    previous_active = self.active if self.status()["ready"] else None
                    try:
                        result = await self.test_model(mid)
                    finally:
                        # Preserve normal serving behavior after an explicit
                        # admin benchmark, including failed experiments.
                        if previous_active and previous_active in CATALOG:
                            try:
                                await self.ensure_installed(previous_active)
                                await self.load(previous_active)
                            except Exception as exc:
                                self.mark_model(previous_active, "error",
                                                "Restore after test: " + str(exc))
                    if result["status"] != "supported":
                        raise RuntimeError(result.get("reason") or "Model failed CPU self-test")
                elif action == "load":
                    # Never evict the currently working models to download a
                    # GGUF that cannot fit the configured container RAM cap.
                    required = model_ram_requirement(int(CATALOG[mid]["size"])) + memory_safety_margin()
                    if required > memory_limit():
                        raise RuntimeError(
                            f"Cannot load {mid}: estimated {required // 1024**2} MiB "
                            f"exceeds worker RAM limit {memory_limit() // 1024**2} MiB. "
                            "Increase Docker mem_limit or choose a smaller model."
                        )
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
            self._diag("operation", "operation_succeeded", model=mid)
            await self.send(ws, {"type": "result", "id": cid, "ok": True})
            await self.send(ws, {"type": "telemetry", "hardware": hardware(), "models": self.status()})
        except Exception as exc:
            self._diag("operation", error_code(exc)
                       if error_code(exc) != "unknown_error" else "operation_failed",
                       level="error", model=mid)
            await self.send(ws, {"type": "error", "id": cid, "error": str(exc)[:400]})
        finally:
            self.tasks.pop(cid, None)
            self._activity("idle")

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
                self._activity("preparing inference", mid, "inference")
                if self.active != mid or not self.status()["ready"]:
                    if self.policy is not None and self.capabilities.get(mid, {}).get("status") != "supported":
                        raise RuntimeError("Model has not passed worker self-test")
                    await self.unload()
                    await self.ensure_installed(mid)
                    await self.load(mid)
                    await self.telemetry(ws)
                self.last_used[mid] = time.time()
                self._save_state()
                self._activity("generating", mid, "inference")
                await self._inference_stream(ws, cid, mid, payload)
                self._diag("inference", "inference_end", model=mid)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._diag("inference", "inference_failed", level="error", model=mid)
            self._diag("inference", error_code(exc), level="error", model=mid)
            await self.send(ws, {"type": "error", "id": cid, "error": str(exc)[:400]})
        finally:
            self.tasks.pop(cid, None)
            self._activity("idle")
            # Immediately release the stale busy flag instead of waiting up to
            # the next 15-second heartbeat before the scheduler scores us.
            await self.telemetry(ws)

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
        self._activity("connecting")
        identity = await self.credentials()
        delay = 2
        while True:
            try:
                async with connect(url, max_size=2 * 1024 * 1024, ping_interval=20) as ws:
                    await self.send(ws, {"type": "auth", **identity, "protocol": 1,
                                         "catalog_protocol": 1})
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 20))
                    if msg.get("type") != "auth_ok":
                        raise RuntimeError("Authentication rejected")
                    self.connected = True
                    self._activity("idle")
                    self._diag("network", "connected")
                    delay = 2
                    heart = asyncio.create_task(self.heartbeat(ws))
                    diagnostics = asyncio.create_task(self._send_diagnostics(ws))
                    try:
                        async for raw in ws:
                            cmd = json.loads(raw)
                            cid, kind = str(cmd.get("id") or ""), cmd.get("type")
                            if kind == "diagnostic_ack":
                                self._ack_diag(cmd.get("seq"))
                            elif kind == "policy":
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
                        diagnostics.cancel()
                        if self.reconcile_task and not self.reconcile_task.done():
                            self.reconcile_task.cancel()
                            await asyncio.gather(self.reconcile_task, return_exceptions=True)
                        self.reconcile_task = None
                        for task in self.tasks.values():
                            task.cancel()
                        await asyncio.gather(heart, diagnostics, *list(self.tasks.values()),
                                             return_exceptions=True)
                        self.tasks.clear()
                        self.connected = False
                        self._activity("disconnected")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = error_code(exc)
                self._diag("network", code, level="warning")
                print(f"Disconnected: {code}", flush=True)
            self.connected = False
            self._activity("disconnected")
            await asyncio.sleep(delay)
            delay = min(60, delay * 2)


if __name__ == "__main__":
    asyncio.run(Worker().run())
