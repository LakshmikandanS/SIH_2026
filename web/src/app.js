/* Citadel workbench UI -- plain JavaScript, no framework, no build step, no external host.
 *
 * Identity lives in a session token the API verifies on every call; nothing here decides
 * what anyone may see. Task progress streams from the task's durable journal over SSE
 * (read with fetch so the token travels in a header, never in a URL).
 */
(() => {
  "use strict";

  // ---------------------------------------------------------------- utilities
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const LEVELS = ["PUBLIC", "INTERNAL", "CONFIDENTIAL"];
  const TERMINAL = new Set(["completed", "failed", "cancelled"]);
  const QUIET = new Set(["completed", "failed", "cancelled", "awaiting_approval"]);

  function levelPill(level) {
    const upper = String(level || "").toUpperCase();
    return `<span class="pill ${esc(upper)}">${esc(upper || "—")}</span>`;
  }
  function statusPill(status) {
    return `<span class="pill ${esc(status)}">${esc(String(status || "").replace(/_/g, " "))}</span>`;
  }
  function deptPills(acl) {
    return (acl || []).map((d) => `<span class="pill dept">${esc(d)}</span>`).join(" ");
  }
  function ago(value) {
    if (!value) return "";
    const then = new Date(value).getTime();
    const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
    return new Date(value).toLocaleString();
  }
  function ms(value) {
    if (value === null || value === undefined) return "—";
    return value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`;
  }
  function allowedLevels(clearance) {
    const top = LEVELS.indexOf(String(clearance || "").toUpperCase());
    return LEVELS.slice(0, top + 1);
  }
  function json(value) {
    return esc(JSON.stringify(value, null, 2));
  }

  // ---------------------------------------------------------------- session
  const session = {
    token: null,
    user: null,
    load() {
      try {
        this.token = sessionStorage.getItem("citadel.token");
        const raw = sessionStorage.getItem("citadel.user");
        this.user = raw ? JSON.parse(raw) : null;
      } catch (_) {
        this.token = null;
        this.user = null;
      }
    },
    save(token, user) {
      this.token = token;
      this.user = user;
      try {
        sessionStorage.setItem("citadel.token", token);
        sessionStorage.setItem("citadel.user", JSON.stringify(user));
      } catch (_) { /* private mode: the session lasts as long as the page */ }
    },
    clear() {
      this.token = null;
      this.user = null;
      try {
        sessionStorage.removeItem("citadel.token");
        sessionStorage.removeItem("citadel.user");
      } catch (_) { /* nothing to clear */ }
    },
    has(role) {
      return !!(this.user && (this.user.roles || []).includes(role));
    },
  };

  class ApiError extends Error {
    constructor(message, status, body) {
      super(message);
      this.status = status;
      this.body = body;
    }
  }

  async function api(path, options = {}, token = session.token) {
    const headers = Object.assign({}, options.headers || {});
    if (token) headers.Authorization = `Bearer ${token}`;
    let body = options.body;
    if (body && !(body instanceof FormData) && typeof body !== "string") {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    }
    const response = await fetch(path, { method: options.method || "GET", headers, body, signal: options.signal });
    if (response.status === 401 && token === session.token) {
      session.clear();
      showLogin("Your session has ended. Sign in again.");
      throw new ApiError("signed out", 401);
    }
    if (options.raw) {
      if (!response.ok) throw new ApiError(`${response.status}`, response.status);
      return response;
    }
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch (_) {
      data = { raw: text };
    }
    if (!response.ok) throw new ApiError((data && data.error) || `request failed (${response.status})`, response.status, data);
    return data;
  }

  async function blobUrl(path) {
    const response = await api(path, { raw: true });
    return URL.createObjectURL(await response.blob());
  }

  async function download(path, fallbackName) {
    const response = await api(path, { raw: true });
    const disposition = response.headers.get("content-disposition") || "";
    const match = disposition.match(/filename="([^"]+)"/);
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = match ? match[1] : fallbackName;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }

  // ---------------------------------------------------------------- toasts & modal
  function toast(message, kind = "") {
    const node = document.createElement("div");
    node.className = `toast ${kind}`;
    node.textContent = message;
    $("#toast-slot").appendChild(node);
    setTimeout(() => node.remove(), kind === "bad" ? 8000 : 4500);
  }
  function openModal(html) {
    $("#modal-body").innerHTML = html;
    $("#modal").hidden = false;
    return $("#modal-body");
  }
  function closeModal() {
    $("#modal").hidden = true;
    $("#modal-body").innerHTML = "";
  }
  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-close]")) closeModal();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#modal").hidden) closeModal();
  });

  // ---------------------------------------------------------------- app state
  const app = {
    health: null,
    demoUsers: [],
    timers: [],
    stream: null, // { taskId, controller }
    taskCache: new Map(),
  };

  function clearTimers() {
    app.timers.forEach((id) => clearInterval(id));
    app.timers = [];
  }
  function every(msInterval, fn) {
    const id = setInterval(fn, msInterval);
    app.timers.push(id);
    return id;
  }
  function stopStream() {
    if (app.stream) {
      app.stream.controller.abort();
      app.stream = null;
    }
  }

  // ---------------------------------------------------------------- sign in
  async function showLogin(message) {
    stopStream();
    clearTimers();
    $("#shell").hidden = true;
    $("#egress-pill").hidden = true;
    $("#identity-slot").innerHTML = "";
    $("#login-screen").hidden = false;
    const grid = $("#user-grid");
    if (message) toast(message);
    try {
      const data = await api("/api/demo/users", {}, null);
      app.demoUsers = data.users || [];
      grid.innerHTML = app.demoUsers
        .map(
          (u) => `
        <button type="button" class="user-card" data-user="${esc(u.user_id)}">
          <div class="name">${esc(u.username)}</div>
          <div class="muted small mono">${esc(u.user_id)}</div>
          <div class="facts">${(u.roles || []).map((r) => `<span class="pill">${esc(r)}</span>`).join("")}
            <span class="pill dept">${esc(u.department)}</span> ${levelPill(u.clearance)}</div>
        </button>`
        )
        .join("");
      $$(".user-card", grid).forEach((card) =>
        card.addEventListener("click", async () => {
          try {
            const reply = await api("/api/auth/session", { method: "POST", body: { user_id: card.dataset.user } }, null);
            session.save(reply.token, reply.user);
            const home = (reply.user.roles || []).includes("approver") ? "#/approvals" : "#/work";
            if (location.hash !== home) history.replaceState(null, "", home);
            await boot();
          } catch (error) {
            toast(error.message, "bad");
          }
        })
      );
    } catch (error) {
      grid.innerHTML = `<div class="notice bad">Cannot reach the Citadel API: ${esc(error.message)}</div>`;
    }
  }

  async function boot() {
    session.load();
    try {
      app.health = await api("/api/health", {}, null);
    } catch (_) {
      app.health = null;
    }
    if (!session.token) return showLogin();
    try {
      session.user = await api("/api/me");
    } catch (_) {
      return;
    }
    $("#login-screen").hidden = true;
    $("#shell").hidden = false;
    const u = session.user;
    $("#identity-slot").innerHTML = `
      <span class="identity"><span class="who">${esc(u.username)}</span>
        <span class="pill dept">${esc(u.department)}</span> ${levelPill(u.clearance)}
        <button type="button" class="btn ghost small" id="sign-out">Sign out</button></span>`;
    $("#sign-out").addEventListener("click", () => {
      session.clear();
      showLogin();
    });
    if (app.health) {
      const pill = $("#profile-pill");
      pill.hidden = false;
      pill.textContent = `${app.health.profile}${app.health.sovereign ? " · sovereign" : ""} · sandbox: ${app.health.sandbox}`;
    }
    $("#sidebar-foot").innerHTML = `Signed in as <b>${esc(u.user_id)}</b><br/>Roles: ${esc((u.roles || []).join(", "))}`;
    if (!app.demoUsers.length) {
      try {
        app.demoUsers = (await api("/api/demo/users", {}, null)).users || [];
      } catch (_) { /* optional */ }
    }
    startGlobalPollers();
    route();
  }

  function startGlobalPollers() {
    const egress = async () => {
      try {
        const panel = await api("/api/sovereignty");
        const counts = panel.status.counts;
        const pill = $("#egress-pill");
        pill.hidden = false;
        pill.classList.toggle("alarm", counts.observed > 0);
        $("#egress-pill-text").textContent =
          counts.observed > 0
            ? `EGRESS OBSERVED: ${counts.observed}`
            : `egress 0 · ${counts.attempts + counts.dns_denied} stopped`;
      } catch (_) { /* the pill simply stays as it was */ }
    };
    const approvals = async () => {
      if (!session.has("approver") && !session.has("admin")) return;
      try {
        const data = await api("/api/artifacts?awaiting=1");
        const badge = $("#approvals-count");
        const n = (data.artifacts || []).length;
        badge.hidden = n === 0;
        badge.textContent = String(n);
      } catch (_) { /* ignore */ }
    };
    egress();
    approvals();
    setInterval(egress, 15000);
    setInterval(approvals, 20000);
  }
  $("#egress-pill").addEventListener("click", () => (location.hash = "#/sovereignty"));

  // ---------------------------------------------------------------- router
  const views = {};
  function route() {
    if (!session.token) return;
    stopStream();
    clearTimers();
    const [, name = "work", arg = ""] = (location.hash || "#/work").split("/");
    $$(".nav-item").forEach((a) => a.classList.toggle("active", a.dataset.view === name));
    const render = views[name] || views.work;
    const root = $("#view");
    root.innerHTML = "";
    render(root, decodeURIComponent(arg)).catch((error) => {
      if (error instanceof ApiError && error.status === 401) return;
      root.innerHTML = `<div class="notice bad">${esc(error.message)}</div>`;
    });
  }
  window.addEventListener("hashchange", route);

  // ---------------------------------------------------------------- citations
  function renderAnswer(text) {
    return esc(text).replace(/\[((?:[EC]\d+)(?:\s*[,;]\s*[EC]\d+)*)\]/g, (_, group) =>
      group
        .split(/\s*[,;]\s*/)
        .map((id) => `<span class="cite ${id[0]}" data-cite="${id}">${id}</span>`)
        .join("")
    );
  }
  function citeChip(id) {
    return id ? `<span class="cite ${esc(id[0])}" data-cite="${esc(id)}">${esc(id)}</span>` : "";
  }

  async function showEvidence(taskId, evidenceId) {
    let detail = app.taskCache.get(taskId);
    let row = detail && (detail.evidence || []).find((e) => e.evidence_id === evidenceId);
    if (!row) {
      detail = await api(`/api/tasks/${taskId}`);
      app.taskCache.set(taskId, detail);
      row = (detail.evidence || []).find((e) => e.evidence_id === evidenceId);
    }
    if (!row) {
      openModal(`<h2>${esc(evidenceId)}</h2><div class="notice warn">This task was never given evidence ${esc(evidenceId)}.</div>`);
      return;
    }
    const detailInfo = row.detail || {};
    if (row.kind === "computation") {
      openModal(`
        <h2>${citeChip(row.evidence_id)} Computation</h2>
        <dl class="kv">
          <dt>Expression</dt><dd class="mono">${esc(detailInfo.expression || "")}</dd>
          <dt>Result</dt><dd class="mono"><b>${esc(detailInfo.result ?? "")}</b></dd>
          <dt>Tool</dt><dd>${esc(detailInfo.tool || "")}</dd>
          <dt>Classification</dt><dd>${levelPill(row.classification)}</dd>
        </dl>
        ${detailInfo.working ? `<h4 style="margin-top:12px">Working</h4><pre>${esc(detailInfo.working.join("\n"))}</pre>` : ""}
        ${detailInfo.stdout ? `<h4 style="margin-top:12px">Printed by the sandboxed script</h4><pre>${esc(detailInfo.stdout)}</pre>` : ""}
        <p class="muted small" style="margin-top:10px">${esc(row.text)}</p>`);
      return;
    }
    const bbox = row.bbox;
    const body = openModal(`
      <h2>${citeChip(row.evidence_id)} ${esc(row.title || detailInfo.title || "Document")}</h2>
      <div class="row small muted" style="margin-bottom:10px">
        version ${esc(row.version)} · page ${esc(row.page)} ·
        region ${bbox ? esc(bbox.map((v) => Number(v).toFixed(2)).join(", ")) : "whole page"} · ${levelPill(row.classification)}
      </div>
      <div class="evidence-grid">
        <div><div class="page-view" id="ev-page"><span class="spin"></span> loading the page…</div></div>
        <div><h4>Cited text</h4><div class="block">${esc(row.text)}</div>
          <p class="muted small">The highlighted box is the exact region this passage was read from — by OCR for a scan,
          from the text layer for a born-digital page. Readings by the vision model cite the page or the re-read region.</p></div>
      </div>`);
    try {
      const url = await blobUrl(`/api/documents/${row.document_id}/pages/${row.page}/image`);
      const holder = $("#ev-page", body);
      holder.innerHTML = `<img alt="page ${esc(row.page)}" src="${url}"/>`;
      if (bbox && bbox.length === 4) {
        const [x0, y0, x1, y1] = bbox.map(Number);
        const box = document.createElement("div");
        box.className = "box";
        Object.assign(box.style, {
          left: `${x0 * 100}%`, top: `${y0 * 100}%`, width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%`,
        });
        holder.appendChild(box);
      }
    } catch (error) {
      $("#ev-page", body).innerHTML = `<div class="notice warn">No page image available (${esc(error.message)}).</div>`;
    }
  }

  // ================================================================ WORKBENCH
  const EXAMPLES = [
    ["Approval note (scan → .docx)", "Prepare an approval note for continued service of heat exchanger E-101 based on the latest inspection report."],
    ["Sandboxed code", "Write and run a Python script that computes the remaining life of each CML on heat exchanger E-101 from the inspection readings, and report the worst location."],
    ["Vision: read the stamp", "What does the stamp on the scanned E-101 inspection report IR-2026-0147 say, and who signed it?"],
    ["Access-controlled question", "Summarise the E-101 flange leak incident and what it means for the exchanger."],
    ["Standard lookup", "What corrosion allowance does standard CS-12 require for carbon steel exchanger shells?"],
  ];

  views.work = async (root, taskId) => {
    const levels = allowedLevels(session.user.clearance);
    root.innerHTML = `
      <div class="view-head"><div><h1>Workbench</h1>
        <div class="sub">Describe the work in your own words. An agent plans it, uses only the tools and documents you may use,
        cites every fact, and hands any deliverable to an approver. Nothing runs inside this page — tasks run in the worker,
        and this view follows their journal live.</div></div></div>
      <div class="work">
        <div>
          <div class="card">
            <h3>New task</h3>
            <label for="goal">Goal</label>
            <textarea id="goal" placeholder="e.g. Prepare an approval note for E-101 from the latest inspection"></textarea>
            <label for="task-level">Task classification</label>
            <select id="task-level">${levels
              .map((l) => `<option value="${l}" ${l === levels[levels.length - 1] ? "selected" : ""}>${l}</option>`)
              .join("")}</select>
            <p class="muted small" style="margin-top:6px">The task sees nothing above this level, and everything it produces is marked with it.
              At CONFIDENTIAL the sandbox and scratch-file tools are unavailable (their registry ceiling is INTERNAL).</p>
            <div class="row"><button type="button" class="btn primary" id="submit-task">Submit task</button></div>
            <div class="examples">${EXAMPLES.map(([label, goal]) => `<span class="example" data-goal="${esc(goal)}">${esc(label)}</span>`).join("")}</div>
          </div>
          <div class="card">
            <div class="card-head"><h3>Your tasks</h3><span class="spacer"></span>
              <button type="button" class="btn small" id="refresh-tasks">Refresh</button></div>
            <div class="task-list" id="task-list"><span class="spin"></span></div>
          </div>
        </div>
        <div id="task-pane"><div class="card"><div class="empty">Choose a task, or submit a new one.</div></div></div>
      </div>`;
    $$(".example", root).forEach((chip) => chip.addEventListener("click", () => {
      $("#goal").value = chip.dataset.goal;
      $("#goal").focus();
    }));
    $("#submit-task").addEventListener("click", async () => {
      const goal = $("#goal").value.trim();
      if (!goal) return toast("Describe the goal first.", "bad");
      $("#submit-task").disabled = true;
      try {
        const task = await api("/api/tasks", { method: "POST", body: { goal, classification: $("#task-level").value } });
        $("#goal").value = "";
        toast("Task submitted — a worker will pick it up.");
        location.hash = `#/work/${task.id}`;
      } catch (error) {
        toast(error.message, "bad");
      } finally {
        $("#submit-task").disabled = false;
      }
    });
    $("#refresh-tasks").addEventListener("click", () => loadTaskList(taskId));
    await loadTaskList(taskId);
    if (taskId) await showTask(taskId);
    every(5000, () => loadTaskList(taskId, true));
  };

  async function loadTaskList(selected, quiet) {
    const list = $("#task-list");
    if (!list) return;
    try {
      const data = await api("/api/tasks");
      const tasks = data.tasks || [];
      if (!tasks.length) {
        list.innerHTML = `<div class="empty">No tasks yet.</div>`;
        return;
      }
      list.innerHTML = tasks
        .map(
          (t) => `
        <div class="task-item ${t.id === selected ? "selected" : ""}" data-task="${esc(t.id)}">
          <div class="goal">${esc(t.title || t.goal)}</div>
          <div class="meta">${statusPill(t.status)} ${levelPill(t.classification)} <span>${esc(ago(t.created_at))}</span></div>
        </div>`
        )
        .join("");
      $$(".task-item", list).forEach((item) =>
        item.addEventListener("click", () => (location.hash = `#/work/${item.dataset.task}`))
      );
    } catch (error) {
      if (!quiet) list.innerHTML = `<div class="notice bad">${esc(error.message)}</div>`;
    }
  }

  async function showTask(taskId) {
    const pane = $("#task-pane");
    pane.innerHTML = `<div class="card"><span class="spin"></span> loading…</div>`;
    const detail = await api(`/api/tasks/${taskId}`);
    app.taskCache.set(taskId, detail);
    const task = detail.task;
    pane.innerHTML = `
      <div class="card">
        <div class="task-head">
          <div class="goal">${esc(task.goal)}</div>
          <div class="row">
            <button type="button" class="btn small" id="probe-btn" title="Try to reach the internet from the API and the sandbox, attributed to this task">Run egress probe</button>
            <button type="button" class="btn small bad" id="cancel-btn">Cancel</button>
          </div>
        </div>
        <div class="task-facts" id="task-facts"></div>
        <div id="task-plan"></div>
      </div>
      <div class="card" id="result-card" hidden></div>
      <div class="card">
        <div class="card-head"><h3>Journal</h3><span class="muted small">every step, as the worker recorded it</span>
          <span class="spacer"></span><span id="live-flag"></span></div>
        <div class="timeline" id="timeline"></div>
      </div>
      <div class="card" id="task-sov"></div>`;
    renderFacts(detail);
    renderPlan(task.plan);
    const timeline = $("#timeline");
    let last = 0;
    for (const entry of detail.journal) {
      appendStep(timeline, entry, taskId);
      last = Math.max(last, entry.step_seq);
    }
    renderResult(detail);
    renderTaskSovereignty(taskId);
    $("#cancel-btn").hidden = QUIET.has(task.status) || task.submitted_by !== session.user.user_id;
    $("#cancel-btn").addEventListener("click", async () => {
      try {
        await api(`/api/tasks/${taskId}/cancel`, { method: "POST" });
        toast("Cancellation requested.");
      } catch (error) {
        toast(error.message, "bad");
      }
    });
    $("#probe-btn").addEventListener("click", async () => {
      $("#probe-btn").disabled = true;
      try {
        const report = await api(`/api/tasks/${taskId}/probe`, { method: "POST" });
        toast(report.egress_blocked ? "Probe: every outbound attempt was stopped. The task carries on." : "Probe: EGRESS WAS NOT BLOCKED", report.egress_blocked ? "good" : "bad");
        renderTaskSovereignty(taskId);
      } catch (error) {
        toast(error.message, "bad");
      } finally {
        $("#probe-btn").disabled = false;
      }
    });
    timeline.addEventListener("click", (event) => {
      const chip = event.target.closest("[data-cite]");
      if (chip) showEvidence(taskId, chip.dataset.cite);
    });
    $("#result-card").addEventListener("click", (event) => {
      const chip = event.target.closest("[data-cite]");
      if (chip) showEvidence(taskId, chip.dataset.cite);
    });
    if (!QUIET.has(task.status)) followTask(taskId, last);
  }

  function renderFacts(detail) {
    const task = detail.task;
    const usage = task.usage || {};
    const elapsed = task.started_at ? Math.round(((task.finished_at ? new Date(task.finished_at) : new Date()) - new Date(task.started_at)) / 1000) : null;
    $("#task-facts").innerHTML = `
      <span>${statusPill(task.status)}</span>
      <span>${levelPill(task.classification)}</span>
      <span>by <b>${esc(task.submitted_by_name || task.submitted_by)}</b> (${esc(task.department)})</span>
      <span>steps <b>${esc(usage.steps ?? "—")}</b></span>
      <span>model calls <b>${esc(usage.model_calls ?? "—")}</b></span>
      <span>tokens <b>${esc((usage.prompt_tokens || 0) + (usage.completion_tokens || 0))}</b></span>
      <span>GPU queue wait <b>${ms(usage.queue_wait_ms)}</b></span>
      <span>elapsed <b>${elapsed === null ? "—" : `${elapsed}s`}</b></span>
      ${task.error ? `<span class="pill failed">${esc(task.error)}</span>` : ""}`;
  }

  function renderPlan(plan) {
    const holder = $("#task-plan");
    if (!holder) return;
    if (!plan || !plan.steps) {
      holder.innerHTML = "";
      return;
    }
    holder.innerHTML = `
      <h4 style="margin-top:8px">Plan${plan.deliverable ? ` · deliverable: ${esc(plan.deliverable)}` : ""} · ${esc(String(plan.primary_capability || "").replace("_", " "))}</h4>
      <ol class="plan-steps">${plan.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>`;
  }

  function renderResult(detail) {
    const card = $("#result-card");
    if (!card) return;
    const task = detail.task;
    const result = task.result || {};
    const artifacts = detail.artifacts || [];
    if (!result.answer && !artifacts.length) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    card.innerHTML = `
      <div class="card-head"><h3>Result</h3>${statusPill(task.status)}</div>
      ${result.answer ? `<div class="answer">${renderAnswer(result.answer)}</div>` : ""}
      ${artifacts.map(renderArtifactRow).join("")}`;
    bindArtifactButtons(card);
  }

  function renderArtifactRow(a) {
    const verification = a.verification || {};
    const tiers = (verification.tiers || [])
      .map((t) => `<span class="pill ${esc(t.status)}" title="${esc((t.issues || []).join("; "))}">${esc(t.tier)} ${esc(t.name)}: ${esc(t.status)}</span>`)
      .join(" ");
    return `
      <div class="artifact">
        <div class="t">${esc(a.title || a.filename)}<div class="muted small mono">${esc(a.filename)} · sha256 ${esc(String(a.sha256 || "").slice(0, 16))}…</div></div>
        ${statusPill(a.status)} ${a.requires_approval ? `<span class="pill">approval required</span>` : ""}
        <div style="flex-basis:100%">${tiers}</div>
        <div class="row">
          ${["docx", "xlsx", "py", "txt", "csv", "json", "md"].includes(a.kind) ? `<button type="button" class="btn small" data-preview="${esc(a.id)}">Preview</button>` : ""}
          <button type="button" class="btn small" data-download="${esc(a.id)}" data-name="${esc(a.filename)}">Download</button>
          ${a.status === "RELEASED" ? `<button type="button" class="btn small" data-provenance="${esc(a.id)}">Provenance</button>` : ""}
        </div>
      </div>`;
  }

  function bindArtifactButtons(root) {
    $$("[data-download]", root).forEach((b) => b.addEventListener("click", () =>
      download(`/api/artifacts/${b.dataset.download}/download`, b.dataset.name).catch((e) => toast(e.message, "bad"))));
    $$("[data-preview]", root).forEach((b) => b.addEventListener("click", () => previewArtifact(b.dataset.preview)));
    $$("[data-provenance]", root).forEach((b) => b.addEventListener("click", () =>
      download(`/api/artifacts/${b.dataset.provenance}/provenance`, `provenance-${b.dataset.provenance}.json`).catch((e) => toast(e.message, "bad"))));
  }

  async function previewArtifact(artifactId) {
    const body = openModal(`<h2>Preview</h2><div class="preview-doc"><span class="spin"></span></div>`);
    try {
      const response = await api(`/api/artifacts/${artifactId}/preview`, { raw: true });
      // The server builds this HTML itself, escaping every string it takes from the file.
      $(".preview-doc", body).innerHTML = await response.text();
    } catch (error) {
      $(".preview-doc", body).innerHTML = `<div class="notice bad">${esc(error.message)}</div>`;
    }
  }

  async function followTask(taskId, after) {
    stopStream();
    const controller = new AbortController();
    app.stream = { taskId, controller };
    const flag = $("#live-flag");
    if (flag) flag.innerHTML = `<span class="spin"></span> <span class="small muted">live</span>`;
    let lastSeq = after;
    try {
      const response = await api(`/api/tasks/${taskId}/events?after=${after}`, { raw: true, signal: controller.signal });
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let index;
        while ((index = buffer.indexOf("\n\n")) >= 0) {
          const chunk = buffer.slice(0, index);
          buffer = buffer.slice(index + 2);
          let event = "message";
          const data = [];
          for (const line of chunk.split("\n")) {
            if (line.startsWith("event: ")) event = line.slice(7);
            else if (line.startsWith("data: ")) data.push(line.slice(6));
          }
          if (!data.length) continue;
          const payload = JSON.parse(data.join("\n"));
          if (event === "step") {
            lastSeq = Math.max(lastSeq, payload.step_seq);
            const timeline = $("#timeline");
            if (!timeline || app.stream?.taskId !== taskId) return;
            appendStep(timeline, payload, taskId, true);
            if (["finished", "failed", "cancelled", "planned", "replanned", "tool_result", "model_call", "decision", "revision_requested"].includes(payload.step_type)) {
              refreshTaskHeader(taskId);
            }
          } else if (event === "end") {
            break;
          }
        }
      }
    } catch (error) {
      if (error.name === "AbortError") return;
    } finally {
      if (app.stream && app.stream.taskId === taskId) app.stream = null;
      const f = $("#live-flag");
      if (f) f.innerHTML = "";
    }
    const latest = await api(`/api/tasks/${taskId}`).catch(() => null);
    if (latest && !QUIET.has(latest.task.status) && $("#timeline") && location.hash.includes(taskId)) {
      setTimeout(() => followTask(taskId, lastSeq), 1500);
    }
  }

  let headerRefresh = null;
  function refreshTaskHeader(taskId) {
    clearTimeout(headerRefresh);
    headerRefresh = setTimeout(async () => {
      try {
        const detail = await api(`/api/tasks/${taskId}`);
        app.taskCache.set(taskId, detail);
        if (!$("#task-facts")) return;
        renderFacts(detail);
        renderPlan(detail.task.plan);
        renderResult(detail);
        const cancel = $("#cancel-btn");
        if (cancel) cancel.hidden = QUIET.has(detail.task.status) || detail.task.submitted_by !== session.user.user_id;
        if (QUIET.has(detail.task.status)) renderTaskSovereignty(taskId);
      } catch (_) { /* next event will retry */ }
    }, 250);
  }

  async function renderTaskSovereignty(taskId) {
    const card = $("#task-sov");
    if (!card) return;
    try {
      const report = await api(`/api/tasks/${taskId}/sovereignty`);
      const c = report.counts;
      card.innerHTML = `
        <div class="card-head"><h3>Sovereignty for this task</h3><span class="spacer"></span>
          <button type="button" class="btn small" id="export-sov">Export report</button></div>
        <div class="notice ${c.observed ? "bad" : "good"}">${esc(report.verdict)}</div>
        <div class="stat-row">
          ${stat("Outbound attempts", c.attempts)}${stat("Blocked", c.blocked)}${stat("DNS denied", c.dns_denied)}
          ${stat("Connections observed", c.observed, c.observed ? "bad" : "good")}
          ${stat("Model calls (local)", (report.model_calls || []).reduce((n, m) => n + Number(m.calls || 0), 0))}
        </div>
        ${(report.events || []).length ? `<details><summary>${report.events.length} recorded event(s)</summary>${egressTable(report.events)}</details>` : ""}`;
      $("#export-sov", card).addEventListener("click", () =>
        download(`/api/tasks/${taskId}/sovereignty`, `sovereignty-${taskId}.json`).catch((e) => toast(e.message, "bad")));
    } catch (error) {
      card.innerHTML = `<div class="notice warn">${esc(error.message)}</div>`;
    }
  }

  function stat(label, value, tone = "", note = "") {
    return `<div class="stat"><div class="label">${esc(label)}</div><div class="value ${tone}">${esc(value ?? "—")}</div>${note ? `<div class="note">${esc(note)}</div>` : ""}</div>`;
  }

  function egressTable(events) {
    return `<div class="table-wrap"><table><thead><tr><th>When</th><th>Process</th><th>Kind</th><th>Detector</th><th>Destination</th><th>Agent</th><th>Detail</th></tr></thead><tbody>
      ${events
        .map(
          (e) => `<tr><td class="nowrap">${esc(ago(e.occurred_at))}</td><td>${esc(e.process)}</td><td>${statusPill(e.kind === "observed" ? "failed" : "denied").replace(/>[^<]*</, `>${esc(e.kind)}<`)}</td>
          <td>${esc(e.detector)}</td><td class="mono">${esc(e.destination)}${e.port ? `:${esc(e.port)}` : ""}</td><td>${esc(e.agent_id || "")}</td>
          <td class="small">${esc((e.detail && (e.detail.error || e.detail.outcome)) || "")}</td></tr>`
        )
        .join("")}</tbody></table></div>`;
  }

  // ---------------------------------------------------------------- journal steps
  function appendStep(timeline, entry, taskId, live) {
    const html = renderStep(entry, taskId);
    if (!html) return;
    const wrapper = document.createElement("div");
    wrapper.innerHTML = html;
    const node = wrapper.firstElementChild;
    timeline.appendChild(node);
    if (live) node.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function stepShell(kind, icon, title, detail = "") {
    return `<div class="step ${kind}"><div class="icon">${icon}</div><div><div class="title">${title}</div>${detail ? `<div class="detail">${detail}</div>` : ""}</div></div>`;
  }

  function renderStep(entry, taskId) {
    const p = entry.payload || {};
    switch (entry.step_type) {
      case "submitted":
        return stepShell("", "•", `Submitted by <b>${esc(p.name || p.by)}</b> (${esc(p.department || "")}) at ${levelPill(p.classification)}`);
      case "claimed":
        return stepShell("", "▶", `${p.resumed ? "Resumed from the journal" : "Picked up"} by <b>${esc(p.worker)}</b> as agent <span class="mono">${esc(p.agent)}</span>`,
          `Tools the policy offers for this task: ${(p.tools || []).map((t) => `<span class="pill">${esc(t)}</span>`).join(" ")}
           ${(p.withheld_tools || []).length ? `<details><summary>${p.withheld_tools.length} tool(s) withheld</summary>${p.withheld_tools.map((w) => `<div class="small"><b>${esc(w.name)}</b> — ${esc(w.why)}</div>`).join("")}</details>` : ""}`);
      case "model_call":
        return renderModelCall(p);
      case "planned":
      case "replanned":
        return stepShell("", "≡", `<b>${entry.step_type === "planned" ? "Planned" : "Replanned"}</b>${p.reason ? ` — ${esc(p.reason)}` : ""}`,
          `<ol class="plan-steps">${((p.plan || {}).steps || []).map((s) => `<li>${esc(s)}</li>`).join("")}</ol>`);
      case "thought":
        return stepShell("", "…", `<span class="thought">${esc(p.text)}</span>`);
      case "tool_call":
        return stepShell("tool", "→", `Calls <b>${esc(p.tool)}</b>`, `<span class="mono small">${esc(JSON.stringify(p.arguments)).slice(0, 400)}</span>`);
      case "tool_result":
        return renderToolResult(p);
      case "waiting":
        return stepShell("wait", "⏳", `Waiting on <b>${esc(String(p.resource).toUpperCase())}</b> admission (queue depth ${esc(p.queue_depth)}) for ${esc(p.purpose)}`,
          "Queued, not hung: model calls are admitted a few at a time so the GPU is never oversubscribed.");
      case "progress":
        return stepShell("wait", "⋯", esc(p.note || `${p.kind || "work"}: ${p.state || ""}${p.sandbox ? ` (${p.sandbox} sandbox)` : ""}`));
      case "revision":
        return stepShell("wait", "↺", `<b>Revision</b> after review: ${esc(p.comment)}`);
      case "revision_requested":
        return stepShell("wait", "↺", `<b>Sent back for revision</b> (${esc(p.revision)} of 1): ${esc(p.comment)}`);
      case "decision":
        return stepShell(p.approved ? "done" : "denied", p.approved ? "✓" : "✗", `<b>${p.approved ? "Approved" : "Rejected"}</b> by ${esc(p.name || p.by)}`, esc(p.comment || ""));
      case "probe": {
        const api_ = (p.api || {}).results || [];
        const sb = (p.sandbox || {}).results || [];
        return stepShell(p.egress_blocked ? "done" : "failed", "⛨", `<b>Deliberate egress probe</b> — ${p.egress_blocked ? "every attempt stopped; the task carried on" : "EGRESS NOT BLOCKED"}`,
          [...api_.map((r) => `api → ${esc(r.target)}: <b>${esc(r.outcome)}</b>`), ...sb.map((r) => `sandbox → ${esc(r.target)}: <b>${esc(r.outcome)}</b>`)].join("<br/>"));
      }
      case "cancel_requested":
        return stepShell("wait", "■", `Cancellation requested by ${esc(p.by)}`);
      case "finished":
        return stepShell("done", "✓", `<b>Finished</b> — ${statusPill(p.status)}`, p.answer ? `<div class="answer">${renderAnswer(p.answer)}</div>` : "");
      case "failed":
        return stepShell("failed", "!", `<b>Failed</b>`, esc(p.error || ""));
      case "cancelled":
        return stepShell("failed", "■", `<b>Cancelled</b>`, esc(p.error || ""));
      default:
        return stepShell("", "·", esc(entry.step_type), `<span class="mono small">${esc(JSON.stringify(p)).slice(0, 300)}</span>`);
    }
  }

  function renderModelCall(p) {
    const usage = p.usage || {};
    const candidates = (p.candidates || [])
      .map((c) => `<tr><td class="mono">${esc(c.model_id)}</td><td>${c.eligible ? "eligible" : "—"}</td><td class="right">${esc(c.total)}</td><td class="small">${esc(c.summary)}</td></tr>`)
      .join("");
    return stepShell("model", "M",
      `<b>${esc(p.purpose)}</b> → <b class="mono">${esc(p.model_id)}</b> <span class="muted mono small">(${esc(p.tag)})</span>
       · ${ms(p.latency_ms)} · ${esc(usage.prompt || 0)}→${esc(usage.completion || 0)} tokens
       ${p.queue_wait_ms ? `· queued ${ms(p.queue_wait_ms)}` : ""}${p.fallback_used ? ` · <span class="pill failed">fallback</span>` : ""}`,
      `<details><summary>Why this model</summary>
        <p class="small">${esc(p.reason)}</p>
        <div class="table-wrap"><table class="score-table"><thead><tr><th>Candidate</th><th></th><th class="right">Score</th><th>Breakdown</th></tr></thead><tbody>${candidates}</tbody></table></div>
        ${(p.degraded || []).length ? `<div class="notice warn">${esc(p.degraded.join("; "))}</div>` : ""}
      </details>`);
  }

  function policyLine(p) {
    const d = p.decision || {};
    if (!d.rule_id && !d.effect) return "";
    const receipt = p.receipt ? ` · receipt <span class="mono">${esc(String(p.receipt.decision_id || "").slice(0, 14))}</span> verified by <b>${esc(p.receipt.verified_by || "—")}</b>` : "";
    return `<div class="small muted">policy: <b>${esc(d.rule_id || "default deny")}</b>${d.reason ? ` (${esc(d.reason)})` : ""}${receipt}</div>`;
  }

  function renderToolResult(p) {
    const out = p.output || {};
    const kind = p.status === "ok" ? "tool" : p.status === "denied" ? "denied" : "failed";
    let body = "";
    if (Array.isArray(out.passages)) {
      body += `<div class="passages">${out.passages
        .slice(0, 8)
        .map((x) => `<div class="passage">${citeChip(x.evidence_id)} <span class="src">${esc(x.document)} · p${esc(x.page)} · ${levelPill(x.classification)}</span><div>${esc(String(x.text || "").slice(0, 280))}</div></div>`)
        .join("")}</div>`;
      const withheld = out.withheld_documents || {};
      if (withheld.count) {
        body += `<div class="denied-box"><b>${esc(withheld.count)} matching document(s) withheld</b> — ${Object.entries(withheld.reasons || {})
          .map(([reason, n]) => `${esc(n)} by ${esc(reason.replace(/_/g, " "))}`)
          .join(", ")}. Their titles and text are not shown to you or to the agent.</div>`;
      }
    }
    if (Array.isArray(out.blocks)) {
      body += `<div class="small">${esc(out.document)} · page ${esc(out.page)}/${esc(out.page_count)} · ${esc(out.blocks.length)} blocks (${esc(out.text_source)}) ${(out.evidence || []).map((e) => citeChip(e.evidence_id)).join("")}</div>`;
    }
    if (out.working) {
      body += `<div class="mono small">${esc(out.expression)} = <b>${esc(out.result)}</b> ${citeChip(out.evidence_id)}</div><pre>${esc(out.working.join("\n"))}</pre>`;
    }
    if (out.stdout !== undefined) {
      body += `<div class="small">exit <b>${esc(out.exit_code)}</b> · ${ms(out.duration_ms)} · ${esc(out.sandbox || "")} ${citeChip(out.evidence_id)}</div>
        ${(p.detail || {}).source ? `<details><summary>Script</summary><pre>${esc(p.detail.source)}</pre></details>` : ""}
        <pre>${esc(out.stdout || "(no output)")}${out.stderr ? `\n--- stderr ---\n${esc(out.stderr)}` : ""}</pre>
        ${(out.files_written || []).length ? `<div class="small">wrote ${out.files_written.map((f) => `<span class="mono">${esc(f)}</span>`).join(", ")}</div>` : ""}`;
    }
    if (Array.isArray(out.verification)) {
      body += `<div class="tiers">${out.verification
        .map((t) => `<div class="tier ${esc(t.status)}"><div class="name">${esc(t.tier)}. ${esc(t.name)} — ${esc(t.status)}</div>${(t.issues || []).length ? `<ul>${t.issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : ""}</div>`)
        .join("")}</div>
        <div class="small">${statusPill(out.artifact_status)} <span class="mono">${esc(out.filename)}</span></div>`;
      if ((out.flagged_claims || []).length) {
        body += `<div class="notice warn small">Numbers that could not be traced to their cited evidence: ${out.flagged_claims.map((c) => `<b>${esc(c.number)}</b> (${esc(c.section)})`).join(", ")}</div>`;
      }
    }
    if (out.fields && out.text !== undefined && !out.blocks) {
      body += `<div class="small">${citeChip(out.evidence_id)} read by <b class="mono">${esc(out.model)}</b></div><pre>${esc(out.text)}</pre>`;
    }
    if (!body && p.status === "ok") {
      body = `<details><summary>Output</summary><pre>${json(out)}</pre></details>`;
    }
    if (p.status !== "ok") body = `<div class="small">${esc(p.error || "")}</div>` + body;
    return stepShell(kind, kind === "tool" ? "✓" : "✗",
      `<b>${esc(p.tool)}</b> ${statusPill(p.status)} ${esc(p.summary || "")} <span class="muted small">${ms(p.duration_ms)}</span>`,
      policyLine(p) + body);
  }

  // ================================================================ DOCUMENTS
  views.documents = async (root) => {
    const levels = allowedLevels(session.user.clearance);
    const departments = Array.from(new Set(app.demoUsers.map((u) => u.department).concat([session.user.department]))).sort();
    root.innerHTML = `
      <div class="view-head"><div><h1>Documents</h1>
        <div class="sub">The corpus as <b>you</b> may see it: ${levelPill(session.user.clearance)} and below, where
        <span class="pill dept">${esc(session.user.department)}</span> is on the access list. Scans are OCR'd with regions and read once by the
        vision model at ingest, so agents reason over rows, not pixels.</div></div></div>
      <div class="grid-2" style="grid-template-columns: minmax(0,2fr) minmax(0,1fr)">
        <div class="card"><div class="card-head"><h3>Visible documents</h3><span class="spacer"></span>
          <button type="button" class="btn small" id="doc-refresh">Refresh</button></div>
          <div id="doc-table"><span class="spin"></span></div></div>
        <div class="card">
          <h3>Upload</h3>
          <p class="muted small">Classification and access list are mandatory. The door refuses a level above your clearance or the
            deployment's ceiling, and an access list that leaves out your own department.</p>
          <label for="up-file">File (PDF, image, DOCX, text)</label><input type="file" id="up-file" />
          <label for="up-title">Title</label><input type="text" id="up-title" />
          <label for="up-level">Classification</label>
          <select id="up-level"><option value="">— choose —</option>${levels.map((l) => `<option>${l}</option>`).join("")}</select>
          <label>Access list (departments)</label>
          <div class="row">${departments.map((d) => `<label class="row small" style="margin:0;font-weight:500"><input type="checkbox" class="up-dept" value="${esc(d)}" ${d === session.user.department ? "checked" : ""}/> ${esc(d)}</label>`).join("")}</div>
          <div class="row" style="margin-top:12px"><button type="button" class="btn primary" id="up-go">Upload</button></div>
        </div>
      </div>`;
    const load = async () => {
      const data = await api("/api/documents");
      const docs = data.documents || [];
      $("#doc-table").innerHTML = docs.length
        ? `<div class="table-wrap"><table><thead><tr><th>Title</th><th>Level</th><th>Access list</th><th>Status</th><th>Pages</th><th>Ingest</th></tr></thead><tbody>
          ${docs
            .map((d) => {
              const r = d.ingest_report || {};
              const ingest = d.status === "ready"
                ? `${Object.entries(r.text_sources || {}).map(([k, v]) => `${esc(k)} ${esc(v)}`).join(", ")}${r.ocr_confidence ? ` · OCR ${esc(r.ocr_confidence)}%` : ""}${(r.vision_pages || []).length ? ` · vision p${esc(r.vision_pages.join(","))}` : ""} · ${esc(r.chunks ?? "")} chunks`
                : esc(r.stage || d.error || "");
              return `<tr class="clickable" data-doc="${esc(d.id)}" data-pages="${esc(d.page_count || 1)}"><td><b>${esc(d.title)}</b><div class="muted small">${esc(d.filename)} · v${esc(d.version)} · ${esc(d.uploaded_by)}</div></td>
                <td>${levelPill(d.classification)}</td><td>${deptPills(d.acl)}</td><td>${statusPill(d.status)}</td><td>${esc(d.page_count ?? "")}</td><td class="small">${ingest}</td></tr>`;
            })
            .join("")}</tbody></table></div>`
        : `<div class="empty">No documents you may see yet. The demonstration corpus is added once the approved models are installed — see Models &amp; routing.</div>`;
      $$("tr[data-doc]").forEach((row) => row.addEventListener("click", () => viewDocument(row.dataset.doc, 1, Number(row.dataset.pages))));
      return docs;
    };
    const docs = await load();
    $("#doc-refresh").addEventListener("click", load);
    every(4000, async () => {
      if (!$("#doc-table")) return;
      const current = await load().catch(() => []);
      if (!current.some((d) => d.status === "pending" || d.status === "processing")) return;
    });
    $("#up-go").addEventListener("click", async () => {
      const file = $("#up-file").files[0];
      if (!file) return toast("Choose a file.", "bad");
      const form = new FormData();
      form.append("file", file);
      if ($("#up-title").value.trim()) form.append("title", $("#up-title").value.trim());
      if ($("#up-level").value) form.append("classification", $("#up-level").value);
      const acl = $$(".up-dept").filter((c) => c.checked).map((c) => c.value).join(",");
      if (acl) form.append("acl", acl);
      try {
        await api("/api/documents", { method: "POST", body: form });
        toast("Uploaded — the worker is ingesting it now.", "good");
        $("#up-file").value = "";
        $("#up-title").value = "";
        load();
      } catch (error) {
        toast(`${error.message}${error.body && error.body.code ? ` [${error.body.code}]` : ""}`, "bad");
      }
    });
    return docs;
  };

  async function viewDocument(documentId, page, pageCount) {
    const body = openModal(`<div id="doc-modal"><span class="spin"></span></div>`);
    const holder = $("#doc-modal", body);
    try {
      const data = await api(`/api/documents/${documentId}/pages/${page}`);
      const doc = data.document;
      holder.innerHTML = `
        <h2>${esc(doc.title)}</h2>
        <div class="row small muted" style="margin-bottom:10px">${levelPill(doc.classification)} ${deptPills(doc.acl)}
          · version ${esc(data.version)} · page ${esc(page)} of ${esc(doc.page_count)} · ${esc((data.page || {}).text_source || "")}
          <span class="spacer"></span>
          <button type="button" class="btn small" id="pg-prev" ${page <= 1 ? "disabled" : ""}>‹ Prev</button>
          <button type="button" class="btn small" id="pg-next" ${page >= (doc.page_count || pageCount) ? "disabled" : ""}>Next ›</button></div>
        <div class="evidence-grid"><div><div class="page-view" id="pg-image">${(data.page || {}).has_image ? `<span class="spin"></span>` : `<div class="notice info">No page image (text layer only).</div>`}</div></div>
          <div class="blocks">${(data.blocks || [])
            .map((b, i) => `<div class="block ${b.low_confidence ? "low" : ""}" data-block="${i}"><div class="k">${esc(b.kind)} · ${esc(b.source)}${b.confidence ? ` · conf ${Math.round(b.confidence)}` : ""}</div>${esc(b.text)}</div>`)
            .join("")}</div></div>`;
      $("#pg-prev", holder)?.addEventListener("click", () => viewDocument(documentId, page - 1, pageCount));
      $("#pg-next", holder)?.addEventListener("click", () => viewDocument(documentId, page + 1, pageCount));
      if ((data.page || {}).has_image) {
        const url = await blobUrl(`/api/documents/${documentId}/pages/${page}/image`);
        const image = $("#pg-image", holder);
        image.innerHTML = `<img alt="page" src="${url}"/>`;
        $$(".block", holder).forEach((node) => {
          const block = data.blocks[Number(node.dataset.block)];
          if (!block.bbox) return;
          node.addEventListener("mouseenter", () => {
            const [x0, y0, x1, y1] = block.bbox.map(Number);
            const box = document.createElement("div");
            box.className = "box";
            Object.assign(box.style, { left: `${x0 * 100}%`, top: `${y0 * 100}%`, width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%` });
            image.appendChild(box);
          });
          node.addEventListener("mouseleave", () => $$(".box", image).forEach((b) => b.remove()));
        });
      }
    } catch (error) {
      holder.innerHTML = `<div class="notice bad">${esc(error.message)}</div>`;
    }
  }

  // ================================================================ SEARCH & ACCESS
  views.search = async (root) => {
    root.innerHTML = `
      <div class="view-head"><div><h1>Search &amp; access</h1>
        <div class="sub">Hybrid retrieval (dense vectors + keywords) with the permission check inside the query itself.
        Run the same question as each demonstration identity to see different evidence — and, for each, how many
        documents matched but were withheld, and why. Withheld documents never show a title or a line of text.</div></div></div>
      <div class="card">
        <div class="row"><input type="search" id="q" class="grow" placeholder="e.g. E-101 flange leak corrosion" value="E-101 flange leak corrosion history" />
          <button type="button" class="btn primary" id="q-me">Search as me</button>
          <button type="button" class="btn" id="q-compare">Compare all demo identities</button></div>
      </div>
      <div id="search-out" style="margin-top:14px"></div>`;
    const run = async (compare) => {
      const query = $("#q").value.trim();
      if (!query) return;
      const out = $("#search-out");
      out.innerHTML = `<span class="spin"></span>`;
      try {
        if (!compare) {
          const result = await api("/api/search", { method: "POST", body: { query } });
          out.innerHTML = `<div class="card">${searchColumn(session.user, result)}</div>`;
          return;
        }
        const columns = await Promise.all(
          app.demoUsers.map(async (u) => {
            const login = await api("/api/auth/session", { method: "POST", body: { user_id: u.user_id } }, null);
            const result = await api("/api/search", { method: "POST", body: { query } }, login.token);
            return `<div class="card">${searchColumn(u, result)}</div>`;
          })
        );
        out.innerHTML = `<div class="compare">${columns.join("")}</div>`;
      } catch (error) {
        out.innerHTML = `<div class="notice bad">${esc(error.message)}</div>`;
      }
    };
    $("#q-me").addEventListener("click", () => run(false));
    $("#q-compare").addEventListener("click", () => run(true));
    $("#q").addEventListener("keydown", (e) => e.key === "Enter" && run(false));
  };

  function searchColumn(user, result) {
    const hits = result.hits || [];
    const denied = result.denied || [];
    return `
      <div class="card-head"><h3>${esc(user.username)}</h3><span class="pill dept">${esc(user.department)}</span>${levelPill(user.clearance)}</div>
      <div class="small muted">${esc(hits.length)} passage(s) from ${esc(new Set(hits.map((h) => h.document_id)).size)} document(s)
        · levels ${esc((result.stats.allowed_levels || []).join(", "))}${(result.stats.degraded || []).length ? ` · <span class="pill failed">${esc(result.stats.degraded.join("; "))}</span>` : ""}</div>
      <div class="passages">${hits
        .map((h) => `<div class="passage"><div class="src"><b>${esc(h.title)}</b> · p${esc(h.page)} · ${levelPill(h.classification)} · score ${esc(h.score)}</div>${esc(String(h.text).slice(0, 240))}</div>`)
        .join("") || `<div class="empty">Nothing you may see matched.</div>`}</div>
      ${denied.length
        ? `<div class="denied-box"><b>${esc(denied.length)} matching document(s) withheld</b>${denied
            .map((d) => `<div class="small">${levelPill(d.classification)} ${deptPills(d.acl)} — ${esc(String(d.reason).replace(/_/g, " "))} (${esc(d.matching_chunks)} matching passage(s))</div>`)
            .join("")}</div>`
        : `<div class="notice good small" style="margin-top:8px">Nothing that matched was withheld.</div>`}`;
  }

  // ================================================================ APPROVALS
  views.approvals = async (root, artifactId) => {
    const approver = session.has("approver");
    root.innerHTML = `
      <div class="view-head"><div><h1>Approvals</h1>
        <div class="sub">A deliverable that passed verification tiers 1–3 waits here for a person. Approval re-renders it with
        your name in the approval block, re-verifies it, freezes it and records one immutable provenance record. Rejection
        sends the task back for exactly one revision, with your comment.</div></div></div>
      <div class="work">
        <div>
          <div class="card"><h3>${approver ? "Awaiting your decision" : "Awaiting approval"}</h3><div id="pending"><span class="spin"></span></div></div>
          <div class="card"><h3>Recent deliverables</h3><div id="recent"><span class="spin"></span></div></div>
        </div>
        <div id="artifact-pane"><div class="card"><div class="empty">Choose a deliverable.</div></div></div>
      </div>`;
    const [pending, recent] = await Promise.all([api("/api/artifacts?awaiting=1"), api("/api/artifacts")]);
    const item = (a) => `
      <div class="task-item ${a.id === artifactId ? "selected" : ""}" data-artifact="${esc(a.id)}">
        <div class="goal">${esc(a.title || a.filename)}</div>
        <div class="meta">${statusPill(a.status)} ${levelPill(a.classification)} <span>${esc(a.submitted_by_name || a.submitted_by)}</span> <span>${esc(ago(a.created_at))}</span>
          ${Number(a.flagged) ? `<span class="pill flagged">${esc(a.flagged)} flagged</span>` : ""}</div></div>`;
    $("#pending").innerHTML = (pending.artifacts || []).map(item).join("") || `<div class="empty">Nothing is waiting.</div>`;
    $("#recent").innerHTML = (recent.artifacts || []).slice(0, 25).map(item).join("") || `<div class="empty">No deliverables yet.</div>`;
    $$("[data-artifact]", root).forEach((n) => n.addEventListener("click", () => (location.hash = `#/approvals/${n.dataset.artifact}`)));
    if (artifactId) await showArtifact(artifactId);
  };

  async function showArtifact(artifactId) {
    const pane = $("#artifact-pane");
    pane.innerHTML = `<div class="card"><span class="spin"></span></div>`;
    const data = await api(`/api/artifacts/${artifactId}`);
    const a = data.artifact;
    const v = a.verification || {};
    const provenance = a.provenance || {};
    const sources = provenance.sources || [];
    const canDecide = session.has("approver") && a.status === "VERIFIED" && a.requires_approval && !(data.approvals || []).length && a.task_owner !== session.user.user_id;
    pane.innerHTML = `
      <div class="card">
        <div class="card-head"><h2>${esc(a.title || a.filename)}</h2>${statusPill(a.status)} ${levelPill(a.classification)}</div>
        <dl class="kv">
          <dt>Task goal</dt><dd>${esc(a.goal)} <a href="#/work/${esc(a.task_id)}">open task</a></dd>
          <dt>Asked by</dt><dd>${esc(a.task_owner)} (${esc(a.owner_department)})</dd>
          <dt>File</dt><dd class="mono">${esc(a.filename)} · v${esc(a.version)}</dd>
          <dt>SHA-256</dt><dd class="mono small">${esc(a.sha256)}</dd>
          ${provenance.draft_sha256 && provenance.draft_sha256 !== a.sha256 ? `<dt>Verified draft</dt><dd class="mono small">${esc(provenance.draft_sha256)}</dd>` : ""}
        </dl>
        <h4 style="margin-top:12px">Verification</h4>
        <div class="tiers">${(v.tiers || [])
          .map((t) => `<div class="tier ${esc(t.status)}"><div class="name">${esc(t.tier)}. ${esc(t.name)} — ${esc(t.status)}</div>${(t.issues || []).length ? `<ul>${t.issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : ""}</div>`)
          .join("")}</div>
        ${(v.flagged_claims || []).length ? `<div class="notice warn">Numbers not traceable to the evidence they cite: ${v.flagged_claims.map((c) => `<b>${esc(c.number)}</b> in ${esc(c.section)} — “${esc(c.text)}”`).join("; ")}</div>` : ""}
        <h4 style="margin-top:12px">Sources</h4>
        <div class="table-wrap"><table><thead><tr><th>Ref</th><th>Source</th><th>Version</th><th>Page</th><th>Region</th></tr></thead><tbody>
          ${sources.map((s) => `<tr><td>[${esc(s.number)}] <span class="mono small">${esc(s.evidence_id)}</span></td><td>${esc(s.title)}</td><td>${esc(s.version ?? "—")}</td><td>${esc(s.page ?? "—")}</td><td class="mono small">${s.bbox ? esc(s.bbox.map((x) => Number(x).toFixed(2)).join(", ")) : esc(s.kind === "computation" ? "computed" : "whole page")}</td></tr>`).join("")}
        </tbody></table></div>
        ${(data.approvals || []).map((d) => `<div class="notice ${d.decision === "approved" ? "good" : "bad"}"><b>${esc(d.decision)}</b> by ${esc(d.display_name || d.approver)} ${esc(ago(d.decided_at))}${d.reason ? ` — ${esc(d.reason)}` : ""}</div>`).join("")}
        <div class="row" style="margin-top:10px">
          <button type="button" class="btn" data-download="${esc(a.id)}" data-name="${esc(a.filename)}">Download</button>
          ${a.status === "RELEASED" ? `<button type="button" class="btn" data-provenance="${esc(a.id)}">Provenance record</button>` : ""}
        </div>
      </div>
      ${canDecide ? `<div class="card"><h3>Your decision</h3>
        <label for="decision-comment">Comment (required to reject)</label><textarea id="decision-comment"></textarea>
        <div class="row" style="margin-top:8px"><button type="button" class="btn good" id="approve">Approve and release</button>
          <button type="button" class="btn bad" id="reject">Reject (one revision)</button></div></div>` : ""}
      ${a.status === "VERIFIED" && session.has("approver") && a.task_owner === session.user.user_id ? `<div class="notice warn">You asked for this deliverable, so you cannot approve it.</div>` : ""}
      <div class="card"><h3>Preview</h3><div class="preview-doc" id="art-preview"><span class="spin"></span></div></div>`;
    bindArtifactButtons(pane);
    try {
      const response = await api(`/api/artifacts/${artifactId}/preview`, { raw: true });
      $("#art-preview").innerHTML = await response.text();
    } catch (error) {
      $("#art-preview").innerHTML = `<div class="notice warn">${esc(error.message)}</div>`;
    }
    const decide = async (approve) => {
      const comment = $("#decision-comment").value.trim();
      if (!approve && !comment) return toast("A rejection needs a comment — it is what the revision works from.", "bad");
      try {
        const reply = await api(`/api/artifacts/${artifactId}/decision`, { method: "POST", body: { approve, comment } });
        toast(approve ? `Released · sha256 ${String(reply.outcome.sha256).slice(0, 12)}…` : "Rejected — the task has one revision.", approve ? "good" : "");
        route();
      } catch (error) {
        toast(error.message, "bad");
      }
    };
    $("#approve")?.addEventListener("click", () => decide(true));
    $("#reject")?.addEventListener("click", () => decide(false));
  }

  // ================================================================ MODELS & ROUTING
  views.models = async (root) => {
    root.innerHTML = `
      <div class="view-head"><div><h1>Models &amp; routing</h1>
        <div class="sub">Every model call is routed by a deterministic, explainable score: hard eligibility (enabled, installed,
        capability, modality, classification ceiling, context) and then capability, task fit, quality and residency — a loaded
        model scores higher than one that would have to be swapped in. The same scoring runs live below for the requests the
        agent actually makes.</div></div></div>
      <div id="gw"><span class="spin"></span></div>
      <div class="card"><div class="card-head"><h3>Routing, right now</h3><span class="spacer"></span>
        <select id="route-level" style="width:auto">${allowedLevels(session.user.clearance).map((l) => `<option ${l === session.user.clearance.toUpperCase() ? "selected" : ""}>${l}</option>`).join("")}</select>
        <button type="button" class="btn small" id="route-refresh">Re-route</button></div>
        <div id="routes"><span class="spin"></span></div></div>`;
    const loadStatus = async () => {
      const s = await api("/api/models");
      const pulling = s.models.some((m) => m.pull && !m.pull.done);
      $("#gw").innerHTML = `
        <div class="card">
          <div class="card-head"><h3>Inference runtime</h3>${s.reachable ? `<span class="pill ok">reachable</span>` : `<span class="pill failed">unreachable</span>`}
            <span class="muted small mono">${esc(s.provider)} · ${esc(s.endpoint)} ${s.version ? `· v${esc(s.version)}` : ""}</span><span class="spacer"></span>
            ${s.missing.length && (session.has("engineer") || session.has("admin")) ? `<button type="button" class="btn primary small" id="pull">Pull ${esc(s.missing.length)} missing model(s)</button>` : ""}</div>
          ${s.error ? `<div class="notice bad">${esc(s.error)}</div>` : ""}
          ${!s.reachable ? `<div class="notice warn">Start Ollama on this machine (it must be listening where the endpoint above says). Citadel keeps running; tasks wait for a model.</div>` : ""}
          ${s.missing.length ? `<div class="notice warn">Not installed yet: ${s.missing.map((m) => `<b class="mono">${esc(m)}</b>`).join(", ")}. Pulling downloads them once, through the Ollama runtime; after that nothing here needs the internet.</div>` : ""}
          <div class="stat-row" style="margin-bottom:12px">
            ${stat("GPU admission", `${s.admission.in_use}/${s.admission.capacity}`, "", `${s.admission.waiting} waiting · avg wait ${ms(s.admission.avg_wait_ms)}`)}
            ${stat("Profile", s.profile, "", s.residency_strategy)}
            ${stat("Models loaded", s.models.filter((m) => m.loaded).length, "", `of ${s.models.length} registered`)}
          </div>
          <div class="table-wrap"><table><thead><tr><th>Model</th><th>Tag</th><th>Installed</th><th>Loaded</th><th>Resident set</th><th>Capabilities</th><th>Ceiling</th><th>Pull</th></tr></thead><tbody>
            ${s.models.map((m) => `<tr><td class="mono"><b>${esc(m.id)}</b>${m.enabled ? "" : ` <span class="pill">disabled</span>`}</td><td class="mono small">${esc(m.tag)}</td>
              <td>${m.installed ? "✓" : "—"}</td><td>${m.loaded ? "✓" : "—"}</td><td>${m.resident_set ? "pinned" : "swapped"}</td>
              <td class="small">${m.capabilities.map((c) => `<span class="term">${esc(c)}</span>`).join("")}</td><td>${levelPill(m.classification_ceiling)}</td>
              <td class="small">${m.pull ? `${esc(m.pull.status)}${m.pull.total ? `<div class="progress"><span style="width:${Math.round((100 * m.pull.completed) / m.pull.total)}%"></span></div>` : ""}${m.pull.error ? `<div class="pill failed">${esc(m.pull.error)}</div>` : ""}` : ""}</td></tr>`).join("")}
          </tbody></table></div>
        </div>`;
      $("#pull")?.addEventListener("click", async () => {
        try {
          const reply = await api("/api/models/pull", { method: "POST", body: {} });
          toast(`Pulling: ${reply.started.join(", ") || "nothing to pull"}`);
          setTimeout(loadStatus, 1000);
        } catch (error) {
          toast(error.message, "bad");
        }
      });
      return pulling;
    };
    const loadRoutes = async () => {
      const data = await api("/api/routing/preview", { method: "POST", body: { classification: $("#route-level").value } });
      $("#routes").innerHTML = `<div class="grid-2">${data.scenarios.map(routeCard).join("")}</div>`;
    };
    await Promise.all([loadStatus(), loadRoutes()]);
    $("#route-refresh").addEventListener("click", loadRoutes);
    $("#route-level").addEventListener("change", loadRoutes);
    every(4000, async () => {
      if (!$("#gw")) return;
      await loadStatus().catch(() => false);
    });
  };

  function routeCard(scenario) {
    const decision = scenario.decision;
    const max = Math.max(1, ...scenario.candidates.map((c) => Math.abs(c.total)));
    const rows = scenario.candidates
      .map((c) => {
        const terms = (c.terms || []).map((t) => `<span class="term ${t.points < 0 ? "neg" : "pos"}">${esc(t.term)}${t.detail ? ` (${esc(t.detail)})` : ""} ${t.points > 0 ? "+" : ""}${esc(t.points)}</span>`).join("");
        const why = (c.ineligible_because || []).map((r) => `<div class="small muted">✗ ${esc(r)}</div>`).join("");
        return `<tr><td class="mono">${c.model_id === decision.selected ? "<b>▶ " + esc(c.model_id) + "</b>" : esc(c.model_id)}</td>
          <td style="width:90px"><div class="score-bar ${c.eligible ? "" : "ineligible"}"><span style="width:${Math.max(0, (100 * c.total) / max)}%"></span></div><div class="small">${esc(c.total)}</div></td>
          <td>${c.eligible ? terms : why}</td></tr>`;
      })
      .join("");
    return `<div class="card route-card">
      <h4>${esc(scenario.label)}</h4>
      <div class="selected-model">${decision.selected ? `→ <span class="mono">${esc(decision.selected)}</span>` : `<span class="pill failed">no eligible model</span>`}</div>
      ${decision.fallback_chain.length ? `<div class="small muted">fallback: ${decision.fallback_chain.map(esc).join(" → ")}</div>` : ""}
      <div class="table-wrap" style="margin-top:8px"><table class="score-table"><tbody>${rows}</tbody></table></div></div>`;
  }

  // ================================================================ SOVEREIGNTY
  views.sovereignty = async (root) => {
    root.innerHTML = `
      <div class="view-head"><div><h1>Sovereignty</h1>
        <div class="sub">Two independent halves. <b>Enforcement</b>: internal-only container networks and a default-deny
        ruleset in each container's own network namespace, plus an in-process fence. <b>Telemetry</b>: a monitor that records
        every outbound attempt and a scanner that asks the operating system which connections exist — whatever the firewall
        did. Either failing would show in the other. The probe tries to leave, on purpose.</div></div>
        <span class="spacer"></span><button type="button" class="btn primary" id="probe">Run the deliberate probe</button></div>
      <div id="sov"><span class="spin"></span></div>`;
    const load = async () => {
      const s = await api("/api/sovereignty");
      const c = s.status.counts;
      const e = s.enforcement || {};
      const proc = s.api_process || {};
      $("#sov").innerHTML = `
        <div class="stat-row">
          ${stat("External connections observed", c.observed, c.observed ? "bad" : "good", "the number that must read zero")}
          ${stat("Outbound attempts recorded", c.attempts, "", "seen by the monitor")}
          ${stat("Blocked", c.blocked, "", "by the fence or the network")}
          ${stat("DNS lookups denied", c.dns_denied)}
          ${stat("Probe attempts", c.probes, "", "outcomes of the deliberate probe")}
          ${stat("Local model calls", s.status.model_calls, "", "to the on-box inference runtime")}
        </div>
        <div class="grid-2" style="margin-top:14px">
          <div class="card"><div class="card-head"><h3>Enforcement</h3>${e.applied ? `<span class="pill ok">${esc(e.mechanism)} applied</span>` : `<span class="pill failed">no network-level enforcement</span>`}</div>
            ${e.note ? `<div class="notice ${e.applied ? "info" : "warn"}">${esc(e.note)}</div>` : ""}
            ${e.rules ? `<details><summary>The ruleset (readable in seconds)</summary><pre>${esc(e.rules)}</pre></details>` : ""}
            <dl class="kv">${e.applied_at ? `<dt>Applied</dt><dd>${esc(e.applied_at)}</dd>` : ""}
              ${e.allowed ? `<dt>Allowed out</dt><dd class="mono small">${esc([].concat(e.allowed).join(", "))}</dd>` : ""}
              ${(e.refused_in || []).length ? `<dt>Refused inbound</dt><dd class="small"><span class="mono">${esc([].concat(e.refused_in).join(", "))}</span> — the sandbox network may answer this container, never call it</dd>` : ""}
              ${e.outside ? `<dt>Outside these rules</dt><dd class="small">${esc(e.outside)}</dd>` : ""}
              <dt>In-process fence</dt><dd>${esc(proc.fence || "—")}${proc.fence_refusals ? ` · ${esc(proc.fence_refusals)} refusal(s)` : ""}</dd>
              <dt>Sandbox</dt><dd>${esc(s.sandbox.kind || "")} — ${esc(s.sandbox.network || "")}</dd>
              <dt>Inference</dt><dd class="mono small">${esc(s.profile.inference_endpoint || "")}</dd></dl></div>
          <div class="card"><div class="card-head"><h3>Telemetry</h3></div>
            <dl class="kv"><dt>Monitor</dt><dd>${esc(proc.monitor || "—")}</dd>
              <dt>Connection scanner</dt><dd>${proc.scanner ? `${esc(proc.scanner.scans)} scans${proc.scanner.last_scan ? `, last ${esc(ago(proc.scanner.last_scan * 1000))}` : ""}` : "—"}</dd>
              <dt>Deployment's own destinations</dt><dd class="mono small">${esc(((proc.allowlist || {}).endpoints || []).join(", "))}</dd></dl>
            <h4 style="margin-top:10px">By process</h4>
            ${(s.status.by_process || []).length ? `<div class="table-wrap"><table><tbody>${s.status.by_process.map((r) => `<tr><td>${esc(r.process)}</td><td>${esc(r.kind)}</td><td class="right">${esc(r.n)}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">Nothing recorded.</div>`}</div>
        </div>
        <div class="card"><h3>Recent events</h3>${(s.status.recent || []).length ? egressTable(s.status.recent) : `<div class="empty">No outbound attempt has ever been recorded.</div>`}</div>
        <div id="probe-out"></div>`;
    };
    await load();
    $("#probe").addEventListener("click", async () => {
      $("#probe").disabled = true;
      $("#probe-out").innerHTML = `<div class="card"><span class="spin"></span> probing from the API process and from inside the sandbox…</div>`;
      try {
        const r = await api("/api/sovereignty/probe", { method: "POST" });
        const rows = [["api", r.api], ["sandbox", r.sandbox]]
          .flatMap(([process, rep]) => ((rep || {}).results || []).map((x) => `<tr><td>${esc(process)}</td><td class="mono">${esc(x.target)}</td><td>${statusPill(x.outcome === "connected" ? "failed" : "ok").replace(/>[^<]*</, `>${esc(x.outcome)}<`)}</td><td class="small">${esc(x.error || "")}</td><td>${ms(x.elapsed_ms)}</td></tr>`))
          .join("");
        $("#probe-out").innerHTML = `<div class="card"><div class="notice ${r.egress_blocked ? "good" : "bad"}">${r.egress_blocked ? "Every outbound attempt was stopped, and recorded." : "EGRESS WAS NOT BLOCKED on this machine — see below."}</div>
          <div class="table-wrap"><table><thead><tr><th>From</th><th>Target</th><th>Outcome</th><th>Error</th><th>Time</th></tr></thead><tbody>${rows}</tbody></table></div>
          ${r.sandbox && r.sandbox.note ? `<p class="muted small">${esc(r.sandbox.note)}</p>` : ""}</div>`;
        await load();
      } catch (error) {
        $("#probe-out").innerHTML = `<div class="notice bad">${esc(error.message)}</div>`;
      } finally {
        $("#probe").disabled = false;
      }
    });
    every(8000, () => $("#sov") && load().catch(() => null));
  };

  // ================================================================ AUDIT & POLICY
  views.audit = async (root) => {
    const tools = (await api("/api/registry/tools", {}, null)).tools || [];
    const departments = Array.from(new Set(app.demoUsers.map((u) => u.department))).sort();
    root.innerHTML = `
      <div class="view-head"><div><h1>Audit &amp; policy</h1>
        <div class="sub">The policy is data (registry/policy.yaml): first match wins, default deny. Try any tool against a resource you
        describe — the decision is made from <b>your</b> verified identity, by the same evaluator the chokepoint uses, and written to
        the hash-chained audit log like every other decision.</div></div></div>
      <div class="grid-2">
        <div class="card"><h3>Try a policy decision</h3>
          <label for="pt-tool">Tool</label><select id="pt-tool">${tools.map((t) => `<option value="${esc(t.name)}">${esc(t.name)} — ${esc(t.side_effect)}</option>`).join("")}</select>
          <label for="pt-level">Resource classification</label><select id="pt-level">${LEVELS.map((l) => `<option>${l}</option>`).join("")}<option value="RESTRICTED">an unknown marking</option></select>
          <label>Resource access list</label>
          <div class="row">${departments.map((d) => `<label class="row small" style="margin:0;font-weight:500"><input type="checkbox" class="pt-dept" value="${esc(d)}" ${d === session.user.department ? "checked" : ""}/> ${esc(d)}</label>`).join("")}</div>
          <div class="row" style="margin-top:10px"><button type="button" class="btn primary" id="pt-go">Evaluate</button></div>
          <div id="pt-out" style="margin-top:10px"></div></div>
        <div class="card"><div class="card-head"><h3>Audit chain</h3><span class="spacer"></span><button type="button" class="btn small" id="verify">Verify the whole chain</button></div>
          <div id="verify-out"></div><div id="audit-out"><span class="spin"></span></div></div>
      </div>`;
    $("#pt-go").addEventListener("click", async () => {
      try {
        const r = await api("/api/policy/try", {
          method: "POST",
          body: { tool: $("#pt-tool").value, resource: { classification: $("#pt-level").value, acl: $$(".pt-dept").filter((c) => c.checked).map((c) => c.value) } },
        });
        $("#pt-out").innerHTML = `<div class="notice ${r.decision.effect === "allow" ? "good" : "bad"}"><b>${esc(r.decision.effect.toUpperCase())}</b> by rule <b>${esc(r.decision.rule_id || "default deny")}</b>${r.decision.reason ? ` — ${esc(r.decision.reason)}` : ""}<br/><span class="small">${esc(r.receipt)} · audit #${esc(r.audit_seq)}</span></div>
          <dl class="kv small"><dt>You, as the policy sees you</dt><dd>${esc(r.actor.role)} · ${esc(r.actor.department)} · max ${esc(r.actor.classification_max)} · capabilities ${esc(r.actor.capabilities.join(", "))}</dd></dl>`;
        loadAudit();
      } catch (error) {
        toast(error.message, "bad");
      }
    });
    const loadAudit = async () => {
      const data = await api("/api/audit/recent?limit=40");
      $("#audit-out").innerHTML = `<div class="small muted">${esc(data.total)} events in the chain; newest last.</div>
        <div class="table-wrap" style="max-height:60vh;overflow-y:auto"><table><thead><tr><th>#</th><th>Event</th><th>Actor</th><th>Payload</th></tr></thead><tbody>
        ${data.events.map((e) => `<tr><td class="mono small">${esc(e.seq)}</td><td><b>${esc(e.event_name)}</b><div class="small muted">${esc(e.occurred_at)}</div></td><td class="small">${esc(e.actor_id || "system")}</td>
          <td><details><summary class="small">${esc(Object.keys(e.payload || {}).slice(0, 4).join(", "))}</summary><pre>${json(e.payload)}</pre></details></td></tr>`).join("")}
        </tbody></table></div>`;
    };
    $("#verify").addEventListener("click", async () => {
      const r = await api("/api/audit/verify");
      $("#verify-out").innerHTML = `<div class="notice ${r.ok ? "good" : "bad"}">${r.ok ? `Intact: all ${esc(r.checked)} rows re-hashed and linked.` : `BROKEN at #${esc(r.first_break_seq)}: ${esc(r.reason)}`}</div>`;
    });
    await loadAudit();
  };

  // ================================================================ METRICS
  views.metrics = async (root) => {
    root.innerHTML = `<div class="view-head"><div><h1>Metrics</h1><div class="sub">Aggregated from the execution trace on read — latency and error rate per kind of work, model usage, and what is queued.</div></div></div><div id="m"><span class="spin"></span></div>`;
    const load = async () => {
      const m = await api("/api/metrics");
      const counts = (rows) => rows.map((r) => `<span class="pill ${esc(r.status)}">${esc(r.status)}: ${esc(r.n)}</span>`).join(" ") || `<span class="muted">none</span>`;
      $("#m").innerHTML = `
        <div class="stat-row">${stat("GPU admission in use", `${m.gpu_admission.in_use}/${m.gpu_admission.capacity}`, "", `${m.gpu_admission.waiting} waiting · max wait ${ms(m.gpu_admission.max_wait_ms)}`)}
          ${stat("Model calls admitted", m.gpu_admission.admitted, "", `avg queue ${ms(m.gpu_admission.avg_wait_ms)}`)}</div>
        <div class="card" style="margin-top:14px"><h3>Work by kind (last hour)</h3><div class="table-wrap"><table><thead><tr><th>Kind</th><th class="right">Calls</th><th class="right">Avg</th><th class="right">p50</th><th class="right">p95</th><th class="right">Errors</th></tr></thead><tbody>
          ${m.spans.map((s) => `<tr><td><b>${esc(s.kind)}</b></td><td class="right">${esc(s.calls)}</td><td class="right">${ms(s.avg_ms)}</td><td class="right">${ms(s.p50_ms)}</td><td class="right">${ms(s.p95_ms)}</td><td class="right">${esc(s.error_pct)}%</td></tr>`).join("") || `<tr><td colspan="6" class="empty">No traced work yet.</td></tr>`}</tbody></table></div></div>
        <div class="card"><h3>Models</h3><div class="table-wrap"><table><thead><tr><th>Model</th><th class="right">Calls</th><th class="right">Avg latency</th><th class="right">Prompt tokens</th><th class="right">Completion tokens</th></tr></thead><tbody>
          ${m.models.map((r) => `<tr><td class="mono">${esc(r.model_id)}</td><td class="right">${esc(r.calls)}</td><td class="right">${ms(r.avg_ms)}</td><td class="right">${esc(r.prompt_tokens)}</td><td class="right">${esc(r.completion_tokens)}</td></tr>`).join("") || `<tr><td colspan="5" class="empty">No model calls yet.</td></tr>`}</tbody></table></div></div>
        <div class="card"><h3>State</h3><dl class="kv"><dt>Tasks</dt><dd>${counts(m.tasks)}</dd><dt>Documents</dt><dd>${counts(m.documents)}</dd><dt>Artifacts</dt><dd>${counts(m.artifacts)}</dd></dl></div>`;
    };
    await load();
    every(10000, () => $("#m") && load().catch(() => null));
  };

  // ---------------------------------------------------------------- start
  boot();
})();
