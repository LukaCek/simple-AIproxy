"""Pinned, administrator-approved GGUF catalogue. No user-supplied download URLs."""
CATALOG = {
    "qwen35-0.8b-q4": {
        "name": "Qwen3.5 0.8B Q4_K_M", "repo": "unsloth/Qwen3.5-0.8B-GGUF",
        "file": "Qwen3.5-0.8B-Q4_K_M.gguf", "revision": "463d2f83eebe018294a9fa14bb50fc6cd90b5a36",
        "size": 533000000,
        "sha256": "bd258782e35f7f458f8aced1adc053e6e92e89bc735ba3be89d38a06121dc517",
        "ctx": 4096,
    },
    "qwen35-2b-q4": {
        "name": "Qwen3.5 2B Q4_K_M", "repo": "unsloth/Qwen3.5-2B-GGUF",
        "file": "Qwen3.5-2B-Q4_K_M.gguf", "revision": "1c466474d208da1a7c4b8cb87ebcdac78f160e34",
        "size": 1280000000,
        "sha256": "aaf42c8b7c3cab2bf3d69c355048d4a0ee9973d48f16c731c0520ee914699223",
        "ctx": 4096,
    },
    "qwen35-4b-q4": {
        "name": "Qwen3.5 4B Q4_K_M", "repo": "unsloth/Qwen3.5-4B-GGUF",
        "file": "Qwen3.5-4B-Q4_K_M.gguf", "revision": "16e0b78fbcd71f5624ada4b3b803c6302c1b8224",
        "size": 2740000000,
        "sha256": "00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4",
        "ctx": 4096,
    },
    "qwen35-9b-q4": {
        "name": "Qwen3.5 9B Q4_K_M", "repo": "unsloth/Qwen3.5-9B-GGUF",
        "file": "Qwen3.5-9B-Q4_K_M.gguf",
        "revision": "dcb5858fb5302827076259ceafff1690cb56d736",
        "size": 5680000000,
        "sha256": "03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8",
        "ctx": 4096,
    },
}


# Bundled entries remain available offline and cannot be overwritten through
# the admin UI. Additional models are stored in the proxy DB, then distributed
# as a validated, checksum-pinned catalog over authenticated WSS.
import re

BUILTIN_CATALOG = {key: dict(value) for key, value in CATALOG.items()}
MAX_CATALOG_MODELS = 64
MAX_MODEL_SIZE = 512 * 1024**3
_MODEL_ID = re.compile(r"[a-z][a-z0-9._-]{1,63}\Z")
_HF_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")
_GGUF_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def validate_model_entry(model_id: str, model: dict) -> dict:
    """Reject arbitrary links, paths, mutable revisions and unpinned blobs.

    Only public GGUF files in a Hugging Face repository at an immutable commit
    can be downloaded. The worker independently validates proxy WSS metadata.
    """
    if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id):
        raise ValueError("Invalid model ID")
    if not isinstance(model, dict):
        raise ValueError("Invalid model metadata")
    name = model.get("name")
    repo = model.get("repo")
    filename = model.get("file")
    revision = model.get("revision")
    size = model.get("size")
    checksum = model.get("sha256")
    ctx = model.get("ctx")
    if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 96
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)):
        raise ValueError("Invalid model name")
    if (not isinstance(repo, str) or len(repo) > 192 or len(repo.split("/")) != 2
            or any(not _HF_COMPONENT.fullmatch(part) or part in (".", "..")
                   for part in repo.split("/"))):
        raise ValueError("Only Hugging Face owner/repository IDs are supported")
    if (not isinstance(filename, str) or not filename.lower().endswith(".gguf")
            or len(filename) > 240 or not filename or filename.startswith("/")
            or any(not _GGUF_COMPONENT.fullmatch(part) or part in (".", "..")
                   for part in filename.split("/"))):
        raise ValueError("Invalid GGUF filename")
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Model revision must be an immutable 40-character commit SHA")
    if not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
        raise ValueError("Model must have a verified SHA256 checksum")
    if type(size) is not int or not 1 <= size <= MAX_MODEL_SIZE:
        raise ValueError("Invalid model size")
    if type(ctx) is not int or not 256 <= ctx <= 262144:
        raise ValueError("Invalid context length")
    return {
        "name": name.strip(), "repo": repo, "file": filename,
        "revision": revision, "size": size, "sha256": checksum, "ctx": ctx,
    }


def apply_custom_catalog(custom: dict[str, dict]) -> None:
    """Update the shared catalog IN PLACE; imported references stay live."""
    if not isinstance(custom, dict) or len(custom) + len(BUILTIN_CATALOG) > MAX_CATALOG_MODELS:
        raise ValueError("Too many custom catalog entries")
    verified = {}
    for mid, definition in custom.items():
        if mid in BUILTIN_CATALOG:
            raise ValueError("Cannot replace bundled catalog models")
        verified[mid] = validate_model_entry(mid, definition)
    CATALOG.clear()
    CATALOG.update(BUILTIN_CATALOG)
    CATALOG.update(verified)
