"""Runtime extension that adds live updates to the admin request-log page.

Completed request logs remain in the existing Logs table. Requests that are still
being processed are tracked in a small SQLite ActiveRequests table and merged
into the live admin feed. This keeps the feature working across Uvicorn workers
without creating duplicate completed Logs rows.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

import codex_entrypoint as _codex
import main as _impl

app = _codex.app


def init_active_requests_schema() -> None:
    """Create the shared table used for requests that have not finished yet."""
    _impl.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _impl.get_db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ActiveRequests (
                request_id TEXT PRIMARY KEY,
                api_key_name TEXT,
                api_key_hash TEXT,
                requested_model TEXT,
                provider_name TEXT,
                provider_model TEXT,
                started_at TEXT NOT NULL,
                prompt TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _active_provider(payload: dict[str, Any]) -> tuple[str, str]:
    requested_model = str(payload.get("model") or "")
    if not requested_model:
        return "-", "-"
    try:
        endpoints = _impl.resolve_requested_model(requested_model)
    except Exception:
        return "-", requested_model
    if not endpoints:
        return "-", requested_model
    endpoint = endpoints[0]
    return (
        str(endpoint.get("name") or "-"),
        str(endpoint.get("model") or requested_model or "-"),
    )


def start_active_request(
    payload: dict[str, Any],
    authorization: str = "",
    request_id: Optional[str] = None,
) -> str:
    """Persist a request before it is sent to an upstream provider."""
    init_active_requests_schema()
    active_id = request_id or f"active-{uuid.uuid4().hex}"
    started_at = datetime.utcnow().isoformat()
    requested_model = str(payload.get("model") or "")
    provider_name, provider_model = _active_provider(payload)
    prompt = _impl.extract_prompt(payload)

    api_key_name = ""
    api_key_hash = ""
    if authorization.startswith("Bearer "):
        token = authorization.split("Bearer ", 1)[1].strip()
        if token:
            api_key_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
            try:
                record = _impl.get_api_key_record(token)
            except Exception:
                record = None
            if record is not None and "name" in record.keys():
                api_key_name = str(record["name"] or "")

    with _impl.get_db_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO ActiveRequests (
                request_id, api_key_name, api_key_hash, requested_model,
                provider_name, provider_model, started_at, prompt, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                active_id,
                api_key_name,
                api_key_hash,
                requested_model,
                provider_name,
                provider_model,
                started_at,
                _impl.truncate_text(prompt),
                started_at,
            ),
        )
        conn.commit()
    return active_id


def finish_active_request(request_id: Optional[str]) -> None:
    """Remove a running marker after the downstream response has fully finished."""
    if not request_id:
        return
    init_active_requests_schema()
    with _impl.get_db_connection() as conn:
        conn.execute(
            "DELETE FROM ActiveRequests WHERE request_id = ?",
            (request_id,),
        )
        conn.commit()


def list_active_requests() -> list[dict[str, Any]]:
    """Return a fresh snapshot of all currently running requests."""
    init_active_requests_schema()
    with _impl.get_db_connection() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT request_id, api_key_name, api_key_hash, requested_model,
                       provider_name, provider_model, started_at, prompt, created_at
                FROM ActiveRequests
                ORDER BY started_at DESC
                """
            ).fetchall()
        ]

    now = datetime.utcnow()
    for row in rows:
        try:
            started = datetime.fromisoformat(str(row.get("started_at") or ""))
            elapsed = max((now - started).total_seconds(), 0.0)
            duration = f"{elapsed:.2f}s"
        except Exception:
            duration = "-"

        row.update(
            {
                "id": row["request_id"],
                "group_name": row.get("requested_model"),
                "status_code": None,
                "status": "running",
                "running": True,
                "first_response_at": None,
                "ended_at": None,
                "first_response_ms": None,
                "total_ms": None,
                "duration": duration,
                "first_response": "-",
                "api_key_display": row.get("api_key_name") or "-",
                "output": "",
                "error": None,
                "type": "LLM",
            }
        )
        row["modal_payload"] = {
            key: value
            for key, value in row.items()
            if key != "modal_payload"
        }
    return rows


def list_logs_after_id(after_id: int = 0, limit: int = 100) -> dict[str, Any]:
    """Return newly completed logs and a snapshot of running requests."""
    safe_after_id = max(int(after_id), 0)
    safe_limit = min(max(int(limit), 1), 200)

    # list_logs returns newest-first. Reverse the filtered rows so the browser
    # can prepend them without losing the database order.
    recent_logs = _impl.list_logs(limit=200)
    new_logs = [
        log
        for log in reversed(recent_logs)
        if int(log.get("id") or 0) > safe_after_id
    ][:safe_limit]
    latest_id = max(
        [safe_after_id, *[int(log.get("id") or 0) for log in recent_logs]]
    )
    return {
        "logs": new_logs,
        "running": list_active_requests(),
        "latest_id": latest_id,
    }


async def admin_logs_live(
    after_id: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
) -> JSONResponse:
    return JSONResponse(
        list_logs_after_id(after_id=after_id, limit=limit),
        headers={"Cache-Control": "no-store"},
    )


if not any(
    getattr(route, "path", None) == "/admin/logs/live"
    for route in app.router.routes
):
    app.add_api_route(
        "/admin/logs/live",
        admin_logs_live,
        methods=["GET"],
        dependencies=[Depends(_impl.verify_admin)],
        response_class=JSONResponse,
        include_in_schema=False,
    )


_LIVE_LOGS_SCRIPT = r"""
<script id="aiproxy-live-logs">
(() => {
  const tableBody = document.querySelector('table tbody');
  if (!tableBody) return;

  let latestId = Math.max(
    0,
    ...Array.from(tableBody.querySelectorAll('tr[data-log-id]'))
      .map((row) => Number(row.dataset.logId || 0))
      .filter(Number.isFinite)
  );
  let failures = 0;
  let stopped = false;

  const header = document.querySelector('section > div:first-child');
  const status = document.createElement('div');
  status.id = 'liveLogsStatus';
  status.className = 'inline-flex items-center gap-2 rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1 text-xs font-semibold text-emerald-700';
  status.innerHTML = '<span class="h-2 w-2 rounded-full bg-emerald-500"></span><span>Live</span>';
  const headerRight = header?.querySelector(':scope > div:last-child');
  if (headerRight) {
    const wrapper = document.createElement('div');
    wrapper.className = 'flex flex-wrap items-center gap-2';
    headerRight.replaceWith(wrapper);
    wrapper.append(status, headerRight);
  }

  const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  })[char]);
  const valueOrDash = (value) => value === null || value === undefined || value === '' ? '-' : String(value);
  const shortTime = (value) => valueOrDash(value).replace('T', ' ');

  function setStatus(kind, text) {
    const styles = {
      live: ['border-emerald-200', 'bg-emerald-50', 'text-emerald-700', 'bg-emerald-500'],
      reconnecting: ['border-amber-200', 'bg-amber-50', 'text-amber-700', 'bg-amber-500'],
      paused: ['border-slate-200', 'bg-slate-50', 'text-slate-600', 'bg-slate-400'],
    };
    const selected = styles[kind] || styles.reconnecting;
    status.className = `inline-flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-semibold ${selected[0]} ${selected[1]} ${selected[2]}`;
    status.innerHTML = `<span class="h-2 w-2 rounded-full ${selected[3]}"></span><span>${escapeHtml(text)}</span>`;
  }

  function statusMarkup(log) {
    if (log.running) {
      return '<span class="inline-flex items-center gap-1.5 rounded-full bg-amber-50 px-2.5 py-1 text-[11px] font-semibold text-amber-700 ring-1 ring-amber-200"><span class="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-500"></span>Running</span>';
    }
    const ok = Number(log.status_code) >= 200 && Number(log.status_code) < 300;
    return ok
      ? '<span class="inline-flex rounded-full bg-emerald-50 px-2.5 py-1 text-[11px] font-semibold text-emerald-700 ring-1 ring-emerald-200">Success</span>'
      : '<span class="inline-flex rounded-full bg-rose-50 px-2.5 py-1 text-[11px] font-semibold text-rose-700 ring-1 ring-rose-200">Failure</span>';
  }

  function payloadId(log) {
    return String(log.request_id || log.id);
  }

  function createRow(log) {
    const row = document.createElement('tr');
    const key = payloadId(log);
    if (log.running) {
      row.dataset.runningId = key;
      row.className = 'group cursor-pointer bg-amber-50/40 transition hover:bg-amber-50 focus-within:bg-amber-50';
    } else {
      row.dataset.logId = String(log.id);
      row.className = 'group cursor-pointer transition hover:bg-sky-50/70 focus-within:bg-sky-50';
    }
    row.setAttribute('role', 'button');
    row.tabIndex = 0;
    row.addEventListener('click', () => window.openLogModal(key));
    row.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        window.openLogModal(key);
      }
    });

    const model = log.provider_model || log.requested_model || '-';
    row.innerHTML = `
      <td class="whitespace-nowrap px-4 py-3 font-medium text-slate-700">${escapeHtml(shortTime(log.created_at || log.started_at))}</td>
      <td class="whitespace-nowrap px-4 py-3"><span class="inline-flex items-center gap-1 rounded-full border border-sky-200 bg-sky-50 px-2 py-1 text-[11px] font-semibold text-sky-700">✦ ${escapeHtml(log.type || 'LLM')}</span></td>
      <td class="whitespace-nowrap px-4 py-3">${statusMarkup(log)}</td>
      <td class="whitespace-nowrap px-4 py-3 font-mono text-[11px] text-sky-700">${escapeHtml(log.request_id || `log-${log.id}`)}</td>
      <td class="whitespace-nowrap px-4 py-3 text-slate-700">${escapeHtml(log.provider_name || '-')}</td>
      <td class="whitespace-nowrap px-4 py-3"><span class="inline-flex max-w-48 items-center gap-1 truncate rounded-full bg-slate-100 px-2 py-1 font-medium text-slate-700" title="${escapeHtml(model)}">⚙ ${escapeHtml(model)}</span></td>
      <td class="whitespace-nowrap px-4 py-3 text-right tabular-nums text-slate-700">${escapeHtml(log.duration || '-')}</td>
      <td class="whitespace-nowrap px-4 py-3 text-right tabular-nums text-slate-700">${escapeHtml(log.first_response || '-')}</td>
      <td class="whitespace-nowrap px-4 py-3 font-mono text-[11px] text-slate-500">${escapeHtml(log.api_key_hash || '-')}</td>
      <td class="whitespace-nowrap px-4 py-3 text-slate-600">${escapeHtml(log.api_key_name || '-')}</td>
      <td class="max-w-sm truncate px-4 py-3 text-slate-500" title="${escapeHtml(log.prompt || '')}">${escapeHtml(log.prompt || '-')}</td>`;
    return row;
  }

  function storePayload(log) {
    const key = payloadId(log);
    let node = document.getElementById(`log-json-${key}`);
    if (!node) {
      node = document.createElement('script');
      node.type = 'application/json';
      node.id = `log-json-${key}`;
      document.body.appendChild(node);
    }
    node.textContent = JSON.stringify(log.modal_payload || log);
  }

  function updateCount() {
    const completed = tableBody.querySelectorAll('tr[data-log-id]').length;
    const running = tableBody.querySelectorAll('tr[data-running-id]').length;
    const badges = Array.from(document.querySelectorAll('section > div:first-child div'));
    const badge = badges.find((node) => /recent calls/.test(node.textContent || ''));
    if (badge) {
      badge.textContent = running
        ? `${completed} recent calls · ${running} running`
        : `${completed} recent calls`;
    }
  }

  function addLog(log) {
    if (!log || !log.id || document.querySelector(`tr[data-log-id="${Number(log.id)}"]`)) return;
    tableBody.querySelector('tr:not([data-log-id]):not([data-running-id])')?.remove();
    tableBody.prepend(createRow(log));
    storePayload(log);

    const rows = Array.from(tableBody.querySelectorAll('tr[data-log-id]'));
    for (const oldRow of rows.slice(200)) {
      document.getElementById(`log-json-log-${oldRow.dataset.logId}`)?.remove();
      document.getElementById(`log-json-${oldRow.dataset.logId}`)?.remove();
      oldRow.remove();
    }
  }

  function syncRunning(logs) {
    const activeKeys = new Set((logs || []).map(payloadId));

    for (const row of Array.from(tableBody.querySelectorAll('tr[data-running-id]'))) {
      if (!activeKeys.has(row.dataset.runningId)) {
        document.getElementById(`log-json-${row.dataset.runningId}`)?.remove();
        row.remove();
      }
    }

    for (const log of [...(logs || [])].reverse()) {
      const key = payloadId(log);
      const current = Array.from(tableBody.querySelectorAll('tr[data-running-id]'))
        .find((row) => row.dataset.runningId === key);
      current?.remove();
      tableBody.prepend(createRow(log));
      storePayload(log);
    }
    updateCount();
  }

  async function poll() {
    if (stopped) return;
    if (document.hidden) {
      setStatus('paused', 'Paused');
      window.setTimeout(poll, 1500);
      return;
    }

    try {
      const response = await fetch(`/admin/logs/live?after_id=${latestId}&limit=100`, {
        cache: 'no-store',
        credentials: 'same-origin',
        headers: {'Accept': 'application/json'},
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      for (const log of data.logs || []) addLog(log);
      latestId = Math.max(latestId, Number(data.latest_id || 0));
      const running = data.running || [];
      syncRunning(running);
      failures = 0;
      setStatus('live', running.length ? `Live · ${running.length} running` : 'Live');
    } catch (error) {
      failures += 1;
      setStatus('reconnecting', failures > 1 ? 'Reconnecting' : 'Retrying');
      console.warn('Live log update failed', error);
    }

    const delay = failures ? Math.min(1000 * (2 ** failures), 15000) : 1000;
    window.setTimeout(poll, delay);
  }

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) setStatus('reconnecting', 'Connecting');
  });
  window.addEventListener('beforeunload', () => { stopped = true; });
  poll();
})();
</script>
"""


@app.middleware("http")
async def track_running_requests(request: Request, call_next: Any) -> Response:
    """Track chat requests until their response body has been fully consumed."""
    should_track = (
        request.method.upper() == "POST"
        and request.url.path == "/v1/chat/completions"
    )
    active_id: Optional[str] = None

    if should_track:
        try:
            body = await request.body()
            payload = json.loads(body) if body else {}
            if isinstance(payload, dict) and payload.get("background") is not True:
                active_id = start_active_request(
                    payload,
                    request.headers.get("Authorization", ""),
                )
        except Exception:
            # Logging must never be able to break the proxy request.
            active_id = None

    try:
        response = await call_next(request)
    except Exception:
        finish_active_request(active_id)
        raise

    original_iterator = getattr(response, "body_iterator", None)
    if active_id and original_iterator is not None:
        async def tracked_body() -> Any:
            try:
                async for chunk in original_iterator:
                    yield chunk
            finally:
                finish_active_request(active_id)

        response.body_iterator = tracked_body()
    else:
        finish_active_request(active_id)
    return response


@app.middleware("http")
async def inject_live_logs_client(request: Request, call_next: Any) -> Response:
    response = await call_next(request)
    if request.url.path != "/admin/logs":
        return response
    if "text/html" not in response.headers.get("content-type", "").lower():
        return response

    body = b"".join([chunk async for chunk in response.body_iterator])
    html = body.decode("utf-8", errors="replace")
    if "id=\"aiproxy-live-logs\"" not in html:
        html = html.replace("</body>", _LIVE_LOGS_SCRIPT + "\n</body>")

    headers = dict(response.headers)
    headers.pop("content-length", None)
    return HTMLResponse(
        content=html,
        status_code=response.status_code,
        headers=headers,
    )
