const api = {
  get: async (path) => {
    const r = await fetch(path);
    if (!r.ok) throw new Error(`GET ${path} failed`);
    return r.json();
  },
  post: async (path, body) => {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.message || data.error || `POST ${path} failed`);
    return data;
  },
};

let state = { loaded: false };
let agentCatalog = [];
let liveSource = null;
let lastTranscriptSig = "";
let autoLoopTimer = null;
let autoLoopBusy = false;

function $(id) {
  return document.getElementById(id);
}

function pretty(obj) {
  return JSON.stringify(obj || {}, null, 2);
}

function renderTranscript(data) {
  const mode = $("verbosity").value;
  const search = ($("searchBox").value || "").toLowerCase();
  const sig = JSON.stringify({
    transcript: data.transcript || [],
    pending_turn: data.pending_turn || 0,
    pending_caller_text: data.pending_caller_text || "",
    pending_caller_metadata: data.pending_caller_metadata || null,
    mode,
    search,
  });
  if (sig === lastTranscriptSig) return;
  lastTranscriptSig = sig;

  const list = $("transcriptList");
  list.innerHTML = "";
  const rows = (data.transcript || []).filter((row) => {
    if (!search) return true;
    return (
      String(row.call_taker || "").toLowerCase().includes(search) ||
      String(row.caller || "").toLowerCase().includes(search)
    );
  });

  rows.forEach((row) => {
    const ct = document.createElement("div");
    ct.className = "bubble ct";
    ct.id = `turn-${row.turn}`;
    ct.innerHTML = `<div class="meta">Turn ${row.turn} · Call-Taker</div><div>${escapeHtml(row.call_taker || "")}</div>`;
    list.appendChild(ct);

    const callerText = String(row.caller || "");
    const hasCaller = callerText.trim().length > 0 || Boolean(row.caller_metadata);
    if (hasCaller) {
      const caller = document.createElement("div");
      caller.className = "bubble caller";
      caller.innerHTML = `<div class="meta">Turn ${row.turn} · Caller</div><div>${escapeHtml(callerText)}</div>`;
      if (mode === "detailed" && row.caller_metadata) {
        caller.innerHTML += `<div class="meta">metadata: ${escapeHtml(JSON.stringify(row.caller_metadata))}</div>`;
      }
      list.appendChild(caller);
    }
  });

  const pendingCaller = String(data.pending_caller_text || "");
  if (pendingCaller) {
    const pendingTurn = Number(data.pending_turn || 0);
    const displayTurn = Math.max(1, pendingTurn - 1);
    const pending = document.createElement("div");
    pending.className = "bubble caller";
    pending.innerHTML = `<div class="meta">Turn ${displayTurn} · Caller (queued)</div><div>${escapeHtml(pendingCaller)}</div>`;
    list.appendChild(pending);
  }
}

function renderInbox(targetId, reqs, isEscalation = false) {
  const root = $(targetId);
  root.innerHTML = "";
  if (!reqs || reqs.length === 0) {
    root.innerHTML = "<div class='meta'>No pending requests.</div>";
    return;
  }
  reqs.forEach((req) => {
    const el = document.createElement("div");
    el.className = "req";
    const esc = isEscalation || String(req.source || "").startsWith("escalation");
    el.innerHTML = `
      <div><strong>${req.request_id}</strong> · ${escapeHtml(req.action_class || "unknown")}</div>
      <div class="meta">source=${escapeHtml(req.source || "checkpoint")} approver=${escapeHtml(req.approver_role || "")}</div>
      <pre>${escapeHtml(JSON.stringify(req.proposed_payload || {}, null, 2))}</pre>
      <div class="actions">
        <button data-id="${req.request_id}" data-decision="approved">Approve</button>
        <button class="deny" data-id="${req.request_id}" data-decision="denied">Deny</button>
        <button class="edit" data-id="${req.request_id}" data-decision="edited_approved">Edit+Approve</button>
        ${esc ? '<button class="edit" data-id="' + req.request_id + '" data-decision="re_escalated">Re-escalate</button>' : ""}
      </div>
    `;
    root.appendChild(el);
  });

  root.querySelectorAll("button[data-id]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const requestId = btn.dataset.id;
      const decision = btn.dataset.decision;
      let payload = { request_id: requestId, decision };
      if (decision === "edited_approved") {
        const editedRaw = prompt("Edited payload JSON:", "{\"edited\":true}");
        try {
          payload.edited_payload = JSON.parse(editedRaw || "{}");
        } catch {
          alert("Invalid JSON");
          return;
        }
      }
      if (decision === "re_escalated") {
        payload.re_escalate_to = prompt("Re-escalate target role:", "commander") || "commander";
      }
      try {
        await api.post("/api/checkpoint/submit", payload);
        await refresh();
      } catch (err) {
        alert(err.message);
      }
    });
  });
}

function renderSop(snippets) {
  const root = $("sopView");
  root.innerHTML = "";
  snippets.forEach((s) => {
    const div = document.createElement("div");
    div.className = "snippet";
    div.innerHTML = `<strong>${escapeHtml(s.title || "SOP")}</strong><div class="meta">${escapeHtml(s.step || "")}</div><div>${escapeHtml(s.text || "")}</div>`;
    root.appendChild(div);
  });
}

function renderSystemMessages(data) {
  const root = $("systemMessagesView");
  if (!root) return;
  const calltakerId = data.agent_profiles?.calltaker || $("calltakerAgentId").value;
  const calltakerProfile = profileFor("calltaker", calltakerId);
  const isManualCallTaker = calltakerProfile && calltakerProfile.mode === "manual";
  const events = (data.system_events || []).filter((ev) => ev && ev.event_type === "system");

  if (!isManualCallTaker) {
    root.innerHTML = "<div class='meta'>Select manual Call-Taker to view live system notifications here.</div>";
    return;
  }
  if (events.length === 0) {
    root.innerHTML = "<div class='meta'>No system messages yet.</div>";
    return;
  }

  root.innerHTML = "";
  events.slice(-12).forEach((ev) => {
    const subtype = String(ev.subtype || "generic");
    const row = document.createElement("div");
    let cls = "system-message";
    if (subtype === "responders_arrived") cls += " arrived";
    if (subtype === "responders_dispatched") cls += " dispatched";
    row.className = cls;
    row.innerHTML = `
      <div class="meta">Turn ${Number(ev.turn || 0)} · ${escapeHtml(subtype)}</div>
      <div>${escapeHtml(String(ev.text || ""))}</div>
    `;
    root.appendChild(row);
  });
}

function render(data) {
  state = data;
  $("metricsView").textContent = pretty({
    loaded: data.loaded,
    scenario_id: data.scenario_id,
    incident_id: data.incident_id,
    phase: data.phase,
    turn_count: data.metrics?.turn_count || 0,
    queue_depth: data.metrics?.checkpoint_queue_depth || 0,
    escalation_depth: data.metrics?.escalation_queue_depth || 0,
    avg_checkpoint_latency_ms: data.metrics?.avg_checkpoint_latency_ms || 0,
    event_count: data.metrics?.event_count || 0,
    agent_profiles: data.agent_profiles || {},
    pending_turn: data.pending_turn || 0,
    has_pending_caller: Boolean(data.pending_caller_text),
    auto_qa_on_seal: Boolean(data.runtime_options?.auto_qa_on_seal),
  });
  $("locationView").textContent = pretty(data.location_panel || {});
  $("cadView").textContent = pretty({
    cad_state: data.cad_state || {},
    record_version: data.record_version,
    field_versions: data.field_versions || {},
  });
  renderTranscript(data);
  renderSystemMessages(data);
  renderInbox("checkpointInbox", data.checkpoint_inbox || [], false);
  renderInbox("escalationInbox", data.escalation_inbox || [], true);
  if (data.last_qa_score) {
    $("setupStatus").textContent = `QA score: ${Number(data.last_qa_score.normalized_score || 0).toFixed(2)}`;
  }
}

async function refresh() {
  try {
    const data = await api.get("/api/state");
    render(data);
  } catch (err) {
    $("setupStatus").textContent = err.message;
  }
}

function closeLiveStream() {
  if (liveSource) {
    liveSource.close();
    liveSource = null;
  }
}

function openLiveStream() {
  closeLiveStream();
  const incident = state.incident_id ? `?incident_id=${encodeURIComponent(state.incident_id)}` : "";
  liveSource = new EventSource(`/api/events/stream${incident}`);
  liveSource.addEventListener("state", (evt) => {
    try {
      const payload = JSON.parse(evt.data || "{}");
      if (payload && payload.state) render(payload.state);
    } catch {
      // ignore malformed frame
    }
  });
}

async function setupEpisode() {
  try {
    stopAutoLoop();
    const callerAgentId = $("callerAgentId").value;
    const calltakerAgentId = $("calltakerAgentId").value;
    const qaAgentId = $("qaAgentId").value;
    const body = {
      scenario_id: $("scenarioId").value,
      caller_fixture: $("callerFixture").value,
      incident_fixture: $("incidentFixture").value,
      qa_fixture: $("qaFixture").value,
      caller_agent_id: callerAgentId,
      calltaker_agent_id: calltakerAgentId,
      qa_agent_id: qaAgentId,
      max_turns: Number($("maxTurns").value || 20),
      auto_qa_on_seal: Boolean($("autoQaOnSeal").checked),
    };
    const out = await api.post("/api/admin/load_start", body);
    if (out.scenario_id) $("scenarioId").value = out.scenario_id;
    const generatedNote = out.scenario_id_generated ? " (auto-generated scenario_id)" : "";
    $("setupStatus").textContent = `Loaded ${out.loaded.incident_id} under ${out.scenario_id}${generatedNote} and started episode`;
    await refresh();
    openLiveStream();
    if (callerAgentId === "replay" || calltakerAgentId === "replay" || qaAgentId === "replay") {
      const replayOut = await api.post("/api/agent/auto_step", { turns: 1 });
      $("setupStatus").textContent = `Loaded ${out.loaded.incident_id}; replay advanced ${replayOut.executed_turns} turn`;
      await refresh();
      return;
    }
    if (shouldAutoLoop(callerAgentId, calltakerAgentId)) {
      $("setupStatus").textContent = `Loaded ${out.loaded.incident_id}. Auto-run started.`;
      startAutoLoop();
    }
  } catch (err) {
    $("setupStatus").textContent = err.message;
  }
}

function profileFor(role, id) {
  return agentCatalog.find((p) => p.role === role && p.id === id) || null;
}

function shouldAutoLoop(callerAgentId, calltakerAgentId) {
  const caller = profileFor("caller", callerAgentId);
  const calltaker = profileFor("calltaker", calltakerAgentId);
  if (!caller || !calltaker) return false;
  return caller.mode === "callable" && calltaker.mode === "callable";
}

function stopAutoLoop() {
  if (autoLoopTimer) {
    clearTimeout(autoLoopTimer);
    autoLoopTimer = null;
  }
}

async function autoLoopTick() {
  if (autoLoopBusy || !state.loaded) return;
  autoLoopBusy = true;
  try {
    const out = await api.post("/api/agent/auto_step", { turns: 1 });
    await refresh();
    if (String(out.phase || "") === "sealed") {
      if (state.last_qa_score && state.last_qa_score.normalized_score != null) {
        $("setupStatus").textContent = `Episode sealed. QA score: ${Number(state.last_qa_score.normalized_score).toFixed(2)}`;
      } else {
        $("setupStatus").textContent = "Episode sealed.";
      }
      stopAutoLoop();
      return;
    }
    if (Number(out.executed_turns || 0) <= 0) {
      $("setupStatus").textContent = "Auto-run paused (no executable turn).";
      stopAutoLoop();
      return;
    }
    autoLoopTimer = setTimeout(autoLoopTick, 250);
  } catch (err) {
    $("setupStatus").textContent = `Auto-run error: ${err.message}`;
    stopAutoLoop();
  } finally {
    autoLoopBusy = false;
  }
}

function startAutoLoop() {
  stopAutoLoop();
  autoLoopTimer = setTimeout(autoLoopTick, 150);
}

function renderAgentSelect(selectId, role, selectedId) {
  const select = $(selectId);
  if (!select) return;
  const options = agentCatalog.filter((p) => p.role === role);
  select.innerHTML = "";
  options.forEach((p) => {
    const opt = document.createElement("option");
    opt.value = p.id;
    const desc = p.description ? ` - ${p.description}` : "";
    opt.textContent = `${p.id} (${p.provider})${desc}`;
    if (p.id === selectedId) opt.selected = true;
    select.appendChild(opt);
  });
}

async function loadAgentCatalog() {
  try {
    const out = await api.get("/api/agent/catalog");
    agentCatalog = out.profiles || [];
    renderAgentSelect("callerAgentId", "caller", "deterministic_v1");
    renderAgentSelect("calltakerAgentId", "calltaker", "deterministic_v1");
    renderAgentSelect("qaAgentId", "qa", "deterministic_v1");
  } catch (err) {
    $("setupStatus").textContent = `Catalog load failed: ${err.message}`;
  }
}

async function postCaller() {
  const text = $("callerText").value.trim();
  if (!text) return;
  try {
    await api.post("/api/caller_turn", { text, metadata: { ui: true } });
    $("callerText").value = "";
    await refresh();
  } catch (err) {
    alert(err.message);
  }
}

async function postCallTaker() {
  const text = $("ctText").value.trim();
  if (!text) return;
  let cad = {};
  try {
    cad = JSON.parse($("ctCadJson").value || "{}");
  } catch {
    alert("Invalid CAD updates JSON");
    return;
  }
  try {
    await api.post("/api/calltaker_turn", { text, cad_updates: cad });
    $("ctText").value = "";
    await refresh();
  } catch (err) {
    alert(err.message);
  }
}

async function endCall() {
  try {
    stopAutoLoop();
    await api.post("/api/end_call", {
      reason: $("endReason").value,
      reason_detail: $("endReasonDetail").value || null,
    });
    await refresh();
  } catch (err) {
    alert(err.message);
  }
}

async function autoStep(turns = 1) {
  try {
    const out = await api.post("/api/agent/auto_step", { turns });
    const executed = Number(out.executed_turns || 0);
    const queued = Number(out.queued_caller_turns || 0);
    if (executed > 0) {
      $("setupStatus").textContent = `Auto executed turns: ${executed}`;
    } else if (queued > 0) {
      const preview = String(out.last_queued_caller_text || "").slice(0, 80);
      $("setupStatus").textContent = `Queued caller turn (${queued}). Awaiting manual call-taker response. ${preview}`;
    } else {
      $("setupStatus").textContent = "No auto turns executed.";
    }
    await refresh();
  } catch (err) {
    alert(err.message);
  }
}

async function evalQa() {
  try {
    const out = await api.post("/api/qa/evaluate", {});
    $("setupStatus").textContent = `QA score: ${Number(out.qa_score?.normalized_score || 0).toFixed(2)}`;
    await refresh();
  } catch (err) {
    alert(err.message);
  }
}

async function saveArtifacts() {
  try {
    const out = await api.post("/api/artifacts/save", { reason: "ui_export" });
    const art = out.artifact || {};
    $("setupStatus").textContent = `Artifacts saved: ${art.episode_dir || ""}`;
    await refresh();
  } catch (err) {
    alert(err.message);
  }
}

async function fetchSop() {
  const type = $("sopIncidentType").value;
  const step = $("sopStep").value;
  try {
    const out = await api.get(`/api/sop?incident_type=${encodeURIComponent(type)}&step=${encodeURIComponent(step)}`);
    renderSop(out.snippets || []);
    await refresh();
  } catch (err) {
    alert(err.message);
  }
}

function jumpToTurn() {
  const n = Number($("jumpTurn").value || "0");
  if (!n) return;
  const el = document.getElementById(`turn-${n}`);
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
}

function escapeHtml(str) {
  return String(str)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function bind() {
  $("refreshBtn").addEventListener("click", refresh);
  $("loadStartBtn").addEventListener("click", setupEpisode);
  $("callerSendBtn").addEventListener("click", postCaller);
  $("ctSendBtn").addEventListener("click", postCallTaker);
  $("endCallBtn").addEventListener("click", endCall);
  $("autoStepBtn").addEventListener("click", () => autoStep(1));
  $("autoRun5Btn").addEventListener("click", () => autoStep(5));
  $("qaEvalBtn").addEventListener("click", evalQa);
  $("saveArtifactsBtn").addEventListener("click", saveArtifacts);
  $("sopFetchBtn").addEventListener("click", fetchSop);
  $("jumpBtn").addEventListener("click", jumpToTurn);
  $("searchBox").addEventListener("input", () => renderTranscript(state));
  $("verbosity").addEventListener("change", () => renderTranscript(state));
}

bind();
loadAgentCatalog().then(async () => {
  await refresh();
  openLiveStream();
});
