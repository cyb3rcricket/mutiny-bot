import { ApiError, api, requestId } from "./api.js";
import { formatResearchMarkdown, renderMessages, renderResearchWorksheet, renderThreads, renderToolResult, showSource, sourceRow } from "./conversation.js";
import { fillModels, renderFacts, renderJobRuns, renderJobs, renderPrivacy } from "./panels.js";

const shell = document.querySelector("#shell");
const banner = document.querySelector("#banner");
const actionNote = document.querySelector("#action-note");
const state = {
  threads: [],
  threadId: null,
  messages: [],
  settings: null,
  models: [],
  status: null,
  sending: false,
  confirm: "",
  currentResearchRun: null,
};

const DRAWER_VIEWS = new Set(["settings", "memory", "jobs", "privacy"]);

function setBanner(text) {
  banner.hidden = !text;
  banner.textContent = text || "";
}

function setView(view) {
  shell.dataset.view = view;
  shell.classList.toggle("drawer-open", DRAWER_VIEWS.has(view));
  for (const button of document.querySelectorAll(".nav button")) {
    const current = button.dataset.view === view;
    if (current) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  for (const name of DRAWER_VIEWS) {
    document.querySelector(`#panel-${name}`).hidden = name !== view;
  }
  if (view === "memory") loadFacts().catch(showError);
  if (view === "jobs") loadJobs().catch(showError);
  if (view === "privacy" && state.status) renderPrivacy(document.querySelector("#privacy-list"), state.status);
}

function showError(error) {
  const message = error instanceof ApiError ? error.message : "The console could not complete that.";
  setBanner(message);
}

async function loadThreads() {
  const payload = await api("/api/threads?limit=50");
  state.threads = payload.items || [];
  renderThreads(document.querySelector("#thread-list"), state.threads, state.threadId, selectThread);
}

async function selectThread(id) {
  state.threadId = id;
  state.confirm = "";
  actionNote.hidden = true;
  const thread = state.threads.find((item) => item.id === id);
  document.querySelector("#thread-title").textContent = thread ? thread.title : "Conversation";
  renderThreads(document.querySelector("#thread-list"), state.threads, id, selectThread);
  const payload = await api(`/api/threads/${encodeURIComponent(id)}/messages?limit=100`);
  state.messages = payload.items || [];
  renderMessages(document.querySelector("#transcript"), state.messages, (source) => {
    showSource(document.querySelector("#source-dialog"), source);
  });
  if (window.matchMedia("(max-width: 899px)").matches) setView("chat");
}

async function createThread() {
  const created = await api("/api/threads", { method: "POST", json: {} });
  await loadThreads();
  await selectThread(created.id);
}

async function sendMessage(event) {
  event.preventDefault();
  const input = document.querySelector("#composer-input");
  const content = input.value.trim();
  if (!content || state.sending) return;
  if (!state.threadId) await createThread();
  state.sending = true;
  document.querySelector("#send").disabled = true;
  setBanner("");
  try {
    await api(`/api/threads/${encodeURIComponent(state.threadId)}/messages`, {
      method: "POST",
      json: { request_id: requestId(), content },
    });
    input.value = "";
    await loadThreads();
    await selectThread(state.threadId);
  } catch (error) {
    showError(error);
    if (state.threadId) {
      await loadThreads();
      await selectThread(state.threadId);
    }
  } finally {
    state.sending = false;
    document.querySelector("#send").disabled = false;
    input.focus();
  }
}

async function loadSettings() {
  const [settings, models, status] = await Promise.all([
    api("/api/settings"),
    api("/api/models"),
    api("/api/status"),
  ]);
  state.settings = settings;
  state.models = models.models || [];
  state.status = status;
  fillModels(document.querySelector("#model-select"), state.models, settings.model);
  const discovery = models.discovery_status === "ok" ? "" : `Discovery: ${models.discovery_status}.`;
  const available = models.selected_model_available ? "" : "The saved model is not in the installed list.";
  document.querySelector("#model-status").textContent = [discovery, available].filter(Boolean).join(" ");
  document.querySelector("#personality").value = settings.system_prompt || "";
  document.querySelector("#timezone").value = settings.automation_timezone || "";
  document.querySelector("#job-zone").value = settings.automation_timezone || "UTC";
  const label = settings.model ? `Message · ${settings.model}` : "Message · no model selected";
  document.querySelector("#model-label").textContent = label;
  renderPrivacy(document.querySelector("#privacy-list"), status);
}

async function saveSettings(event) {
  event.preventDefault();
  const model = document.querySelector("#model-select").value;
  const body = {
    system_prompt: document.querySelector("#personality").value,
    automation_timezone: document.querySelector("#timezone").value.trim(),
  };
  if (model && state.models.some((item) => item.name === model)) body.model = model;
  await api("/api/settings", { method: "PATCH", json: body });
  await loadSettings();
  setBanner("Settings saved.");
}

async function loadFacts() {
  const payload = await api("/api/memory/facts?limit=50");
  renderFacts(document.querySelector("#fact-list"), payload.items || []);
}

async function remember(event) {
  event.preventDefault();
  const field = document.querySelector("#remember-text");
  const content = field.value.trim();
  if (!content) return;
  const fact = await api("/api/memory/facts", {
    method: "POST",
    json: { request_id: requestId(), content },
  });
  field.value = "";
  await loadFacts();
  setBanner(fact.palace_status === "indexed" ? "Saved and indexed." : `Saved. Palace status: ${fact.palace_status}.`);
}

async function runTool(name, arguments_) {
  const run = await api(`/api/tools/${encodeURIComponent(name)}/runs`, {
    method: "POST",
    json: { request_id: requestId(), arguments: arguments_ },
  });
  const lab = document.querySelector("#research-lab");
  if (lab) lab.hidden = true;
  const section = document.querySelector("#tool-result");
  renderToolResult(section, run);
  const host = section.querySelector("#tool-result-sources");
  host.replaceChildren();
  if (run.sources && run.sources.length) {
    host.append(sourceRow(run.sources, (source) => showSource(document.querySelector("#source-dialog"), source)));
  }
  if (window.matchMedia("(max-width: 899px)").matches) setView("chat");
  return run;
}

async function runResearch(question) {
  const cleanQ = (question || "").trim();
  if (!cleanQ) return;
  setBanner("");
  const button = document.querySelector("#research-notes");
  if (button) button.disabled = true;
  try {
    const run = await api("/api/tools/research/runs", {
      method: "POST",
      json: {
        request_id: requestId(),
        arguments: { question: cleanQ, mode: "closed" },
      },
    });
    state.currentResearchRun = run;
    const toolSection = document.querySelector("#tool-result");
    if (toolSection) toolSection.hidden = true;
    const labSection = document.querySelector("#research-lab");
    renderResearchWorksheet(labSection, run, (source) => {
      showSource(document.querySelector("#source-dialog"), source);
    });
    if (window.matchMedia("(max-width: 899px)").matches) setView("chat");
    return run;
  } catch (error) {
    showError(error);
  } finally {
    if (button) button.disabled = false;
  }
}

async function copyMarkdown(text, button) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (_e) {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
  }
  if (button) {
    const orig = button.textContent;
    button.textContent = "Copied!";
    setTimeout(() => { button.textContent = orig; }, 1500);
  }
}

function downloadMarkdown(text, filename) {
  const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

async function loadJobs() {
  const payload = await api("/api/jobs");
  const note = document.querySelector("#jobs-status");
  const parts = [];
  if (!payload.scheduler_available) parts.push(payload.unavailable_reason || "Scheduler is unavailable.");
  if (payload.legacy_jobs_pending) parts.push("Legacy schedules are waiting for an explicit migration. They are not running.");
  note.textContent = parts.join(" ");
  renderJobs(
    document.querySelector("#job-list"),
    payload.items || [],
    async (job) => {
      const runs = await api(`/api/jobs/${encodeURIComponent(job.id)}/runs?limit=20`);
      renderJobRuns(document.querySelector("#job-runs"), runs.items || []);
    },
    async (job) => {
      await api(`/api/jobs/${encodeURIComponent(job.id)}`, {
        method: "PATCH",
        json: { paused: !job.paused },
      });
      await loadJobs();
    },
    async (job) => {
      await api(`/api/jobs/${encodeURIComponent(job.id)}`, { method: "DELETE" });
      document.querySelector("#job-runs").replaceChildren();
      await loadJobs();
    },
  );
}

async function createJob(event) {
  event.preventDefault();
  await api("/api/jobs", {
    method: "POST",
    json: {
      name: document.querySelector("#job-name").value.trim(),
      tool_name: "get_morning_briefing",
      arguments: {},
      schedule: {
        type: "daily",
        time: document.querySelector("#job-time").value,
        timezone: document.querySelector("#job-zone").value.trim(),
      },
    },
  });
  document.querySelector("#job-name").value = "";
  await loadJobs();
}

function armAction(kind, note) {
  if (state.confirm !== kind) {
    state.confirm = kind;
    actionNote.hidden = false;
    actionNote.textContent = note;
    return false;
  }
  state.confirm = "";
  actionNote.hidden = true;
  return true;
}

document.querySelector(".nav").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  setView(button.dataset.view);
});

document.querySelector("#new-thread").addEventListener("click", () => {
  createThread().catch(showError);
});

document.querySelector("#composer").addEventListener("submit", (event) => {
  sendMessage(event).catch(showError);
});

document.querySelector("#composer-input").addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    event.preventDefault();
    document.querySelector("#composer").requestSubmit();
  }
});

document.querySelector("#settings-form").addEventListener("submit", (event) => {
  saveSettings(event).catch(showError);
});

document.querySelector("#remember-form").addEventListener("submit", (event) => {
  remember(event).catch(showError);
});

document.querySelector("#recall-form").addEventListener("submit", (event) => {
  event.preventDefault();
  runTool("recall", { query: document.querySelector("#recall-query").value, limit: 20 }).catch(showError);
});

document.querySelector("#ask-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const question = document.querySelector("#ask-question").value.trim();
  if (!question) return;
  const arguments_ = { question, limit: 8 };
  if (state.threadId) arguments_.thread_id = state.threadId;
  runTool("ask_notes", arguments_).catch(showError);
});

document.querySelector("#research-notes").addEventListener("click", () => {
  const question = document.querySelector("#ask-question").value.trim();
  if (!question) {
    document.querySelector("#ask-question").focus();
    return;
  }
  runResearch(question).catch(showError);
});

document.querySelector("#research-copy").addEventListener("click", (event) => {
  if (!state.currentResearchRun) return;
  const md = formatResearchMarkdown(state.currentResearchRun);
  copyMarkdown(md, event.target);
});

document.querySelector("#research-download").addEventListener("click", () => {
  if (!state.currentResearchRun) return;
  const md = formatResearchMarkdown(state.currentResearchRun);
  downloadMarkdown(md, "research-run.md");
});

document.querySelector("#research-close").addEventListener("click", () => {
  document.querySelector("#research-lab").hidden = true;
});

document.querySelector("#job-form").addEventListener("submit", (event) => {
  createJob(event).catch(showError);
});

document.querySelector("#run-briefing").addEventListener("click", () => {
  runTool("get_morning_briefing", {}).catch(showError);
});

document.querySelector("#clear-history").addEventListener("click", () => {
  if (!state.threadId) return;
  if (!armAction("clear", "Clear history removes messages from this conversation. Saved facts stay.")) return;
  api(`/api/threads/${encodeURIComponent(state.threadId)}/messages`, { method: "DELETE" })
    .then(() => selectThread(state.threadId))
    .catch(showError);
});

document.querySelector("#reset-context").addEventListener("click", () => {
  if (!state.threadId) return;
  if (!armAction("reset", "Reset context keeps the messages on screen. Later replies ignore the earlier ones.")) return;
  api(`/api/threads/${encodeURIComponent(state.threadId)}/reset-context`, { method: "POST", json: {} })
    .then(() => setBanner("Context reset. The transcript is still here."))
    .catch(showError);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && DRAWER_VIEWS.has(shell.dataset.view)) setView("chat");
});

function initSidebarResize() {
  const resizer = document.querySelector("#sidebar-resizer");
  const sidebar = document.querySelector("#sidebar");
  if (!resizer || !sidebar) return;

  const MIN_WIDTH = 160;
  const DEFAULT_WIDTH = 240;

  function getMaxWidth() {
    return Math.max(MIN_WIDTH, Math.min(800, window.innerWidth - 320));
  }

  function setWidth(width, save = true) {
    const clamped = Math.max(MIN_WIDTH, Math.min(getMaxWidth(), Math.round(width)));
    document.documentElement.style.setProperty("--sidebar", `${clamped}px`);
    resizer.setAttribute("aria-valuenow", String(clamped));
    if (save) {
      try {
        localStorage.setItem("mutiny_sidebar_width", String(clamped));
      } catch (_) {}
    }
    return clamped;
  }

  try {
    const saved = localStorage.getItem("mutiny_sidebar_width");
    if (saved) {
      const parsed = parseInt(saved, 10);
      if (!Number.isNaN(parsed)) setWidth(parsed, false);
    }
  } catch (_) {}

  let isDragging = false;
  let startX = 0;
  let startWidth = 0;

  resizer.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    if (window.matchMedia("(max-width: 899px)").matches) return;
    isDragging = true;
    resizer.setPointerCapture(event.pointerId);
    startX = event.clientX;
    startWidth = sidebar.getBoundingClientRect().width;
    resizer.classList.add("is-resizing");
    document.body.classList.add("resizing-sidebar");
    event.preventDefault();
  });

  resizer.addEventListener("pointermove", (event) => {
    if (!isDragging) return;
    const delta = event.clientX - startX;
    setWidth(startWidth + delta, false);
  });

  function stopDragging(event) {
    if (!isDragging) return;
    isDragging = false;
    try {
      resizer.releasePointerCapture(event.pointerId);
    } catch (_) {}
    resizer.classList.remove("is-resizing");
    document.body.classList.remove("resizing-sidebar");
    const finalWidth = sidebar.getBoundingClientRect().width;
    setWidth(finalWidth, true);
  }

  resizer.addEventListener("pointerup", stopDragging);
  resizer.addEventListener("pointercancel", stopDragging);

  resizer.addEventListener("dblclick", () => {
    if (window.matchMedia("(max-width: 899px)").matches) return;
    setWidth(DEFAULT_WIDTH, true);
  });

  resizer.addEventListener("keydown", (event) => {
    if (window.matchMedia("(max-width: 899px)").matches) return;
    const current = sidebar.getBoundingClientRect().width;
    const step = event.shiftKey ? 30 : 10;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      setWidth(current - step, true);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      setWidth(current + step, true);
    } else if (event.key === "Home") {
      event.preventDefault();
      setWidth(MIN_WIDTH, true);
    } else if (event.key === "End" || event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setWidth(DEFAULT_WIDTH, true);
    }
  });
}

async function boot() {
  initSidebarResize();
  await api("/api/session");
  await loadSettings();
  await loadThreads();
  if (state.threads.length) await selectThread(state.threads[0].id);
  setView("chat");
}

boot().catch(showError);
