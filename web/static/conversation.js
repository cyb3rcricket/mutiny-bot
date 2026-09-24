function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function renderThreads(list, threads, selectedId, onSelect) {
  list.replaceChildren();
  if (!threads.length) {
    list.append(el("li", "note", "No conversations yet."));
    return;
  }
  for (const thread of threads) {
    const item = el("li");
    const button = el("button", "", thread.title || "Untitled");
    button.type = "button";
    button.setAttribute("aria-current", thread.id === selectedId ? "true" : "false");
    button.addEventListener("click", () => onSelect(thread.id));
    item.append(button);
    list.append(item);
  }
}

export function renderMessages(transcript, messages, onSource) {
  transcript.replaceChildren();
  if (!messages.length) {
    transcript.append(el("p", "note", "This conversation is empty."));
    return;
  }
  for (const message of messages) {
    const role = message.role === "assistant" ? "assistant" : "user";
    const block = el("article", `message ${role} ${message.status || "complete"}`);
    const who = role === "assistant" ? "Mutiny" : "You";
    block.append(el("p", "who", who));
    let body = message.content || "";
    if (message.status === "pending") body = "Waiting for the local model.";
    if (message.status === "failed" && !body) body = "This turn failed.";
    block.append(el("div", "body", body));
    if (message.status && message.status !== "complete") {
      block.append(el("p", "meta", message.status));
    }
    const sources = Array.isArray(message.sources) ? message.sources : [];
    if (sources.length) block.append(sourceRow(sources, onSource));
    transcript.append(block);
  }
  transcript.scrollTop = transcript.scrollHeight;
}

export function sourceRow(sources, onSource) {
  const row = el("div", "sources");
  sources.forEach((source, index) => {
    const chip = el("button", "", source.title || source.kind || "Source");
    chip.type = "button";
    chip.addEventListener("click", () => onSource(source, index));
    row.append(chip);
  });
  return row;
}

export function showSource(dialog, source) {
  dialog.querySelector("#source-title").textContent = source.title || "Source";
  dialog.querySelector("#source-kind").textContent = source.kind || "";
  dialog.querySelector("#source-excerpt").textContent = source.excerpt || "";
  const linkHost = dialog.querySelector("#source-link");
  linkHost.replaceChildren();
  const url = safeHttpUrl(source.external_url);
  if (url) {
    const link = el("a", "", url);
    link.href = url;
    link.rel = "noopener noreferrer";
    link.target = "_blank";
    linkHost.append(link);
  }
  dialog.showModal();
}

export function renderToolResult(section, run) {
  if (!run) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  const meta = section.querySelector("#tool-result-meta");
  const body = section.querySelector("#tool-result-body");
  meta.textContent = `${run.tool_name || "tool"} · ${run.status || ""}`;
  body.textContent = run.output || run.error_code || "";
}

function safeHttpUrl(value) {
  if (!value) return "";
  try {
    const url = new URL(value);
    if (url.protocol === "http:" || url.protocol === "https:") return url.href;
  } catch (_error) {
    return "";
  }
  return "";
}
