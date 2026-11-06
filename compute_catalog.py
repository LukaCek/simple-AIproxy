"""Pinned, administrator-approved GGUF catalogue. No user-supplied download URLs."""
CATALOG = {
    "qwen35-4b-q4": {
        "name": "Qwen3.5 4B Q4_K_M", "repo": "unsloth/Qwen3.5-4B-GGUF",
        "file": "Qwen3.5-4B-Q4_K_M.gguf", "revision": "main",
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
