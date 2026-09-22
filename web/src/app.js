// Citadel workbench UI -- M0 checkpoint. Plain vanilla JS: no framework, no
// build step, no bundler. See web/AGENTS.md for why (this checkpoint's
// sandbox could not install a package registry's worth of npm dependencies,
// and root AGENTS.md invariant 10 forbids a CDN reference regardless) and
// for what the originally-planned Vite+React+Tailwind build still owns
// once this runs somewhere with real internet access.
//
// Everything here talks to /api/* on this same origin (app.py mounts this
// directory's files and the API on one Starlette app) -- there is no CORS
// configuration anywhere because there is nothing cross-origin to allow.
//
// sessionStorage holds the current bearer token + user so a page refresh
// does not force a re-login; it is cleared by closing the tab, same as the
// session token's own lifetime is meant to be bounded. This is an ordinary
// web-app choice for a page a real browser loads from a real server -- not
// the artifact-preview sandbox that in-conversation renders forbid browser
// storage in.
//
// Identity is never invented client-side: the only user-facing state this
// script trusts is exactly what /api/me and /api/policy/try hand back,
// both of which come from the server's own verified session token.

'use strict';

const state = {
  session: { token: null, user: null },
  tools: [],
  departments: [],
  pendingHighlightSeq: null,
};

const RULE_EXPLANATIONS = {
  'deny-unknown-classification':
    "Denied — the resource's classification marking isn't a recognised level, so it fails closed instead of being guessed at.",
  'deny-above-clearance':
    "Denied — the resource's classification is above your clearance.",
  'deny-acl-disjoint':
    "Denied — the resource's ACL doesn't include your department.",
  'deny-tool-ceiling':
    "Denied — the resource's classification exceeds what this tool is ever allowed to touch.",
  'deny-missing-receipt':
    "Denied — this tool requires a signed receipt and none was presented. Receipt issuance isn't wired up yet in this checkpoint, so this is expected every time for a receipt-requiring tool, not a bug.",
  'allow-read-with-capability':
    'Allowed — your role grants every capability this read tool requires.',
  'allow-write-with-capability':
    'Allowed — your role grants every capability this write tool requires.',
  'allow-execute-engineer':
    'Allowed — engineers and admins may execute, and your role grants every capability this tool requires.',
};
const DEFAULT_DENY_EXPLANATION = 'Denied — no policy rule matched. Default deny.';

function el(id) {
  return document.getElementById(id);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

function showError(message) {
  el('error-slot').innerHTML = `<div class="error-banner">${escapeHtml(message)}</div>`;
}

function clearError() {
  el('error-slot').innerHTML = '';
}

// ---- session persistence (sessionStorage; see the file header) ----

function saveSession() {
  try {
    if (state.session.token) {
      sessionStorage.setItem('citadel.token', state.session.token);
      sessionStorage.setItem('citadel.user', JSON.stringify(state.session.user));
    } else {
      sessionStorage.removeItem('citadel.token');
      sessionStorage.removeItem('citadel.user');
    }
  } catch (err) {
    // Private browsing / storage disabled -- the session still works for
    // the rest of this page load, it just won't survive a refresh.
  }
}

function restoreSession() {
  try {
    const token = sessionStorage.getItem('citadel.token');
    const userJson = sessionStorage.getItem('citadel.user');
    if (token && userJson) {
      state.session.token = token;
      state.session.user = JSON.parse(userJson);
      return true;
    }
  } catch (err) {
    // ignore -- falls through to the login screen
  }
  return false;
}

// ---- thin fetch wrapper ----

async function api(path, { method = 'GET', body, auth = false } = {}) {
  const headers = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (auth) {
    if (!state.session.token) throw new Error('not signed in');
    headers['Authorization'] = `Bearer ${state.session.token}`;
  }
  const res = await fetch(path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try {
    data = await res.json();
  } catch (err) {
    // no/invalid JSON body -- fall through with data left null
  }
  if (!res.ok) {
    const message = data && data.error ? data.error : `${res.status} ${res.statusText}`;
    throw new Error(message);
  }
  return data;
}

// ---- header ----

async function loadHealth() {
  try {
    const health = await api('/api/health');
    const badge = el('profile-badge');
    badge.textContent = `profile: ${health.profile}`;
    badge.hidden = false;
  } catch (err) {
    // Cosmetic only -- the rest of the UI works without it.
  }
}

function renderIdentity() {
  const slot = el('identity-slot');
  if (!state.session.user) {
    slot.innerHTML = '';
    return;
  }
  const u = state.session.user;
  slot.innerHTML = `
    <div class="identity-chip">
      <span class="who">${escapeHtml(u.username)}</span>
      <span class="meta">${escapeHtml((u.roles || []).join(', '))} · ${escapeHtml(u.department)} · ${escapeHtml(u.clearance)}</span>
    </div>
    <button type="button" id="logout-btn" class="ghost">Switch identity</button>
  `;
  el('logout-btn').addEventListener('click', logout);
}

// ---- login screen ----

function departmentsFromUsers(users) {
  const set = new Set();
  users.forEach((u) => {
    if (u.department) set.add(u.department);
  });
  return Array.from(set).sort();
}

async function loadDemoUsers() {
  try {
    const result = await api('/api/demo/users');
    renderUserGrid(result.users);
    return result.users;
  } catch (err) {
    el('user-grid').innerHTML = `<p class="empty-state">Could not load demo identities: ${escapeHtml(err.message)}</p>`;
    return [];
  }
}

function renderUserGrid(users) {
  el('user-grid').innerHTML = users
    .map(
      (u) => `
    <button type="button" class="user-card" data-user-id="${escapeHtml(u.user_id)}">
      <span class="username">${escapeHtml(u.username)}</span>
      <span class="user-id">${escapeHtml(u.user_id)}</span>
      <span class="badges">
        <span class="badge">${escapeHtml((u.roles || []).join(', '))}</span>
        <span class="badge">${escapeHtml(u.department)}</span>
        <span class="badge">${escapeHtml(u.clearance)}</span>
      </span>
    </button>`
    )
    .join('');
  el('user-grid')
    .querySelectorAll('.user-card')
    .forEach((btn) => btn.addEventListener('click', () => login(btn.dataset.userId)));
}

async function login(userId) {
  clearError();
  try {
    const result = await api('/api/auth/session', { method: 'POST', body: { user_id: userId } });
    state.session.token = result.token;
    state.session.user = result.user;
    saveSession();
    await enterWorkbench();
  } catch (err) {
    showError(`Sign-in failed: ${err.message}`);
  }
}

function logout() {
  state.session.token = null;
  state.session.user = null;
  saveSession();
  showLoginScreen();
}

function showLoginScreen() {
  el('workbench').hidden = true;
  el('login-screen').hidden = false;
  renderIdentity();
}

function resetDecisionPanel() {
  // The tool/classification/ACL selections deliberately persist across a
  // user switch (so "try the same thing as the other engineer" is one
  // click), but a *decision result* is only ever true for the identity it
  // was evaluated against -- carrying last time's result over into a new
  // identity's screen would show S. Nair's header next to R. Kulkarni's
  // decision, quietly misrepresenting who it was evaluated as.
  el('decision-result').innerHTML =
    '<p class="decision-placeholder">Run an evaluation to see the decision, the rule that made it, and the facts it was evaluated against.</p>';
  state.pendingHighlightSeq = null;
}

async function enterWorkbench() {
  clearError();
  el('login-screen').hidden = true;
  el('workbench').hidden = false;
  renderIdentity();
  resetDecisionPanel();
  if (!state.tools.length) {
    await loadTools();
  }
  switchTab('policy');
}

// ---- try a tool ----

async function loadTools() {
  try {
    const result = await api('/api/registry/tools');
    state.tools = result.tools;
    const select = el('tool-select');
    select.innerHTML = state.tools
      .map((t) => `<option value="${escapeHtml(t.name)}">${escapeHtml(t.name)}</option>`)
      .join('');
    if (state.tools.some((t) => t.name === 'fs.read')) {
      select.value = 'fs.read'; // no receipt required -- a first click lands on a real allow
    }
    renderAclCheckboxes();
    onToolChange();
    select.addEventListener('change', onToolChange);
  } catch (err) {
    showError(`Could not load tool registry: ${err.message}`);
  }
}

function renderAclCheckboxes() {
  el('acl-checkboxes').innerHTML = state.departments
    .map(
      (dept) => `<label><input type="checkbox" value="${escapeHtml(dept)}" checked /> ${escapeHtml(dept)}</label>`
    )
    .join('');
}

function onToolChange() {
  const name = el('tool-select').value;
  const tool = state.tools.find((t) => t.name === name);
  const meta = el('tool-meta');
  if (!tool) {
    meta.innerHTML = '';
    return;
  }
  let html = `<span class="badge">${escapeHtml(tool.side_effect)}</span>`;
  html += `<span class="badge">ceiling: ${escapeHtml(tool.classification_ceiling)}</span>`;
  if (tool.required_capabilities.length) {
    html += `<span class="badge">needs: ${escapeHtml(tool.required_capabilities.join(', '))}</span>`;
  }
  if (tool.requires_receipt) {
    html += `<span class="badge" style="color:var(--warn);border-color:var(--warn)">receipt required (not yet issued in this checkpoint)</span>`;
  }
  meta.innerHTML = html;
}

async function evaluateDecision() {
  clearError();
  const tool = el('tool-select').value;
  const classification = el('classification-select').value;
  const acl = Array.from(el('acl-checkboxes').querySelectorAll('input:checked')).map((i) => i.value);
  const resourceId = el('resource-id').value.trim() || 'demo-resource-1';
  const resourceType = el('resource-type').value.trim() || 'document';

  el('evaluate-btn').disabled = true;
  try {
    const result = await api('/api/policy/try', {
      method: 'POST',
      auth: true,
      body: {
        tool,
        resource: { resource_id: resourceId, type: resourceType, classification, acl },
      },
    });
    renderDecision(result);
  } catch (err) {
    showError(`Evaluation failed: ${err.message}`);
  } finally {
    el('evaluate-btn').disabled = false;
  }
}

function renderDecision(result) {
  const { decision, actor, audit_seq: auditSeq } = result;
  const explanation =
    RULE_EXPLANATIONS[decision.rule_id] ||
    (decision.effect === 'allow' ? `Allowed by rule ${decision.rule_id}.` : DEFAULT_DENY_EXPLANATION);
  const pillClass = decision.effect === 'allow' ? 'allow' : 'deny';
  const pillText = decision.effect === 'allow' ? '✓ ALLOW' : '✕ DENY';
  const ruleLine = decision.rule_id
    ? `<span class="rule-id">rule: ${escapeHtml(decision.rule_id)}${decision.reason ? ' · reason: ' + escapeHtml(decision.reason) : ''}</span>`
    : `<span class="rule-id">no rule matched — default deny</span>`;

  el('decision-result').innerHTML = `
    <div class="decision-pill ${pillClass}">${pillText}</div>
    <div class="decision-explain">${escapeHtml(explanation)}${ruleLine}</div>
    <dl class="fact-grid">
      <dt>Evaluated as role</dt><dd>${escapeHtml(actor.role)}</dd>
      <dt>Department</dt><dd>${escapeHtml(actor.department)}</dd>
      <dt>Clearance (max)</dt><dd>${escapeHtml(actor.classification_max)}</dd>
      <dt>Capabilities</dt><dd>${actor.capabilities.map(escapeHtml).join(', ') || '(none)'}</dd>
    </dl>
    <a href="#" class="audit-link" id="view-in-audit">↳ View this decision in the audit log (seq ${auditSeq})</a>
  `;
  state.pendingHighlightSeq = auditSeq;
  el('view-in-audit').addEventListener('click', (e) => {
    e.preventDefault();
    switchTab('audit');
  });
}

// ---- audit log ----

function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.tab === name);
  });
  el('panel-policy').classList.toggle('active', name === 'policy');
  el('panel-audit').classList.toggle('active', name === 'audit');
  if (name === 'audit') {
    loadAudit();
  }
}

async function loadAudit() {
  try {
    const result = await api('/api/audit/recent?limit=25', { auth: true });
    renderAuditRows(result.events);
  } catch (err) {
    el('audit-tbody').innerHTML = `<tr><td colspan="6" class="empty-state">${escapeHtml(err.message)}</td></tr>`;
  }
}

function summarizePayload(eventName, payload) {
  if (eventName === 'policy.decision') {
    const effectClass = payload.effect === 'allow' ? 'allow' : 'deny';
    const resourceClassification = payload.resource ? payload.resource.classification : '?';
    return `<span class="effect-tag ${effectClass}">${escapeHtml(payload.effect)}</span> ${escapeHtml(payload.tool)} on ${escapeHtml(resourceClassification)}${payload.rule_id ? ' · ' + escapeHtml(payload.rule_id) : ''}`;
  }
  if (eventName === 'identity.verified') {
    return `signed in as ${escapeHtml(payload.username || '')}`;
  }
  return `<span class="mono">${escapeHtml(JSON.stringify(payload))}</span>`;
}

function renderAuditRows(events) {
  const tbody = el('audit-tbody');
  if (!events.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-state">No audit events yet.</td></tr>';
    return;
  }
  tbody.innerHTML = events
    .slice()
    .reverse()
    .map((ev) => {
      const isHighlight = state.pendingHighlightSeq === ev.seq;
      return `<tr data-seq="${ev.seq}" class="${isHighlight ? 'highlight' : ''}">
      <td class="mono">${ev.seq}</td>
      <td class="mono">${escapeHtml(ev.occurred_at)}</td>
      <td>${escapeHtml(ev.actor_id || '—')}</td>
      <td>${escapeHtml(ev.event_name)}</td>
      <td>${summarizePayload(ev.event_name, ev.payload)}</td>
      <td class="mono">${escapeHtml(ev.row_hash.slice(0, 12))}…</td>
    </tr>`;
    })
    .join('');

  if (state.pendingHighlightSeq !== null) {
    const row = tbody.querySelector(`tr[data-seq="${state.pendingHighlightSeq}"]`);
    if (row) row.scrollIntoView({ behavior: 'smooth', block: 'center' });
    state.pendingHighlightSeq = null;
  }
}

async function verifyChain() {
  const banner = el('chain-banner');
  banner.textContent = 'Verifying…';
  banner.className = '';
  try {
    const result = await api('/api/audit/verify', { auth: true });
    if (result.ok) {
      banner.textContent = `Chain intact — ${result.checked} event(s) verified.`;
      banner.className = 'chain-banner ok';
    } else {
      banner.textContent = `BROKEN at seq ${result.first_break_seq}: ${result.reason}`;
      banner.className = 'chain-banner broken';
    }
  } catch (err) {
    showError(err.message);
  }
}

// ---- boot ----

async function init() {
  loadHealth();
  const users = await loadDemoUsers();
  state.departments = departmentsFromUsers(users);

  if (restoreSession()) {
    try {
      const me = await api('/api/me', { auth: true });
      state.session.user = Object.assign({}, state.session.user, me);
      await enterWorkbench();
      return;
    } catch (err) {
      state.session.token = null;
      state.session.user = null;
      saveSession();
    }
  }
  showLoginScreen();
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
  el('evaluate-btn').addEventListener('click', evaluateDecision);
  el('verify-btn').addEventListener('click', verifyChain);
  el('refresh-audit-btn').addEventListener('click', loadAudit);
  init();
});
