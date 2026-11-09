"""Explicit allowlisted SimpleAI MCP tools.

Tools reuse live proxy/compute logic and database; there is no shell command,
filesystem browsing, Docker socket or generic admin API forwarding.
Never return API keys, OAuth tokens, worker secrets or raw prompts.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import re
import secrets
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

import simpleai_mcp_logging as runtime_log


def spec(name: str, description: str, properties: dict, required=(), *,
         write=False, destructive=False, open_world=False) -> dict:
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": list(required), "additionalProperties": False},
            "annotations": {"readOnlyHint": not write,
                            "destructiveHint": destructive,
                            "openWorldHint": open_world,
                            "idempotentHint": not write}}


def string(description: str, *, maxlen: int = 256) -> dict:
    return {"type": "string", "description": description, "maxLength": maxlen}


def bounded(description: str, *, top=100, low=1) -> dict:
    return {"type": "integer", "minimum": low, "maximum": top,
            "description": description}


LIMIT = {"limit": bounded("Newest records, pagination via before_id where offered", top=200)}
CONFIRM = {"confirm": {"type": "boolean", "description":
                      "Must be true: operator explicitly approves this change."}}


TOOLS = [
    spec("system_status", "Inspect proxy and database health, counts, workers and recent failure rates; secrets excluded.", {}),
    spec("ui_state", "Inspect the actual backend state powering the Compute Nodes, Groups, Providers and Playground pages; identify stale references and mismatches. Not a screenshot.", {}),
    spec("list_providers", "Read providers, Codex connection state, models and modes without keys, endpoints or OAuth credentials.", {}),
    spec("list_groups", "Read all routing groups with strategies and members without credentials.", {}),
    spec("list_models", "Show provider, routing-group and dynamic GGUF model catalog, policy and real worker availability.", {}),
    spec("list_workers", "List all registered workers, current health, resource caps, model test outcomes and trust flags.", {}),
    spec("get_worker", "Inspect one worker's model tests, capabilities, hardware limits and online status.", {"node_id": string("Worker UUID, as returned by list_workers", maxlen=48)}, ("node_id",)),
    spec("worker_events", "Read central structured worker diagnostics, errors, disconnects and model test history. May be delayed until an offline worker reconnects.",
         {"node_id": string("Optional worker ID; empty means all",maxlen=48),
          "level": {"type": "string", "enum": ["", "info", "warning", "error"]}, **LIMIT}),
    spec("request_logs", "Inspect request log metadata, timing, models and provider routing without API keys, prompts, completions or raw exception messages.",
         {"model": string("Optional requested model filter",maxlen=128),
          "errors_only": {"type":"boolean"}, "before_id": bounded("Optional earlier log ID for pagination",top=2147483647), **LIMIT}),
    spec("provider_attempts", "Inspect provider/Codex routing attempts, retry actions, status and structured error categories; never credentials.",
         {"request_id": string("Optional request ID",maxlen=64),
          "provider": string("Optional provider name",maxlen=128), **LIMIT}),
    spec("proxy_runtime_logs", "Inspect bounded in-memory proxy Python error categories (not raw Docker stdout/stderr).",
         {"level":{"type":"string","enum":["", "warning","error","critical"]},**LIMIT}),
    spec("error_overview", "Aggregate recent request, provider and worker errors for quick debugging.", {}),
    spec("codex_status", "Inspect Codex provider account availability and recent error categories; OAuth tokens withheld.", {}),
    spec("get_operation", "Read the status of an earlier worker model action.", {"operation_id":string("Operation ID",maxlen=40)}, ("operation_id",)),
    spec("inspect_gguf", "Inspect public Hugging Face GGUF repository using pinned SHA256/commit metadata; does not download weights.",
         {"source":string("Hugging Face repository or GGUF link",maxlen=350)}, ("source",),open_world=True),
    spec("playground_targets", "List models/groups available for the same user-facing Playground.", {}),
    spec("add_gguf_model", "Register a verified GGUF model and optionally enable self-tests. Downloads happen on eligible workers, not this server.",
         {"source":string("Public Hugging Face GGUF repo or link",maxlen=350),
          "file":string("Exact GGUF filename returned by inspect_gguf",maxlen=240),
          "display_name":string("Optional human display name",maxlen=100),
          "context_tokens":bounded("Context window",top=32768,low=256),
          "enable":{"type":"boolean"},**CONFIRM},("source","file","confirm"),
         write=True,open_world=True),
    spec("set_model_policy", "Enable chosen model IDs globally; triggers worker policy sync and tests.",
         {"models":{"type":"array","items":string("Approved model ID",maxlen=80),"maxItems":80},
          **CONFIRM},("models","confirm"),write=True),
    spec("set_placement", "Choose automatic or manual worker placement.", 
         {"mode":{"type":"string","enum":["manual","auto"]},**CONFIRM},("mode","confirm"),write=True),
    spec("worker_model_action", "Install, test, load or unload a model on a connected worker; delete requires extra explicit destructive flag.",
         {"node_id":string("Registered worker ID",maxlen=48),
          "model":string("Approved model ID",maxlen=80),
          "action":{"type":"string","enum":["install","test","load","unload","delete"]},
          "confirm_delete":{"type":"boolean"},**CONFIRM},
         ("node_id","model","action","confirm"),write=True,destructive=True),
    spec("save_group", "Create/update routing group; existing provider/model and cycle validation preserved.",
         {"name":string("Group name",maxlen=100),
          "description":string("Description",maxlen=300),
          "strategy":{"type":"string","enum":["round_robin","fallback"]},
          "members":{"type":"array","items":{"type":"object"},"minItems":1,"maxItems":32},
          "original_name":string("Existing group name if editing",maxlen=100),**CONFIRM},
         ("name","strategy","members","confirm"),write=True),
    spec("run_playground", "Submit a bounded real private test through SimpleAI /v1/chat/completions; may consume provider credits. Never expose the temporary API key.",
         {"model":string("Model ID or routing group",maxlen=128),
          "prompt":string("Short test prompt; stored in the usual proxy request log",maxlen=1024),
          "max_tokens":bounded("Maximum tokens (up to 256)",top=256),
          **CONFIRM},("model","prompt","confirm"),write=True,open_world=True),
]
TOOL_BY_NAME = {t["name"]: t for t in TOOLS}


def _limit(args: dict) -> int:
    v = args.get("limit", 50)
    if type(v) is not int or not 1 <= v <= 200:
        raise ValueError("limit must be 1 to 200")
    return v


def _read_safe(value: object, limit: int = 120) -> str:
    """Metadata only. Never use this on raw log output or key material."""
    s = str(value or "")[:limit]
    s = re.sub(r"(?i)(bearer|api[_-]?key|token|password|secret|authorization)\s*[:=]\s*[^\s,;]+",
               r"\1=[REDACTED]", s)
    s = re.sub(r"(?:sk-|ghp_|gho_|github_pat_)[A-Za-z0-9_-]{8,}", "[REDACTED]", s)
    return s


def _code(value: object) -> str:
    # Fixed allowlist diagnostics rather than forwarding freeform exceptions.
    from compute_worker_diagnostics import error_code
    return error_code(value)


def compute_catalog_ids() -> set[str]:
    import compute_plane
    return set(compute_plane.CATALOG)


def _public_worker(node: dict) -> dict:
    from compute_plane import _public_node
    d = _public_node(node)
    cap = d.get("models", {}).get("capabilities", {})
    hardware = d.get("hardware", {})
    models = d.get("models", {})
    return {
        "id": d.get("id"), "name": _read_safe(d.get("name")),
        "online": bool(d.get("online")), "revoked": bool(d.get("revoked")),
        "allow_private": bool(d.get("allow_private")),
        "last_seen": d.get("last_seen"),
        "hardware": {
            "arch": _read_safe(hardware.get("arch"), 40),
            "os": _read_safe(hardware.get("os"), 40),
            "cpus": hardware.get("cpus"),
            "memory_limit_bytes": hardware.get("memory_limit_bytes"),
            "memory_available_bytes": hardware.get("memory_available_bytes"),
            "disk_free_bytes": hardware.get("disk_free_bytes"),
            "backend": _read_safe(hardware.get("backend"), 50),
            "catalog_protocol": hardware.get("catalog_protocol")
                if type(hardware.get("catalog_protocol")) is int else None,
            "build_revision": _read_safe(hardware.get("build_revision"), 64),
        },
        "models": {"active": models.get("active")
                       if models.get("active") in compute_catalog_ids() else None,
                   "ready": bool(models.get("ready")),
                   "installed": models.get("installed", []),
                   "busy": bool(models.get("busy")), "disk_limit_bytes": models.get("disk_limit_bytes"),
                   "disk_used_bytes": models.get("disk_used_bytes"),
                   "policy_revision": models.get("policy_revision")
                       if type(models.get("policy_revision")) is int else None,
                   "capabilities": {mid: {
                       "status": state.get("status"), "reason_code": _code(state.get("reason")),
                       "test_seconds": state.get("test_seconds"),
                       "tested_at": state.get("tested_at"),
                       "benchmark": state.get("benchmark", {}),
                   } for mid, state in cap.items() if isinstance(state, dict)}}}


def _workers(core, compute) -> list[dict]:
    compute._setup()
    with core.get_db_connection() as db:
        rows = db.execute("SELECT * FROM ComputeNodes ORDER BY created_at DESC LIMIT 250").fetchall()
    return [_public_worker(dict(row)) for row in rows]


def _groups(core) -> list[dict]:
    with core.config_lock:
        entries = core.config_data.get("groups", {})
        return [{"name": _read_safe(name), "description": _read_safe(group.get("description"), 240),
                 "strategy": _read_safe(group.get("strategy"), 40),
                 "members": [{k: _read_safe(v) for k,v in member.items()
                              if k in ("group", "provider", "model")}
                             for member in group.get("members", []) if isinstance(member, dict)]}
                for name, group in entries.items() if isinstance(group, dict)]


def _providers(core) -> list[dict]:
    with core.config_lock:
        entries = core.config_data.get("providers", [])
        return [{"name": _read_safe(p.get("name")), "api_mode": _read_safe(p.get("api_mode", "openai_chat_completions")),
                 "description": _read_safe(p.get("description"), 200),
                 "models": [_read_safe(x, 128) for x in p.get("models", [])][:100],
                 "oauth": bool(p.get("oauth")),
                 "oauth_connected": core.oauth_provider_connected(p) if p.get("oauth") else None,
                 "needs_reauth": bool(p.get("oauth_reauth_required"))}
                for p in entries if isinstance(p, dict)]


def _catalog(compute) -> dict:
    compute._setup()
    return {mid: {k: definition.get(k) for k in
                  ("name", "repo", "file", "revision", "size", "ctx", "sha256")}
            for mid, definition in list(compute.CATALOG.items())[:150]}


def _logs(core, args) -> list[dict]:
    where, params = [], []
    if args.get("errors_only"):
        where.append("(status_code IS NULL OR status_code >= 400)")
    if args.get("model"):
        where.append("requested_model = ?")
        params.append(args["model"])
    if args.get("before_id"):
        if type(args["before_id"]) is not int or args["before_id"] < 1:
            raise ValueError("Invalid before_id")
        where.append("id < ?")
        params.append(args["before_id"])
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(_limit(args))
    with core.get_db_connection() as db:
        rows = db.execute(
            "SELECT id, request_id, requested_model, group_name, provider_name, "
            "provider_model, status_code, started_at, ended_at, first_response_ms, "
            "total_ms, created_at, error, api_key_name "
            "FROM Logs" + clause + " ORDER BY id DESC LIMIT ?", params).fetchall()
    return [{
        "id": r["id"], "request_id": r["request_id"],
        "requested_model": _read_safe(r["requested_model"]),
        "group_name": _read_safe(r["group_name"]),
        "provider": _read_safe(r["provider_name"]),
        "provider_model": _read_safe(r["provider_model"]),
        "api_key_label": _read_safe(r["api_key_name"]),  # only user-assigned display label
        "status_code": r["status_code"], "started_at": r["started_at"],
        "ended_at": r["ended_at"], "first_response_ms": r["first_response_ms"],
        "total_ms": r["total_ms"], "created_at": r["created_at"],
        "error_category": _code(r["error"]) if r["error"] else "",
    } for r in rows]


def _attempts(core, args) -> list[dict]:
    where, params = [], []
    if args.get("request_id"):
        where.append("request_id = ?")
        params.append(args["request_id"])
    if args.get("provider"):
        where.append("provider_name = ?")
        params.append(args["provider"])
    query = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(_limit(args))
    with core.get_db_connection() as db:
        rows = db.execute("SELECT id,request_id,attempt_no,requested_model,provider_name,"
                          "provider_model,status_code,action,error,total_ms,created_at "
                          "FROM ProviderAttempts" + query + " ORDER BY id DESC LIMIT ?", params).fetchall()
    return [{"id":r["id"],"request_id":r["request_id"],"attempt_no":r["attempt_no"],
             "model":_read_safe(r["requested_model"]), "provider":_read_safe(r["provider_name"]),
             "provider_model":_read_safe(r["provider_model"]),
             "status_code":r["status_code"],"action":_read_safe(r["action"]),
             "error_category":_code(r["error"]) if r["error"] else "",
             "total_ms":r["total_ms"],"created_at":r["created_at"]} for r in rows]


def _events(core, args) -> list[dict]:
    where = ["created_at>=?"]
    params = [int(time.time()) - 30 * 86400]
    if args.get("node_id"):
        where.append("node_id=?")
        params.append(args["node_id"])
    if args.get("level"):
        if args["level"] not in ("info","warning","error"):
            raise ValueError("Invalid level")
        where.append("status=?")
        params.append(args["level"])
    params.append(_limit(args))
    with core.get_db_connection() as db:
        rows = db.execute("SELECT id,node_id,event,model,status,detail,created_at "
                          "FROM ComputeEvents WHERE " + " AND ".join(where) +
                          " ORDER BY id DESC LIMIT ?", params).fetchall()
    return [{"id":r["id"], "node_id":r["node_id"], "event":_read_safe(r["event"]),
             "model":_read_safe(r["model"]), "status":_read_safe(r["status"]),
             "detail":_read_safe(r["detail"],220), "created_at":r["created_at"]}
            for r in rows]


class _Body:
    def __init__(self, data: dict): self.data = data
    async def json(self) -> dict: return self.data


def _confirm(args):
    if args.get("confirm") is not True:
        raise ValueError("confirm=true is required for modifications")


async def _playground(core, app, args) -> dict:
    _confirm(args)
    model = args.get("model")
    prompt = args.get("prompt")
    if (not isinstance(model,str) or not 1 <= len(model) <= 128
            or not isinstance(prompt,str) or not 1 <= len(prompt) <= 1024):
        raise ValueError("Invalid model or short test prompt")
    max_tokens = args.get("max_tokens", 96)
    if type(max_tokens) is not int or not 1 <= max_tokens <= 256:
        raise ValueError("max_tokens must be 1..256")
    ephemeral_key = secrets.token_urlsafe(36)
    now = datetime.now(timezone.utc).isoformat()
    with core.get_db_connection() as db:
        cur = db.execute("INSERT INTO API_Keys(name,key,created_at) VALUES(?,?,?)",
                         ("MCP one-time Playground test", ephemeral_key, now))
        key_id = cur.lastrowid
    start = time.monotonic()
    try:
        # ASGI loopback executes the same complete proxy routing as UI Playground,
        # no external SSRF host and no permanent credential. Always private by default.
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,base_url="http://simpleai-mcp-internal",
                                     timeout=90, follow_redirects=False) as client:
            response = await asyncio.wait_for(client.post(
                "/v1/chat/completions",
                headers={"Authorization":"Bearer "+ephemeral_key},
                json={"model": model, "stream":False, "think":False,
                      "aiproxy_visibility":"private",
                      "max_tokens":max_tokens,
                      "messages":[{"role":"user","content":prompt}]}), timeout=100)
        try:
            body=response.json()
        except ValueError:
            body={}
        if response.status_code != 200:
            return {"status_code":response.status_code,"model":model,
                    "elapsed_ms":int((time.monotonic()-start)*1000),
                    "error_category":_code(body.get("detail") if isinstance(body,dict) else "")}
        choices = body.get("choices",[]) if isinstance(body,dict) else []
        text = ""
        if choices and isinstance(choices[0],dict):
            text = str(choices[0].get("message",{}).get("content") or "")[:2500]
        return {"status_code":200,"model":model,"elapsed_ms":int((time.monotonic()-start)*1000),
                "text":text, "finish_reason":choices[0].get("finish_reason") if choices else None}
    finally:
        with core.get_db_connection() as db:
            db.execute("DELETE FROM API_Keys WHERE id=?", (key_id,))


async def execute(name: str, args: dict, app) -> dict:
    import main_impl as core
    import compute_plane as compute
    if name not in TOOL_BY_NAME or not isinstance(args, dict):
        raise ValueError("Unknown MCP tool")
    allowed=set(TOOL_BY_NAME[name]["inputSchema"]["properties"])
    if not set(args) <= allowed:
        raise ValueError("Unexpected tool argument")
    for key in TOOL_BY_NAME[name]["inputSchema"]["required"]:
        if key not in args:
            raise ValueError("Missing required argument: " + key)
    if name == "system_status":
        nodes=_workers(core,compute)
        with core.get_db_connection() as db:
            count=db.execute("SELECT count(*) FROM Logs").fetchone()[0]
            last=db.execute("SELECT count(*) FROM Logs WHERE status_code >= 400 AND id > "
                            "(SELECT max(id)-200 FROM Logs)").fetchone()[0]
        return {"status":"ok","service":"SimpleAI Proxy","time_utc":datetime.now(timezone.utc).isoformat(),
                "registered_workers":len(nodes),"online_workers":sum(n["online"] for n in nodes),
                "models":len(compute.CATALOG),"groups":len(_groups(core)),
                "providers":len(_providers(core)),
                "logged_requests":count,"failures_in_last_200_requests":last,
                "mcp_access":"separate revocable scoped credentials; no secret export"}
    if name == "list_providers": return {"providers":_providers(core)}
    if name == "list_groups": return {"groups":_groups(core)}
    if name == "list_workers": return {"workers":_workers(core,compute)}
    if name == "get_worker":
        found=next((n for n in _workers(core,compute) if n["id"]==args.get("node_id")),None)
        if found is None: raise ValueError("Worker not found")
        return {"worker":found}
    if name == "worker_events": return {"events":_events(core,args)}
    if name == "request_logs": return {"logs":_logs(core,args)}
    if name == "provider_attempts": return {"attempts":_attempts(core,args)}
    if name == "proxy_runtime_logs": return runtime_log.events(level=args.get("level",""),limit=_limit(args))
    if name == "error_overview":
        return {"recent_request_failures":_logs(core,{"limit":35,"errors_only":True}),
                "recent_provider_attempts":_attempts(core,{"limit":35}),
                "worker_diagnostics":[e for e in _events(core,{"limit":90})
                                      if e["status"] in ("warning","error","failed")],
                "proxy_runtime":runtime_log.events(level="error",limit=30)["events"]}
    if name == "codex_status":
        return {"providers":[p for p in _providers(core)
                             if "codex" in str(p["api_mode"]).lower() or "codex" in p["name"].lower()],
                "recent_provider_attempts":[a for a in _attempts(core,{"limit":150})
                                            if "codex" in a["provider"].lower()][:50]}
    if name in ("list_models","playground_targets"):
        policy=compute._model_policy()
        groups=_groups(core)
        providers=_providers(core)
        available=[mid for mid in compute.CATALOG if compute.endpoints("local/"+mid)]
        return {"providers":providers,"groups":groups,
                "compute_catalog":_catalog(compute),
                "policy":policy,"routable_local_models":["local/"+x for x in available],
                "local_auto_available":bool(available)}
    if name == "ui_state":
        groups=_groups(core)
        providers=_providers(core)
        nodes=_workers(core,compute)
        policy=compute._model_policy()
        provider_models={p["name"]:set(p["models"]) for p in providers}
        group_names={g["name"] for g in groups}
        warnings=[]
        for group in groups:
            for member in group["members"]:
                if "group" in member and member["group"] not in group_names:
                    warnings.append({"group":group["name"],"issue":"missing nested group"})
                if "provider" in member and member.get("model") not in provider_models.get(member["provider"],set()):
                    warnings.append({"group":group["name"],"issue":"missing provider/model"})
        for mid in policy["models"]:
            if mid not in compute.CATALOG:
                warnings.append({"model":mid,"issue":"enabled but missing from catalog"})
        return {"pages":["/admin/compute-nodes","/admin/groups","/admin/providers",
                          "/admin/codex-usage","/admin/playground","/admin/logs"],
                "backing_state":{"workers":nodes,"groups":groups,"providers":providers,
                                 "model_policy":policy,"catalog_size":len(compute.CATALOG)},
                "consistency_warnings":warnings,
                "limitation":"Server-side UI data consistency, not browser pixels or client-side JS"}
    if name == "get_operation":
        op_id=args.get("operation_id")
        if not isinstance(op_id,str) or not re.fullmatch(r"[a-f0-9]{32}",op_id):
            raise ValueError("Invalid operation ID")
        with core.get_db_connection() as db:
            row=db.execute("SELECT id,node_id,action,status,created_at,updated_at "
                           "FROM ComputeOperations WHERE id=?",(op_id,)).fetchone()
        if not row: raise ValueError("Operation not found")
        return {"operation":dict(row)}
    if name == "inspect_gguf":
        from compute_hf_catalog import lookup_gguf_models
        src=args.get("source")
        if not isinstance(src,str) or len(src)>350: raise ValueError("Invalid source")
        inspected=await lookup_gguf_models(src)
        return {"repository":inspected.get("repo"),"revision":inspected.get("revision"),
                "files":[{"file":f.get("file"),"size":f.get("size"),
                          "sha256":f.get("sha256"),"revision":f.get("revision")}
                         for f in inspected.get("files",[])][:70]}
    if name == "add_gguf_model":
        _confirm(args)
        source=args.get("source")
        filename=args.get("file")
        if not isinstance(source,str) or not isinstance(filename,str):
            raise ValueError("Invalid source or filename")
        result=await compute.add_custom_model(_Body({
            "source":source,"file":filename,"name":args.get("display_name") or None,
            "ctx":args.get("context_tokens",4096),"enable":args.get("enable",True)}))
        return {"registered":result}
    if name == "set_model_policy":
        _confirm(args)
        return {"policy":await compute.change_model_policy(_Body({"models":args["models"]}))}
    if name == "set_placement":
        _confirm(args)
        return await compute.update_placement(_Body({"mode":args["mode"]}))
    if name == "worker_model_action":
        _confirm(args)
        action=args.get("action")
        if action not in ("install","test","load","unload","delete"):
            raise ValueError("Invalid action")
        if action=="delete" and args.get("confirm_delete") is not True:
            raise ValueError("confirm_delete=true is required to delete cached weights")
        result=await compute.start_operation(args.get("node_id"),_Body({
            "action":action,"model":args.get("model")}))
        return {"operation":result,"node_id":args.get("node_id"),
                "model":args.get("model"),"action":action}
    if name == "save_group":
        _confirm(args)
        members=args.get("members")
        if (not isinstance(members,list) or len(members)>32
                or not all(isinstance(m,dict) and
                           (set(m)=={"group"} or set(m)=={"provider","model"})
                           and all(isinstance(v,str) and len(v)<=100 for v in m.values())
                           for m in members)):
            raise ValueError("Members must reference a single existing group or provider/model")
        core.save_group(args["name"],args.get("description",""),
                        args["strategy"],members,args.get("original_name") or None)
        return {"saved":True,"name":args["name"],"groups":_groups(core)}
    if name == "run_playground":
        return await _playground(core,app,args)
    raise ValueError("Unknown tool")
