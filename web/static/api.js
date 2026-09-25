export class ApiError extends Error {
  constructor(status, payload) {
    const error = payload && payload.error ? payload.error : {};
    super(error.message || "The request failed.");
    this.status = status;
    this.code = error.code || "request_failed";
    this.retryable = Boolean(error.retryable);
  }
}

export async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = new Headers(options.headers || {});
  if (method !== "GET" && method !== "HEAD") {
    headers.set("X-Mutiny-Request", "1");
  }
  let body;
  if (options.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(options.json);
  }
  const response = await fetch(path, {
    method,
    headers,
    body,
    credentials: "same-origin",
  });
  if (response.status === 204) return null;
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch (_error) {
      payload = null;
    }
  }
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload;
}

export function requestId() {
  if (globalThis.crypto && crypto.randomUUID) return crypto.randomUUID();
  return `req-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
