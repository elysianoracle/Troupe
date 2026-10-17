// Troupe web UI. No build step, no framework.
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const S = { token: "", state: null, room: null, view: "chat", ws: null, agents: {} };

  // ---------- auth ----------
  function getToken() {
    const url = new URL(location.href);
    const t = url.searchParams.get("token");
    if (t) {
      try { localStorage.setItem("troupe_token", t); } catch {}
      url.searchParams.delete("token");
      history.replaceState(null, "", url.pathname);
      return t;
    }
    try { return localStorage.getItem("troupe_token") || ""; } catch { return ""; }
  }
  async function api(path, opts = {}) {
    const r = await fetch(path, {
      ...opts,
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${S.token}`, ...(opts.headers || {}) },
      body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
    });
    if (r.status === 401) { if (S.ws) { S.ws.onclose = null; S.ws.close(); } showLogin(); throw new Error("unauthorized"); }
    if (!r.ok) { const t = await r.text(); alert(t); throw new Error(t); }
    return r.json();
  }
  let tokenMode = false;
  function showLogin() {
    S.token = "";
    try { localStorage.removeItem("troupe_token"); } catch {}
    $("#login").classList.remove("hidden"); $("#app").classList.add("hidden");
    fetch("/api/auth-info").then((r) => r.json()).then((d) => {
      if (!d.password_set && !tokenMode) $("#login-hint").innerHTML = "No password is set yet. On the server run <code>sudo troupe-password</code>, or use the admin token.";
    }).catch(() => {});
    $("#token-input").focus();
  }
  $("#token-mode").onclick = () => {
    tokenMode = !tokenMode;
    $("#token-input").placeholder = tokenMode ? "Admin token" : "Password";
    $("#login-hint").innerHTML = tokenMode ? "Paste the admin token (run <code>troupe token</code> on the server)." : "Sign in with your Troupe password.";
    $("#token-mode").textContent = tokenMode ? "Use my password instead" : "Use the admin token instead";
    $("#login-error").textContent = "";
  };
  $("#login-form").onsubmit = async (e) => {
    e.preventDefault();
    const value = $("#token-input").value;
    $("#login-error").textContent = "";
    if (tokenMode) {
      S.token = value.trim();
    } else {
      const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: value }) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { $("#login-error").textContent = d.detail || "Sign-in failed"; return; }
      S.token = d.token;
    }
    $("#token-input").value = "";
    try { localStorage.setItem("troupe_token", S.token); } catch {}
    boot();
  };
  $("#logout-btn").onclick = async () => {
    try { await fetch("/api/logout", { method: "POST", headers: { Authorization: `Bearer ${S.token}` } }); } catch {}
    if (S.ws) { S.ws.onclose = null; S.ws.close(); }
    showLogin();
  };

  // ---------- markdown-lite ----------
  function md(text) {
    const blocks = String(text || "").split(/```/);
    return blocks.map((b, i) => {
      if (i % 2) return `<pre>${esc(b.replace(/^\w*\n/, ""))}</pre>`;
      let h = esc(b)
        .replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
        .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
      return h.split(/\n{2,}/).map((p) => p.trim() ? `<p>${p.replace(/\n/g, "<br>")}</p>` : "").join("");
    }).join("");
  }
  // Monogram seal for an agent or room: one letter, two when initials would collide.
  function initials(name) {
    const n = String(name || "?").trim();
    const parts = n.split(/[\s_-]+/).filter(Boolean);
    if (parts.length > 1) return (parts[0][0] + parts[1][0]).toUpperCase();
    // Two agents sharing a first letter: the one later in the alphabet gets two letters (Chief C, Courier Co).
    const names = Object.values(S.agents || {}).map((a) => a.name);
    const clash = names.some((o) => o !== n && o[0]?.toUpperCase() === n[0]?.toUpperCase() && o.localeCompare(n) < 0);
    return clash ? n[0].toUpperCase() + n.slice(1, 2).toLowerCase() : n[0].toUpperCase();
  }
  const mono = (name, extra = "") => `<span class="mono ${extra}" aria-hidden="true">${esc(initials(name))}</span>`;
  const icon = (id) => `<svg class="icon" aria-hidden="true"><use href="#i-${id}"/></svg>`;
  function setTitle(title, eyebrow = "") { $("#title").textContent = title; $("#eyebrow").textContent = eyebrow; }
  const ago = (ts) => {
    const s = Date.now() / 1000 - ts;
    if (s < 60) return "now"; if (s < 3600) return `${Math.floor(s / 60)}m`; if (s < 86400) return `${Math.floor(s / 3600)}h`;
    return new Date(ts * 1000).toLocaleDateString();
  };

  // ---------- state ----------
  async function refresh() {
    S.state = await api("/api/state");
    S.agents = Object.fromEntries(S.state.agents.map((a) => [a.id, a]));
    if (!S.room || !S.state.rooms.find((r) => r.id === S.room)) {
      const group = S.state.rooms.find((r) => r.kind === "group");
      S.room = (group || S.state.rooms[0] || {}).id || null;
    }
    renderSidebar(); renderApprovals();
    const pb = $("#pause-btn");
    pb.textContent = S.state.paused ? "Paused — resume" : "Pause all";
    pb.classList.toggle("on", S.state.paused);
  }
  function renderSidebar() {
    $("#room-list").innerHTML = S.state.rooms.map((r) => {
      const sub = r.kind === "group" ? r.members.map((m) => S.agents[m]?.name).filter(Boolean).join(", ")
                                     : (S.agents[r.members[0]]?.role || "");
      return `<li><button data-room="${r.id}" class="${r.id === S.room && S.view === "chat" ? "active" : ""}">
        ${mono(r.name, "sm" + (r.kind === "group" ? " group" : ""))}<span class="txt"><span class="name">${esc(r.name)}</span><span class="sub">${esc(sub)}</span></span></button></li>`;
    }).join("");
    document.querySelectorAll("[data-room]").forEach((b) => b.onclick = () => { S.room = b.dataset.room; show("chat"); closeMenu(); });
  }
  function renderApprovals() {
    const list = S.state.approvals || [];
    $("#approvals").innerHTML = list.map((a) => `
      <section class="approval" data-id="${a.id}" aria-label="Approval needed">
        <div class="kicker">${icon("lock")} Your approval</div>
        <div><strong>${esc(a.agent_name)}</strong> would like to run <code>${esc(a.tool)}</code>.</div>
        <div class="why">${esc(a.reason)}</div>
        <details><summary class="meta">Details</summary><pre>${esc(JSON.stringify(a.args, null, 2)).slice(0, 2000)}</pre></details>
        <div class="actions">
          <button class="btn primary" data-act="y">Approve</button>
          <button class="btn" data-act="n">Decline</button>
          <label class="sr-only" for="note-${a.id}">Note to the agent</label>
          <input id="note-${a.id}" placeholder="Optional note to ${esc(a.agent_name)}">
        </div>
      </section>`).join("");
    document.querySelectorAll(".approval").forEach((el) => el.querySelectorAll("[data-act]").forEach((b) => b.onclick = async () => {
      await api(`/api/approvals/${el.dataset.id}`, { method: "POST", body: { approve: b.dataset.act === "y", note: $("input", el).value } });
      refresh();
    }));
  }

  // ---------- views ----------
  function show(view) {
    S.view = view;
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
    document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
    $(`#view-${view}`).classList.remove("hidden");
    renderSidebar();
    ({ chat: loadChat, agents: renderAgents, skills: renderSkills, schedules: renderSchedules, audit: renderAudit, settings: renderSettings })[view]();
  }
  document.querySelectorAll("#tabs button").forEach((b) => b.onclick = () => { show(b.dataset.view); closeMenu(); });
  $("#menu-btn").onclick = () => $("#sidebar").classList.toggle("open");
  const closeMenu = () => $("#sidebar").classList.remove("open");

  // ---------- chat ----------
  async function loadChat() {
    const room = S.state.rooms.find((r) => r.id === S.room);
    if (!room) { setTitle("No rooms yet"); $("#messages").innerHTML = ""; return; }
    const members = room.members.map((m) => S.agents[m]?.name).filter(Boolean);
    setTitle(room.kind === "group" ? room.name : room.name,
             room.kind === "group" ? `Group room · ${members.join(", ")}` : `Private room · ${S.agents[room.members[0]]?.role || "agent"}`);
    const data = await api(`/api/rooms/${room.id}/messages`);
    $("#messages").innerHTML = data.messages.map(msgHtml).join("");
    setPlan(data.plan);
    setWorking(data.active_tasks);
    const teach = room.teach;
    $("#teach-banner").classList.toggle("hidden", !teach);
    if (teach) $("#teach-banner").textContent = `Teaching ${S.agents[teach.agent_id]?.name || "agent"} the skill “${teach.skill}”. Send /done when finished.`;
    scrollDown();
  }
  function msgHtml(m) {
    const kind = m.meta?.kind || "";
    const who = m.sender_type === "user" ? "" : m.sender_type === "agent" ? (m.agent_name || S.agents[m.sender_id]?.name || "agent") : "";
    const label = kind === "question" ? " · asks you" : kind === "handoff" ? " · hands over" : kind === "update" ? " · progress" : "";
    if (m.sender_type === "agent") {
      return `<article class="msg agent ${kind}">${mono(who)}<div class="body"><div class="who"><strong>${esc(who)}</strong>${label}</div>${md(m.content)}</div></article>`;
    }
    return `<div class="msg ${m.sender_type} ${kind}">${md(m.content)}</div>`;
  }
  function setPlan(plan) { $("#plan").classList.toggle("hidden", !plan); $("#plan-body").innerHTML = md(plan || ""); }
  function setWorking(tasks) {
    $("#working").textContent = (tasks || []).map((t) =>
      `${t.agent_name} ${t.status === "waiting_approval" ? "is waiting for your approval" : t.status === "queued" ? "is queued" : `is working (step ${t.steps})`}…`).join("  ·  ");
  }
  const scrollDown = () => { const m = $("#messages"); m.scrollTop = m.scrollHeight; };
  $("#composer").onsubmit = async (e) => {
    e.preventDefault();
    const text = $("#input").value.trim();
    if (!text || !S.room) return;
    $("#input").value = ""; autosize();
    await api(`/api/rooms/${S.room}/messages`, { method: "POST", body: { text } });
    if (text.startsWith("/")) { await refresh(); loadChat(); }
  };
  const autosize = () => { const t = $("#input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 180) + "px"; };
  $("#input").addEventListener("input", autosize);
  $("#input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); } });

  // voice: dictation in, speech out (browser built-ins, nothing runs on the VPS)
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) $("#mic").classList.add("hidden");
  let rec = null;
  $("#mic").onclick = () => {
    if (rec) { rec.stop(); return; }
    rec = new SR(); rec.interimResults = true; rec.lang = navigator.language || "en-US";
    const base = $("#input").value;
    rec.onresult = (ev) => { $("#input").value = base + Array.from(ev.results).map((r) => r[0].transcript).join(""); autosize(); };
    rec.onend = () => { rec = null; $("#mic").classList.remove("rec"); };
    $("#mic").classList.add("rec"); rec.start();
  };
  try { $("#speak").checked = localStorage.getItem("troupe_speak") === "1"; } catch {}
  $("#speak").onchange = () => { try { localStorage.setItem("troupe_speak", $("#speak").checked ? "1" : "0"); } catch {} };

  // ---------- live events ----------
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(S.token)}`);
    S.ws = ws;
    ws.onopen = () => { $("#status").textContent = ""; };
    ws.onclose = (ev) => {
      if (ev.code === 4401) { showLogin(); return; }
      $("#status").textContent = "reconnecting…"; setTimeout(connect, 3000);
    };
    ws.onmessage = (ev) => {
      const { type, data } = JSON.parse(ev.data);
      if (type === "message" && data.room_id === S.room && S.view === "chat") {
        $("#messages").insertAdjacentHTML("beforeend", msgHtml(data)); scrollDown();
        if ($("#speak").checked && data.sender_type === "agent" && window.speechSynthesis) {
          speechSynthesis.speak(new SpeechSynthesisUtterance(String(data.content).replace(/[*`#]/g, "").slice(0, 600)));
        }
      }
      if (type === "message" && data.sender_type === "agent" && data.room_id !== S.room && document.hidden && Notification?.permission === "granted") {
        new Notification(data.agent_name || "Troupe", { body: String(data.content).slice(0, 140) });
      }
      if (type === "plan" && data.room_id === S.room) setPlan(data.plan);
      if (type === "task" && data.room_id === S.room && S.view === "chat") debounce("work", refreshWorking, 300);
      if (type === "tool" && data.room_id === S.room) debounce("work", refreshWorking, 800);
      if (type === "approval" || type === "approval_decided" || type === "paused") debounce("state", refresh, 200);
      if (type === "message") debounce("side", refresh, 1500);
    };
  }
  const timers = {};
  const debounce = (k, fn, ms) => { clearTimeout(timers[k]); timers[k] = setTimeout(fn, ms); };
  async function refreshWorking() { if (!S.room) return; const d = await api(`/api/rooms/${S.room}/messages?limit=1`); setWorking(d.active_tasks); }

  // ---------- dialogs ----------
  function dialog(title, fields, onSubmit) {
    const f = $("#dialog-form");
    f.innerHTML = `<h3>${esc(title)}</h3>` + fields.map((x) => `<label>${esc(x.label)}${
      x.type === "textarea" ? `<textarea name="${x.name}" rows="${x.rows || 4}">${esc(x.value || "")}</textarea>`
      : x.type === "select" ? `<select name="${x.name}" ${x.multiple ? "multiple size=5" : ""}>${x.options.map((o) => `<option value="${esc(o.value)}" ${o.selected ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select>`
      : `<input name="${x.name}" type="${x.type || "text"}" value="${esc(x.value ?? "")}" ${x.step ? `step="${x.step}"` : ""} placeholder="${esc(x.placeholder || "")}">`
    }</label>`).join("") + `<div class="row"><button class="btn primary" value="ok">Save</button><button class="btn" value="cancel" formnovalidate>Cancel</button></div>`;
    const d = $("#dialog");
    d.onclose = async () => {
      if (d.returnValue !== "ok") return;
      const fd = new FormData(f); const out = {};
      fields.forEach((x) => { out[x.name] = x.multiple ? fd.getAll(x.name) : fd.get(x.name); });
      try { await onSubmit(out); } catch {}
    };
    d.returnValue = ""; d.showModal();
  }
  $("#new-room").onclick = () => dialog("New group room", [
    { name: "name", label: "Name", placeholder: "Trip to Lisbon" },
    { name: "agents", label: "Agents (ctrl/cmd-click for several)", type: "select", multiple: true, options: S.state.agents.map((a) => ({ value: a.id, label: a.name })) },
    { name: "lead", label: "Lead agent (gets messages without an @mention)", type: "select", options: S.state.agents.map((a) => ({ value: a.id, label: a.name })) },
  ], async (v) => {
    const r = await api("/api/rooms", { method: "POST", body: { name: v.name || "Group", agent_ids: v.agents.length ? v.agents : [v.lead], lead_agent_id: v.lead } });
    S.room = r.id; await refresh(); show("chat");
  });
  $("#pause-btn").onclick = async () => { await api("/api/pause", { method: "POST", body: { paused: !S.state.paused } }); refresh(); };

  // ---------- agents ----------
  const POLICY = ["auto", "ask", "deny"];
  function renderAgents() {
    setTitle("Agents", "Your troupe");
    const v = $("#view-agents");
    v.innerHTML = `<div class="row"><button class="btn primary" id="add-agent">New agent</button></div>` + S.state.agents.map((a) => `
      <div class="card">
        <h3>${mono(a.name)} ${esc(a.name)} <span class="badge ${a.status === "active" ? "ok" : "warn"}">${a.status}</span>
            <span class="badge">${esc(a.model || S.state.default_model)}</span></h3>
        <div>${esc(a.role)}</div>
        <div class="meta">Email: ${esc(a.email || "not configured")} · AI spend this week: $${a.llm_spend_week.toFixed(2)} / $${a.llm_budget_usd_week}
          ${a.spend_budget_usd_week > 0 ? ` · purchases: $${a.money_spend_week} / $${a.spend_budget_usd_week}` : ""}</div>
        <div class="meta">Permissions: ${Object.entries(a.permissions).filter(([k]) => k !== "connectors").map(([k, val]) => `${k}=${val}`).join(", ")}
          ${Object.keys(a.permissions.connectors || {}).length ? ` · connectors: ${esc(JSON.stringify(a.permissions.connectors))}` : ""}</div>
        <div class="row">
          <button class="btn" data-edit="${a.id}">Edit</button>
          <button class="btn" data-toggle="${a.id}">${a.status === "active" ? "Pause" : "Resume"}</button>
          <button class="btn" data-mem="${a.id}">Memory</button>
          <button class="btn" data-hook="${a.id}">Webhook URL</button>
          <button class="btn danger" data-del="${a.id}">Delete</button>
        </div>
        <div id="mem-${a.id}"></div>
      </div>`).join("");
    $("#add-agent").onclick = () => editAgent(null);
    v.querySelectorAll("[data-edit]").forEach((b) => b.onclick = () => editAgent(S.agents[b.dataset.edit]));
    v.querySelectorAll("[data-toggle]").forEach((b) => b.onclick = async () => {
      const a = S.agents[b.dataset.toggle];
      await api(`/api/agents/${a.id}`, { method: "PATCH", body: { status: a.status === "active" ? "paused" : "active" } });
      await refresh(); renderAgents();
    });
    v.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
      if (!confirm(`Delete ${S.agents[b.dataset.del].name}? Its memory, skills and schedules are removed.`)) return;
      await api(`/api/agents/${b.dataset.del}`, { method: "DELETE" }); await refresh(); renderAgents();
    });
    v.querySelectorAll("[data-hook]").forEach((b) => b.onclick = async () => {
      const r = await api(`/api/agents/${b.dataset.hook}/hook`); prompt("POST anything to this URL to give the agent a task:", r.url);
    });
    v.querySelectorAll("[data-mem]").forEach((b) => b.onclick = () => renderMemory(b.dataset.mem));
  }
  async function renderMemory(id) {
    const rows = await api(`/api/agents/${id}/memories`);
    const box = $(`#mem-${id}`);
    box.innerHTML = `<div class="row"><input id="newmem-${id}" placeholder="Add a memory (e.g. I prefer aisle seats)"><button class="btn" id="addmem-${id}">Add (pinned)</button></div>` +
      (rows.map((m) => `<div class="row"><span class="badge">${m.pinned ? icon("pin") + " pinned" : "#" + m.id}</span><span style="flex:1">${esc(m.content)}</span><button class="link" data-forget="${m.id}">forget</button></div>`).join("") || `<div class="meta">No memories yet.</div>`);
    $(`#addmem-${id}`).onclick = async () => { const c = $(`#newmem-${id}`).value.trim(); if (c) { await api(`/api/agents/${id}/memories`, { method: "POST", body: { content: c, pinned: true } }); renderMemory(id); } };
    box.querySelectorAll("[data-forget]").forEach((b) => b.onclick = async () => { await api(`/api/memories/${b.dataset.forget}`, { method: "DELETE" }); renderMemory(id); });
  }
  function editAgent(a) {
    const p = a?.permissions || { network: true, shell: "auto", files: "auto", browser: "auto", type_secret: "ask", email_send: "contacts_auto", sms_send: "ask", purchase: "ask", handoff: "auto", schedule: "auto", connectors: {} };
    const sel = (name, opts) => ({ name, label: name.replace("_", " "), type: "select", options: opts.map((o) => ({ value: o, label: o, selected: String(p[name]) === o })) });
    dialog(a ? `Edit ${a.name}` : "New agent", [
      ...(a ? [] : [{ name: "name", label: "Name (one word)", placeholder: "Scout" }]),
      { name: "role", label: "Role", value: a?.role, placeholder: "Researcher: finds and compares options" },
      { name: "instructions", label: "Standing instructions", type: "textarea", value: a?.instructions },
      { name: "model", label: `Model (blank = ${S.state.default_model})`, value: a?.model },
      { name: "llm_budget_usd_week", label: "Weekly AI budget (USD, 0 = unlimited)", type: "number", step: "0.5", value: a?.llm_budget_usd_week ?? 10 },
      { name: "spend_budget_usd_week", label: "Weekly purchase budget (USD, 0 = can't buy)", type: "number", step: "1", value: a?.spend_budget_usd_week ?? 0 },
      { name: "contacts", label: "Contacts it may email without asking (comma-separated)", value: (a?.contacts || []).join(", ") },
      sel("network", ["true", "false"]), sel("shell", POLICY), sel("browser", POLICY), sel("email_send", ["contacts_auto", ...POLICY]),
      sel("sms_send", POLICY), sel("purchase", ["ask", "deny"]), sel("type_secret", ["ask", "deny"]), sel("handoff", POLICY),
      { name: "connectors", label: 'Connectors JSON, e.g. {"github": "reads_auto"}', value: JSON.stringify(p.connectors || {}) },
    ], async (v) => {
      const perms = { network: v.network === "true", shell: v.shell, browser: v.browser, email_send: v.email_send, sms_send: v.sms_send, purchase: v.purchase, type_secret: v.type_secret, handoff: v.handoff };
      try { perms.connectors = JSON.parse(v.connectors || "{}"); } catch { alert("Connectors must be JSON"); return; }
      const body = { role: v.role, instructions: v.instructions, model: v.model, permissions: perms,
        llm_budget_usd_week: parseFloat(v.llm_budget_usd_week) || 0, spend_budget_usd_week: parseFloat(v.spend_budget_usd_week) || 0,
        contacts: v.contacts.split(",").map((s) => s.trim()).filter(Boolean) };
      if (a) await api(`/api/agents/${a.id}`, { method: "PATCH", body });
      else await api("/api/agents", { method: "POST", body: { name: v.name, ...body } });
      await refresh(); renderAgents();
    });
  }

  // ---------- skills ----------
  async function renderSkills() {
    setTitle("Skills", "What your agents know how to do");
    const rows = await api("/api/skills");
    $("#view-skills").innerHTML = `<div class="card meta">Teach a skill by demonstration: in any room send <code>/teach skill-name @Agent</code>,
      walk the agent through the task once, then send <code>/done</code>. Run it later by saying “use skill skill-name”, or schedule it.</div>` +
      (rows.map((s) => `<div class="card"><h3>${esc(s.name)} <span class="badge">${esc(s.agent_name)}</span></h3>
        <div>${esc(s.description)}</div><div class="meta">Params: ${esc(s.params.join(", ") || "none")}</div>
        <details><summary>Playbook</summary><textarea rows="10" data-pb="${s.id}">${esc(s.playbook)}</textarea>
        <div class="row"><button class="btn" data-save="${s.id}">Save edits</button></div></details>
        <div class="row"><button class="btn" data-sched="${s.id}">Schedule</button><button class="btn danger" data-del="${s.id}">Delete</button></div></div>`).join("") || `<div class="meta">No skills yet.</div>`);
    const v = $("#view-skills");
    v.querySelectorAll("[data-save]").forEach((b) => b.onclick = async () => { await api(`/api/skills/${b.dataset.save}`, { method: "PATCH", body: { playbook: $(`[data-pb="${b.dataset.save}"]`).value } }); });
    v.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => { if (confirm("Delete this skill?")) { await api(`/api/skills/${b.dataset.del}`, { method: "DELETE" }); renderSkills(); } });
    v.querySelectorAll("[data-sched]").forEach((b) => b.onclick = () => {
      const s = rows.find((r) => r.id === b.dataset.sched);
      newSchedule(s.agent_id, `Use skill ${s.name}` + (s.params.length ? ` with ${s.params.map((p) => `${p}=...`).join(", ")}` : ""));
    });
  }

  // ---------- schedules ----------
  async function renderSchedules() {
    setTitle("Schedules", "Work that starts on its own");
    const rows = await api("/api/schedules");
    $("#view-schedules").innerHTML = `<div class="row"><button class="btn primary" id="add-sched">New schedule</button><span class="meta">Cron, in UTC. “0 8 * * 1-5” = weekdays 08:00.</span></div>` +
      (rows.map((s) => `<div class="card"><h3>${esc(s.agent_name)} <span class="badge">${esc(s.cron)}</span><span class="badge ${s.enabled ? "ok" : "warn"}">${s.enabled ? "on" : "off"}</span></h3>
        <div>${esc(s.prompt)}</div><div class="meta">Next: ${new Date(s.next_run * 1000).toLocaleString()}${s.last_run ? ` · last ${ago(s.last_run)} ago` : ""}</div>
        <div class="row"><button class="btn" data-toggle="${s.id}" data-on="${s.enabled}">${s.enabled ? "Disable" : "Enable"}</button><button class="btn danger" data-del="${s.id}">Delete</button></div></div>`).join("") || `<div class="meta">No schedules yet. Agents can also create their own.</div>`);
    $("#add-sched").onclick = () => newSchedule();
    const v = $("#view-schedules");
    v.querySelectorAll("[data-toggle]").forEach((b) => b.onclick = async () => { await api(`/api/schedules/${b.dataset.toggle}`, { method: "PATCH", body: { enabled: b.dataset.on !== "1" } }); renderSchedules(); });
    v.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => { await api(`/api/schedules/${b.dataset.del}`, { method: "DELETE" }); renderSchedules(); });
  }
  function newSchedule(agentId, prompt) {
    dialog("New schedule", [
      { name: "agent_id", label: "Agent", type: "select", options: S.state.agents.map((a) => ({ value: a.id, label: a.name, selected: a.id === agentId })) },
      { name: "cron", label: "Cron (UTC)", value: "0 8 * * 1-5" },
      { name: "prompt", label: "What to do", type: "textarea", value: prompt || "" },
    ], async (v) => { await api("/api/schedules", { method: "POST", body: v }); show("schedules"); });
  }

  // ---------- audit ----------
  async function renderAudit() {
    setTitle("Activity", "Every action, in order");
    const rows = await api("/api/audit?limit=300");
    const total = rows.filter((r) => r.kind === "llm").reduce((s, r) => s + r.cost_usd, 0);
    $("#view-audit").innerHTML = `<div class="meta">Every model call, tool call, approval and trigger is logged. Last 300 entries · AI cost shown: $${total.toFixed(3)}</div>
      <div class="table-wrap"><table><tr><th>When</th><th>Agent</th><th>Kind</th><th>What</th><th>Detail</th><th>$</th></tr>` +
      rows.map((r) => `<tr><td>${ago(r.ts)}</td><td>${esc(r.agent_name || "")}</td><td>${esc(r.kind)}</td><td>${esc(r.name)}</td><td class="detail">${esc(r.detail.slice(0, 400))}</td><td>${r.cost_usd ? r.cost_usd.toFixed(4) : ""}</td></tr>`).join("") + `</table></div>`;
  }

  // ---------- vault & system ----------
  async function renderSettings() {
    setTitle("Vault & system", "Keys, purchases and the machine");
    const [secrets, sys, buys] = await Promise.all([api("/api/secrets"), api("/api/system"), api("/api/purchases")]);
    const mem = sys.memory_mb || {};
    $("#view-settings").innerHTML = `
      <div class="card"><h3>System</h3>
        <div class="meta">Provider: ${esc(S.state.provider)} · default model ${esc(S.state.default_model)} · email ${S.state.email_provider === "agentmail" ? "AgentMail" : S.state.email_enabled ? "IMAP/SMTP" : "off"} · SMS ${S.state.sms_enabled ? "on" : "off"}</div>
        <div class="meta">RAM available: ${mem.MemAvailable ?? "?"} / ${mem.MemTotal ?? "?"} MB · running tasks ${sys.running_tasks}/${sys.max_parallel} · sandboxes ${sys.sandboxes.length}/${sys.max_sandboxes}</div>
        ${sys.sandboxes.map((s) => `<div class="meta">${icon("box")} ${esc(S.agents[s.agent_id]?.name || s.agent_id)}: ${esc(s.memory)} · CPU ${esc(s.cpu)}</div>`).join("")}
        <div class="meta">Connectors: ${esc(Object.keys(S.state.connectors).join(", ") || "none (add data/connectors.json)")}</div>
        <div class="row"><button class="btn" id="notif">Enable desktop notifications</button></div>
      </div>
      <div class="card"><h3>Purchases <span class="badge">manual: you pay</span></h3>
        <div class="meta">Agents with a weekly purchase budget can ask to buy things. Every purchase needs your approval and must fit that budget; then the agent gives you the exact link and you pay yourself.</div>
        ${buys.length ? `<div class="table-wrap"><table><tr><th>#</th><th>Agent</th><th>Merchant</th><th>Amount</th><th>Status</th></tr>${buys.map((b) => `<tr><td>${b.id}</td><td>${esc(b.agent_name)}</td><td>${b.url ? `<a href="${esc(b.url)}" target="_blank" rel="noopener noreferrer">${esc(b.merchant)}</a>` : esc(b.merchant)}<div class="meta">${esc(b.description)}</div></td><td>$${b.amount_usd.toFixed(2)}</td><td>${esc(b.status)}</td></tr>`).join("")}</table></div>` : `<div class="meta">No purchases yet. Give an agent a weekly purchase budget in Agents → Edit.</div>`}
      </div>
      <div class="card"><h3>Vault</h3>
        <div class="meta">Secrets are encrypted at rest and never shown to the model. Save your AgentMail key as <code>agentmail_api_key</code> to give every agent its own inbox. Reference them as <code>vault:NAME</code> in connectors.json, or let agents type them into login forms on the allowed domains (with your approval).</div>
        ${secrets.map((s) => `<div class="row"><code>${esc(s.name)}</code><span class="meta">${esc((s.domains || []).join(", ") || "no domains")}</span><button class="link" data-delsec="${esc(s.name)}">delete</button></div>`).join("")}
        <div class="grid2"><input id="sec-name" placeholder="NAME"><input id="sec-val" type="password" placeholder="value"></div>
        <input id="sec-dom" placeholder="Allowed domains for type_secret (comma-separated, optional)">
        <div class="row"><button class="btn primary" id="sec-add">Save secret</button></div>
      </div>`;
    $("#sec-add").onclick = async () => {
      await api("/api/secrets", { method: "POST", body: { name: $("#sec-name").value.trim(), value: $("#sec-val").value, domains: $("#sec-dom").value.split(",").map((s) => s.trim()).filter(Boolean) } });
      renderSettings();
    };
    document.querySelectorAll("[data-delsec]").forEach((b) => b.onclick = async () => { await api(`/api/secrets/${encodeURIComponent(b.dataset.delsec)}`, { method: "DELETE" }); renderSettings(); });
    $("#notif").onclick = () => window.Notification && Notification.requestPermission();
  }

  // ---------- boot ----------
  async function boot() {
    S.token = S.token || getToken();
    if (!S.token) return showLogin();
    try { await refresh(); } catch { return; }
    $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
    show("chat"); connect();
  }
  boot();
})();
