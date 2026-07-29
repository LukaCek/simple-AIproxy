"""Runtime extension that adds live updates to the admin request-log page.

The existing application writes completed request records to SQLite. This module
adds a small authenticated polling endpoint and injects a browser client into
/admin/logs so newly written records appear without a page refresh.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

import codex_entrypoint as _codex
import main as _impl

app = _codex.app


def list_logs_after_id(after_id: int = 0, limit: int = 100) -> dict[str, Any]:
    """Return newly completed logs in chronological order."""
    safe_after_id = max(int(after_id), 0)
    safe_limit = min(max(int(limit), 1), 200)

    # list_logs returns newest-first. Read the same bounded history used by the
    # page, filter it, then reverse it so the browser can prepend in order.
    recent_logs = _impl.list_logs(limit=200)
    new_logs = [
        log
        for log in reversed(recent_logs)
        if int(log.get("id") or 0) > safe_after_id
    ][:safe_limit]
    latest_id = max(
        [safe_after_id, *[int(log.get("id") or 0) for log in recent_logs]]
    )
    return {"logs": new_logs, "latest_id": latest_id}


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
    const ok = Number(log.status_code) >= 200 && Number(log.status_code) < 300;
    return ok
      ? '<span class="inline-flex rounded-full bg-emerald-50 px-2.5 py-1 text-[11px] font-semibold text-emerald-700 ring-1 ring-emerald-200">Success</span>'
      : '<span class="inline-flex rounded-full bg-rose-50 px-2.5 py-1 text-[11px] font-semibold text-rose-700 ring-1 ring-rose-200">Failure</span>';
  }

  function createRow(log) {
    const row = document.createElement('tr');
    row.dataset.logId = String(log.id);
    row.className = 'group cursor-pointer transition hover:bg-sky-50/70 focus-within:bg-sky-50';
    row.setAttribute('role', 'button');
    row.tabIndex = 0;
    row.addEventListener('click', () => window.openLogModal(String(log.id)));
    row.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        window.openLogModal(String(log.id));
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
    let node = document.getElementById(`log-json-${log.id}`);
    if (!node) {
      node = document.createElement('script');
      node.type = 'application/json';
      node.id = `log-json-${log.id}`;
      document.body.appendChild(node);
    }
    node.textContent = JSON.stringify(log.modal_payload || log);
  }

  function updateCount() {
    const count = tableBody.querySelectorAll('tr[data-log-id]').length;
    const badges = Array.from(document.querySelectorAll('section > div:first-child div'));
    const badge = badges.find((node) => /recent calls/.test(node.textContent || ''));
    if (badge) badge.textContent = `${count} recent calls`;
  }

  function addLog(log) {
    if (!log || !log.id || document.querySelector(`tr[data-log-id="${Number(log.id)}"]`)) return;
    tableBody.querySelector('tr:not([data-log-id])')?.remove();
    tableBody.prepend(createRow(log));
    storePayload(log);

    const rows = Array.from(tableBody.querySelectorAll('tr[data-log-id]'));
    for (const oldRow of rows.slice(200)) {
      document.getElementById(`log-json-${oldRow.dataset.logId}`)?.remove();
      oldRow.remove();
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
      failures = 0;
      setStatus('live', 'Live');
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
