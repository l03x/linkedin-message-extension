// Talks to the backend only (never to LinkedIn). Settings live in chrome.storage.local.

export const DEFAULT_BACKEND = "http://localhost:8765";
const TIMEOUT_MS = 200_000; // the backend allows 180 s for the LLM, plus a retry margin

export async function getSettings() {
  const { backendUrl, token } = await chrome.storage.local.get(["backendUrl", "token"]);
  return { backendUrl: (backendUrl || DEFAULT_BACKEND).replace(/\/+$/, ""), token: token || "" };
}

export async function saveSettings({ backendUrl, token }) {
  await chrome.storage.local.set({ backendUrl: backendUrl.trim().replace(/\/+$/, ""), token: token.trim() });
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function describe(status, body) {
  const d = body && body.detail;
  if (d && typeof d === "object" && !Array.isArray(d) && d.message) return d.message;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((e) => `${(e.loc || []).slice(1).join(".")}: ${e.msg}`).join("; ");
  return `HTTP ${status}`;
}

export async function api(path, { method = "GET", body } = {}) {
  const { backendUrl, token } = await getSettings();
  if (!token) throw new ApiError("Set the backend token in Settings first.", 0);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  let resp;
  try {
    resp = await fetch(backendUrl + path, {
      method,
      headers: { Authorization: `Bearer ${token}`, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
  } catch (e) {
    if (e.name === "AbortError") throw new ApiError("The backend took too long to answer.", 0);
    throw new ApiError(`Can't reach the backend at ${backendUrl}. Is "docker compose up" running?`, 0);
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  try {
    data = await resp.json();
  } catch {
    /* empty or non-JSON body */
  }
  if (!resp.ok) throw new ApiError(describe(resp.status, data), resp.status);
  return data;
}
