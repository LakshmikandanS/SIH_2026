/* The workbench's editor tabs -- plain JavaScript, no dependency, no external host.
 *
 *   welcome   write a report: say what it should cover; the agents write it
 *   draft     a task file (/task <name>): write the request in detail, then commit it
 *   task      a live task: its agents, journal, shared state, pause / resume / revise
 *   agent     one agent's plan (agent_2.plan): goal, progress, what it did and found
 *   report    the report editor: every section editable, re-verified as a new version
 *   doc       a document: its pages, every issue, and what changed between issues
 *   obs       observability, metrics and evaluation panels
 *   state     the environment and sandbox state tree for a task
 *   memory    what the workbench remembers, curated by people
 *
 * Every tab kind is { title(tab), icon, render(pane, tab, ide) }; render may register
 * cleanups on tab.cleanups (streams, timers) that the shell runs when the tab closes.
 */
(() => {
  "use strict";
  const C = window.Citadel;
  const { $, $$, esc, api, toast, levelPill, statusPill, deptPills, ago, ms, allowedLevels, renderAnswer,
          showEvidence, renderStep, download, session, openModal, previewArtifact, viewDocument } = C;
  const IDE = (window.CitadelIDE = window.CitadelIDE || { kinds: {} });
  const kinds = IDE.kinds;

  const ACTIVE = new Set(["submitted", "planning", "running", "revision_required", "paused"]);
  const REPORT_EXAMPLES = [
    ["Lathe L-1, full report", "Report on lathe L-1 in machine shop sector 1: its condition and workload, what company policy requires today, the options, and a recommendation."],
    ["Lathe L-1, two sections", "Report on lathe L-1 covering only its condition and the vendor options"],
    ["Heat exchanger E-101", "Report on heat exchanger E-101: its inspection findings, its remaining life, and what to do next."],
  ];

  // ------------------------------------------------------------------ small helpers
  const cited = (text) => renderAnswer(String(text ?? ""));
  function bindCites(root, taskId) {
    // Once per element: panes are redrawn in place, and a listener added on every redraw
    // would open the same evidence once per redraw.
    root.dataset.citesTask = taskId || "";
    if (root.dataset.citesBound) return;
    root.dataset.citesBound = "1";
    root.addEventListener("click", (event) => {
      const chip = event.target.closest("[data-cite]");
      if (chip && root.dataset.citesTask) showEvidence(root.dataset.citesTask, chip.dataset.cite);
    });
  }
  function every(tab, msInterval, fn) {
    const id = setInterval(() => { if (tab.visible !== false) fn(); }, msInterval);
    tab.cleanups.push(() => clearInterval(id));
  }
  function levelsSelect(id, selected) {
    const levels = allowedLevels(session.user.clearance);
    const pick = selected ? String(selected).toUpperCase() : levels[levels.length - 1];
    return `<select id="${id}">${levels.map((l) => `<option value="${l}" ${l === pick ? "selected" : ""}>${l}</option>`).join("")}</select>`;
  }
  function tiersHtml(verification) {
    const tiers = (verification || {}).tiers || [];
    if (!tiers.length) return "";
    return `<div class="tiers">${tiers.map((t) => `<div class="tier ${esc(t.status)}"><div class="name">${esc(t.tier)}. ${esc(t.name)} — ${esc(t.status)}</div>
      ${(t.issues || []).length ? `<ul>${t.issues.slice(0, 6).map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : ""}</div>`).join("")}</div>`;
  }
  function table(headers, rows) {
    if (!rows.length) return `<div class="empty">Nothing yet.</div>`;
    return `<div class="table-wrap"><table><thead><tr>${headers.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead>
      <tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  }
  const num = (v) => (v === null || v === undefined ? "—" : esc(v));
  // A text box as tall as its text, so a section reads like the page it becomes. Only
  // while it is on screen: a hidden box measures zero.
  function autosize(box) {
    if (!box || !box.offsetParent) return;
    box.style.height = "1px"; // measured from nothing, so it shrinks as well as grows
    box.style.height = `${box.scrollHeight + 2}px`;
  }
  // A heartbeat's detail as words: uptime_s 231 -> "uptime 231 s".
  const UNITS = [["_seconds", " s"], ["_ms", " ms"], ["_mb", " MB"], ["_s", " s"]];
  const kv = (key, value) => {
    const unit = UNITS.find(([suffix]) => key.endsWith(suffix));
    return [(unit ? key.slice(0, -unit[0].length) : key).replace(/_/g, " "), `${value}${unit ? unit[1] : ""}`];
  };
  const detailKv = (detail) => Object.entries(detail || {})
    .filter(([k]) => !["rss_mb", "pid", "python"].includes(k))
    .map(([k, v]) => {
      if (v && typeof v === "object") {
        const inner = Object.entries(v).filter(([, x]) => x !== null && typeof x !== "object").map(([a, x]) => kv(a, x).join(" ")).join(", ");
        return `<span><b>${esc(k.replace(/_/g, " "))}</b> ${esc(inner)}</span>`;
      }
      const [label, value] = kv(k, v);
      return `<span><b>${esc(label)}</b> ${esc(value)}</span>`;
    }).join(" ");
  const bytes = (n) => (n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : n > 1024 ? `${Math.round(n / 1024)} kB` : `${n || 0} B`);

  // ------------------------------------------------------------------ welcome: write a report
  kinds.welcome = {
    title: () => "Welcome",
    icon: "◇",
    async render(pane, tab, ide) {
      pane.innerHTML = `<div class="ide-page">
        <h1>Write a report</h1>
        <p class="muted">Say what the report should cover, in your own words and in the order you want it. An agent — or a
          small team of them — finds the facts in the documents you may read, cites every one, and writes the report with
          one section per thing you asked for. Then it is yours: edit any section yourself, or ask the agents to revise it.
          Every version is rendered into the report template and checked against its sources before you see it.</p>
        <textarea id="w-prompt" rows="4" placeholder="Report on …: its …, …, and a recommendation."></textarea>
        <div class="row" style="margin-top:8px">
          <label class="inline">Classification ${levelsSelect("w-level")}</label>
          <span class="spacer"></span>
          <button type="button" class="btn" id="w-draft" title="Open a task file to write a longer request, then commit it">Open as a task file</button>
          <button type="button" class="btn primary" id="w-go">Write the report</button>
        </div>
        <div class="examples">${REPORT_EXAMPLES.map(([label, text]) => `<span class="example" data-text="${esc(text)}">${esc(label)}</span>`).join("")}</div>
        <div class="grid-2" style="margin-top:18px">
          <div><h3>Your reports</h3><div id="w-reports"><span class="spin"></span></div></div>
          <div><h3>Command line</h3>
            <div class="cli-help small">
              <div><code>/report &lt;what to cover&gt;</code> write a report now</div>
              <div><code>/task &lt;name&gt;</code> open a task file to write a detailed request, then commit it</div>
              <div><code>/revise &lt;instruction&gt;</code> send the open report back to the agents</div>
              <div><code>/ask &lt;question&gt;</code> ask about documents or about the work in progress</div>
              <div><code>/note</code>, <code>/steer &lt;agent&gt;</code>, <code>/pause</code>, <code>/resume</code>, <code>/cancel</code> work alongside the agents</div>
              <div><code>/open</code>, <code>/search</code>, <code>/run</code>, <code>/remember</code>, <code>/status</code>, <code>/help</code></div>
            </div></div>
        </div></div>`;
      $$(".example", pane).forEach((chip) => chip.addEventListener("click", () => {
        $("#w-prompt", pane).value = chip.dataset.text;
        $("#w-prompt", pane).focus();
      }));
      $("#w-go", pane).addEventListener("click", async () => {
        const goal = $("#w-prompt", pane).value.trim();
        if (!goal) return toast("Say what the report should cover.", "bad");
        await ide.submitReport(goal, $("#w-level", pane).value);
        $("#w-prompt", pane).value = "";
      });
      $("#w-draft", pane).addEventListener("click", () => ide.newDraft("report", $("#w-prompt", pane).value.trim()));
      const load = async () => {
        const holder = $("#w-reports", pane);
        const tasks = (await api("/api/tasks").catch(() => ({ tasks: [] }))).tasks || [];
        const mine = tasks.filter((t) => t.kind !== "ask").slice(0, 12);
        holder.innerHTML = mine.length ? `<div class="list">${mine.map((t) => `
          <div class="list-item" data-task="${esc(t.id)}"><div class="grow">${esc(t.title || t.goal)}</div>
            ${statusPill(t.status)} <span class="muted small">${esc(ago(t.created_at))}</span></div>`).join("")}</div>`
          : `<div class="empty">No reports yet — write one above.</div>`;
        $$(".list-item", holder).forEach((row) => row.addEventListener("click", () => ide.openReportOrTask(row.dataset.task)));
      };
      await load();
      every(tab, 8000, () => load().catch(() => null));
    },
  };

  // ------------------------------------------------------------------ draft: a task file
  kinds.draft = {
    title: (tab) => `${tab.name || "untitled"}.task`,
    icon: "✎",
    async render(pane, tab, ide) {
      const drafts = (await api("/api/drafts")).drafts || [];
      const draft = drafts.find((d) => d.id === tab.ref);
      if (!draft) {
        pane.innerHTML = `<div class="ide-page"><div class="notice warn">This task file no longer exists.</div></div>`;
        return;
      }
      const committed = !!draft.committed_task_id;
      pane.innerHTML = `<div class="ide-page ide-draft">
        <div class="row"><input type="text" id="d-name" value="${esc(draft.name)}" ${committed ? "disabled" : ""} style="max-width:420px"/>
          <label class="inline">Classification ${levelsSelect("d-level", draft.classification)}</label>
          <span class="spacer"></span><span class="muted small" id="d-saved">saved ${esc(ago(draft.updated_at))}</span>
          ${committed ? `<button type="button" class="btn" id="d-open">Open the task ▸</button>`
                      : `<button type="button" class="btn primary" id="d-commit" title="Submit this request as a task">Commit ▸</button>`}</div>
        <p class="muted small">Write the request the way you would brief a colleague: what the report is about, what it must
          cover (in order), what to leave out, and anything you already know. The agents see it verbatim.</p>
        <textarea id="d-body" class="task-file" spellcheck="true" ${committed ? "disabled" : ""}
          placeholder="Report on lathe L-1 in machine shop sector 1: its condition and workload, what company policy requires today, the options, and a recommendation.">${esc(draft.body)}</textarea>
        ${committed ? `<div class="notice info">Committed ${esc(ago(draft.updated_at))}: this file is the record of what was asked.</div>` : ""}
      </div>`;
      if (committed) {
        $("#d-open", pane).addEventListener("click", () => ide.openTask(draft.committed_task_id));
        return;
      }
      let timer = null;
      const save = async () => {
        try {
          const body = { name: $("#d-name", pane).value.trim() || "untitled task", body: $("#d-body", pane).value,
                         classification: $("#d-level", pane).value };
          await api(`/api/drafts/${draft.id}`, { method: "PUT", body });
          tab.name = body.name;
          ide.retitle(tab);
          $("#d-saved", pane).textContent = "saved";
        } catch (error) {
          $("#d-saved", pane).textContent = error.message;
        }
      };
      const soon = () => {
        $("#d-saved", pane).textContent = "editing…";
        clearTimeout(timer);
        timer = setTimeout(save, 700);
      };
      ["#d-name", "#d-body"].forEach((sel) => $(sel, pane).addEventListener("input", soon));
      $("#d-level", pane).addEventListener("change", soon);
      tab.cleanups.push(() => clearTimeout(timer));
      $("#d-commit", pane).addEventListener("click", async () => {
        clearTimeout(timer);
        await save();
        try {
          const task = await api(`/api/drafts/${draft.id}/commit`, { method: "POST" });
          toast("Committed — the agents are on it.", "good");
          ide.log(`committed ${draft.name} → task ${task.id.slice(0, 8)}`, "ok");
          ide.closeTab(tab.key);
          ide.openTask(task.id, task.title);
        } catch (error) {
          toast(error.message, "bad");
        }
      });
      $("#d-body", pane).focus();
    },
  };

  // ------------------------------------------------------------------ task: agents, journal, shared state
  function agentCard(agent) {
    const progress = agent.progress || {};
    const pct = progress.limit ? Math.min(100, Math.round((100 * (progress.steps || 0)) / progress.limit)) : 0;
    return `<button type="button" class="agent-card ${esc(agent.status)}" data-agent="${esc(agent.agent_id)}">
      <div class="row"><b>${esc(agent.name)}</b>${statusPill(agent.status)}</div>
      <div class="small muted clamp">${esc(agent.current_step || agent.goal || "")}</div>
      <div class="progress"><span style="width:${pct}%"></span></div>
      <div class="small muted">${esc(progress.steps || 0)}/${esc(progress.limit || "—")} steps · ${esc(progress.tool_calls || 0)} tool calls</div>
    </button>`;
  }
  IDE.agentCard = agentCard;

  function agentPopover(anchor, agent, taskId, ide) {
    const card = document.createElement("div");
    card.className = "popover agent-pop";
    card.innerHTML = `
      <div class="row"><b>${esc(agent.name)}</b>${statusPill(agent.status)}<span class="spacer"></span><button type="button" class="btn link small" data-x>✕</button></div>
      <dl class="kv small">
        <dt>Goal</dt><dd>${esc(agent.goal)}</dd>
        <dt>Current step</dt><dd>${esc(agent.current_step || (agent.status === "done" ? "finished" : "—"))}</dd>
        <dt>Completed</dt><dd>${(agent.completed || []).length ? `<ul>${agent.completed.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : "—"}</dd>
        <dt>Using</dt><dd>${esc(agent.using || "—")}</dd>
        <dt>Waiting for</dt><dd>${(agent.waiting_for || []).length ? esc(agent.waiting_for.map((w) => `${w}'s findings`).join(", ")) : "—"}</dd>
      </dl>
      <div class="row"><button type="button" class="btn small" data-plan>Open ${esc(agent.agent_id)}.plan</button>
        <button type="button" class="btn small" data-steer>Tell ${esc(agent.name)}…</button></div>`;
    document.body.appendChild(card);
    const box = anchor.getBoundingClientRect();
    card.style.left = `${Math.min(window.innerWidth - 360, Math.max(8, box.left))}px`;
    card.style.top = `${Math.min(window.innerHeight - 280, box.bottom + 6)}px`;
    const close = () => card.remove();
    $("[data-x]", card).addEventListener("click", close);
    $("[data-plan]", card).addEventListener("click", () => { close(); ide.openAgent(taskId, agent.agent_id); });
    $("[data-steer]", card).addEventListener("click", () => { close(); ide.focusCli(`/steer ${agent.agent_id} `); });
    setTimeout(() => document.addEventListener("click", function away(event) {
      if (!card.contains(event.target)) { close(); document.removeEventListener("click", away); }
    }), 0);
  }
  IDE.agentPopover = agentPopover;

  kinds.task = {
    title: (tab) => tab.name || `task ${String(tab.ref).slice(0, 8)}`,
    icon: "▶",
    async render(pane, tab, ide) {
      const taskId = tab.ref;
      pane.innerHTML = `<div class="ide-task">
        <div class="ide-task-head" id="t-head"><span class="spin"></span></div>
        <div class="agent-row" id="t-agents"></div>
        <div class="ide-split">
          <div class="ide-col"><div class="col-head"><h4>Journal</h4><span class="spacer"></span><span id="t-live"></span></div>
            <div class="timeline" id="t-timeline"></div></div>
          <div class="ide-col side">
            <div id="t-report"></div>
            <div class="col-head"><h4>Shared state</h4></div><div id="t-shared"></div>
            <form class="note-form" id="t-note">
              <div class="row"><select id="t-kind">${["note", "fact", "decision", "assumption", "question"].map((k) => `<option>${k}</option>`).join("")}</select>
                <select id="t-to"><option value="">everyone</option></select></div>
              <textarea id="t-text" rows="2" placeholder="Add to the shared state — cite evidence like [E2]"></textarea>
              <div class="row"><span class="spacer"></span><button type="submit" class="btn small">Add</button></div>
            </form>
          </div>
        </div></div>`;
      bindCites(pane, taskId);
      const timeline = $("#t-timeline", pane);
      let last = 0;
      let detail = null;
      let state = null;
      const refresh = async () => {
        [detail, state] = await Promise.all([api(`/api/tasks/${taskId}`), api(`/api/tasks/${taskId}/state`)]);
        C.app.taskCache.set(taskId, detail);
        const task = detail.task;
        tab.name = task.title || task.goal.slice(0, 40);
        ide.retitle(tab);
        ide.setTask(taskId);
        const owner = task.submitted_by === session.user.user_id;
        const reportArtifacts = (detail.artifacts || []).filter((a) => a.template_id);
        $("#t-head", pane).innerHTML = `
          <div class="grow"><div class="goal">${esc(task.goal)}</div>
            <div class="row small">${statusPill(task.status)} ${levelPill(task.classification)}
              <span class="muted">${esc(state.task.phase)}</span>
              <span class="muted">· ${esc((task.usage || {}).steps ?? 0)} steps · ${esc((task.usage || {}).model_calls ?? 0)} model calls
                · GPU wait ${ms((task.usage || {}).queue_wait_ms)}</span>
              ${task.error ? `<span class="pill failed">${esc(task.error)}</span>` : ""}</div></div>
          <div class="row">
            ${owner && ["running", "planning", "submitted", "revision_required"].includes(task.status) ? `<button type="button" class="btn small" data-act="pause">⏸ Pause</button>` : ""}
            ${owner && (task.status === "paused" || task.pause_requested) ? `<button type="button" class="btn small" data-act="resume">▶ Resume</button>` : ""}
            ${owner && ACTIVE.has(task.status) ? `<button type="button" class="btn small bad" data-act="cancel">Cancel</button>` : ""}
            ${reportArtifacts.length ? `<button type="button" class="btn small primary" data-act="report">Open the report</button>` : ""}
            <button type="button" class="btn small" data-act="state">Sandbox state</button>
          </div>`;
        $$("[data-act]", $("#t-head", pane)).forEach((b) => b.addEventListener("click", () => ide.taskAction(b.dataset.act, taskId)));
        const agents = state.agents || [];
        $("#t-agents", pane).innerHTML = agents.map(agentCard).join("");
        $$(".agent-card", $("#t-agents", pane)).forEach((card) => card.addEventListener("click", (event) => {
          event.stopPropagation();
          const agent = agents.find((a) => a.agent_id === card.dataset.agent);
          if (agent) agentPopover(card, agent, taskId, ide);
        }));
        const to = $("#t-to", pane);
        const chosen = to.value;
        to.innerHTML = `<option value="">everyone</option>${agents.map((a) => `<option value="${esc(a.agent_id)}">${esc(a.name)}</option>`).join("")}`;
        to.value = chosen;
        const shared = state.shared_state || {};
        const groups = [["plan", "Shared plan"], ["decisions", "Decisions"], ["facts", "Discovered facts"],
                        ["assumptions", "Assumptions"], ["questions", "Questions for a person"], ["notes", "Notes"]];
        $("#t-shared", pane).innerHTML = groups.map(([key, label]) => {
          const items = shared[key] || [];
          if (!items.length) return "";
          const body = key === "plan"
            ? `<ol class="plan-steps">${items.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>`
            : items.map((s) => `<div class="shared-item ${key === "questions" ? "q" : ""}"><span class="muted small">${esc(s.by)}${s.to ? ` → ${esc(s.to)}` : ""}</span><div>${cited(s.content)}${(s.evidence || []).map((e) => ` <span class="cite ${esc(e[0])}" data-cite="${esc(e)}">${esc(e)}</span>`).join("")}</div></div>`).join("");
          return `<details open><summary>${esc(label)} <span class="muted">(${key === "plan" ? items.length : items.length})</span></summary>${body}</details>`;
        }).join("") || `<div class="empty small">Nothing shared yet.</div>`;
        const reports = reportArtifacts.slice(-1);
        $("#t-report", pane).innerHTML = reports.map((a) => `
          <div class="report-chip" data-open-report>
            <div><b>📄 ${esc(a.title || a.filename)}</b></div>
            <div class="small">${statusPill(a.status)} v${esc(a.version)} · ${esc((a.verification || {}).passed ? "verified" : "not verified")}
              ${(a.verification || {}).flagged_claims && a.verification.flagged_claims.length ? ` · ${a.verification.flagged_claims.length} flagged` : ""}</div>
          </div>`).join("") + (task.result && task.result.answer ? `<div class="answer small">${cited(task.result.answer)}</div>` : "");
        const chip = $("[data-open-report]", pane);
        if (chip) chip.addEventListener("click", () => ide.openReport(taskId));
        return detail;
      };
      const first = await refresh();
      for (const entry of first.journal) {
        ide.appendStep(timeline, entry, taskId);
        last = Math.max(last, entry.step_seq);
      }
      $("#t-note", pane).addEventListener("submit", async (event) => {
        event.preventDefault();
        const text = $("#t-text", pane).value.trim();
        if (!text) return;
        try {
          await ide.addNote(taskId, $("#t-kind", pane).value, text, $("#t-to", pane).value || null);
          $("#t-text", pane).value = "";
          refresh();
        } catch (error) {
          toast(error.message, "bad");
        }
      });
      let pending = null;
      const soon = () => { clearTimeout(pending); pending = setTimeout(() => refresh().catch(() => null), 400); };
      tab.cleanups.push(() => clearTimeout(pending));
      const follow = () => ide.follow(taskId, last, tab, (entry) => {
        last = Math.max(last, entry.step_seq);
        ide.appendStep(timeline, entry, taskId, true);
        soon();
      }, $("#t-live", pane)).then(async () => {
        const again = await api(`/api/tasks/${taskId}`).catch(() => null);
        if (again && ACTIVE.has(again.task.status) && !tab.closed) setTimeout(follow, 1500);
        else soon();
      });
      follow();
      every(tab, 6000, () => refresh().catch(() => null));
    },
  };

  // ------------------------------------------------------------------ agent: one agent's plan
  kinds.agent = {
    title: (tab) => `${tab.agent}.plan`,
    icon: "◉",
    async render(pane, tab, ide) {
      const [taskId, agentId] = [tab.ref, tab.agent];
      const draw = async () => {
        const [state, detail] = await Promise.all([api(`/api/tasks/${taskId}/state`), api(`/api/tasks/${taskId}`)]);
        const agent = (state.agents || []).find((a) => a.agent_id === agentId);
        if (!agent) {
          pane.innerHTML = `<div class="ide-page"><div class="notice warn">No agent ${esc(agentId)} on this task.</div></div>`;
          return;
        }
        const mine = detail.journal.filter((e) => e.agent_id === agentId);
        pane.innerHTML = `<div class="ide-page">
          <div class="row"><h2 style="margin:0">${esc(agent.name)}</h2>${statusPill(agent.status)}<span class="spacer"></span>
            <button type="button" class="btn small" id="a-steer">Tell ${esc(agent.name)}…</button></div>
          <dl class="kv">
            <dt>Goal</dt><dd>${esc(agent.goal)}</dd>
            <dt>Current step</dt><dd>${esc(agent.current_step || "—")}</dd>
            <dt>Progress</dt><dd>${esc(agent.progress.steps)}/${esc(agent.progress.limit)} steps · ${esc(agent.progress.tool_calls)} tool calls</dd>
            <dt>Focus</dt><dd>${(agent.focus || []).map((f) => `<span class="pill">${esc(f)}</span>`).join(" ") || "—"}</dd>
            <dt>Waits for</dt><dd>${esc((agent.waiting_for || []).join(", ") || "—")}</dd>
          </dl>
          <h4>Plan</h4><ol class="plan-steps">${(agent.plan || []).map((s) => `<li>${esc(s)}</li>`).join("") || "<li class='muted'>(the task's plan)</li>"}</ol>
          <h4>Working memory</h4>
          <div class="small">${agent.working_memory.last_thought ? `<div><b>Last thought:</b> <i>${esc(agent.working_memory.last_thought)}</i></div>` : ""}
            <div><b>Evidence held:</b> ${(agent.working_memory.evidence || []).map((e) => `<span class="cite ${esc(e[0])}" data-cite="${esc(e)}">${esc(e)}</span>`).join("") || "—"}</div></div>
          ${agent.working_memory.findings ? `<h4>Findings</h4><div class="answer">${cited(agent.working_memory.findings)}</div>` : ""}
          <h4>Steps</h4><div class="timeline" id="a-steps"></div></div>`;
        const steps = $("#a-steps", pane);
        mine.forEach((e) => ide.appendStep(steps, e, taskId));
        $("#a-steer", pane).addEventListener("click", () => ide.focusCli(`/steer ${agentId} `));
        return agent;
      };
      bindCites(pane, taskId);
      const agent = await draw();
      if (agent && ["running", "waiting", "pending"].includes(agent.status)) every(tab, 4000, () => draw().catch(() => null));
    },
  };

  // ------------------------------------------------------------------ report: the editor
  function inline(item) {
    // An item as the person edits it: its text with its citations back inline.
    if (item === null || item === undefined) return "";
    if (typeof item === "string" || typeof item === "number") return String(item);
    const text = item.text ?? item.value ?? item.content ?? "";
    const cites = [].concat(item.citations || item.citation || item.sources || item.refs || []);
    const missing = cites.filter((c) => !String(text).includes(`[${c}]`));
    return `${text}${missing.map((c) => ` [${c}]`).join("")}`;
  }
  function sectionText(section) {
    const body = section.text ?? section.body ?? section.content ?? section.paragraphs ?? "";
    const lines = Array.isArray(body) ? body.map(inline) : [String(body)];
    const extra = [].concat(section.citations || []).filter((c) => !lines.join(" ").includes(`[${c}]`));
    if (extra.length && lines.length) lines[lines.length - 1] += extra.map((c) => ` [${c}]`).join("");
    return lines.join("\n");
  }

  function fieldHtml(spec, value, editable) {
    const dis = editable ? "" : "disabled";
    const label = `${spec.key.replace(/_/g, " ")}${spec.required ? "" : spec.omit_when_empty ? " (optional — left out of the report when empty)" : " (optional)"}${spec.cited ? " · cite every fact" : ""}`;
    if (spec.type === "sections") {
      const items = Array.isArray(value) ? value : [];
      return `<div class="field" data-key="${esc(spec.key)}" data-type="sections"><label>${esc(label)}</label>
        <div class="sections">${items.map((s) => sectionHtml(s.heading || s.title || "", sectionText(s), editable)).join("")}</div>
        ${editable ? `<button type="button" class="btn small" data-add-section>+ Add a section</button>` : ""}</div>`;
    }
    if (spec.type === "text" || spec.type === "date" || spec.type === "value") {
      return `<div class="field" data-key="${esc(spec.key)}" data-type="${esc(spec.type)}"><label>${esc(label)}</label>
        <input type="text" value="${esc(inline(value))}" ${dis}/></div>`;
    }
    if (spec.type === "list") {
      const items = Array.isArray(value) ? value : value ? [value] : [];
      return `<div class="field" data-key="${esc(spec.key)}" data-type="list"><label>${esc(label)} — one per line</label>
        <textarea rows="${Math.max(2, items.length + 1)}" ${dis}>${esc(items.map(inline).join("\n"))}</textarea></div>`;
    }
    if (spec.type === "table") {
      return `<div class="field" data-key="${esc(spec.key)}" data-type="table"><label>${esc(label)} — rows as JSON</label>
        <textarea class="mono" rows="6" ${dis}>${esc(JSON.stringify(value || [], null, 2))}</textarea></div>`;
    }
    return `<div class="field" data-key="${esc(spec.key)}" data-type="rich_text"><label>${esc(label)}</label>
      <textarea rows="4" ${dis}>${esc(Array.isArray(value) ? value.map(inline).join("\n") : inline(value))}</textarea></div>`;
  }
  function sectionHtml(heading, text, editable) {
    const dis = editable ? "" : "disabled";
    return `<div class="section-edit">
      <div class="row"><input type="text" class="s-heading" value="${esc(heading)}" placeholder="Section heading" ${dis}/>
        ${editable ? `<button type="button" class="btn small" data-move="-1" title="Move up">↑</button>
        <button type="button" class="btn small" data-move="1" title="Move down">↓</button>
        <button type="button" class="btn small" data-remove title="Remove this section">✕</button>` : ""}</div>
      <textarea class="s-text" rows="${Math.max(3, text.split("\n").length + 1)}" ${dis}
        placeholder="One paragraph per line; start a line with '- ' for a bullet. Cite like [E2].">${esc(text)}</textarea></div>`;
  }

  function collect(editor) {
    const content = {};
    for (const field of $$(".field", editor)) {
      const key = field.dataset.key;
      const type = field.dataset.type;
      if (type === "sections") {
        content[key] = $$(".section-edit", field)
          .map((s) => ({ heading: $(".s-heading", s).value.trim(), text: $(".s-text", s).value.trim() }))
          .filter((s) => s.heading || s.text);
      } else if (type === "list") {
        content[key] = $("textarea", field).value.split("\n").map((l) => l.trim()).filter(Boolean);
      } else if (type === "table") {
        const raw = $("textarea", field).value.trim();
        try {
          content[key] = raw ? JSON.parse(raw) : [];
        } catch (_) {
          throw new Error(`The ${key.replace(/_/g, " ")} table is not valid JSON.`);
        }
      } else {
        const node = $("textarea, input", field);
        const value = node.value.trim();
        if (value) content[key] = value;
      }
    }
    return content;
  }

  kinds.report = {
    title: (tab) => tab.name || "report",
    icon: "📄",
    async render(pane, tab, ide) {
      const taskId = tab.ref;
      let lastFocus = null;
      const draw = async (artifactId) => {
        const detail = await api(`/api/tasks/${taskId}`);
        const reports = (detail.artifacts || []).filter((a) => a.template_id);
        if (!reports.length) {
          tab.lastStatus = detail.task.status;
          pane.innerHTML = `<div class="ide-page"><div class="notice info">No deliverable yet — the agents are still working.
            ${ACTIVE.has(detail.task.status) ? `<span class="spin"></span>` : ""}</div></div>`;
          return null;
        }
        const target = artifactId || reports[reports.length - 1].id;
        const data = await api(`/api/artifacts/${target}/content`);
        const artifact = data.artifact;
        // What this view shows is what the watch below compares against: a revision that
        // finishes between two ticks must still be noticed.
        tab.lastStatus = detail.task.status;
        tab.name = `${(detail.task.title || data.content.title || "report").slice(0, 36)}`;
        ide.retitle(tab);
        ide.setTask(taskId);
        const editable = data.editable;
        const busy = ACTIVE.has(detail.task.status);
        const specs = (data.template || {}).sections || [];
        pane.innerHTML = `<div class="ide-report">
          <div class="report-toolbar">
            <b class="grow clamp">${esc(artifact.title || artifact.filename)}</b>
            <select id="r-version" title="Every version is kept">${data.versions.map((v) => `<option value="${esc(v.id)}" ${v.id === artifact.id ? "selected" : ""}>v${esc(v.version)} · ${esc(v.status)} · ${esc((v.note || "").slice(0, 40))}</option>`).join("")}</select>
            ${statusPill(artifact.status)}
            <button type="button" class="btn small" id="r-preview">Preview</button>
            <button type="button" class="btn small" id="r-download">Download .${esc(artifact.kind)}</button>
          </div>
          ${busy ? `<div class="notice info"><span class="spin"></span> The agents are working on this task (${esc(detail.task.status.replace("_", " "))}); this view refreshes when they finish.</div>` : ""}
          ${!editable && data.why_not && !busy ? `<div class="notice warn small">${esc(data.why_not)}</div>` : ""}
          <div class="report-body">
            <div class="report-editor" id="r-editor">
              ${specs.map((spec) => fieldHtml(spec, data.content[spec.key], editable)).join("")}
              ${editable ? `<div class="save-bar">
                <input type="text" id="r-note" placeholder="What did you change? (goes in the revision history)"/>
                <span class="muted small" id="r-dirty" hidden>unsaved changes</span>
                <button type="button" class="btn small" id="r-discard" hidden title="Back to v${esc(artifact.version)} as saved">Discard</button>
                <button type="button" class="btn primary" id="r-save" title="Ctrl+S">Save as v${esc(data.versions.length + 1)}</button></div>` : ""}
            </div>
            <div class="report-side">
              <div class="side-card"><h4>Ask the agents to revise</h4>
                <textarea id="r-instruction" rows="2" placeholder="e.g. Add a section on operator training" ${detail.task.submitted_by === session.user.user_id && !busy ? "" : "disabled"}></textarea>
                <div class="row"><span class="muted small grow">They start from the newest verified version, your edits included.</span>
                  <button type="button" class="btn small" id="r-revise" ${detail.task.submitted_by === session.user.user_id && !busy ? "" : "disabled"}>Revise</button></div></div>
              <div class="side-card"><h4>Verification — v${esc(artifact.version)}</h4>${tiersHtml(artifact.verification)}
                ${((artifact.verification || {}).flagged_claims || []).length ? `<div class="notice warn small">Numbers not found in the evidence they cite: ${artifact.verification.flagged_claims.map((c) => `<b>${esc(c.number)}</b> (${esc(c.section)})`).join(", ")}</div>` : ""}</div>
              <div class="side-card"><h4>Evidence this task holds</h4><p class="muted small">${editable ? "Click to insert its id where you are typing." : "Click to see where it came from."}</p>
                <div class="evidence-list">${data.evidence.map((e) => `<div class="ev" data-ev="${esc(e.evidence_id)}">
                  <span class="cite ${esc(e.evidence_id[0])}">${esc(e.evidence_id)}</span>
                  <span class="small"><b>${esc(e.title || e.kind)}</b>${e.page ? ` · p${esc(e.page)}` : ""}${e.version ? ` · v${esc(e.version)}` : ""}</span>
                  <div class="small muted clamp3">${esc(e.text)}</div></div>`).join("") || `<div class="empty small">None.</div>`}</div></div>
            </div>
          </div></div>`;
        const sizeAll = () => $$("#r-editor textarea", pane).forEach(autosize);
        tab.onShow = sizeAll;
        requestAnimationFrame(sizeAll);
        tab.dirty = false;
        const markDirty = () => {
          if (tab.dirty || !editable) return;
          tab.dirty = true;
          ["#r-dirty", "#r-discard"].forEach((sel) => { const el = $(sel, pane); if (el) el.hidden = false; });
        };
        $("#r-editor", pane).addEventListener("input", (event) => {
          if (event.target.matches("textarea")) autosize(event.target);
          if (event.target.id !== "r-note") markDirty();
        });
        $("#r-version", pane).addEventListener("change", (event) => {
          if (tab.dirty) {
            event.target.value = artifact.id;
            return toast("You have unsaved changes: save them as a new version, or Discard them, first.", "bad");
          }
          draw(event.target.value);
        });
        const discard = $("#r-discard", pane);
        if (discard) discard.addEventListener("click", () => { tab.dirty = false; draw(artifact.id); });
        $("#r-preview", pane).addEventListener("click", () => previewArtifact(artifact.id));
        $("#r-download", pane).addEventListener("click", () =>
          download(`/api/artifacts/${artifact.id}/download`, artifact.filename).catch((e) => toast(e.message, "bad")));
        const editor = $("#r-editor", pane);
        editor.addEventListener("focusin", (event) => {
          if (event.target.matches("textarea, input")) lastFocus = event.target;
        });
        $$(".ev", pane).forEach((row) => row.addEventListener("click", () => {
          const id = row.dataset.ev;
          if (editable && lastFocus && editor.contains(lastFocus) && !lastFocus.disabled) {
            const at = lastFocus.selectionStart ?? lastFocus.value.length;
            const before = lastFocus.value.slice(0, at);
            const insert = `${before && !before.endsWith(" ") ? " " : ""}[${id}]`;
            lastFocus.value = before + insert + lastFocus.value.slice(lastFocus.selectionEnd ?? at);
            lastFocus.focus();
            lastFocus.selectionStart = lastFocus.selectionEnd = at + insert.length;
            if (lastFocus.id !== "r-note") markDirty();
          } else {
            showEvidence(taskId, id);
          }
        }));
        if (editable) {
          editor.addEventListener("click", (event) => {
            const section = event.target.closest(".section-edit");
            if (event.target.closest("[data-add-section]")) {
              const holder = $(".sections", event.target.closest(".field"));
              holder.insertAdjacentHTML("beforeend", sectionHtml("", "", true));
              $$(".s-heading", holder).pop().focus();
              autosize($$(".s-text", holder).pop());
            } else if (section && event.target.closest("[data-remove]")) {
              section.remove();
            } else if (section && event.target.closest("[data-move]")) {
              const step = Number(event.target.closest("[data-move]").dataset.move);
              const sibling = step < 0 ? section.previousElementSibling : section.nextElementSibling;
              if (sibling) step < 0 ? sibling.before(section) : sibling.after(section);
            } else {
              return;
            }
            markDirty();
          });
          $("#r-save", pane).addEventListener("click", async () => {
            let content;
            try {
              content = collect(editor);
            } catch (error) {
              return toast(error.message, "bad");
            }
            $("#r-save", pane).disabled = true;
            try {
              const saved = await api(`/api/artifacts/${artifact.id}/edit`, { method: "POST",
                body: { content, note: $("#r-note", pane).value.trim() } });
              const g = saved.generated;
              if (g.status === "VERIFIED") {
                toast(`Saved as v${g.version} — verified${g.verification.flagged_claims.length ? `, ${g.verification.flagged_claims.length} number(s) flagged` : ""}.`, "good");
              } else {
                toast(`v${g.version} did not pass verification — see the tiers; the last verified version still stands.`, "bad");
              }
              ide.log(`report v${g.version}: ${g.status}`, g.status === "VERIFIED" ? "ok" : "warn");
              tab.dirty = false;
              await draw(g.artifact_id);
            } catch (error) {
              toast(error.message, "bad");
              $("#r-save", pane).disabled = false;
            }
          });
        }
        $("#r-revise", pane).addEventListener("click", async () => {
          const instruction = $("#r-instruction", pane).value.trim();
          if (!instruction) return toast("Say what should change.", "bad");
          if (tab.dirty) return toast("Save your changes first: the agents start from the newest saved version.", "bad");
          await ide.revise(taskId, instruction);
          await draw();
        });
        return detail;
      };
      bindCites(pane, taskId);
      // Ctrl+S saves, as in any editor.
      pane.addEventListener("keydown", (event) => {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
          event.preventDefault();
          const save = $("#r-save", pane);
          if (save && !save.disabled) save.click();
        }
      });
      tab.cleanups.push(() => { tab.dirty = false; });
      await draw();
      // While the agents work on it (a revision, say), watch the task; redraw when it
      // starts or stops, so the editor never shows a stale version as current.
      every(tab, 4000, async () => {
        const current = await api(`/api/tasks/${taskId}`).catch(() => null);
        if (!current) return;
        const was = tab.lastStatus;
        tab.lastStatus = current.task.status;
        if (was !== current.task.status && (ACTIVE.has(was) || ACTIVE.has(current.task.status))) {
          if (!ACTIVE.has(current.task.status)) ide.log(`the agents finished: ${current.task.status.replace("_", " ")}`, "ok");
          if (tab.dirty) return ide.log("the report changed while you were editing: save your version, or Discard to see theirs", "warn");
          await draw();
        }
      });
    },
  };

  // ------------------------------------------------------------------ doc: pages, issues, what changed
  kinds.doc = {
    title: (tab) => tab.name || "document",
    icon: "▤",
    async render(pane, tab, ide) {
      const documentId = tab.ref;
      const meta = (ide.docs || []).find((d) => d.id === documentId) || {};
      const versions = (await api(`/api/documents/${documentId}/versions`).catch(() => ({ versions: [] }))).versions || [];
      const current = versions.find((v) => v.current) || versions[versions.length - 1] || { version: meta.version || 1 };
      const draw = async (version, page) => {
        const data = await api(`/api/documents/${documentId}/pages/${page}?version=${version}`);
        const doc = data.document || meta;
        tab.name = doc.title || meta.title;
        ide.retitle(tab);
        pane.innerHTML = `<div class="ide-page">
          <div class="row"><h2 style="margin:0" class="grow">${esc(doc.title)}</h2>${levelPill(doc.classification)} ${deptPills(doc.acl)}</div>
          <div class="row small muted" style="margin:6px 0 10px">${esc(meta.folder || "")}${meta.folder ? " · " : ""}
            <select id="doc-v" class="compact">${versions.map((v) => `<option value="${esc(v.version)}" ${Number(v.version) === Number(version) ? "selected" : ""}>version ${esc(v.version)}${v.effective ? ` · effective ${esc(v.effective)}` : ""}${v.current ? " · current" : ""}</option>`).join("") || `<option>version ${esc(version)}</option>`}</select>
            page ${esc(page)} of ${esc(doc.page_count || 1)}
            <button type="button" class="btn small" id="doc-prev" ${page <= 1 ? "disabled" : ""}>‹</button>
            <button type="button" class="btn small" id="doc-next" ${page >= (doc.page_count || 1) ? "disabled" : ""}>›</button>
            <span class="spacer"></span>
            ${versions.length > 1 ? `<button type="button" class="btn small" id="doc-diff">What changed ▸</button>` : ""}
            <button type="button" class="btn small" id="doc-image">Page image</button></div>
          <div id="doc-diff-out"></div>
          <div class="blocks">${(data.blocks || []).map((b) => `<div class="block"><div class="k">${esc(b.kind)} · ${esc(b.source)}</div>${esc(b.text)}</div>`).join("") || `<div class="empty">No text on this page.</div>`}</div>
        </div>`;
        $("#doc-v", pane).addEventListener("change", (e) => draw(Number(e.target.value), 1));
        $("#doc-prev", pane).addEventListener("click", () => draw(version, page - 1));
        $("#doc-next", pane).addEventListener("click", () => draw(version, page + 1));
        $("#doc-image", pane).addEventListener("click", () => viewDocument(documentId, page, doc.page_count || 1));
        const diffBtn = $("#doc-diff", pane);
        if (diffBtn) diffBtn.addEventListener("click", () => showDiff());
        if (tab.diff) showDiff();
      };
      const showDiff = async () => {
        const out = $("#doc-diff-out", pane);
        out.innerHTML = `<span class="spin"></span>`;
        const diff = await api(`/api/documents/${documentId}/diff`);
        const from = (diff.versions || []).find((v) => Number(v.version) === Number(diff.from_version)) || {};
        const to = (diff.versions || []).find((v) => Number(v.version) === Number(diff.to_version)) || {};
        out.innerHTML = `<div class="side-card"><h4>Version ${esc(diff.from_version)}${from.effective ? ` (${esc(from.effective)})` : ""} → version ${esc(diff.to_version)}${to.effective ? ` (${esc(to.effective)})` : ""}</h4>
          ${(diff.changes || []).map((c) => `<div class="diff-row"><span class="pill ${c.change === "added" ? "ok" : c.change === "removed" ? "failed" : "VERIFIED"}">${esc(c.change)}</span>
            ${c.before ? `<div class="diff-old">${esc(c.before)}</div>` : ""}${c.after ? `<div class="diff-new">${esc(c.after)}</div>` : ""}</div>`).join("") || `<div class="empty">No differences in the text.</div>`}
          <div class="muted small">${esc(diff.unchanged_sentences ?? 0)} sentence(s) unchanged.</div></div>`;
      };
      await draw(current.version, 1);
    },
  };

  // ------------------------------------------------------------------ obs: observability, metrics, evaluation
  const OBS = {
    models: "Model evaluation", agents: "Agent behaviour", database: "DB monitoring", alerts: "Alerts",
    security: "Security", policy: "Policy auditing", trace: "Trace", containers: "Container health", resources: "Resource metrics",
  };
  IDE.OBS = OBS;
  const OBS_RENDER = {
    models(d) {
      return `<p class="muted small">${esc(d.about)}</p>
        <div class="stat-row">${C.stat("Grounding rate", d.grounding_rate === null ? "—" : `${Math.round(d.grounding_rate * 100)}%`, "", "quantitative claims traced to their sources")}
          ${C.stat("Answers with citations", `${d.answers_with_citations.cited}/${d.answers_with_citations.finished}`)}</div>
        <h4>Per model and purpose</h4>${table(["Model", "Purpose", "Calls", "Avg", "p95", "Errors", "Fallbacks", "Retried", "Tokens in→out"],
          (d.per_model || []).map((r) => [`<span class="mono">${esc(r.model_id)}</span>`, esc(r.purpose), num(r.calls), ms(r.avg_ms), ms(r.p95_ms), num(r.errors), num(r.fallbacks), num(r.retried), `${num(r.prompt_tokens)}→${num(r.completion_tokens)}`]))}
        <h4>Verification ladder, by tier</h4>${table(["Tier", "Status", "Deliverables"], (d.verification_by_tier || []).map((r) => [esc(r.tier), statusPill(r.status), num(r.n)]))}`;
    },
    agents(d) {
      return `<div class="stat-row">${C.stat("Team tasks", (d.teams || {}).team_tasks ?? 0)}${C.stat("Helper agents", (d.teams || {}).helpers ?? 0)}
          ${C.stat("Failed agents", (d.teams || {}).failed_agents ?? 0)}${C.stat("Human steps", d.human_steps)}</div>
        <h4>Tasks</h4>${table(["Kind", "Status", "Tasks", "Avg steps", "Avg tool calls", "Avg seconds"], (d.tasks || []).map((r) => [esc(r.kind), statusPill(r.status), num(r.n), num(r.avg_steps), num(r.avg_tool_calls), num(r.avg_seconds)]))}
        <h4>Behaviour</h4>${table(["Event", "Count"], (d.events || []).map((r) => [esc(r.step_type), num(r.n)]))}
        <h4>Tools</h4>${table(["Tool", "Outcome", "Calls"], (d.tools || []).map((r) => [esc(r.tool), statusPill(r.status), num(r.n)]))}
        <h4>Why tasks stopped</h4>${table(["Cause", "Tasks"], (d.failures || []).map((r) => [esc(r.cause), num(r.n)]))}`;
    },
    database(d) {
      const db = d.database || {};
      return `<div class="stat-row">${C.stat("Database size", bytes(db.bytes))}${C.stat("Connections", db.connections)}
          ${C.stat("Longest query", `${Math.round(db.longest_query_s || 0)} s`)}${C.stat("Postgres / pgvector", `${esc(String(db.postgres || "").split(" ")[0])} / ${esc(db.pgvector || "—")}`)}</div>
        <h4>Tables</h4>${table(["Table", "Rows", "Size", "Seq scans", "Index scans"], (d.tables || []).map((r) => [`<span class="mono">${esc(r.table)}</span>`, num(r.rows), bytes(r.bytes), num(r.seq_scans), num(r.index_scans)]))}
        <h4>Queues</h4><div class="row">${["ingestion", "tasks"].map((k) => `<div><b>${k}</b> ${((d.queues || {})[k] || []).map((r) => `<span class="pill ${esc(r.status)}">${esc(r.status)}: ${esc(r.n)}</span>`).join(" ")}</div>`).join("")}</div>`;
    },
    alerts(d) {
      const list = Array.isArray(d) ? d : [];
      return list.length ? list.map((a) => `<div class="alert ${esc(a.level)}"><b>${esc(a.title)}</b> <span class="pill">${esc(a.count)}</span><div class="small">${esc(a.detail)}</div></div>`).join("")
        : `<div class="notice good">Nothing needs attention.</div>`;
    },
    security(d) {
      const s = (d.sovereignty || {}).counts || {};
      return `<div class="stat-row">${C.stat("External connections", s.observed ?? 0, s.observed ? "bad" : "good")}${C.stat("Outbound attempts stopped", (s.attempts || 0) + (s.dns_denied || 0))}
          ${C.stat("Enforcement", (d.enforcement || {}).applied ? "applied" : "in-process only")}</div>
        <h4>Security events, last day</h4>${table(["Event", "Count"], (d.last_day || []).map((r) => [esc(r.event_name), num(r.n)]))}`;
    },
    policy(d) {
      return `<p class="muted small">${esc(d.audit_rows)} rows in the hash-chained audit log. The rules, first match wins, default deny:</p>
        ${table(["Rule", "Effect", "Reason"], (d.rules || []).map((r) => [`<span class="mono">${esc(r.id)}</span>`, statusPill(r.effect === "allow" ? "ok" : "denied").replace(/>[^<]*</, `>${esc(r.effect)}<`), esc(r.reason || "")]))}
        <h4>Decisions by rule and tool (7 days)</h4>${table(["Decision", "Rule", "Tool", "Count"], (d.by_rule || []).map((r) => [esc(r.event_name), `<span class="mono">${esc(r.rule_id || "default deny")}</span>`, esc(r.tool), num(r.n)]))}
        <h4>Recent</h4>${table(["#", "Decision", "Tool", "Rule", "Who"], (d.recent || []).map((r) => [num(r.seq), esc(r.event_name.replace("policy.", "")), esc(r.tool), `<span class="mono small">${esc(r.rule_id || "")}</span> <span class="small muted">${esc(r.reason || "")}</span>`, esc(r.agent_id || r.actor_id || "")]))}`;
    },
    containers(d) {
      const rt = d.inference_runtime || {};
      return `<div class="stat-row">${C.stat("Inference runtime", rt.reachable ? "reachable" : "not answering", rt.reachable ? "good" : "bad", rt.endpoint || "")}
          ${C.stat("Sandbox", (d.sandbox || {}).ok ? (d.sandbox.kind || "ok") : "down", (d.sandbox || {}).ok ? "good" : "bad")}
          ${C.stat("API process", `${(d.api_process || {}).rss_mb ?? "—"} MB`, "", `${(d.api_process || {}).threads ?? "—"} threads`)}</div>
        <h4>Services (heartbeats)</h4>${table(["Service", "Instance", "Last seen", "Memory", "Detail"], (d.services || []).map((s) => [`<b>${esc(s.service)}</b> ${s.stale ? `<span class="pill failed">stale</span>` : `<span class="pill ok">alive</span>`}`, `<span class="mono small">${esc(s.instance)}</span>`, `${esc(s.seconds_since)} s ago`, `${esc((s.detail || {}).rss_mb ?? "—")} MB`, `<div class="detail-kv">${detailKv(s.detail)}</div>`]))}`;
    },
    resources(d) {
      const h = d.host || {};
      const g = d.gpu_admission || {};
      return `<div class="stat-row">${C.stat("CPUs", h.cpus ?? "—", "", h.load ? `load ${h.load.join(" / ")}` : "")}
          ${C.stat("Memory used", h.memory_used_pct === undefined ? "—" : `${h.memory_used_pct}%`, "", h.memory_total_mb ? `of ${h.memory_total_mb} MB` : "")}
          ${C.stat("Data disk free", h.data_disk ? `${h.data_disk.free_gb} GB` : "—", "", h.data_disk ? `of ${h.data_disk.total_gb} GB` : "")}
          ${C.stat("GPU admission", `${g.in_use ?? 0}/${g.capacity ?? "—"}`, "", `${g.waiting ?? 0} waiting · avg ${ms(g.avg_wait_ms)}`)}</div>
        <p class="muted small">${esc(d.note)}</p>`;
    },
    trace(d) {
      return table(["When", "Span", "Kind", "Task", "Duration", "Status"], (d.spans || []).map((s) => [esc(ago(s.started_at)), `<span class="mono small">${esc(s.name)}</span>`, esc(s.kind), `<span class="mono small">${esc(String(s.task_id || "").slice(0, 8))}</span>`, ms(s.duration_ms), statusPill(s.status === "ok" ? "ok" : "failed")]));
    },
  };
  kinds.obs = {
    title: (tab) => OBS[tab.ref] || tab.ref,
    icon: "∿",
    async render(pane, tab) {
      const section = tab.ref;
      const draw = async () => {
        const data = section === "trace" ? await api("/api/traces") : (await api(`/api/observability?sections=${section}`))[section];
        pane.innerHTML = `<div class="ide-page"><div class="row"><h2 style="margin:0" class="grow">${esc(OBS[section])}</h2>
          <span class="muted small">refreshed ${new Date().toLocaleTimeString()}</span><button type="button" class="btn small" id="o-refresh">Refresh</button></div>
          ${data && data.error ? `<div class="notice bad">${esc(data.error)}</div>` : (OBS_RENDER[section] || (() => ""))(data || {})}</div>`;
        $("#o-refresh", pane).addEventListener("click", () => draw().catch((e) => toast(e.message, "bad")));
      };
      await draw();
      every(tab, 15000, () => draw().catch(() => null));
    },
  };

  // ------------------------------------------------------------------ state: the sandbox tree
  function node(label, body, open = true, id = "") {
    return `<details class="tree" ${open ? "open" : ""} ${id ? `id="${id}"` : ""}><summary>${label}</summary><div class="tree-body">${body}</div></details>`;
  }
  function leaf(label, value) {
    return `<div class="leaf"><span class="k">${esc(label)}</span> ${value}</div>`;
  }
  kinds.state = {
    title: (tab) => (tab.ref ? `sandbox · ${String(tab.name || tab.ref).slice(0, 24)}` : "environment"),
    icon: "⌗",
    async render(pane, tab, ide) {
      const draw = async () => {
        if (!tab.ref) {
          const env = await api("/api/workbench/environment");
          pane.innerHTML = `<div class="ide-page tree-page"><h2>Environment</h2>
            ${node("Tasks", `${Object.entries(env.tasks.by_status).map(([k, v]) => `<span class="pill ${esc(k)}">${esc(k.replace("_", " "))}: ${esc(v)}</span>`).join(" ")}
              ${env.tasks.active.map((t) => `<div class="leaf link" data-task="${esc(t.id)}">▶ ${esc(t.title)} ${statusPill(t.status)}</div>`).join("")}`)}
            ${node("Shared state", env.shared_state.map((s) => `<div class="leaf"><span class="pill">${esc(s.kind)}</span> <span class="muted small">${esc(s.by)}</span> ${cited(s.content)}</div>`).join("") || "<div class='muted small'>nothing yet</div>")}
            ${node("Agent state", env.agents.map((a) => `<div class="leaf link" data-task="${esc(a.task_id)}"><b>${esc(a.who)}</b> ${statusPill(a.status)} <span class="small muted">${esc(a.current_step || a.goal)}</span></div>`).join("") || "<div class='muted small'>no agents working</div>")}
            ${node("Artifacts", env.artifacts.map((a) => `<div class="leaf link" data-report="${esc(a.task_id)}">📄 ${esc(a.title)} <span class="muted small">v${esc(a.version)}</span> ${statusPill(a.status)}</div>`).join("") || "<div class='muted small'>none</div>")}
            ${node("Resources", leaf("Models", env.resources.models.map((m) => `<span class="pill ${m.installed ? "ok" : "failed"}">${esc(m.id)}</span>`).join(" ")) + leaf("Tools", env.resources.tools.map((t) => `<span class="pill">${esc(t)}</span>`).join(" ")) + leaf("MCP servers", "<span class='muted'>none — tools are in-process plugins behind the policy chokepoint</span>") + leaf("Sandbox", esc(env.resources.sandbox)))}
          </div>`;
        } else {
          const s = await api(`/api/tasks/${tab.ref}/state`);
          tab.name = s.task.title || s.task.objective.slice(0, 30);
          ide.retitle(tab);
          const sh = s.shared_state;
          const list = (items) => (items || []).map((x) => `<div class="leaf">${cited(x.content)} <span class="muted small">— ${esc(x.by)}${x.to ? ` → ${esc(x.to)}` : ""}</span></div>`).join("") || "<div class='muted small'>none</div>";
          pane.innerHTML = `<div class="ide-page tree-page"><h2>Sandbox</h2>
            ${node("Task", leaf("Objective", esc(s.task.objective)) + leaf("Constraints", `${levelPill(s.task.constraints.classification)} deliverable <b>${esc(s.task.constraints.deliverable || "none")}</b> · budgets ${esc(s.task.constraints.budgets.steps)} steps, ${esc(s.task.constraints.budgets.agent_steps)} per helper, ${esc(s.task.constraints.budgets.tokens)} tokens, ${esc(s.task.constraints.budgets.seconds)} s`) + leaf("Status", statusPill(s.task.status)) + leaf("Current phase", esc(s.task.phase)), true, "st-task")}
            ${node("Shared state", leaf("Shared plan", `<ol class="plan-steps">${(sh.plan || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ol>`) + node("Decisions", list(sh.decisions), true) + node("Discovered facts", list(sh.facts), true) + node("Assumptions", list(sh.assumptions), true) + node("Questions", list(sh.questions), true) + node("Notes", list(sh.notes), false), true, "st-shared")}
            ${node("Agent state", s.agents.map((a) => node(`${esc(a.name)} ${statusPill(a.status)}`, leaf("Current goal", esc(a.goal)) + leaf("Plan", `<ol class="plan-steps">${(a.plan || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ol>`) + leaf("Progress", `${esc(a.progress.steps)}/${esc(a.progress.limit)} steps · ${esc(a.progress.tool_calls)} tool calls${a.current_step ? ` · now: ${esc(a.current_step)}` : ""}`) + leaf("Working memory", `${a.working_memory.last_thought ? `<i>${esc(a.working_memory.last_thought)}</i> · ` : ""}evidence ${(a.working_memory.evidence || []).map((e) => `<span class="cite ${esc(e[0])}" data-cite="${esc(e)}">${esc(e)}</span>`).join("") || "—"}`) + `<button type="button" class="btn small" data-agent="${esc(a.agent_id)}">Open ${esc(a.agent_id)}.plan</button>`, a.status === "running")).join("") || "<div class='muted small'>not started</div>", true, "st-agents")}
            ${node("Artifacts", leaf("Files", s.artifacts.files.map((f) => `<span class="mono small">${esc(f.path)}</span> (${bytes(f.bytes)})`).join(", ") || "—") + leaf("Datasets", s.artifacts.datasets.map((f) => `<span class="mono small">${esc(f.path)}</span>`).join(", ") || "—") + leaf("Generated code", s.artifacts.generated_code.map((c) => `<details><summary>step ${esc(c.step)} by ${esc(c.who)}</summary><pre>${esc(c.source)}</pre></details>`).join("") || "—") + leaf("Outputs", s.artifacts.outputs.map((o) => `<span class="link" data-report="${esc(s.task.id)}">📄 ${esc(o.title)}</span> <span class="muted small">v${esc(o.version)}</span> ${statusPill(o.status)}`).join("<br/>") || "—"), true, "st-artifacts")}
            ${node("Resources", leaf("Available tools", (s.resources.tools || []).map((t) => `<span class="pill">${esc(t)}</span>`).join(" ") || "—") + leaf("Withheld", (s.resources.withheld_tools || []).map((t) => `<span class="pill failed" title="${esc(t.why)}">${esc(t.name)}</span>`).join(" ") || "—") + leaf("MCP servers", "<span class='muted'>none</span>") + leaf("Models used", s.resources.models.map((m) => `<span class="pill mono">${esc(m)}</span>`).join(" ") || "—"), true, "st-resources")}
            ${node("Event / execution history", leaf("Actions", esc(s.history.counts.actions)) + leaf("Tool calls", esc(s.history.counts.tool_calls)) + leaf("State transitions", esc(s.history.counts.state_transitions)) + leaf("Observations", esc(s.history.counts.observations)) + leaf("Human steps", esc(s.history.counts.human_steps)) + `<div class="history">${s.history.recent.map((r) => `<div class="act ${esc(r.level)}"><span class="muted small">${esc(ago(r.at))}</span> ${cited(r.text)}</div>`).join("")}</div>`, true, "st-history")}
          </div>`;
          bindCites(pane, tab.ref);
        }
        $$("[data-task]", pane).forEach((el) => el.addEventListener("click", () => ide.openTask(el.dataset.task)));
        $$("[data-report]", pane).forEach((el) => el.addEventListener("click", () => ide.openReport(el.dataset.report)));
        $$("[data-agent]", pane).forEach((el) => el.addEventListener("click", () => ide.openAgent(tab.ref, el.dataset.agent)));
        if (tab.focus) {
          const target = $(`#st-${tab.focus}`, pane);
          if (target) { target.open = true; target.scrollIntoView({ block: "start" }); }
          tab.focus = null;
        }
      };
      await draw();
      every(tab, 5000, () => draw().catch(() => null));
    },
  };

  // ------------------------------------------------------------------ memory: what the workbench remembers
  kinds.memory = {
    title: () => "memory",
    icon: "◆",
    async render(pane, tab, ide) {
      pane.innerHTML = `<div class="ide-page">
        <div class="row"><h2 class="grow" style="margin:0">What the workbench remembers</h2>
          <select id="m-status" class="compact"><option value="active">active</option><option value="superseded">superseded</option><option value="archived">archived</option></select></div>
        <p class="muted small">Facts, outcomes and lessons the memory manager kept from finished work, and what people stated. Agents
          recall them before they plan — to know where to look — but cite documents, never memories. You see only what your
          department and clearance allow, exactly as for documents.</p>
        <div class="row"><input type="search" id="m-q" placeholder="Recall by meaning, e.g. lathe L-1 repair cost" class="grow"/><button type="button" class="btn" id="m-go">Recall</button></div>
        <div id="m-list" style="margin-top:10px"><span class="spin"></span></div>
        <h3 style="margin-top:16px">State something the workbench should know</h3>
        <div class="row"><input type="text" id="m-subject" placeholder="Subject (e.g. Lathe L-1)" style="max-width:220px"/>
          <input type="text" id="m-content" class="grow" placeholder="One self-contained statement"/>
          <button type="button" class="btn primary" id="m-add">Remember</button></div>
        <h3 style="margin-top:16px">The memory manager's decisions</h3><div id="m-events"></div></div>`;
      const load = async () => {
        const q = $("#m-q", pane).value.trim();
        const data = await api(`/api/memory?${q ? `q=${encodeURIComponent(q)}` : `status=${$("#m-status", pane).value}`}`);
        $("#m-list", pane).innerHTML = (data.memories || []).map((m) => `<div class="memory ${esc(m.status)}" data-id="${esc(m.id)}">
          <div class="row small"><span class="pill">${esc(m.tier)}</span><span class="pill">${esc(m.memory_type)}</span>
            <b class="grow clamp" title="${esc(m.subject || "")}">${esc(m.subject || "")}</b>
            ${levelPill(m.classification)} <span class="muted" style="white-space:nowrap">${esc(ago(m.created_at))} · used ${esc(m.access_count)}×${m.score ? ` · score ${esc(m.score)}` : ""}</span>
            <button type="button" class="btn small" data-edit>Edit</button>
            <button type="button" class="btn small" data-status="${m.status === "archived" ? "active" : "archived"}">${m.status === "archived" ? "Restore" : "Archive"}</button></div>
          <div class="content">${esc(m.content)}</div></div>`).join("") || `<div class="empty">Nothing remembered${q ? " about that" : ""} yet.</div>`;
        $$(".memory", pane).forEach((row) => {
          $("[data-edit]", row).addEventListener("click", () => {
            // Edited in place, like everything else here: the text becomes a box with Save and Cancel.
            const content = $(".content", row);
            if ($("textarea", row)) return;
            const before = content.textContent;
            content.innerHTML = `<textarea>${esc(before)}</textarea>
              <div class="row"><span class="spacer"></span><button type="button" class="btn small" data-cancel>Cancel</button>
              <button type="button" class="btn small primary" data-save>Save</button></div>`;
            const box = $("textarea", content);
            box.focus();
            $("[data-cancel]", content).addEventListener("click", () => { content.textContent = before; });
            $("[data-save]", content).addEventListener("click", async () => {
              const text = box.value.trim();
              if (!text || text === before) { content.textContent = before; return; }
              try { await api(`/api/memory/${row.dataset.id}`, { method: "PUT", body: { content: text } }); load(); } catch (e) { toast(e.message, "bad"); }
            });
          });
          $("[data-status]", row).addEventListener("click", async (event) => {
            try { await api(`/api/memory/${row.dataset.id}/status`, { method: "POST", body: { status: event.target.dataset.status } }); load(); } catch (e) { toast(e.message, "bad"); }
          });
        });
        const events = (await api("/api/memory/events")).events || [];
        $("#m-events", pane).innerHTML = table(["When", "Decision", "Candidate", "Why", "By"], events.slice(0, 30).map((e) => [esc(ago(e.created_at)), `<b>${esc(e.operation)}</b>`, `<span class="small">${esc(String(e.candidate || e.memory_content || "").slice(0, 160))}</span>`, `<span class="small muted">${esc(e.reason || "")}</span>`, esc(e.actor)]));
      };
      $("#m-go", pane).addEventListener("click", () => load().catch((e) => toast(e.message, "bad")));
      $("#m-q", pane).addEventListener("keydown", (e) => { if (e.key === "Enter") load(); });
      $("#m-status", pane).addEventListener("change", () => { $("#m-q", pane).value = ""; load(); });
      $("#m-add", pane).addEventListener("click", async () => {
        const content = $("#m-content", pane).value.trim();
        if (!content) return;
        try {
          const outcome = await api("/api/memory", { method: "POST", body: { content, subject: $("#m-subject", pane).value.trim() } });
          toast(`Memory manager: ${outcome.operation}${outcome.reason ? ` — ${outcome.reason}` : ""}`, "good");
          $("#m-content", pane).value = "";
          load();
        } catch (e) {
          toast(e.message, "bad");
        }
      });
      await load();
    },
  };
})();
