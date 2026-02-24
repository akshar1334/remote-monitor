/**
 * app.js – Shared utilities for the Remote Monitor portal
 */

// ── Cookie helpers ─────────────────────────────────────────────────────────────
function getCookie(name) {
  const match = document.cookie.match(new RegExp(`(^| )${name}=([^;]+)`));
  return match ? decodeURIComponent(match[2]) : null;
}

// ── Auth helpers ───────────────────────────────────────────────────────────────
let _meCache = null;

async function getMe() {
  if (_meCache) return _meCache;
  try {
    const res = await fetch('/api/auth/me', { credentials: 'include' });
    if (!res.ok) throw new Error('Unauthenticated');
    _meCache = await res.json();
    return _meCache;
  } catch {
    return null;
  }
}

async function requireAuth(requiredRole = null) {
  const me = await getMe();
  if (!me) { window.location.href = '/login'; return; }
  if (requiredRole && me.role !== requiredRole) {
    window.location.href = '/dashboard';
    return;
  }
  // Show navbar
  document.getElementById('navbar').style.display = 'flex';
  document.getElementById('nav-username').textContent = `${me.username} (${me.role})`;
  if (me.role === 'admin') {
    document.getElementById('nav-admin-links').style.display = 'inline';
  }
  return me;
}

async function logout() {
  await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' });
  _meCache = null;
  window.location.href = '/login';
}

// ── API fetch wrapper ──────────────────────────────────────────────────────────
async function apiFetch(url, options = {}) {
  const res = await fetch(url, { credentials: 'include', ...options });
  if (res.status === 401) { window.location.href = '/login'; return; }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || 'Request failed');
  }
  return res.json();
}

// ── WebSocket manager ──────────────────────────────────────────────────────────
function openPortalWS(token, onMessage) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/portal?token=${encodeURIComponent(token)}`);

  ws.onopen = () => setWsStatus('connected');
  ws.onclose = () => {
    setWsStatus('disconnected');
    setTimeout(() => openPortalWS(token, onMessage), 5000);
  };
  ws.onerror = () => setWsStatus('error');
  ws.onmessage = (e) => {
    try { onMessage(JSON.parse(e.data)); } catch (err) { console.error('WS parse error', err); }
  };

  return ws;
}

function setWsStatus(state) {
  const el = document.getElementById('ws-status');
  if (!el) return;
  const labels = { connected: '⚡ Live', disconnected: '🔴 Reconnecting…', error: '⚠️ WS Error' };
  el.textContent = labels[state] || state;
  el.className = `ws-status ${state}`;
}

// ── Modal helpers ──────────────────────────────────────────────────────────────
function showModal(id) {
  document.getElementById(id).style.display = 'flex';
}
function hideModal(id) {
  document.getElementById(id).style.display = 'none';
}

// Close modals on overlay click
document.addEventListener('click', (e) => {
  if (e.target.classList.contains('modal-overlay')) {
    e.target.style.display = 'none';
  }
});
