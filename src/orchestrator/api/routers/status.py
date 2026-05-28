"""F10 — single-file HTML status page at GET /.

Bible §9.3 contract:
- Single static HTML (< 20 KB gzipped)
- Bearer token entered via sessionStorage + prompt() at first load
- 5 panels: Health, Platforms, Active Jobs, Stats, Recent Errors
- Every status indicator uses color + icon + text label
  (operator is colorblind per Intake §9 — text label is the hard
  constraint, color and icon are convenience)
- Polling: /health, /platforms, /jobs?active every 2 s;
  recent errors every 10 s
- Back off to 10 s on 5xx until success

The HTML is embedded as a module-level constant so deployment doesn't
need to ship a separate assets directory. UAT-3 S2-B-style: serves
the operator's own UI, no public surface.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["status"])


_STATUS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex,nofollow">
<title>lancache-orchestrator</title>
<style>
  :root {
    --bg: #0f0f17;
    --bg-panel: #1e1e2e;
    --bg-elev: #313244;
    --text: #cdd6f4;
    --text-dim: #a6adc8;
    --border: #45475a;
    --ok: #a6e3a1;
    --warn: #f9e2af;
    --error: #f38ba8;
    --unknown: #6c7086;
    --accent: #89b4fa;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
    padding: 16px;
    max-width: 1200px;
    margin: 0 auto;
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 24px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
  }
  h1 { font-size: 1.4rem; color: var(--accent); }
  .meta { color: var(--text-dim); font-size: 0.85rem; }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: 16px;
  }
  section.panel {
    background: var(--bg-panel);
    border-radius: 8px;
    padding: 16px;
    border-left: 4px solid var(--border);
  }
  section.panel.ok { border-left-color: var(--ok); }
  section.panel.warn { border-left-color: var(--warn); }
  section.panel.error { border-left-color: var(--error); }
  section.panel.unknown { border-left-color: var(--unknown); }
  h2 { font-size: 1rem; margin-bottom: 12px; color: var(--text); }
  .pill {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 0.75rem;
    font-weight: 600;
    margin-left: 8px;
    background: var(--bg-elev);
    color: var(--text);
  }
  .pill.ok { background: var(--ok); color: #1e1e2e; }
  .pill.warn { background: var(--warn); color: #1e1e2e; }
  .pill.error { background: var(--error); color: #1e1e2e; }
  .pill.unknown { background: var(--unknown); color: var(--text); }
  .row {
    display: flex;
    justify-content: space-between;
    padding: 6px 0;
    border-bottom: 1px solid var(--bg-elev);
    font-size: 0.9rem;
  }
  .row:last-child { border-bottom: none; }
  .row .key { color: var(--text-dim); }
  .row .val { color: var(--text); font-variant-numeric: tabular-nums; }
  .empty { color: var(--text-dim); font-style: italic; font-size: 0.85rem; padding: 4px 0; }
  .error-item {
    padding: 8px 0;
    border-bottom: 1px solid var(--bg-elev);
    font-size: 0.85rem;
  }
  .error-item:last-child { border-bottom: none; }
  .error-item .kind { color: var(--accent); font-weight: 500; }
  .error-item .when { color: var(--text-dim); font-size: 0.75rem; margin-left: 8px; }
  .error-item .msg { color: var(--error); font-family: ui-monospace, monospace; font-size: 0.8rem; word-break: break-word; margin-top: 4px; }
  footer { margin-top: 32px; color: var(--text-dim); font-size: 0.75rem; text-align: center; }
  .icon { display: inline-block; width: 1em; text-align: center; }
  /* Accessibility: keep visible-but-de-emphasized focus indicator */
  *:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
</style>
</head>
<body>
<header>
  <h1>lancache-orchestrator</h1>
  <div class="meta">
    <span id="version">v?</span> | <span id="last-update">never updated</span>
  </div>
</header>

<main class="grid">
  <section id="panel-health" class="panel unknown">
    <h2>Health <span id="health-pill" class="pill unknown"><span class="icon">?</span> UNKNOWN</span></h2>
    <div id="health-body"><div class="empty">Loading...</div></div>
  </section>

  <section id="panel-platforms" class="panel unknown">
    <h2>Platforms <span id="platforms-pill" class="pill unknown"><span class="icon">?</span> UNKNOWN</span></h2>
    <div id="platforms-body"><div class="empty">Loading...</div></div>
  </section>

  <section id="panel-jobs" class="panel unknown">
    <h2>Active Jobs <span id="jobs-pill" class="pill unknown"><span class="icon">?</span> UNKNOWN</span></h2>
    <div id="jobs-body"><div class="empty">Loading...</div></div>
  </section>

  <section id="panel-stats" class="panel unknown">
    <h2>Library Stats <span id="stats-pill" class="pill unknown"><span class="icon">?</span> UNKNOWN</span></h2>
    <div id="stats-body"><div class="empty">Loading...</div></div>
  </section>

  <section id="panel-errors" class="panel unknown">
    <h2>Recent Errors <span id="errors-pill" class="pill unknown"><span class="icon">?</span> UNKNOWN</span></h2>
    <div id="errors-body"><div class="empty">Loading...</div></div>
  </section>
</main>

<footer>
  <span id="footer-status">Operator: enter bearer token when prompted. Token is held in sessionStorage only and cleared on tab close.</span>
</footer>

<script>
'use strict';

const API = '/api/v1';
const POLL_FAST_MS = 2000;
const POLL_SLOW_MS = 10000;
const BACKOFF_MS = 10000;

function getToken() {
  let tok = sessionStorage.getItem('orch_token');
  if (!tok) {
    tok = prompt('orchestrator bearer token (held in sessionStorage only):');
    if (tok) sessionStorage.setItem('orch_token', tok);
  }
  return tok;
}

async function apiGet(path) {
  const tok = getToken();
  if (!tok) throw new Error('no token');
  const r = await fetch(API + path, {
    headers: { 'Authorization': 'Bearer ' + tok }
  });
  if (r.status === 401) {
    sessionStorage.removeItem('orch_token');
    throw new Error('bearer rejected');
  }
  if (r.status >= 500) {
    throw new Error('server ' + r.status);
  }
  return r.json();
}

function setPill(prefix, klass, icon, label) {
  const pill = document.getElementById(prefix + '-pill');
  const panel = document.getElementById('panel-' + prefix);
  pill.className = 'pill ' + klass;
  pill.innerHTML = '<span class="icon">' + icon + '</span> ' + label;
  panel.className = 'panel ' + klass;
}

function row(key, val) {
  return '<div class="row"><span class="key">' + escapeHtml(key) +
         '</span><span class="val">' + escapeHtml(val) + '</span></div>';
}

function escapeHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function fmtAge(iso) {
  if (!iso) return '-';
  try {
    const d = new Date(iso.replace(' ', 'T') + 'Z');
    const dt = (Date.now() - d.getTime()) / 1000;
    if (dt < 60) return Math.floor(dt) + 's ago';
    if (dt < 3600) return Math.floor(dt/60) + 'm ago';
    if (dt < 86400) return Math.floor(dt/3600) + 'h ago';
    return Math.floor(dt/86400) + 'd ago';
  } catch (e) { return '-'; }
}

// --- Renderers ---

async function pollHealth() {
  try {
    const tok = getToken();
    const r = await fetch(API + '/health', { headers: tok ? {'Authorization':'Bearer '+tok}:{} });
    const h = await r.json();
    document.getElementById('version').textContent = 'v' + h.version + ' (' + h.git_sha + ')';
    const allOk = h.status === 'ok' && h.scheduler_running && h.lancache_reachable
                  && h.cache_volume_mounted && h.validator_healthy;
    if (allOk) setPill('health', 'ok', '✓', 'OK');
    else setPill('health', 'warn', '⚠', 'DEGRADED');
    let html = row('status', h.status);
    html += row('uptime', Math.floor(h.uptime_sec/60) + 'm ' + (h.uptime_sec%60) + 's');
    html += row('scheduler_running', h.scheduler_running ? 'yes' : 'NO');
    html += row('lancache_reachable', h.lancache_reachable ? 'yes' : 'NO');
    html += row('cache_volume_mounted', h.cache_volume_mounted ? 'yes' : 'NO');
    html += row('validator_healthy', h.validator_healthy ? 'yes' : 'NO');
    document.getElementById('health-body').innerHTML = html;
  } catch (e) {
    setPill('health', 'error', '✗', 'ERROR');
    document.getElementById('health-body').innerHTML = '<div class="empty">' + escapeHtml(e.message) + '</div>';
  }
}

async function pollPlatforms() {
  try {
    const data = await apiGet('/platforms');
    let html = '';
    let allOk = true;
    for (const p of (data.platforms || [])) {
      html += row(p.name + ' auth_status', p.auth_status);
      if (p.auth_status !== 'ok' && p.auth_status !== 'never') allOk = false;
      if (p.last_sync_at) html += row(p.name + ' last_sync', fmtAge(p.last_sync_at));
      if (p.last_error) html += row(p.name + ' last_error', p.last_error);
    }
    if (allOk) setPill('platforms', 'ok', '✓', 'OK');
    else setPill('platforms', 'warn', '⚠', 'NEEDS ATTENTION');
    document.getElementById('platforms-body').innerHTML = html || '<div class="empty">No platforms.</div>';
  } catch (e) {
    setPill('platforms', 'error', '✗', 'ERROR');
    document.getElementById('platforms-body').innerHTML = '<div class="empty">' + escapeHtml(e.message) + '</div>';
  }
}

async function pollJobs() {
  try {
    const queued = await apiGet('/jobs?state=queued&limit=10');
    const running = await apiGet('/jobs?state=running&limit=10');
    const total = (queued.meta.total || 0) + (running.meta.total || 0);
    if (total === 0) setPill('jobs', 'ok', '✓', 'IDLE');
    else setPill('jobs', 'warn', '⚠', total + ' ACTIVE');
    let html = row('queued', String(queued.meta.total || 0));
    html += row('running', String(running.meta.total || 0));
    const items = (running.jobs || []).concat(queued.jobs || []);
    for (const j of items.slice(0, 6)) {
      html += row('#' + j.id + ' ' + j.kind + ' (' + j.state + ')', fmtAge(j.started_at) || '-');
    }
    document.getElementById('jobs-body').innerHTML = html;
  } catch (e) {
    setPill('jobs', 'error', '✗', 'ERROR');
    document.getElementById('jobs-body').innerHTML = '<div class="empty">' + escapeHtml(e.message) + '</div>';
  }
}

async function pollStats() {
  try {
    const games = await apiGet('/games?limit=1');
    const manifests = await apiGet('/manifests?limit=1');
    setPill('stats', 'ok', '✓', 'OK');
    let html = row('games (all platforms)', String(games.meta.total || 0));
    html += row('manifests stored', String(manifests.meta.total || 0));
    document.getElementById('stats-body').innerHTML = html;
  } catch (e) {
    setPill('stats', 'error', '✗', 'ERROR');
    document.getElementById('stats-body').innerHTML = '<div class="empty">' + escapeHtml(e.message) + '</div>';
  }
}

async function pollErrors() {
  try {
    const data = await apiGet('/jobs?state=failed&limit=5');
    const jobs = data.jobs || [];
    if (jobs.length === 0) {
      setPill('errors', 'ok', '✓', 'NONE');
      document.getElementById('errors-body').innerHTML = '<div class="empty">No recent failures.</div>';
      return;
    }
    setPill('errors', 'warn', '⚠', jobs.length + ' FAILED');
    let html = '';
    for (const j of jobs) {
      html += '<div class="error-item">';
      html += '<span class="kind">' + escapeHtml(j.kind) + ' #' + j.id + '</span>';
      html += '<span class="when">' + escapeHtml(fmtAge(j.finished_at)) + '</span>';
      if (j.error) html += '<div class="msg">' + escapeHtml(j.error) + '</div>';
      html += '</div>';
    }
    document.getElementById('errors-body').innerHTML = html;
  } catch (e) {
    setPill('errors', 'error', '✗', 'ERROR');
    document.getElementById('errors-body').innerHTML = '<div class="empty">' + escapeHtml(e.message) + '</div>';
  }
}

// --- Polling loop with backoff ---

function tickFooter() {
  document.getElementById('last-update').textContent = 'updated ' + new Date().toLocaleTimeString();
}

let fastInterval = POLL_FAST_MS;
let slowInterval = POLL_SLOW_MS;

async function fastTick() {
  try {
    await Promise.all([pollHealth(), pollPlatforms(), pollJobs()]);
    tickFooter();
    fastInterval = POLL_FAST_MS;
  } catch (e) { fastInterval = BACKOFF_MS; }
  setTimeout(fastTick, fastInterval);
}

async function slowTick() {
  try {
    await Promise.all([pollStats(), pollErrors()]);
    slowInterval = POLL_SLOW_MS;
  } catch (e) { slowInterval = BACKOFF_MS; }
  setTimeout(slowTick, slowInterval);
}

// First fires immediately
fastTick();
slowTick();
</script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse)
async def get_status_page() -> HTMLResponse:
    """Serve the F10 status page. Unauthenticated (Bible §9.3): the JS
    embedded in the page prompts the operator for the bearer token at
    first load and persists it in sessionStorage. All API calls from
    the page (/api/v1/...) ARE auth-gated by BearerAuthMiddleware.
    """
    return HTMLResponse(
        content=_STATUS_HTML,
        headers={
            # Status page is a private operator surface — block search
            # engines + caching proxies from caching the bearer-prompt
            # flow. Bible §9.3 + Intake §6.
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )
