import { DEFAULT_BACKEND, api, getSettings, saveSettings } from "./api.js";
import { extractProfile } from "./extract.js";

const PROFILE_URL = /^https:\/\/www\.linkedin\.com\/in\/([^/?#]+)/;
const $ = (id) => document.getElementById(id);
// Count like the backend: Unicode code points, not UTF-16 units.
const count = (s) => [...s].length;
const slugOf = (url) => {
  const m = (url || "").match(PROFILE_URL);
  return m ? decodeURIComponent(m[1]).toLowerCase() : "";
};
const initials = (name) =>
  (name || "")
    .split(/\s+/)
    .filter((p) => /\p{L}/u.test(p[0] || ""))
    .slice(0, 2)
    .map((p) => p[0].toUpperCase())
    .join("") || "?";

const ICONS = {
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
  monitor: '<rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
  alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>',
  history: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/>',
};
const icon = (name) =>
  `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;

const state = {
  tab: null, // {id, slug, title} of the active tab when it's a profile, else null
  options: null, // from /api/options
  mode: null,
  language: null,
  payload: null, // the profile last extracted; Regenerate reuses it without re-reading the page
  draft: null, // the last /api/generate response, plus {sent}
};

// --- persistence (survives closing/reopening the panel, cleared when the browser closes) ---

async function persist() {
  await chrome.storage.session.set({
    payload: state.payload,
    draft: state.draft && { ...state.draft, subject: $("subject").value, body: $("body").value },
  });
}

let persistTimer;
function persistSoon() {
  clearTimeout(persistTimer);
  persistTimer = setTimeout(persist, 300);
}

// --- small UI helpers ---

function setStatus(el, kind, text) {
  el.hidden = !text;
  el.className = `status ${kind}`;
  el.textContent = text || "";
}

let toastTimer;
function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 1800);
}

function segmented(container, items, selected, onPick) {
  container.replaceChildren(
    ...items.map((it) => {
      const b = document.createElement("button");
      b.type = "button";
      b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", String(it.key === selected));
      b.dataset.key = it.key;
      b.textContent = it.short;
      b.title = it.title;
      if (it.dir) b.dir = it.dir;
      b.addEventListener("click", () => {
        container.querySelectorAll("button").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
        onPick(it.key);
      });
      return b;
    })
  );
}

// --- theme ---

const THEME_ORDER = ["system", "light", "dark"];
const THEME_ICON = { system: "monitor", light: "sun", dark: "moon" };

function renderTheme() {
  const t = window.panelTheme.get();
  $("themeBtn").innerHTML = icon(THEME_ICON[t]);
  $("themeBtn").title = `Theme: ${t} (click to change)`;
  document.querySelectorAll("[data-theme-choice]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themeChoice === t)));
}

// --- current tab (URL only; the page itself is read only when you click Generate) ---

async function refreshTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  // With host permission only for linkedin.com/in/*, tab.url is undefined on every other site.
  const m = tab && tab.url && tab.url.match(PROFILE_URL);
  const title = m ? (tab.title || "").split("|")[0].replace(/^\(\d+\)\s*/, "").trim() : "";
  state.tab = m ? { id: tab.id, slug: slugOf(tab.url), title } : null;
  $("profile").classList.toggle("off", !state.tab);
  if (state.tab) {
    $("targetName").textContent = title || state.tab.slug;
    $("targetSub").textContent = `linkedin.com/in/${state.tab.slug}`;
    $("avatar").textContent = initials(title || state.tab.slug);
  } else {
    $("targetName").textContent = "Open a LinkedIn profile";
    $("targetSub").textContent = "Go to a page like linkedin.com/in/someone, then generate a draft here.";
    $("avatar").textContent = "in";
  }
  updateButtons();
  renderDraftFor();
}

// --- options ---

const modeInfo = (key) => state.options && state.options.modes.find((m) => m.key === key);
const languageInfo = (key) => state.options && state.options.languages.find((l) => l.key === key);

function modeHint(m) {
  if (!m) return "";
  if (m.limits.subject) return `Subject up to ${m.limits.subject} · body up to ${m.limits.body} characters`;
  if (m.target_chars) return `Aims for about ${m.target_chars} characters (hard limit ${m.limits.body})`;
  return `Up to ${m.limits.body} characters`;
}

async function loadOptions() {
  try {
    state.options = await api("/api/options");
    await chrome.storage.local.set({ options: state.options });
  } catch (e) {
    // Fall back to the last options we saw, so the panel is usable when the backend is briefly down.
    state.options = (await chrome.storage.local.get("options")).options || null;
    if (!state.options) setStatus($("status"), "error", e.message);
  }
  if (!state.options) return;
  const { lastMode, lastLanguage } = await chrome.storage.local.get(["lastMode", "lastLanguage"]);
  const { modes, languages } = state.options;
  state.mode = modes.some((m) => m.key === lastMode) ? lastMode : modes[0].key;
  state.language = languages.some((l) => l.key === lastLanguage) ? lastLanguage : languages[0].key;

  segmented(
    $("modeSeg"),
    modes.map((m) => ({ key: m.key, short: m.short_label || m.label, title: m.label })),
    state.mode,
    (key) => {
      state.mode = key;
      $("modeHint").textContent = modeHint(modeInfo(key));
      chrome.storage.local.set({ lastMode: key });
    }
  );
  segmented(
    $("langSeg"),
    languages.map((l) => ({ key: l.key, short: l.label, title: l.label, dir: l.rtl ? "rtl" : undefined })),
    state.language,
    (key) => {
      state.language = key;
      chrome.storage.local.set({ lastLanguage: key });
    }
  );
  $("modeHint").textContent = modeHint(modeInfo(state.mode));
  updateButtons();
}

// --- generation ---

let busy = false;

function setBusy(on, label) {
  busy = on;
  const btn = $("generate");
  btn.querySelector(".spinner").hidden = !on;
  btn.querySelector(".btn-label").textContent = label || "Generate draft";
  updateButtons();
}

async function runGeneration(payload) {
  const started = Date.now();
  const tick = () => setBusy(true, `Writing… ${Math.round((Date.now() - started) / 1000)}s`);
  tick();
  setStatus($("status"), "info", "");
  const timer = setInterval(tick, 1000);
  try {
    const res = await api("/api/generate", {
      method: "POST",
      body: {
        profile: { url: payload.url, name: payload.name, headline: payload.headline, raw_text: payload.raw_text },
        mode: state.mode,
        language: state.language,
        extra_instruction: $("extra").value,
      },
    });
    state.draft = { ...res, profileName: payload.name || payload.url, profileUrl: payload.url, sent: false };
    renderDraft();
    await persist();
    $("draftSection").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    setStatus($("status"), "error", e.message);
  } finally {
    clearInterval(timer);
    setBusy(false);
  }
}

async function onGenerate() {
  if (!state.tab || busy) return;
  let payload;
  try {
    const [injection] = await chrome.scripting.executeScript({ target: { tabId: state.tab.id }, func: extractProfile });
    payload = injection && injection.result;
  } catch (e) {
    setStatus($("status"), "error", `Couldn't read this page: ${e.message}`);
    return;
  }
  if (!payload || !payload.raw_text) {
    setStatus($("status"), "error", "The profile looks empty. Wait for the page to finish loading, then try again.");
    return;
  }
  state.payload = payload;
  await runGeneration(payload);
}

async function onRegenerate() {
  if (state.payload && !busy) await runGeneration(state.payload);
}

// --- draft view ---

function notice(kind, iconName, text) {
  const div = document.createElement("div");
  div.className = `notice ${kind}`;
  div.innerHTML = icon(iconName);
  const span = document.createElement("span");
  span.textContent = text;
  div.append(span);
  return div;
}

function renderDraft() {
  const d = state.draft;
  $("draftSection").hidden = !d;
  if (!d) return;

  const notices = [];
  for (const p of d.previous || []) {
    const when = p.sent_at ? new Date(p.sent_at + "Z").toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "?";
    const mode = (modeInfo(p.mode) || { label: p.mode }).label;
    notices.push(notice("info", "history", `You already contacted this person: ${mode} on ${when} (${p.status.replace("_", " ")}).`));
  }
  if (d.over_limit) notices.push(notice("bad", "alert", "Still over the character limit after a retry. Trim it before sending."));
  for (const w of d.warnings || []) {
    if (d.over_limit && /over the limit/i.test(w)) continue;
    notices.push(notice("", "alert", w));
  }
  $("notices").replaceChildren(...notices);

  const rtl = (languageInfo(d.language) || {}).rtl;
  for (const id of ["subject", "body"]) $(id).dir = rtl ? "rtl" : "auto";
  $("subjectWrap").hidden = d.subject === null || d.subject === undefined;
  $("subject").value = d.subject || "";
  $("body").value = d.body || "";
  $("subject").readOnly = $("body").readOnly = !!d.sent;
  $("draftBadge").hidden = !d.sent;
  $("draftBadge").textContent = "Sent";
  setStatus($("draftStatus"), "info", "");
  renderDraftFor();
  updateCounters();
}

function renderDraftFor() {
  const d = state.draft;
  if (!d) return;
  const mode = (modeInfo(d.mode) || { label: d.mode }).label;
  const other = state.tab && slugOf(d.profileUrl) !== state.tab.slug;
  const el = $("draftFor");
  el.classList.toggle("other", !!other);
  el.textContent = other
    ? `For ${d.profileName}, not this profile`
    : `${d.profileName} · ${mode} · ${d.prompt_version.name}`;
}

function overLimit() {
  const d = state.draft;
  if (!d) return [];
  return Object.entries(d.limits)
    .map(([field, limit]) => [field, count($(field).value), limit])
    .filter(([, n, limit]) => n > limit)
    .map(([field, n, limit]) => `${field} is ${n}/${limit}`);
}

function updateCounters() {
  const d = state.draft;
  if (!d) return;
  for (const field of ["subject", "body"]) {
    const limit = d.limits[field];
    if (!limit) continue;
    const n = count($(field).value);
    const target = field === "body" && (modeInfo(d.mode) || {}).target_chars;
    const scale = target || limit; // the meter fills against the soft target when there is one
    const counter = $(`${field}Count`);
    counter.textContent = target ? `${n} characters · aim for ~${target}, max ${limit}` : `${n} / ${limit}`;
    counter.classList.toggle("over", n > limit);
    $(field).classList.toggle("over", n > limit);
    const bar = $(`${field}Bar`);
    bar.style.width = `${Math.min(100, (n / scale) * 100)}%`;
    bar.classList.toggle("over", n > limit);
    bar.classList.toggle("near", n <= limit && n > scale * 0.9);
  }
  updateButtons();
}

function updateButtons() {
  $("generate").disabled = !(state.options && state.tab) || busy;
  $("regenerate").disabled = busy || !state.payload || !state.options;
  const d = state.draft;
  const over = overLimit();
  const empty = d && !$("body").value.trim();
  const btn = $("markSent");
  btn.disabled = busy || !d || d.sent || over.length > 0 || empty;
  btn.title = over.length ? `Over the limit: ${over.join(", ")}` : empty ? "The message is empty" : "Records the text as you edited it";
  btn.textContent = d && d.sent ? "Marked as sent ✓" : "Mark as sent";
  btn.classList.toggle("done", !!(d && d.sent));
}

async function copy(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    toast(`${label} copied — paste it into LinkedIn`);
  } catch (e) {
    setStatus($("draftStatus"), "error", `Copy failed: ${e.message}`);
  }
}

async function onMarkSent() {
  const d = state.draft;
  if (!d || d.sent) return;
  const body = { final_text: $("body").value };
  if (d.limits.subject) body.final_subject = $("subject").value;
  $("markSent").disabled = true;
  try {
    await api(`/api/generations/${d.generation_id}/sent`, { method: "POST", body });
    d.sent = true;
    d.subject = d.limits.subject ? $("subject").value : d.subject;
    d.body = $("body").value;
    renderDraft();
    await persist();
    toast("Saved to your history");
  } catch (e) {
    setStatus($("draftStatus"), "error", e.message);
    updateButtons();
  }
}

// --- settings ---

async function loadSettingsForm() {
  const s = await getSettings();
  $("backendUrl").value = s.backendUrl;
  $("token").value = s.token;
  if (!s.token) $("settings").open = true;
}

async function storeSettingsForm() {
  await saveSettings({ backendUrl: $("backendUrl").value || DEFAULT_BACKEND, token: $("token").value });
}

async function onSaveSettings() {
  await storeSettingsForm();
  setStatus($("settingsStatus"), "ok", "Saved.");
  await loadOptions();
}

async function onTestConnection() {
  await storeSettingsForm();
  setStatus($("settingsStatus"), "info", "Testing…");
  try {
    const r = await api("/api/me");
    setStatus($("settingsStatus"), "ok", `Connected. LLM model: ${r.model}.`);
    await loadOptions();
  } catch (e) {
    setStatus($("settingsStatus"), "error", e.message);
  }
}

// --- wiring ---

async function init() {
  $("generate").addEventListener("click", onGenerate);
  $("regenerate").addEventListener("click", onRegenerate);
  $("copy").addEventListener("click", () => copy($("body").value, "Message"));
  $("copySubject").addEventListener("click", () => copy($("subject").value, "Subject"));
  $("markSent").addEventListener("click", onMarkSent);
  $("saveSettings").addEventListener("click", onSaveSettings);
  $("testConnection").addEventListener("click", onTestConnection);
  for (const id of ["subject", "body"]) {
    $(id).addEventListener("input", () => {
      updateCounters();
      persistSoon();
    });
  }
  $("themeBtn").addEventListener("click", () => {
    const t = window.panelTheme.get();
    window.panelTheme.set(THEME_ORDER[(THEME_ORDER.indexOf(t) + 1) % THEME_ORDER.length]);
  });
  document.querySelectorAll("[data-theme-choice]").forEach((b) =>
    b.addEventListener("click", () => window.panelTheme.set(b.dataset.themeChoice))
  );
  document.addEventListener("themechange", renderTheme);
  renderTheme();

  // Follow the active tab's URL (LinkedIn is a single-page app, so the URL changes in place).
  chrome.tabs.onActivated.addListener(refreshTab);
  chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
    if (tab.active && (info.url || info.title || info.status === "complete")) refreshTab();
  });

  await loadSettingsForm();
  const saved = await chrome.storage.session.get(["payload", "draft"]);
  state.payload = saved.payload || null;
  state.draft = saved.draft || null;
  await loadOptions();
  await refreshTab();
  renderDraft();
}

init();
