/* Adapted from veloren_observer_dashboard.js: sequential state polling,
   token-protected recording actions, textContent-only display and registered URLs. */
"use strict";
const el = id => document.getElementById(id);
let token = null, state = null, connected = false, busy = false, history = false;
let shown = null, shownKey = null, sequence = null, offset = 0, total = 0;
let lastPoll = 0, liveDeadline = 0, imagePending = false;
const text = (id, value) => {el(id).textContent = value;};
async function api(path, options = {}) {
  const response = await fetch(path, {cache:"no-store", ...options, signal:AbortSignal.timeout(10000)});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
  return value;
}
function link(id, url) {
  const node = el(id);
  const valid = typeof url === "string" && /^\/download\/[0-9a-f]{32}$/.test(url);
  node.hidden = !valid;
  if (valid) node.href = url; else node.removeAttribute("href");
}
function buttons() {
  const rec = state?.recording?.state;
  el("start").disabled = busy || !connected || state?.mode !== "live" ||
    state?.feed_state !== "live" || ["recording","encoding"].includes(rec);
  el("stop").disabled = busy || !connected || rec !== "recording";
  el("screenshot").disabled = busy || !connected || !shownKey;
}
function hideLive() {
  if (!history) {
    el("preview").hidden = true;
    el("preview").removeAttribute("src");
    sequence = null; shown = null; shownKey = null;
    text("display", "No fresh live image. Browse saved frames to inspect historical pixels.");
  }
}
function display(row, key, url, label) {
  shown = row; shownKey = key;
  text("display", label);
  text("provenance", JSON.stringify(row, null, 2));
  const image = el("preview");
  imagePending = true;
  image.src = url;
  image.hidden = false;
}
function render(value) {
  state = value;
  text("feed", `${value.mode.toUpperCase()} source · ${value.feed_state} · capture age ` +
    `${value.age_seconds == null ? "unknown" : value.age_seconds.toFixed(1) + "s"} · ` +
    `${value.capture_mode || rowOrigin(value.frame)} · ${value.error || value.run.error || ""}`);
  text("identity", `Run ${value.run.run_id} · task ${value.run.task_id} · terrain ${value.run.terrain}`);
  const row = value.frame;
  // Live pixels are hidden as soon as freshness fails; history is always labelled saved.
  liveDeadline = performance.now() + Math.max(0, value.fresh_for_seconds ?? 0) * 1000;
  if (!history && value.mode === "live" && value.feed_state !== "live") hideLive();
  else if (!history && row && sequence !== value.sequence && !imagePending) {
    sequence = value.sequence;
    // Receipt keys are server-derived, never event-supplied URLs.
    const key = value.frame_key;
    if (/^[0-9a-f]{64}$/.test(key)) display(row, key, `/preview/${key}.png`,
      `${value.mode === "live" ? "Latest received" : "Saved source"} ${rowOrigin(row)} sample ` +
      `${row.frame_id} · session ${row.session_id} · native frame ${row.native_frame_id ?? "NONE"}`);
  }
  function rowOrigin(row) {return row?.capture_origin?.kind || "agent-requested-native-frame";}
  text("recording", JSON.stringify(value.recording));
  link("download", value.recording.download); link("metadata", value.recording.metadata);
  buttons();
}
async function action(path, body = {}) {
  busy = true; buttons(); text("error", "");
  try {
    const value = await api(path, {method:"POST",
      headers:{"Content-Type":"application/json", "X-Observer-Token":token}, body:JSON.stringify(body)});
    if (path === "/api/screenshot") {
      link("shotDownload", value.download); link("shotMetadata", value.metadata);
    }
  } catch (error) {
    text("error", `${error.message}. A timeout does not prove the server did not execute the action; check state.`);
  } finally {busy = false; buttons();}
}
async function page() {
  try {
    const value = await api(`/api/frames/${offset}`); total = value.total;
    const select = el("frames"); select.replaceChildren();
    for (const item of value.frames) {
      const option = document.createElement("option");
      option.value = item.key; option.textContent = `${item.frame.frame_id} · ${item.frame.session_id}`;
      option.row = item.frame; select.append(option);
    }
    for (const id of ["frames","previous","next"]) el(id).hidden = false;
    el("previous").disabled = offset === 0; el("next").disabled = offset + 100 >= total;
    select.dispatchEvent(new Event("change"));
  } catch (error) {text("error", error.message);}
}
el("frames").addEventListener("change", () => {
  const option = el("frames").selectedOptions[0];
  if (option) display(option.row, option.value, `/frame/${option.value}.png`,
    `SAVED frame ${option.row.frame_id} · session ${option.row.session_id} · not live`);
  buttons();
});
el("history").onclick = () => {history = true; page();};
el("previous").onclick = () => {offset = Math.max(0, offset - 100); page();};
el("next").onclick = () => {offset += 100; page();};
el("latest").onclick = () => {
  history = false; sequence = null;
  for (const id of ["frames","previous","next"]) el(id).hidden = true;
  if (state) render(state);
};
el("start").onclick = () => action("/api/record/start");
el("stop").onclick = () => action("/api/record/stop");
el("screenshot").onclick = () => action("/api/screenshot", {frame:shownKey});
el("preview").onload = () => {imagePending = false;};
el("preview").onerror = () => {
  imagePending = false; sequence = null; el("preview").hidden = true;
  text("error", "Frame unavailable/invalid; no replacement or fallback image.");
};
setInterval(() => {
  if (performance.now() - lastPoll > 2500) {
    connected = false; text("connection", "Connection/state stale — no live claim"); hideLive(); buttons();
  }
  if (!history && state?.mode === "live" && performance.now() >= liveDeadline) hideLive();
}, 200);
async function poll() {
  try {
    if (!token) token = (await api("/api/bootstrap")).token;
    const value = await api("/api/state");
    connected = true; lastPoll = performance.now();
    text("connection", "Connected to read-only selected source"); render(value);
  } catch (error) {
    connected = false; token = null; hideLive();
    text("connection", `Disconnected/stale: ${error.message}`); buttons();
  } finally {setTimeout(poll, 500);}
}
poll();
