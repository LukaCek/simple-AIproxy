"""Safe Hugging Face GGUF metadata lookup for admin-approved compute models.

The proxy never fetches model weights. It reads public HF metadata at a fixed
host, pins the real revision SHA and checksum, and sends validated metadata to
worker nodes after explicit admin approval.
"""
from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlsplit

import httpx

from compute_catalog import validate_model_entry

MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_OPTIONS = 120
_SOURCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")


def parse_source(source: str) -> tuple[str, str, str | None]:
    if not isinstance(source, str) or len(source) > 512 or not source.strip():
        raise ValueError("Provide a Hugging Face repository or GGUF file URL")
    source = source.strip()
    if source.startswith("https://"):
        url = urlsplit(source)
        if (url.scheme != "https" or url.hostname != "huggingface.co"
                or url.username or url.password or url.port or url.fragment):
            raise ValueError("Only https://huggingface.co links are accepted")
        parts = [unquote(p) for p in url.path.strip("/").split("/")]
    else:
        if ":" in source or "?" in source or "#" in source:
            raise ValueError("Provide a Hugging Face repository or file URL")
        parts = source.strip("/").split("/")
    if len(parts) < 2 or not all(_SOURCE_RE.fullmatch(p) for p in parts[:2]):
        raise ValueError("Expected Hugging Face owner/repository")
    repo = "/".join(parts[:2])
    if len(parts) == 2:
        return repo, "main", None
    if len(parts) < 5 or parts[2] not in ("blob", "resolve"):
        raise ValueError("Use a Hugging Face GGUF file URL or owner/repository")
    ref = parts[3]
    if not _REF_RE.fullmatch(ref):
        raise ValueError("Unsupported Hugging Face revision")
    filename = "/".join(parts[4:])
    # Reuse model-entry validation before any network request.
    validate_model_entry("candidate", {
        "name": "candidate", "repo": repo, "file": filename,
        "revision": "a" * 40, "size": 1, "sha256": "b" * 64, "ctx": 4096,
    })
    return repo, ref, filename


async def lookup_gguf_models(source: str) -> dict:
    repo, ref, filename = parse_source(source)
    url = (f"https://huggingface.co/api/models/"
           f"{quote(repo, safe='/')}/revision/{quote(ref, safe='')}")
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.get(url, params={"blobs": "true"})
            response.raise_for_status()
            if len(response.content) > MAX_METADATA_BYTES:
                raise ValueError("Hugging Face repository metadata is too large")
            info = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError("Cannot fetch public Hugging Face metadata") from exc
    if not isinstance(info, dict):
        raise ValueError("Invalid Hugging Face repository metadata")
    sha = info.get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("Hugging Face did not return a pinned commit")
    siblings = info.get("siblings")
    if not isinstance(siblings, list):
        raise ValueError("Hugging Face did not return a file listing")
    files = []
    for item in siblings:
        if not isinstance(item, dict):
            continue
        path = item.get("rfilename")
        lfs = item.get("lfs")
        if not isinstance(path, str) or not path.lower().endswith(".gguf"):
            continue
        if filename and path != filename:
            continue
        # Large GGUF files must provide a real content hash, not an ETag of
        # unknown representation or a mutable branch name.
        if not isinstance(lfs, dict):
            continue
        checksum = lfs.get("sha256")
        size = lfs.get("size", item.get("size"))
        try:
            definition = validate_model_entry("candidate", {
                "name": path.split("/")[-1].removesuffix(".gguf")[:96],
                "repo": repo, "file": path, "revision": sha, "size": size,
                "sha256": checksum, "ctx": 4096,
            })
        except ValueError:
            continue
        files.append(definition)
        if len(files) >= MAX_OPTIONS:
            break
    files.sort(key=lambda obj: (obj["size"], obj["file"]))
    if filename and not files:
        raise ValueError("GGUF file not found with trustworthy SHA256/size metadata")
    if not files:
        raise ValueError("No downloadable SHA256-pinned GGUF files found")
    return {"repo": repo, "revision": sha, "files": files}
