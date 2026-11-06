"""Bounded model storage and safe LRU eviction for SimpleAI Compute Worker.

A Docker named volume has no portable Compose disk quota. This module enforces
an application-level cap on model downloads, including transient .part files.
It never downloads arbitrary URLs: catalog models resolve to pinned HF commits.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import os
from pathlib import Path
import shutil

import httpx
from huggingface_hub import hf_hub_url

from compute_catalog import CATALOG

MIB = 1024 * 1024
GIB = 1024 ** 3
MIN_HOST_FREE = 64 * MIB


def disk_limit_bytes(raw: str) -> int:
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("WORKER_DISK_LIMIT_GB must be a positive number") from None
    if not value.is_finite() or value <= 0 or value > 100000:
        raise ValueError("WORKER_DISK_LIMIT_GB must be positive and finite")
    return int(value * GIB)


def used_bytes(folder: Path) -> int:
    """Sum logical bytes once per inode, without following symlinks."""
    total = 0
    seen = set()
    if not folder.exists():
        return 0
    for root, dirs, names in os.walk(folder, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
        for name in names:
            path = Path(root) / name
            try:
                stat = path.lstat()
            except OSError:
                continue
            if path.is_symlink() or not path.is_file():
                continue
            unique = (stat.st_dev, stat.st_ino)
            if unique not in seen:
                seen.add(unique)
                total += stat.st_size
    return total


def target_path(models: Path, model: str) -> Path:
    if model not in CATALOG:
        raise ValueError("Model not in approved catalog")
    return models / (model + ".gguf")


def remove_model(models: Path, model: str) -> None:
    target = target_path(models, model)
    for path in (target, target.with_suffix(".part")):
        if path.is_file() or path.is_symlink():
            path.unlink()
    # Legacy installs used per-model Hugging Face cache + root symlink.
    cache = models / ".hf-cache" / model
    if cache.is_dir():
        shutil.rmtree(cache)


def prune_to_limit(models: Path, limit: int, last_used: dict) -> list[str]:
    """Enforce a reduced quota on previously cached models at worker startup."""
    evicted = []
    if used_bytes(models) <= limit:
        return evicted
    cached = sorted(
        (mid for mid in CATALOG
         if target_path(models, mid).exists()
         or (models / ".hf-cache" / mid).is_dir()),
        key=lambda mid: last_used.get(mid, 0),
    )
    for mid in cached:
        if used_bytes(models) <= limit:
            break
        remove_model(models, mid)
        evicted.append(mid)
    # Legacy Hugging Face Xet transfer cache is not needed by bounded
    # direct-download worker. Remove stale shared transfer bytes if required.
    xet = models / ".hf-cache" / "xet"
    if used_bytes(models) > limit and xet.is_dir():
        shutil.rmtree(xet)
    if used_bytes(models) > limit:
        raise RuntimeError("Existing model-store files exceed configured disk quota")
    return evicted


def install_model(models: Path, mid: str, limit: int, last_used: dict,
                  protected: set[str] | None = None) -> list[str]:
    """Download within model-store cap; reclaim old cached files if necessary.

    Returns the models evicted before installation. A verified existing file
    needs no network and never evicts another model.
    """
    model = CATALOG[mid]
    target = target_path(models, mid)
    if target.exists():
        try:
            verify_model(target, model["sha256"])
            return []
        except RuntimeError:
            # A stale/corrupt cached GGUF must not permanently block autonomous
            # recovery. Remove known per-model cache and re-download safely.
            remove_model(models, mid)
    if target.is_symlink():
        target.unlink()
    # Clear a partial file left by a prior crash BEFORE computing used bytes.
    # Otherwise quota accounting could deadlock the next download.
    part = target.with_suffix(".part")
    if part.exists() or part.is_symlink():
        part.unlink()
    expected = int(model["size"])
    # Catalog sizes are approximate; verify hash after an up-to-15% allowance.
    max_download = max(expected + 16 * MIB, int(expected * 1.15))
    if max_download > limit:
        raise RuntimeError("Model exceeds configured disk quota")

    protected = set(protected or ())
    used = used_bytes(models)
    evicted = []
    candidates = sorted(
        (name for name in CATALOG if name != mid and name not in protected
         and (target_path(models, name).exists()
              or (models / ".hf-cache" / name).exists())),
        key=lambda name: last_used.get(name, 0),
    )
    for other in candidates:
        if used + max_download <= limit:
            break
        remove_model(models, other)
        evicted.append(other)
        used = used_bytes(models)

    if used + max_download > limit:
        # Releases orphaned legacy Xet transfer cache. New worker downloads do
        # not use this shared cache, so it can be removed safely here.
        transfer_cache = models / ".hf-cache" / "xet"
        if transfer_cache.is_dir():
            shutil.rmtree(transfer_cache)
            used = used_bytes(models)
    if used + max_download > limit:
        raise RuntimeError("Not enough quota; no evictable cached models")
    if shutil.disk_usage(models).free < max_download + MIN_HOST_FREE:
        raise RuntimeError("Not enough physical disk space")

    url = hf_hub_url(repo_id=model["repo"], filename=model["file"],
                     revision=model["revision"])
    count = 0
    try:
        # Stream directly into the target volume, without a duplicate HF cache.
        with httpx.Client(follow_redirects=True,
                          timeout=httpx.Timeout(90, connect=20)) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with part.open("xb") as output:
                    for piece in response.iter_bytes(chunk_size=MIB):
                        count += len(piece)
                        if count > max_download or used + count > limit:
                            raise RuntimeError("Download exceeded worker disk quota")
                        if shutil.disk_usage(models).free < MIN_HOST_FREE:
                            raise RuntimeError("Host disk reserve reached")
                        output.write(piece)
        verify_model(part, model["sha256"])
        part.replace(target)
    finally:
        if part.exists() or part.is_symlink():
            part.unlink()
    return evicted


def verify_model(path: Path, expected_sha: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(4 * MIB):
            digest.update(block)
    if digest.hexdigest() != expected_sha:
        raise RuntimeError("Model SHA256 mismatch")
