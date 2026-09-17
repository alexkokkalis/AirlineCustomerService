const $ = (selector) => document.querySelector(selector);
const state = { jobId: null, runs: new Set(), events: new Set(), eventSource: null, iteration: 0, tools: 0, turns: 0 };

function setStatus(label, tone = "neutral") {
  $("#phase").textContent = label.replaceAll("_", " ");
  const pill = $("#connection-status");
  pill.textContent = label.replaceAll("_", " ");
  pill.className = `status-pill ${tone}`;
}

function clearPanels() {
  state.runs.clear(); state.events.clear(); state.iteration = 0; state.tools = 0; state.turns = 0;
  $("#conversation").innerHTML = ""; $("#evaluation").innerHTML = ""; $("#refinement").innerHTML = "";
  $("#diff").textContent = "No refinement diff is available yet.";
  $("#metric-iteration").textContent = "0"; $("#metric-tools").textContent = "0"; $("#metric-turns").textContent = "0";
}

function append(parent, node) { parent.append(node); parent.scrollTop = parent.scrollHeight; }
function card(title, content, extra = "") { const node = document.createElement("section"); node.className = "card"; node.innerHTML = `<h3>${title}${extra}</h3><p>${content}</p>`; return node; }
function toolName(event) { const path = event.path || "tool"; if (path.includes("/policies")) return "get_policy"; if (path.endsWith("/seats")) return "get_available_seats"; if (path.endsWith("/quote")) return "quote_reschedule"; if (path.endsWith("/reschedule")) return "reschedule_booking"; if (path.includes("/ancillaries")) return "add_ancillary"; if (path.includes("/bookings")) return event.method === "GET" ? "get_booking" : "booking action"; if (path.includes("/flights")) return "search_flights"; return path; }

function addConversationEvent(event) {
  const panel = $("#conversation");
  if (event.event_type === "run_started") {
    state.iteration += 1; state.runs.add(event.run_id);
    const divider = document.createElement("div"); divider.className = "iteration-divider"; divider.textContent = `Iteration ${state.iteration} · ${event.run_id.slice(-8)}`; append(panel, divider);
    $("#conversation-run").textContent = event.run_id; $("#metric-iteration").textContent = String(state.iteration);
  }
  if (event.event_type === "message" && typeof event.text === "string") {
    const bubble = document.createElement("div"); bubble.className = `bubble ${event.role === "customer" ? "customer" : "agent"}`;
    bubble.innerHTML = `<small>${event.role === "customer" ? "Customer" : "Erling"}</small>`;
    bubble.append(document.createTextNode(event.text)); append(panel, bubble);
    if (event.role === "customer") { state.turns += 1; $("#metric-turns").textContent = String(state.turns); }
  }
  if (event.event_type === "tool_request_finished") {
    state.tools += 1; $("#metric-tools").textContent = String(state.tools);
    const chip = document.createElement("span"); chip.className = "tool-chip"; chip.textContent = `${event.status_code >= 400 ? "⚠" : "✓"} ${toolName(event)} · ${event.status_code}`; append(panel, chip);
  }
}

function renderEvaluation(detail) {
  const target = $("#evaluation"); target.innerHTML = "";
  const deterministic = detail.deterministic_evaluation;
  const llm = detail.llm_evaluation;
  if (deterministic) {
    const list = deterministic.checks.map((check) => `<li><span>${check.message}</span><b class="${check.status}">${check.status}</b></li>`).join("");
    const node = card(`Deterministic · ${deterministic.overall_status}`, `<ul class="check-list">${list}</ul>`); append(target, node);
  }
  if (llm) {
    const scores = Object.entries(llm.criteria || {}).map(([name, criterion]) => `<li><span>${name.replaceAll("_", " ")}</span><b class="score">${criterion.score}/10</b></li>`).join("");
    const node = card(`LLM review · ${llm.overall_score}/10`, `<ul class="check-list">${scores}</ul><p>${llm.decision?.reason || llm.summary || ""}</p>`); append(target, node);
  }
  $("#evaluation-status").textContent = llm?.decision?.decision || deterministic?.overall_status || "Waiting";
}

function colorizeDiff(diff) {
  return diff.split("\n").map((line) => `<span class="${line.startsWith("+") ? "add" : line.startsWith("-") ? "remove" : ""}">${escapeHtml(line)}</span>`).join("\n");
}
function escapeHtml(text) { const node = document.createElement("span"); node.textContent = text; return node.innerHTML; }

function renderRefinement(detail) {
  const target = $("#refinement"); const plan = detail.refinement_plan; const applied = detail.applied_refinement;
  if (plan) {
    target.innerHTML = "";
    append(target, card(`Plan · ${plan.status}`, `${plan.rationale}<br><br><b>Target:</b> ${plan.target_file || "none"}`));
    if (applied) append(target, card(`Application · ${applied.status}`, `Verification: ${applied.verification_scenario_id}`));
    $("#refinement-status").textContent = plan.status;
  }
  if (detail.change_diff) { $("#diff").innerHTML = colorizeDiff(detail.change_diff); $("#diff-status").textContent = applied?.status || "planned"; }
}

async function refreshRun(runId, replay = false) {
  const response = await fetch(`/dashboard-api/runs/${encodeURIComponent(runId)}`); if (!response.ok) return;
  const detail = await response.json();
  if (replay) {
    // A historic run is loaded independently, so recover its pipeline
    // iteration from the durable terminal event rather than always labelling
    // the first displayed run as iteration 1.
    const persistedIteration = detail.events.find((event) => event.event_type === "refinement_iteration_completed")?.iteration;
    if (Number.isInteger(persistedIteration) && persistedIteration > 0) state.iteration = persistedIteration - 1;
    detail.events.forEach(handleRunEvent);
  }
  renderEvaluation(detail); renderRefinement(detail);
}

function handleRunEvent(event) {
  if (event.event_id && state.events.has(event.event_id)) return; if (event.event_id) state.events.add(event.event_id);
  addConversationEvent(event);
  if (["evaluation_completed", "llm_evaluation_completed", "refinement_plan_created", "refinement_apply_completed"].includes(event.event_type)) refreshRun(event.run_id);
  if (event.event_type === "refinement_planning_failed") {
    const target = $("#refinement"); target.innerHTML = "";
    append(target, card("Planning stopped safely", event.reason || "The Refiner could not produce a valid bounded plan."));
    $("#refinement-status").textContent = "planning failed";
    setStatus("stopped safely", "failed");
  }
  const phases = { customer_simulator_started: "simulating", llm_evaluation_started: "evaluating", refinement_planning_started: "planning", refinement_apply_started: "applying" };
  if (phases[event.event_type]) setStatus(phases[event.event_type], "running");
}

function handleDashboardEvent(payload) {
  if (payload.kind === "run") return handleRunEvent(payload.event);
  const event = payload;
  if (event.event_type === "dashboard_job_started") setStatus("running", "running");
  if (event.event_type === "dashboard_job_completed") { setStatus(event.status, event.status === "passed" ? "done" : "neutral"); $("#run-button").disabled = false; }
  if (event.event_type === "dashboard_job_failed") {
    setStatus(event.status, "failed"); $("#run-button").disabled = false;
    const target = $("#refinement");
    if (!target.children.length || target.classList.contains("empty-state")) { target.innerHTML = ""; append(target, card("Job stopped safely", event.error || "No further safe action was taken.")); }
  }
}

function connect(jobId) {
  state.eventSource?.close(); const source = new EventSource(`/dashboard-api/jobs/${encodeURIComponent(jobId)}/events`); state.eventSource = source;
  source.onmessage = (message) => handleDashboardEvent(JSON.parse(message.data));
  source.onerror = () => { if (source.readyState === EventSource.CONNECTING) setStatus("reconnecting", "neutral"); };
}

async function startJob() {
  const applyChanges = $("#apply-changes").checked;
  if (applyChanges && !window.confirm("Apply any validated refinement? This may update Erling's dedicated ElevenLabs refinement branch or an allowlisted local source file on the autonomous-refinement Git branch. Main is not targeted.")) return;
  clearPanels(); $("#run-button").disabled = true; setStatus("starting", "running");
  const scenarioId = $("#scenario-select").value; $("#metric-scenario").textContent = scenarioId;
  const response = await fetch("/dashboard-api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scenario_id: scenarioId, apply_changes: applyChanges }) });
  if (!response.ok) { setStatus("start failed", "failed"); $("#run-button").disabled = false; return; }
  const job = await response.json(); state.jobId = job.job_id; $("#job-id").textContent = job.job_id; connect(job.job_id);
}

async function initialise() {
  const response = await fetch("/dashboard-api/scenarios"); const payload = await response.json();
  $("#scenario-select").innerHTML = payload.scenarios.map((scenario) => `<option value="${scenario.id}">${scenario.title}</option>`).join("");
  $("#scenario-select").disabled = false; $("#run-button").disabled = false; setStatus("idle");
  $("#run-button").addEventListener("click", startJob);
  $("#apply-changes").addEventListener("change", (event) => { $("#run-button").textContent = event.target.checked ? "Run refinement loop" : "Run dry simulation"; });
  const historicRun = new URLSearchParams(location.search).get("run_id"); if (historicRun) { $("#metric-scenario").textContent = "Historic run"; await refreshRun(historicRun, true); }
}
initialise().catch(() => setStatus("dashboard unavailable", "failed"));
