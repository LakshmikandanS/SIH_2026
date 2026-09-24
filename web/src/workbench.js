/* The Workbench view: an IDE for reports, where people and agents work on the same tasks.
 *
 *   left    RESOURCES (the document tree you may read, and the database), REPORTS (your
 *           tasks), TOOLS RUNNING (what the agents are using right now; the Tools panel)
 *   center  editor tabs (task files, reports, tasks, agent plans, documents, panels) and
 *           the command line: /report, /task, /ask, /revise, /note, /steer, /pause ...
 *   right   OBSERVABILITY / METRICS / EVAL, ENVIRONMENT / SANDBOX STATE, and ACTIVITY
 *
 * Plain JavaScript on the same origin as the API; no dependency, no external host. The
 * tab kinds live in workbench-tabs.js.
 */
(() => {
  "use strict";
  const C = window.Citadel;
  const IDE = window.CitadelIDE;
  const { $, $$, esc, api, toast, statusPill, levelPill, ago, renderAnswer, renderStep, showEvidence, session, views } = C;
  const kinds = IDE.kinds;
  const ACTIVE = new Set(["submitted", "planning", "running", "revision_required", "paused"]);
  const STORE = () => `citadel.ide.${session.user ? session.user.user_id : "anon"}`;

  const OBS_ITEMS = ["models", "agents", "database", "alerts", "security", "policy", "trace", "containers", "resources"];
  const STATE_ITEMS = [["task", "Tasks"], ["shared", "Shared state"], ["agents", "Agent state"], ["artifacts", "Artifacts"], ["resources", "Resources"], ["history", "Event history"]];

  views.work = async (root, arg) => {
    root.classList.add("ide-host");
    root.innerHTML = `
      <div class="ide">
        <aside class="ide-left">
          <section class="pane grow">
            <div class="pane-head"><span>RESOURCES</span><span class="spacer"></span>
              <span class="seg"><button type="button" class="seg-btn active" data-res="files">Files</button><button type="button" class="seg-btn" data-res="db">DB</button></span></div>
            <div class="pane-body" id="ide-tree"><span class="spin"></span></div>
          </section>
          <section class="pane">
            <div class="pane-head"><span>REPORTS</span><span class="spacer"></span><button type="button" class="icon-btn" id="ide-new" title="Write a new report">＋</button></div>
            <div class="pane-body short" id="ide-reports"></div>
          </section>
          <section class="pane">
            <div class="pane-head"><span>TOOLS RUNNING</span><span class="spacer"></span><button type="button" class="icon-btn" id="ide-tools" title="Tools: available, active, history">TOOLS ▾</button></div>
            <div class="pane-body short" id="ide-running"><div class="muted small">idle</div></div>
          </section>
        </aside>
        <section class="ide-center">
          <div class="tabs" id="ide-tabs" role="tablist"></div>
          <div class="tab-panes" id="ide-panes"></div>
          <div class="cli" id="ide-cli"><div class="cli-grip" id="cli-grip" title="Drag to resize"></div>
            <div class="cli-head"><span>CLI</span><span class="muted small" id="cli-context"></span><span class="spacer"></span>
              <button type="button" class="icon-btn" id="cli-clear" title="Clear">⌫</button><button type="button" class="icon-btn" id="cli-toggle" title="Collapse">▾</button></div>
            <div class="cli-log" id="cli-log"></div>
            <form class="cli-input" id="cli-form" autocomplete="off"><span class="prompt">citadel ›</span>
              <input type="text" id="cli-in" spellcheck="false" placeholder="/report on lathe L-1: its condition and workload …   ·   /ask …   ·   /help"/></form>
          </div>
        </section>
        <aside class="ide-right">
          <section class="pane">
            <div class="pane-head"><span>OBSERVABILITY / METRICS / EVAL</span></div>
            <div class="pane-body list" id="ide-obs">${OBS_ITEMS.map((k) => `<div class="nav-row" data-obs="${k}"><span>${esc(IDE.OBS[k])}</span><span class="spacer"></span><span class="badge" id="badge-${k}"></span></div>`).join("")}</div>
          </section>
          <section class="pane">
            <div class="pane-head"><span>ENVIRONMENT / SANDBOX STATE</span></div>
            <div class="pane-body list" id="ide-env">${STATE_ITEMS.map(([k, label]) => `<div class="nav-row" data-state="${k}">${esc(label)}</div>`).join("")}
              <div class="nav-row" data-memory>Memory</div></div>
          </section>
          <section class="pane grow">
            <div class="pane-head"><span>ACTIVITY</span><span class="spacer"></span><span class="live-dot" title="live"></span></div>
            <div class="pane-body" id="ide-activity"><div class="muted small" data-empty style="padding:4px 8px">What you and the agents do appears here.</div></div>
          </section>
        </aside>
      </div>`;

    const ide = {
      tabs: [],
      active: null,
      task: null,
      docs: [],
      cursor: 0,
      history: [],
      historyAt: -1,
      closed: false,
    };
    const panes = $("#ide-panes", root);
    const cleanups = [];
    C.app.cleanups.push(() => {
      ide.closed = true;
      ide.tabs.forEach((t) => closeResources(t));
      cleanups.forEach((fn) => fn());
    });
    const poll = (msInterval, fn) => {
      const id = setInterval(() => { if (!ide.closed) fn().catch(() => null); }, msInterval);
      cleanups.push(() => clearInterval(id));
      fn().catch(() => null);
    };

    // ---------------------------------------------------------------- tabs
    function tabKey(kind, ref, extra) {
      return [kind, ref, extra].filter((v) => v !== undefined && v !== null && v !== "").join(":");
    }
    function persist() {
      try {
        sessionStorage.setItem(STORE(), JSON.stringify({
          tabs: ide.tabs.map(({ key, kind, ref, agent, name }) => ({ key, kind, ref, agent, name })), active: ide.active,
        }));
      } catch (_) { /* private mode: tabs last as long as the page */ }
      const tab = ide.tabs.find((t) => t.key === ide.active);
      history.replaceState(null, "", `#/work${tab && tab.kind !== "welcome" ? `/${encodeURIComponent(tab.key)}` : ""}`);
    }
    function drawTabs() {
      $("#ide-tabs", root).innerHTML = ide.tabs.map((t) => `
        <div class="tab ${t.key === ide.active ? "active" : ""}" data-tab="${esc(t.key)}" role="tab" title="${esc(kinds[t.kind].title(t))}">
          <span class="tab-icon">${kinds[t.kind].icon}</span><span class="tab-title">${esc(kinds[t.kind].title(t))}</span>
          ${t.kind === "welcome" && ide.tabs.length === 1 ? "" : `<button type="button" class="tab-x" data-close="${esc(t.key)}" title="Close">×</button>`}
        </div>`).join("");
      const active = $(".tab.active", $("#ide-tabs", root));
      if (active) active.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
    function closeResources(tab) {
      tab.closed = true;
      (tab.cleanups || []).splice(0).forEach((fn) => { try { fn(); } catch (_) { /* best effort */ } });
    }
    async function renderTab(tab) {
      closeResources(tab);
      tab.closed = false;
      tab.cleanups = [];
      const pane = tab.pane;
      pane.innerHTML = `<div class="ide-page"><span class="spin"></span></div>`;
      try {
        await kinds[tab.kind].render(pane, tab, ide);
      } catch (error) {
        if (error.status === 401) return;
        pane.innerHTML = `<div class="ide-page"><div class="notice bad">${esc(error.message)}</div></div>`;
      }
    }
    ide.open = (kind, ref, options = {}) => {
      const key = tabKey(kind, ref, options.agent);
      let tab = ide.tabs.find((t) => t.key === key);
      if (!tab) {
        tab = { key, kind, ref, agent: options.agent, name: options.name, cleanups: [], visible: false };
        tab.pane = document.createElement("div");
        tab.pane.className = "tab-pane";
        tab.pane.hidden = true;
        panes.appendChild(tab.pane);
        ide.tabs.push(tab);
        Object.assign(tab, options);
        renderTab(tab);
      } else if (options.refresh) {
        Object.assign(tab, options);
        renderTab(tab);
      } else {
        Object.assign(tab, options);
      }
      ide.activate(key);
      return tab;
    };
    ide.activate = (key) => {
      ide.active = key;
      ide.tabs.forEach((t) => {
        t.visible = t.key === key;
        t.pane.hidden = !t.visible;
      });
      const tab = ide.tabs.find((t) => t.key === key);
      if (tab && ["task", "report", "agent", "state"].includes(tab.kind) && tab.ref) ide.setTask(tab.ref);
      if (tab && tab.onShow) requestAnimationFrame(() => tab.onShow());
      drawTabs();
      persist();
    };
    ide.closeTab = (key) => {
      const index = ide.tabs.findIndex((t) => t.key === key);
      if (index < 0) return;
      const closing = ide.tabs[index];
      if (closing.dirty && !closing.closeArmed) {
        closing.closeArmed = true;
        setTimeout(() => { closing.closeArmed = false; }, 4000);
        return toast("This report has unsaved changes. Close it again to discard them.", "bad");
      }
      const [tab] = ide.tabs.splice(index, 1);
      closeResources(tab);
      tab.pane.remove();
      if (!ide.tabs.length) ide.open("welcome");
      else if (ide.active === key) ide.activate(ide.tabs[Math.max(0, index - 1)].key);
      else drawTabs();
      persist();
    };
    ide.retitle = () => { drawTabs(); persist(); };
    $("#ide-tabs", root).addEventListener("click", (event) => {
      const close = event.target.closest("[data-close]");
      if (close) return ide.closeTab(close.dataset.close);
      const tab = event.target.closest("[data-tab]");
      if (tab) ide.activate(tab.dataset.tab);
    });
    $("#ide-tabs", root).addEventListener("auxclick", (event) => {
      const tab = event.target.closest("[data-tab]");
      if (tab && event.button === 1) ide.closeTab(tab.dataset.tab);
    });

    ide.openTask = (id, name) => ide.open("task", id, name ? { name } : {});
    ide.openReport = (id) => ide.open("report", id, { refresh: true });
    ide.openAgent = (taskId, agentId) => ide.open("agent", taskId, { agent: agentId });
    ide.openDoc = (id, name, diff = false) => ide.open("doc", id, { name, diff, refresh: diff });
    ide.openReportOrTask = async (id) => {
      const detail = await api(`/api/tasks/${id}`).catch(() => null);
      if (detail && (detail.artifacts || []).some((a) => a.template_id) && !ACTIVE.has(detail.task.status)) ide.openReport(id);
      else ide.openTask(id, detail ? detail.task.title : undefined);
    };
    ide.setTask = (id) => {
      ide.task = id;
      const cached = C.app.taskCache.get(id);
      $("#cli-context", root).textContent = id ? `· on ${cached ? (cached.task.title || cached.task.goal).slice(0, 48) : id.slice(0, 8)}` : "";
    };
    ide.appendStep = (timeline, entry, taskId, live) => {
      const html = renderStep(entry, taskId);
      if (!html) return;
      const holder = document.createElement("div");
      holder.innerHTML = html;
      const nodeEl = holder.firstElementChild;
      timeline.appendChild(nodeEl);
      if (live) nodeEl.scrollIntoView({ block: "nearest", behavior: "smooth" });
    };

    // The journal, live: Server-Sent Events read with fetch so the token stays in a header.
    ide.follow = async (taskId, after, tab, onEntry, flag) => {
      const controller = new AbortController();
      tab.cleanups.push(() => controller.abort());
      if (flag) flag.innerHTML = `<span class="spin"></span> <span class="small muted">live</span>`;
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
            if (event === "step") onEntry(JSON.parse(data.join("\n")));
            else if (event === "end") return;
          }
        }
      } catch (error) {
        if (error.name !== "AbortError") throw error;
      } finally {
        if (flag) flag.innerHTML = "";
      }
    };

    // ---------------------------------------------------------------- actions shared by tabs and the CLI
    ide.submitReport = async (goal, classification) => {
      const text = /^\s*(write |prepare |draft )?(a |the )?report\b/i.test(goal) ? goal : `Report on ${goal}`;
      const task = await api("/api/tasks", { method: "POST", body: { goal: text, classification } });
      ide.log(`report task ${task.id.slice(0, 8)} submitted — the agents are on it`, "ok");
      ide.openTask(task.id, task.title);
      loadReports();
      return task;
    };
    ide.newDraft = async (name, body) => {
      const draft = await api("/api/drafts", { method: "POST", body: { name: name || "untitled task", body: body || "" } });
      ide.log(`opened task file ${draft.name}.task — write the request, then Commit`, "info");
      ide.open("draft", draft.id, { name: draft.name });
    };
    ide.taskAction = async (action, taskId) => {
      try {
        if (action === "report") return ide.openReport(taskId);
        if (action === "state") return ide.open("state", taskId, { refresh: true });
        const reply = await api(`/api/tasks/${taskId}/${action}`, { method: "POST" });
        ide.log(`${action}: task is ${reply.status.replace("_", " ")}${reply.pause_requested ? " (pause requested)" : ""}`, "info");
        const tab = ide.tabs.find((t) => t.kind === "task" && t.ref === taskId);
        if (tab) renderTab(tab);
      } catch (error) {
        toast(error.message, "bad");
        ide.log(error.message, "bad");
      }
    };
    ide.revise = async (taskId, instruction) => {
      try {
        const reply = await api(`/api/tasks/${taskId}/revise`, { method: "POST", body: { instruction } });
        ide.log(`sent back to the agents: "${instruction}" — task is ${reply.status.replace("_", " ")}`, "ok");
        toast("The agents are revising it; the report updates when they finish.", "good");
      } catch (error) {
        toast(error.message, "bad");
        ide.log(error.message, "bad");
      }
    };
    ide.addNote = async (taskId, kind, content, to) => {
      const evidence = Array.from(new Set((content.match(/\b[EC]\d+\b/g) || [])));
      const body = { kind, content: content.replace(/\s*\[([EC]\d+)\]/g, "").trim() || content, evidence };
      if (to) body.to = to;
      const reply = await api(`/api/tasks/${taskId}/notes`, { method: "POST", body });
      ide.log(`${to ? `told ${to}` : `added a ${kind}`}: ${reply.content}`, "info");
      return reply;
    };

    // ---------------------------------------------------------------- the CLI
    const log = $("#cli-log", root);
    ide.log = (text, level = "", html = false) => {
      const line = document.createElement("div");
      line.className = `cli-line ${level}`;
      line.innerHTML = `<span class="t">${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</span> ${html ? text : esc(text)}`;
      log.appendChild(line);
      while (log.children.length > 300) log.firstChild.remove();
      log.scrollTop = log.scrollHeight;
      return line;
    };
    ide.focusCli = (text) => {
      const input = $("#cli-in", root);
      $("#ide-cli", root).classList.remove("collapsed");
      input.value = text || "";
      input.focus();
    };
    log.addEventListener("click", (event) => {
      const chip = event.target.closest("[data-cite]");
      if (chip && chip.dataset.task) showEvidence(chip.dataset.task, chip.dataset.cite);
      const open = event.target.closest("[data-open-task]");
      if (open) ide.openReportOrTask(open.dataset.openTask);
    });

    const needTask = () => {
      if (!ide.task) throw new Error("no task in focus — open a task or report tab first");
      return ide.task;
    };
    const findTask = async (words) => {
      if (!words) return needTask();
      const tasks = (await api("/api/tasks")).tasks || [];
      const w = words.toLowerCase();
      const hit = tasks.find((t) => t.id.startsWith(w)) || tasks.find((t) => `${t.title} ${t.goal}`.toLowerCase().includes(w));
      if (!hit) throw new Error(`no task of yours matches "${words}"`);
      return hit.id;
    };
    const waitFor = async (taskId, line) => {
      for (let i = 0; i < 600 && !ide.closed; i += 1) {
        const detail = await api(`/api/tasks/${taskId}`);
        if (!ACTIVE.has(detail.task.status)) return detail;
        if (line) line.querySelector(".status").textContent = detail.task.status.replace("_", " ");
        await new Promise((r) => setTimeout(r, 1200));
      }
      throw new Error("still waiting — see the task tab");
    };
    const HELP = [
      ["/report <what to cover>", "write a report now: e.g. /report on lathe L-1: its condition, the options, and a recommendation"],
      ["/task <name>", "open a task file to write a detailed request; Commit runs it"],
      ["/revise <instruction>", "send the report in focus back to the agents (they keep your edits)"],
      ["/ask <question>", "a quick answer from the documents, or about the work itself (what is agent 2 doing?)"],
      ["/open <words>", "open a document, report or task by name"],
      ["/note <text>  /fact  /decision  /question", "add to the focused task's shared state (cite like [E2])"],
      ["/steer <agent> <text>", "tell one agent something at its next step (lead, agent_1 …)"],
      ["/pause  /resume  /cancel [task]", "the focused task, or one you name"],
      ["/run <tool> <json>", 'run a tool on your task through the chokepoint: /run docs.search {"query": "L-1 runout"}'],
      ["/search <query>", "search the documents you may read, with what was withheld"],
      ["/remember <text>", "tell the workbench something to remember"],
      ["/status", "your tasks in progress"],
      ["/memory  /env  /state", "open the memory, environment or sandbox-state tab"],
      ["/clear", "clear this log"],
    ];
    const commands = {
      async help() {
        ide.log(`<div class="help">${HELP.map(([c, d]) => `<div><code>${esc(c)}</code> ${esc(d)}</div>`).join("")}</div>`, "", true);
      },
      async clear() { log.innerHTML = ""; },
      async report(rest) {
        if (!rest) throw new Error("say what the report should cover: /report on lathe L-1: its condition, …");
        await ide.submitReport(rest.replace(/^on\s+/i, "Report on "), undefined);
      },
      async task(rest) { await ide.newDraft(rest || "untitled task", ""); },
      async revise(rest) {
        if (!rest) throw new Error("say what should change: /revise add a section on operator training");
        const id = needTask();
        await ide.revise(id, rest);
        ide.openReport(id);
      },
      async ask(rest) {
        if (!rest) throw new Error("ask something: /ask what changed in policy 1 since last month?");
        const task = await api("/api/tasks", { method: "POST", body: { goal: rest, kind: "ask" } });
        const line = ide.log(`<span class="spin"></span> asking… <span class="status muted">queued</span>`, "", true);
        const detail = await waitFor(task.id, line);
        line.remove();
        const answer = (detail.task.result || {}).answer || detail.task.error || "(no answer)";
        ide.log(`<div class="answer small">${renderAnswer(answer).replace(/data-cite=/g, `data-task="${esc(task.id)}" data-cite=`)}</div>`,
          detail.task.status === "completed" ? "answer" : "bad", true);
      },
      async open(rest) {
        const w = (rest || "").toLowerCase();
        if (!w) throw new Error("open what? /open policy 1");
        const doc = ide.docs.find((d) => d.title.toLowerCase().includes(w) || d.filename.toLowerCase().includes(w));
        if (doc) return ide.openDoc(doc.id, doc.title);
        const id = await findTask(rest);
        await ide.openReportOrTask(id);
      },
      async diff(rest) {
        const w = (rest || "").toLowerCase();
        const doc = ide.docs.find((d) => d.title.toLowerCase().includes(w));
        if (!doc) throw new Error(`no document of yours matches "${rest}"`);
        ide.openDoc(doc.id, doc.title, true);
      },
      async note(rest) { await ide.addNote(needTask(), "note", rest); },
      async fact(rest) { await ide.addNote(needTask(), "fact", rest); },
      async decision(rest) { await ide.addNote(needTask(), "decision", rest); },
      async question(rest) { await ide.addNote(needTask(), "question", rest); },
      async assumption(rest) { await ide.addNote(needTask(), "assumption", rest); },
      async steer(rest) {
        const match = (rest || "").match(/^(lead|agent[_ ]?\d+)\s+(.+)$/i);
        if (!match) throw new Error("/steer <lead|agent_1|agent_2 …> <what to tell it>");
        await ide.addNote(needTask(), "note", match[2], match[1].toLowerCase().replace(" ", "_").replace(/^agent(\d)/, "agent_$1"));
      },
      async pause(rest) { await ide.taskAction("pause", await findTask(rest)); },
      async resume(rest) { await ide.taskAction("resume", await findTask(rest)); },
      async cancel(rest) { await ide.taskAction("cancel", await findTask(rest)); },
      async run(rest) {
        const match = (rest || "").match(/^(\S+)\s*(.*)$/);
        if (!match) throw new Error('/run <tool> <json arguments>, e.g. /run docs.search {"query": "L-1 runout"}');
        let args = {};
        if (match[2]) {
          try { args = JSON.parse(match[2]); } catch (_) { args = { query: match[2] }; }
        }
        const id = needTask();
        const result = await api(`/api/tasks/${id}/tools/${encodeURIComponent(match[1])}`, { method: "POST", body: { arguments: args } });
        ide.log(`${esc(match[1])}: ${statusPill(result.status)} ${esc(result.summary || result.error || "")} ${(result.evidence || []).map((e) => `<span class="cite ${esc(e[0])}" data-task="${esc(id)}" data-cite="${esc(e)}">${esc(e)}</span>`).join("")}`,
          result.status === "ok" ? "ok" : "bad", true);
      },
      async search(rest) {
        if (!rest) throw new Error("/search <query>");
        const found = await api("/api/search", { method: "POST", body: { query: rest, top_k: 6 } });
        ide.log(`<div>${(found.hits || []).map((h) => `<div class="small"><b>${esc(h.title)}</b> p${esc(h.page)} — ${esc(String(h.text).slice(0, 160))}</div>`).join("") || "no matches you may see"}
          ${found.denied_count ? `<div class="small bad-text">${esc(found.denied_count)} matching document(s) withheld from you</div>` : ""}</div>`, "", true);
      },
      async remember(rest) {
        if (!rest) throw new Error("/remember <one self-contained statement>");
        const outcome = await api("/api/memory", { method: "POST", body: { content: rest } });
        ide.log(`memory manager: ${outcome.operation}${outcome.reason ? ` — ${outcome.reason}` : ""}`, "ok");
      },
      async status() {
        const tasks = ((await api("/api/tasks")).tasks || []).filter((t) => ACTIVE.has(t.status) || t.status === "awaiting_approval");
        ide.log(tasks.length ? tasks.map((t) => `<div><span class="link" data-open-task="${esc(t.id)}">${esc(t.title || t.goal)}</span> ${statusPill(t.status)}</div>`).join("")
          : "nothing in progress", "", true);
      },
      async memory() { ide.open("memory", ""); },
      async env() { ide.open("state", "", { refresh: true }); },
      async state() { ide.open("state", needTask(), { refresh: true }); },
    };
    $("#cli-form", root).addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = $("#cli-in", root);
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      ide.history.push(text);
      ide.historyAt = ide.history.length;
      ide.log(`<span class="cmd">› ${esc(text)}</span>`, "", true);
      const match = text.match(/^\/(\w+)\s*([\s\S]*)$/);
      const [name, rest] = match ? [match[1].toLowerCase(), match[2].trim()] : ["ask", text];
      const command = commands[name];
      if (!command) return ide.log(`unknown command /${name} — try /help`, "bad");
      try {
        await command(rest);
      } catch (error) {
        ide.log(error.message, "bad");
      }
    });
    $("#cli-in", root).addEventListener("keydown", (event) => {
      if (event.key === "ArrowUp" && ide.history.length) {
        ide.historyAt = Math.max(0, ide.historyAt - 1);
        event.target.value = ide.history[ide.historyAt] || "";
        event.preventDefault();
      } else if (event.key === "ArrowDown" && ide.history.length) {
        ide.historyAt = Math.min(ide.history.length, ide.historyAt + 1);
        event.target.value = ide.history[ide.historyAt] || "";
        event.preventDefault();
      }
    });
    $("#cli-clear", root).addEventListener("click", () => { log.innerHTML = ""; });
    $("#cli-toggle", root).addEventListener("click", () => $("#ide-cli", root).classList.toggle("collapsed"));
    // The command line's height: drag its top edge; remembered in this browser only.
    const cliBox = $("#ide-cli", root);
    const CLI_H = "citadel.ide.cli-height";
    try {
      const h = Number(localStorage.getItem(CLI_H));
      if (h >= 96) cliBox.style.setProperty("--cli-h", `${h}px`);
    } catch (_) { /* storage may be unavailable; the default height stands */ }
    $("#cli-grip", root).addEventListener("mousedown", (down) => {
      down.preventDefault();
      const startY = down.clientY;
      const startH = cliBox.getBoundingClientRect().height;
      const limit = $(".ide-center", root).getBoundingClientRect().height * 0.75;
      cliBox.classList.add("dragging");
      const move = (event) => {
        const h = Math.round(Math.max(96, Math.min(limit, startH + (startY - event.clientY))));
        cliBox.style.setProperty("--cli-h", `${h}px`);
      };
      const up = () => {
        cliBox.classList.remove("dragging");
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        try { localStorage.setItem(CLI_H, String(Math.round(cliBox.getBoundingClientRect().height))); } catch (_) { /* fine */ }
      };
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
    const keys = (event) => {
      if (event.key === "`" && event.ctrlKey) { event.preventDefault(); ide.focusCli(); }
    };
    document.addEventListener("keydown", keys);
    cleanups.push(() => document.removeEventListener("keydown", keys));
    // Leaving the page with an unsaved report edit asks first (the browser's own prompt).
    const unsaved = (event) => {
      if (ide.tabs.some((t) => t.dirty)) { event.preventDefault(); event.returnValue = ""; }
    };
    window.addEventListener("beforeunload", unsaved);
    cleanups.push(() => window.removeEventListener("beforeunload", unsaved));
    const guard = () => (ide.tabs.some((t) => t.dirty) ? "A report has unsaved changes: save them, or Discard them, before leaving the workbench." : null);
    C.app.leaveGuards.push(guard);
    cleanups.push(() => { C.app.leaveGuards = C.app.leaveGuards.filter((g) => g !== guard); });

    // ---------------------------------------------------------------- left: resources, reports, tools
    let resMode = "files";
    const openFolders = new Set(["COMPANY_POLICIES", "MANUFACTURING_DEPT", "PROCUREMENT_DEPT"]);
    const drawTree = () => {
      const holder = $("#ide-tree", root);
      const tree = {};
      for (const doc of ide.docs) {
        let level = tree;
        for (const part of (doc.folder || "UNFILED").split("/")) {
          level[part] = level[part] || { __docs: [] };
          level = level[part];
        }
        level.__docs.push(doc);
      }
      const walk = (level, path, depth) => Object.keys(level).filter((k) => k !== "__docs").sort().map((name) => {
        const full = path ? `${path}/${name}` : name;
        const open = openFolders.has(full);
        const inner = level[name];
        return `<div class="tree-row folder" data-folder="${esc(full)}" style="padding-left:${6 + depth * 12}px"><span class="caret">${open ? "▾" : "▸"}</span>📁 ${esc(name)}</div>
          ${open ? walk(inner, full, depth + 1) + inner.__docs.map((d) => `<div class="tree-row doc ${d.status !== "ready" ? "muted" : ""}" data-doc="${esc(d.id)}" title="${esc(d.title)}" style="padding-left:${22 + depth * 12}px">📄 ${esc(d.title.replace(/ - .*$/, ""))} <span class="ver">v${esc(d.version)}</span></div>`).join("") : ""}`;
      }).join("");
      holder.innerHTML = walk(tree, "", 0) || `<div class="muted small">No documents you may read yet.</div>`;
    };
    const drawDb = async () => {
      const holder = $("#ide-tree", root);
      const data = await api("/api/observability?sections=database");
      const tables = ((data.database || {}).tables || []);
      holder.innerHTML = `<div class="small muted" style="padding:4px 8px">Postgres + pgvector — the retrieval DB, the journal and the memory store.</div>
        ${tables.map((t) => `<div class="tree-row"><span class="mono small">▦ ${esc(t.table)}</span><span class="spacer"></span><span class="muted small">${esc(t.rows)}</span></div>`).join("")}
        <div class="tree-row link" data-obs-db>▸ DB monitoring</div>`;
      const more = $("[data-obs-db]", holder);
      if (more) more.addEventListener("click", () => ide.open("obs", "database"));
    };
    $$("[data-res]", root).forEach((b) => b.addEventListener("click", () => {
      resMode = b.dataset.res;
      $$("[data-res]", root).forEach((x) => x.classList.toggle("active", x === b));
      (resMode === "files" ? Promise.resolve(drawTree()) : drawDb()).catch(() => null);
    }));
    $("#ide-tree", root).addEventListener("click", (event) => {
      const folder = event.target.closest("[data-folder]");
      if (folder) {
        const name = folder.dataset.folder;
        openFolders.has(name) ? openFolders.delete(name) : openFolders.add(name);
        return drawTree();
      }
      const doc = event.target.closest("[data-doc]");
      if (doc) {
        const meta = ide.docs.find((d) => d.id === doc.dataset.doc);
        ide.openDoc(doc.dataset.doc, meta ? meta.title : "document");
      }
    });
    const loadDocs = async () => {
      ide.docs = (await api("/api/documents")).documents || [];
      if (resMode === "files") drawTree();
    };
    const loadReports = async () => {
      const tasks = ((await api("/api/tasks")).tasks || []).filter((t) => t.kind !== "ask").slice(0, 25);
      const holder = $("#ide-reports", root);
      holder.innerHTML = tasks.map((t) => `<div class="tree-row ${t.id === ide.task ? "selected" : ""}" data-report="${esc(t.id)}" title="${esc(t.goal)}">
        <span class="dot ${esc(t.status)}"></span><span class="clamp grow">${esc(t.title || t.goal)}</span></div>`).join("")
        || `<div class="muted small" style="padding:4px 8px">No reports yet. <span class="link" id="ide-first">Write one</span>.</div>`;
      $$("[data-report]", holder).forEach((row) => row.addEventListener("click", () => ide.openReportOrTask(row.dataset.report)));
      const first = $("#ide-first", holder);
      if (first) first.addEventListener("click", () => ide.open("welcome"));
    };
    $("#ide-new", root).addEventListener("click", () => ide.open("welcome"));
    const loadRunning = async () => {
      const data = await api("/api/workbench/tools");
      ide.toolsData = data;
      const holder = $("#ide-running", root);
      holder.innerHTML = (data.running || []).map((r) => `<div class="tool-run" data-task="${esc(r.task_id)}">
          <div><span class="spin tiny"></span> <b>${esc(r.label)}</b> <span class="muted small">— ${esc(r.who)}</span></div>
          <div class="small muted clamp">${esc(r.detail || r.tool)}</div></div>`).join("") || `<div class="muted small" style="padding:4px 8px">idle</div>`;
      $$(".tool-run", holder).forEach((row) => row.addEventListener("click", () => ide.openTask(row.dataset.task)));
    };
    $("#ide-tools", root).addEventListener("click", (event) => {
      event.stopPropagation();
      const data = ide.toolsData || { available: [], active: [], history: [] };
      const card = document.createElement("div");
      card.className = "popover tools-pop";
      card.innerHTML = `<div class="row"><b>TOOLS</b><span class="spacer"></span><button type="button" class="btn link small" data-x>✕</button></div>
        <h4>Available to you</h4><div class="tools-grid">${data.available.map((t) => `<div class="tool ${t.available ? "" : "off"}" title="${esc(t.available ? t.description : t.why_not)}">
          <b>${esc(t.name)}</b><span class="small muted">${esc(t.side_effect)}${t.offered_in ? ` · ${esc(t.offered_in.join("/"))}` : ""}</span></div>`).join("")}</div>
        <h4>Active now</h4><div>${data.active.map((t) => `<span class="pill VERIFIED">${esc(t)}</span>`).join(" ") || `<span class="muted small">none</span>`}</div>
        <h4>Task history ✓</h4><div class="history">${data.history.slice(0, 14).map((h) => `<div class="small">${statusPill(h.status)} <b>${esc(h.tool)}</b> <span class="muted">${esc(h.who)} · ${esc(ago(h.created_at))}</span><div class="muted clamp">${esc(h.summary)}</div></div>`).join("") || `<span class="muted small">none</span>`}</div>
        <p class="muted small">${esc(data.about || "")}</p>`;
      document.body.appendChild(card);
      const box = event.target.getBoundingClientRect();
      card.style.left = `${Math.max(8, Math.min(box.left, window.innerWidth - card.offsetWidth - 8))}px`;
      card.style.top = `${Math.max(56, Math.min(box.top - card.offsetHeight - 6, window.innerHeight - card.offsetHeight - 8))}px`;
      const close = () => card.remove();
      $("[data-x]", card).addEventListener("click", close);
      setTimeout(() => document.addEventListener("click", function away(e) {
        if (!card.contains(e.target)) { close(); document.removeEventListener("click", away); }
      }), 0);
    });

    // ---------------------------------------------------------------- right: panels, state, activity
    $("#ide-obs", root).addEventListener("click", (event) => {
      const row = event.target.closest("[data-obs]");
      if (row) ide.open("obs", row.dataset.obs);
    });
    $("#ide-env", root).addEventListener("click", (event) => {
      if (event.target.closest("[data-memory]")) return ide.open("memory", "");
      const row = event.target.closest("[data-state]");
      if (!row) return;
      if (ide.task) ide.open("state", ide.task, { focus: row.dataset.state, refresh: true });
      else ide.open("state", "", { refresh: true });
    });
    const activity = $("#ide-activity", root);
    activity.addEventListener("click", (event) => {
      const item = event.target.closest("[data-task]");
      if (item) ide.openTask(item.dataset.task);
    });
    const loadActivity = async () => {
      const data = await api(`/api/activity?after=${ide.cursor}&limit=60`);
      ide.cursor = data.cursor;
      if (!data.items.length) return;
      const empty = $("[data-empty]", activity);
      if (empty) empty.remove();
      const atBottom = activity.scrollHeight - activity.scrollTop - activity.clientHeight < 40;
      activity.insertAdjacentHTML("beforeend", data.items.map((i) => `
        <div class="act ${esc(i.level)}" data-task="${esc(i.task_id)}" title="${esc(i.task)}">
          <span class="muted small">${esc(new Date(i.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))}</span>
          <span>${renderAnswer(i.text)}</span></div>`).join(""));
      while (activity.children.length > 250) activity.firstElementChild.remove();
      if (atBottom) activity.scrollTop = activity.scrollHeight;
      if (data.items.some((i) => ["finished", "failed", "agent", "human", "revision_requested"].includes(i.kind))) loadReports().catch(() => null);
    };
    const loadAlerts = async () => {
      const data = await api("/api/observability?sections=alerts");
      const list = Array.isArray(data.alerts) ? data.alerts : [];
      const badge = $("#badge-alerts", root);
      const serious = list.filter((a) => a.level !== "info");
      badge.textContent = serious.length ? String(serious.length) : "";
      badge.className = `badge ${list.some((a) => a.level === "alert") ? "bad" : serious.length ? "warn" : ""}`;
    };

    // ---------------------------------------------------------------- start
    let saved = null;
    try { saved = JSON.parse(sessionStorage.getItem(STORE()) || "null"); } catch (_) { saved = null; }
    const restore = (saved && saved.tabs) || [];
    for (const t of restore) {
      if (!kinds[t.kind]) continue;
      const tab = ide.open(t.kind, t.ref, { agent: t.agent, name: t.name });
      tab.visible = false;
    }
    if (!ide.tabs.length) ide.open("welcome");
    const wanted = arg && decodeURIComponent(arg);
    if (wanted) {
      const [kind, ref, extra] = wanted.split(":");
      if (kinds[kind]) ide.open(kind, ref, kind === "agent" ? { agent: extra } : {});
    } else if (saved && saved.active && ide.tabs.some((t) => t.key === saved.active)) {
      ide.activate(saved.active);
    } else {
      ide.activate(ide.tabs[ide.tabs.length - 1].key);
    }
    ide.log(`Welcome, ${session.user.username}. Type <code>/report on …</code> to write a report, or <code>/help</code>.`, "", true);
    poll(20000, loadDocs);
    poll(7000, loadReports);
    poll(4000, loadRunning);
    poll(3000, loadActivity);
    poll(20000, loadAlerts);
    IDE.current = ide;
  };
})();
