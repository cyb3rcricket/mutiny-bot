function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function fillModels(select, models, selected) {
  select.replaceChildren();
  if (!models.length) {
    const option = el("option", "", "No local models are installed");
    option.value = "";
    option.disabled = true;
    option.selected = true;
    select.append(option);
    return;
  }
  for (const model of models) {
    const option = el("option", "", model.preferred ? `${model.name} (preferred)` : model.name);
    option.value = model.name;
    if (model.name === selected) option.selected = true;
    select.append(option);
  }
  if (selected && !models.some((model) => model.name === selected)) {
    const missing = el("option", "", `${selected} (not installed)`);
    missing.value = selected;
    missing.selected = true;
    select.prepend(missing);
  }
}

export function renderFacts(list, facts) {
  list.replaceChildren();
  if (!facts.length) {
    list.append(el("li", "note", "No saved facts."));
    return;
  }
  for (const fact of facts) {
    const item = el("li");
    item.append(el("p", "", fact.content));
    const status = fact.palace_status ? `palace: ${fact.palace_status}` : "";
    if (status) item.append(el("p", "meta", status));
    list.append(item);
  }
}

export function renderJobs(list, jobs, onOpen, onPause, onStop) {
  list.replaceChildren();
  if (!jobs.length) {
    list.append(el("li", "note", "No scheduled jobs."));
    return;
  }
  for (const job of jobs) {
    const item = el("li", "job-card");
    const open = el("button", "job-open", job.name || job.id);
    open.type = "button";
    open.addEventListener("click", () => onOpen(job));
    item.append(open);
    const when = job.schedule ? `${job.schedule.time || ""} ${job.schedule.timezone || ""}` : "";
    item.append(el("p", "meta", `${job.tool_name} · ${when} · ${job.paused ? "paused" : "active"}`));
    const actions = el("div", "job-actions");
    const pause = el("button", "btn btn-sm btn-outline-secondary", job.paused ? "Resume" : "Pause");
    pause.type = "button";
    pause.addEventListener("click", () => onPause(job));
    const stop = el("button", "btn btn-sm btn-outline-danger", "Stop");
    stop.type = "button";
    stop.addEventListener("click", () => onStop(job));
    actions.append(pause, stop);
    item.append(actions);
    list.append(item);
  }
}

export function renderJobRuns(host, runs) {
  host.replaceChildren();
  if (!runs.length) {
    host.append(el("p", "note", "No runs for this job yet."));
    return;
  }
  host.append(el("h3", "", "Recent runs"));
  for (const run of runs) {
    const block = el("article", "job-card");
    block.append(el("p", "meta", `${run.status} · ${run.finished_at || run.started_at || ""}`));
    block.append(el("pre", "", run.output || run.error_code || ""));
    host.append(block);
  }
}

export function renderPrivacy(list, status) {
  const rows = [
    ["Bind", `${status.bind_host || ""}:${status.port || ""}`],
    ["Ollama", status.ollama_target || ""],
    ["Ollama reachable", status.ollama_available ? "yes" : "no"],
    ["Memory", status.memory_backend || ""],
    ["Memory note", status.memory_degraded_reason || "none"],
    ["Scheduler", status.scheduler_available ? "available" : "unavailable"],
    ["Outbound", status.outbound_enabled ? "enabled" : "off"],
    ["Dangerous tools", status.dangerous_tools_enabled ? "enabled" : "off"],
  ];
  list.replaceChildren();
  for (const [label, value] of rows) {
    list.append(el("dt", "", label), el("dd", "", String(value)));
  }
}
