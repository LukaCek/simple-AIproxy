"""SimpleAI CPU worker: outbound WSS, bounded local llama.cpp process."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
from urllib.parse import urlsplit, urlunsplit

import httpx
from huggingface_hub import hf_hub_download
from websockets.asyncio.client import connect
from compute_catalog import CATALOG

DATA = Path(os.getenv("WORKER_DATA_DIR", "/data"))
MODELS = Path(os.getenv("WORKER_MODEL_DIR", "/models"))
IDENTITY = DATA / "identity.json"
PORT = 18228
PROXY = os.environ["SIMPLEAI_PROXY_URL"].rstrip("/")
TOKEN = os.getenv("WORKER_ENROLLMENT_TOKEN", "")
NAME = os.getenv("WORKER_NAME", platform.node())[:80]


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


def memory_available():
    host = memory_limit()
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                host = int(line.split()[1]) * 1024
                break
    except (OSError, IndexError, ValueError):
        pass
    # /proc/meminfo reflects host memory on many container setups. Deduct
    # actual cgroup consumption before deciding whether to load a model.
    try:
        current = int(Path("/sys/fs/cgroup/memory.current").read_text())
        return max(0, min(host, memory_limit() - current))
    except (OSError, ValueError):
        return min(host, memory_limit())


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


def model_file(mid):
    if mid not in CATALOG:
        raise ValueError("Model not in approved catalog")
    return MODELS / (mid + ".gguf")


def verify(path, checksum):
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(4 * 1024 * 1024):
            sha.update(data)
    if sha.hexdigest() != checksum:
        raise RuntimeError("Model SHA256 mismatch")


def download(mid):
    info = CATALOG[mid]
    target = model_file(mid)
    if target.exists():
        verify(target, info["sha256"])
        return
    if shutil.disk_usage(MODELS).free < info["size"] * 1.15:
        raise RuntimeError("Insufficient disk space")
    path = Path(hf_hub_download(repo_id=info["repo"], filename=info["file"],
                                revision=info["revision"], cache_dir=str(MODELS / ".hf-cache" / mid)))
    verify(path, info["sha256"])
    temp = target.with_suffix(".tmp")
    if temp.exists() or temp.is_symlink():
        temp.unlink()
    temp.symlink_to(path)
    os.replace(temp, target)


class Worker:
    def __init__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        MODELS.mkdir(parents=True, exist_ok=True)
        self.process = None
        self.active = None
        self.lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()
        self.tasks = {}

    def status(self):
        ready = bool(self.process and self.process.returncode is None and self.active)
        return {"installed": [k for k in CATALOG if model_file(k).exists()],
                "active": self.active if ready else None, "ready": ready}

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
        reserve = min(2 * 1024**3, max(512 * 1024**2, memory_limit() // 8))
        required = int(path.stat().st_size * 1.25) + reserve
        if required > min(memory_limit(), memory_available()):
            raise RuntimeError("Insufficient available memory for this model")
        await self.unload()
        self.process = await asyncio.create_subprocess_exec(
            "/usr/local/bin/llama-server", "-m", str(path), "--alias", mid,
            "--host", "127.0.0.1", "--port", str(PORT),
            "-t", str(min(int(os.getenv("WORKER_THREADS", "4")), cpu_count())),
            "-c", str(CATALOG[mid]["ctx"]), "-np", "1", "-ngl", "0",
            "--no-webui", "--no-mmproj", "--jinja",
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
                    await asyncio.to_thread(download, mid)
                elif action == "load":
                    await self.load(mid)
                elif action == "unload":
                    await self.unload()
                elif action == "delete":
                    if mid == self.active:
                        await self.unload()
                    p = model_file(mid)
                    if p.exists() or p.is_symlink():
                        p.unlink()
                    # Each model uses an isolated Hugging Face cache. Removing
                    # only the symlink would leave the multi-GB cached blob.
                    cache = MODELS / ".hf-cache" / mid
                    if cache.is_dir():
                        shutil.rmtree(cache)
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
            if mid not in CATALOG or self.active != mid or not self.status()["ready"]:
                raise RuntimeError("Model is not loaded")
            payload = msg.get("payload")
            if not isinstance(payload, dict) or len(json.dumps(payload)) > 256 * 1024:
                raise ValueError("Invalid inference payload")
            payload = {**payload, "model": mid}
            async with httpx.AsyncClient(timeout=httpx.Timeout(3600, connect=10)) as client:
                async with client.stream(
                    "POST", f"http://127.0.0.1:{PORT}/v1/chat/completions", json=payload
                ) as res:
                    if res.status_code != 200:
                        body = await res.aread()
                        raise RuntimeError(f"llama.cpp HTTP {res.status_code}: {body[:160]!r}")
                    async for part in res.aiter_bytes(chunk_size=32768):
                        if part:
                            await self.send(ws, {"type": "chunk", "id": cid,
                                                 "body": base64.b64encode(part).decode()})
            await self.send(ws, {"type": "end", "id": cid, "status": 200})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.send(ws, {"type": "error", "id": cid, "error": str(exc)[:400]})
        finally:
            self.tasks.pop(cid, None)

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
                            if kind == "cancel":
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
