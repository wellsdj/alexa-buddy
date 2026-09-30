// Buddy console. The page joins the server exactly like the ESP32 does (same WebSocket
// protocol), so everything you test here is the real pipeline.
"use strict";

const $ = (id) => document.getElementById(id);
const S = {
  ws: null, token: localStorage.getItem("buddyToken") || "", sessionId: null, state: "offline",
  handsFree: false, ptt: false, retry: 0, settings: {}, status: {}, ringing: null, meterSource: "me",
  sessions: [],
};
const STATE_TEXT = {
  offline: ["Offline", "Can't reach the Buddy server. It retries automatically."],
  idle: ["Ready", "Say “hey buddy” with hands-free on, hold the talk button, or type below."],
  listening: ["Listening…", "Go ahead, I'm listening."],
  thinking: ["Thinking…", "Working on it."],
  speaking: ["Speaking", "Click the orb or press Stop to interrupt."],
  ringing: ["Ringing!", "Stop or snooze below, or say “hey buddy, stop”."],
};

// ---------------------------------------------------------------- helpers
function toast(msg, ms = 2600) {
  const t = $("toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), ms);
}
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null) e.append(k.nodeType ? k : document.createTextNode(k));
  return e;
}
async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(S.token ? { "X-Buddy-Token": S.token } : {}) };
  const res = await fetch(path, { ...opts, headers: { ...headers, ...(opts.headers || {}) },
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body });
  if (res.status === 401) { askToken(); throw new Error("token required"); }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
    throw new Error(detail);
  }
  const type = res.headers.get("content-type") || "";
  return type.includes("json") ? res.json() : res;
}
function askToken() {
  const t = prompt("This Buddy server needs its access token:");
  if (t) { S.token = t.trim(); localStorage.setItem("buddyToken", S.token); location.reload(); }
}
const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

// ---------------------------------------------------------------- audio out
const Audio_ = {
  ctx: null, gain: null, playhead: 0, sources: [], seq: -1, endTimer: null, volume: 0.8,
  ensure() {
    if (!this.ctx) {
      this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      this.gain = this.ctx.createGain();
      this.gain.gain.value = this.volume;
      this.gain.connect(this.ctx.destination);
    }
    if (this.ctx.state === "suspended") this.ctx.resume();
    return this.ctx;
  },
  begin(seq) { this.stop(); this.seq = seq; this.ensure(); this.playhead = this.ctx.currentTime + 0.08; },
  push(seq, pcm) {
    if (seq !== this.seq || !pcm.length) return;
    const ctx = this.ensure();
    const buf = ctx.createBuffer(1, pcm.length, 16000);
    const ch = buf.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.gain);
    const at = Math.max(ctx.currentTime + 0.03, this.playhead);
    src.start(at);
    this.playhead = at + buf.duration;
    this.sources.push(src);
    src.onended = () => { this.sources = this.sources.filter((s) => s !== src); };
  },
  end(seq) {
    if (seq !== this.seq) return;
    const wait = Math.max(0, (this.playhead - (this.ctx ? this.ctx.currentTime : 0)) * 1000) + 120;
    clearTimeout(this.endTimer);
    this.endTimer = setTimeout(() => send({ type: "playback_done", seq }), wait);
  },
  stop() {
    clearTimeout(this.endTimer);
    for (const s of this.sources) { try { s.stop(); } catch { /* already stopped */ } }
    this.sources = [];
    if (this.ctx) this.playhead = this.ctx.currentTime;
    speechSynthesis.cancel();
  },
  setVolume(level) { this.volume = Math.max(0, Math.min(10, level)) / 10; if (this.gain) this.gain.gain.value = this.volume; },
};

// Little synthesized sounds, same idea as the ESP32's (no audio files needed).
function earcon(name) {
  const ctx = Audio_.ensure();
  const notes = {
    wake: [[660, 0, 0.09], [990, 0.1, 0.14]],
    end: [[880, 0, 0.07], [660, 0.08, 0.1]],
    nospeech: [[520, 0, 0.12], [390, 0.13, 0.18]],
    error: [[220, 0, 0.12], [220, 0.18, 0.12]],
    ring: [[784, 0, 0.16], [988, 0.2, 0.16], [1175, 0.4, 0.3]],
    chime: [[988, 0, 0.14], [1319, 0.16, 0.34]],
  }[name] || [];
  const t0 = ctx.currentTime + 0.01;
  for (const [f, at, dur] of notes) {
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.type = "sine"; o.frequency.value = f;
    g.gain.setValueAtTime(0.0001, t0 + at);
    g.gain.exponentialRampToValueAtTime(0.25, t0 + at + 0.015);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + at + dur);
    o.connect(g).connect(Audio_.gain);
    o.start(t0 + at); o.stop(t0 + at + dur + 0.02);
  }
}

// ---------------------------------------------------------------- mic in
const Mic = {
  ctx: null, stream: null, node: null, starting: null,
  async start() {
    if (this.node) return;
    if (this.starting) return this.starting;
    this.starting = (async () => {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error("This browser can't use the microphone here (it needs https or localhost).");
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      await this.ctx.audioWorklet.addModule("/static/mic-worklet.js");
      const src = this.ctx.createMediaStreamSource(this.stream);
      this.node = new AudioWorkletNode(this.ctx, "buddy-mic");
      const mute = this.ctx.createGain(); mute.gain.value = 0;
      src.connect(this.node).connect(mute).connect(this.ctx.destination);
      this.node.port.onmessage = (e) => {
        if (S.ws?.readyState === 1 && (S.handsFree || S.ptt || S.state === "listening")) S.ws.send(e.data);
      };
    })();
    try { await this.starting; } finally { this.starting = null; }
  },
  stop() {
    if (!this.node) return;
    this.stream.getTracks().forEach((t) => t.stop());
    this.ctx.close();
    this.node = this.ctx = this.stream = null;
  },
};

// ---------------------------------------------------------------- connection
function send(obj) { if (S.ws?.readyState === 1) S.ws.send(JSON.stringify(obj)); }

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const q = new URLSearchParams({ role: "ui", name: "browser" });
  if (S.token) q.set("token", S.token);
  const ws = new WebSocket(`${proto}://${location.host}/ws?${q}`);
  ws.binaryType = "arraybuffer";
  S.ws = ws;
  ws.onopen = () => {
    S.retry = 0;
    $("pill-server").dataset.ok = "true";
    if (S.handsFree) send({ type: "audio_mode", on: true });
    setState("idle");
    refreshAll();
  };
  ws.onclose = (e) => {
    $("pill-server").dataset.ok = "false";
    setState("offline");
    if (e.code === 4401) { askToken(); return; }
    const wait = Math.min(15000, 500 * 2 ** S.retry++);
    setTimeout(connect, wait);
  };
  ws.onmessage = (e) => {
    if (typeof e.data !== "string") {
      const bytes = new Uint8Array(e.data);
      const pcm = new Int16Array(e.data.slice(1, 1 + ((bytes.length - 1) & ~1)));
      Audio_.push(bytes[0], pcm);
      return;
    }
    onMessage(JSON.parse(e.data));
  };
}

function onMessage(m) {
  switch (m.type) {
    case "hello_ack":
      S.sessionId = m.session;
      Audio_.setVolume(m.volume ?? 6);
      $("assistant-name").textContent = m.name || "Buddy";
      break;
    case "state": setState(S.ringing ? "ringing" : m.state); break;
    case "earcon": earcon(m.name); break;
    case "audio_start": Audio_.begin(m.seq); break;
    case "audio_end": Audio_.end(m.seq); break;
    case "stop_audio": Audio_.stop(); break;
    case "say": {
      // No voice engine available on the server: use the browser's own voice.
      const u = new SpeechSynthesisUtterance(m.text);
      u.lang = "en-GB";
      u.onend = u.onerror = () => send({ type: "playback_done", seq: m.seq });
      speechSynthesis.speak(u);
      break;
    }
    case "volume": Audio_.setVolume(m.level); break;
    case "alarm_fire": startRinging(m); break;
    case "alarm_stop": stopRinging(m.quiet ? "snooze" : "voice"); break;
    case "event": onEvent(m); break;
  }
}

function onEvent(m) {
  switch (m.kind) {
    case "level":
      if ((S.meterSource === "me" && m.session === S.sessionId) || m.session === S.meterSource) showLevel(m.db, m.wake);
      break;
    case "transcript": addChat(m); break;
    case "log": addLog(m.entry); if (m.entry.event === "turn") loadStatusSoon(); break;
    case "alarms": loadAlarms(); break;
    case "memory": loadMemory(); break;
    case "settings": loadSettings(); break;
    case "sessions": case "device": case "state": loadStatusSoon(); break;
    case "ringing": loadStatusSoon(); break;
  }
}

// ---------------------------------------------------------------- talk panel
function setState(state) {
  S.state = state;
  $("orb").dataset.state = state;
  const [label, hint] = STATE_TEXT[state] || STATE_TEXT.idle;
  $("state-label").textContent = label;
  $("state-hint").textContent = hint;
}

function showLevel(db, wake) {
  const pct = Math.max(0, Math.min(100, ((db + 70) / 60) * 100));
  $("level-bar").style.width = pct + "%";
  $("level-val").textContent = db <= -119 ? "–" : `${Math.round(db)} dB`;
  $("wake-bar").style.width = Math.round((wake || 0) * 100) + "%";
  $("wake-val").textContent = (wake || 0).toFixed(2);
}

function addChat(m) {
  const chat = $("chat");
  chat.querySelector(".empty")?.remove();
  if (!m.text) return;
  const meta = m.role === "assistant" && (m.model || m.intent)
    ? el("span", { class: "meta" }, m.model ? m.model.replace("openai/", "") : `instant · ${m.intent}`) : null;
  const li = el("li", { class: `${m.role}${m.session && m.session !== S.sessionId ? " from-device" : ""}` }, m.text, meta);
  chat.append(li);
  while (chat.children.length > 120) chat.firstChild.remove();
  chat.scrollTop = chat.scrollHeight;
}

async function setHandsFree(on) {
  try {
    if (on) await Mic.start();
    S.handsFree = on;
    $("handsfree").setAttribute("aria-pressed", String(on));
    send({ type: "audio_mode", on });
    if (!on && !S.ptt) Mic.stop();
    toast(on ? "Hands-free on: say “hey buddy”." : "Hands-free off.");
  } catch (err) { toast("Microphone: " + err.message, 5000); }
}

async function pttDown() {
  if (S.ptt) return;
  Audio_.ensure();
  try { await Mic.start(); } catch (err) { toast("Microphone: " + err.message, 5000); return; }
  S.ptt = true;
  $("ptt").classList.add("active");
  send({ type: "ptt_start" });
}
function pttUp() {
  if (!S.ptt) return;
  S.ptt = false;
  $("ptt").classList.remove("active");
  send({ type: "ptt_end" });
  if (!S.handsFree) setTimeout(() => { if (!S.ptt && !S.handsFree) Mic.stop(); }, 1500);
}

// ---------------------------------------------------------------- ringing
function startRinging(m) {
  S.ringing = m.id;
  $("ring-text").textContent = m.kind === "timer" ? `Timer${m.label ? ": " + m.label : ""} is done`
    : m.kind === "reminder" ? `Reminder: ${m.label}` : `Alarm${m.label ? ": " + m.label : ""}`;
  $("ring-banner").hidden = false;
  setState("ringing");
  if (m.kind === "reminder") { earcon("chime"); setTimeout(() => stopRinging(null), 8000); return; }
  earcon("ring");
  clearInterval(S.ringTimer);
  S.ringTimer = setInterval(() => earcon("ring"), 1400);
  clearTimeout(S.ringGiveUp);
  S.ringGiveUp = setTimeout(() => stopRinging("timeout"), 10 * 60 * 1000);
}
function stopRinging(reason) {
  clearInterval(S.ringTimer);
  clearTimeout(S.ringGiveUp);
  $("ring-banner").hidden = true;
  if (S.ringing && reason) send({ type: "alarm_stopped", id: S.ringing, reason });
  S.ringing = null;
  if (S.state === "ringing") setState("idle");
}

// ---------------------------------------------------------------- alarms panel
async function loadAlarms() {
  let rows;
  try { rows = await api("/api/alarms"); } catch { return; }
  const list = $("alarm-list");
  list.replaceChildren();
  if (!rows.length) list.append(el("li", {}, el("span", { class: "note" }, "Nothing set. Try “hey buddy, wake me up at half seven on weekdays”.")));
  for (const a of rows) {
    const sw = el("button", { class: "switch", role: "switch", "aria-checked": String(a.enabled), "aria-label": "Enabled",
      onclick: async () => { await api(`/api/alarms/${a.id}`, { method: "PATCH", body: { enabled: !a.enabled } }); } });
    list.append(el("li", {},
      el("span", { class: "kind" }, a.kind),
      el("div", { class: "main" }, el("div", { class: "title" }, a.description[0].toUpperCase() + a.description.slice(1)),
        el("div", { class: "sub" }, a.next_local ? `next: ${a.next_local}` : "not scheduled")),
      a.kind === "timer" ? null : sw,
      el("button", { class: "icon-btn", title: "Delete", "aria-label": "Delete",
        onclick: async () => { await api(`/api/alarms/${a.id}`, { method: "DELETE" }); } }, "✕")));
  }
}
function alarmFormMode() {
  const k = $("alarm-kind").value;
  $("alarm-minutes").hidden = k !== "timer";
  $("alarm-time").hidden = k === "timer";
  $("alarm-repeat").hidden = k === "timer";
  $("alarm-label").placeholder = k === "reminder" ? "What to remind you (required)" : "Label (optional)";
}

// ---------------------------------------------------------------- memory panel
async function loadMemory() {
  let data;
  try { data = await api("/api/memory"); } catch { return; }
  $("memory-status").textContent = `${data.items.length} memories${data.status ? " · " + data.status : ""}`;
  const list = $("memory-list");
  list.replaceChildren();
  if (!data.items.length) list.append(el("li", {}, el("span", { class: "note" }, "Empty. Say “remember that…” or add one above. Buddy also learns from conversations.")));
  for (const m of data.items) {
    const text = el("span", { class: "text" }, m.text);
    const edit = el("button", { class: "icon-btn", title: "Edit", onclick: () => {
      text.contentEditable = "true"; text.focus();
      const save = async () => {
        text.contentEditable = "false";
        const t = text.textContent.trim();
        if (t && t !== m.text) await api(`/api/memory/${m.id}`, { method: "PATCH", body: { text: t } });
      };
      text.onblur = save;
      text.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); text.blur(); } };
    } }, "Edit");
    list.append(el("li", {},
      el("span", { class: "kind" }, `#${m.id}`),
      el("div", { class: "main" }, text, el("div", { class: "sub" }, `${m.category.replace("_", " ")} · ${m.source}`)),
      el("button", { class: "icon-btn pin", "aria-pressed": String(m.pinned), title: m.pinned ? "Pinned: never removed automatically" : "Pin",
        onclick: async () => { await api(`/api/memory/${m.id}`, { method: "PATCH", body: { pinned: !m.pinned } }); } }, "Pin"),
      edit,
      el("button", { class: "icon-btn", title: "Forget", onclick: async () => { await api(`/api/memory/${m.id}`, { method: "DELETE" }); } }, "✕")));
  }
}

// ---------------------------------------------------------------- weather panel
async function loadWeather() {
  let w;
  try { w = await api("/api/weather"); } catch (err) { $("weather-spoken").textContent = "Weather unavailable: " + err.message; return; }
  $("weather-spoken").textContent = w.spoken;
  $("weather-place").textContent = w.place;
  const days = $("weather-days");
  days.replaceChildren();
  w.daily.time.forEach((d, i) => {
    const name = i === 0 ? "Today" : new Date(d + "T12:00").toLocaleDateString([], { weekday: "short" });
    days.append(el("div", { class: "day" }, el("b", {}, name),
      el("div", { class: "hi" }, `${Math.round(w.daily.temperature_2m_max[i])}°`),
      el("div", { class: "lo" }, `${Math.round(w.daily.temperature_2m_min[i])}°`),
      el("div", { class: "pr" }, `${w.daily.precipitation_probability_max[i] ?? 0}%`)));
  });
  const rain = $("weather-rain");
  rain.replaceChildren();
  const nowHour = new Date().getHours();
  const probs = (w.hourly.precipitation_probability || []).slice(nowHour, nowHour + 24);
  probs.forEach((p, i) => rain.append(el("i", { style: `height:${Math.max(2, p)}%`, title: `${(nowHour + i) % 24}:00 · ${p}%` })));
}

// ---------------------------------------------------------------- settings panel
async function loadSettings() {
  let data;
  try { data = await api("/api/settings"); } catch { return; }
  S.settings = data.settings;
  for (const input of document.querySelectorAll("[data-setting]")) {
    const v = S.settings[input.dataset.setting];
    if (document.activeElement === input) continue;
    if (input.type === "checkbox") input.checked = !!v;
    else input.value = Array.isArray(v) ? v.join(", ") : v ?? "";
  }
  $("thr-out").textContent = Number(S.settings.wake_threshold).toFixed(2);
  $("vthr-out").textContent = Number(S.settings.wake_verify_threshold).toFixed(2);
  $("speed-out").textContent = Number(S.settings.voice_speed).toFixed(2) + "×";
  $("wake-thr").style.left = S.settings.wake_threshold * 100 + "%";
  $("place-current").textContent = `Currently: ${S.settings.location_name} (${S.settings.latitude}, ${S.settings.longitude}, ${S.settings.timezone})`;
  showKeys(data.keys);
}
function showKeys(keys) {
  const k = (name, id) => {
    const s = keys[name];
    $(id).textContent = s.set ? `set (${s.source})` : "missing";
    $(id).className = "keystate " + (s.set ? "ok" : "missing");
  };
  k("GROQ_API_KEY", "key-groq");
  k("ELEVENLABS_API_KEY", "key-eleven");
  let skipEleven = false;
  try { skipEleven = localStorage.getItem("buddy-skip-eleven") === "1"; } catch {}
  $("setup-groq").hidden = keys.GROQ_API_KEY.set;
  $("setup-eleven").hidden = keys.ELEVENLABS_API_KEY.set || skipEleven;
  $("setup-box").hidden = $("setup-groq").hidden && $("setup-eleven").hidden;
  S.lastKeys = keys;
  const pill = $("pill-keys");
  pill.dataset.ok = keys.GROQ_API_KEY.set && keys.ELEVENLABS_API_KEY.set ? "true" : keys.GROQ_API_KEY.set ? "warn" : "false";
  pill.title = keys.GROQ_API_KEY.set ? (keys.ELEVENLABS_API_KEY.set ? "Both keys set" : "No ElevenLabs key: offline voice will be used") : "Groq key missing";
}
async function saveSetting(input) {
  const key = input.dataset.setting;
  const value = input.type === "checkbox" ? input.checked : input.value;
  try {
    await api("/api/settings", { method: "POST", body: { [key]: value } });
    toast("Saved");
  } catch (err) { toast("Couldn't save: " + err.message); }
}

// ---------------------------------------------------------------- activity panel
let statusTimer = null;
function loadStatusSoon() { clearTimeout(statusTimer); statusTimer = setTimeout(loadStatus, 250); }
async function loadStatus() {
  let st;
  try { st = await api("/api/status"); } catch { return; }
  S.status = st;
  S.sessions = st.sessions;
  const devices = st.sessions.filter((s) => s.role === "device");
  const pill = $("pill-device");
  pill.dataset.ok = devices.length ? "true" : "false";
  pill.textContent = devices.length ? `Desk unit · ${devices[0].state}` : "Desk unit";
  const sel = $("meter-source");
  const cur = sel.value;
  sel.replaceChildren(el("option", { value: "me" }, "this browser"),
    ...devices.map((d) => el("option", { value: d.id }, `desk unit (${d.name})`)));
  sel.value = [...sel.options].some((o) => o.value === cur) ? cur : "me";
  showKeys(st.keys);
  const dl = $("model-list");
  dl.replaceChildren(...(st.llm.available || []).map((m) => el("option", { value: m })));
  $("model-note").textContent = st.llm.available
    ? `Your key can use: ${st.llm.available.filter((m) => !/whisper|guard|tts|orpheus/.test(m)).join(", ")}`
    : "Add a Groq key to see which models your account can use.";
  const q = st.tts.elevenlabs.quota;
  $("eleven-quota").textContent = q && q.limit ? `ElevenLabs credits used this month: ${q.used.toLocaleString()} of ${q.limit.toLocaleString()} (${q.tier})` : "";
  const cool = Object.entries(st.llm.cooldowns || {}).map(([m, s]) => `${m.replace("openai/", "")} ${s}s`).join(", ");
  const stats = [
    ["Everyday model", (st.llm.fast || [])[0] || "none available"],
    ["Big-task model", (st.llm.smart || [])[0] || "none available"],
    ["Rate-limited", cool || "none"],
    ["Voice", st.tts.last_engine || (st.tts.elevenlabs.available ? "ElevenLabs" : st.tts.piper.available ? "Piper" : "browser only")],
    ["Memory", `${st.memory.count} items`],
    ["Connected", st.sessions.map((s) => `${s.role}:${s.state}`).join(", ") || "–"],
  ];
  if (devices[0]?.info?.rssi) stats.push(["Desk unit Wi-Fi", `${devices[0].info.rssi} dBm · heap ${Math.round((devices[0].info.heap || 0) / 1024)} KB`]);
  if (st.llm.last_error) stats.push(["Last model error", st.llm.last_error.slice(0, 140)]);
  if (st.weather_error) stats.push(["Weather error", st.weather_error.slice(0, 140)]);
  $("status-grid").replaceChildren(...stats.map(([k, v]) => el("div", { class: "stat" }, el("b", {}, k), el("span", {}, v))));
}
function addLog(e) {
  const log = $("log");
  let text = "";
  if (e.event === "turn") text = `“${e.heard}” → “${(e.said || "").slice(0, 120)}” [${e.model || e.intent}${e.stt_ms ? `, stt ${e.stt_ms}ms` : ""}, brain ${e.brain_ms}ms]`;
  else if (e.event === "wake_rejected" || e.event === "wake_verified") text = `heard “${e.heard}”`;
  else if (e.event === "error") text = `${e.where}: ${e.error}`;
  else if (e.event === "memory_learning") text = e.status || JSON.stringify(e.result);
  else text = JSON.stringify(Object.fromEntries(Object.entries(e).filter(([k]) => !["t", "event"].includes(k))));
  log.prepend(el("li", {}, el("span", { class: "t" }, fmtTime(e.t)), el("span", { class: `ev ${e.event === "error" ? "error" : ""}` }, e.event), text));
  while (log.children.length > 200) log.lastChild.remove();
}
async function loadLog() {
  try { (await api("/api/log")).forEach(addLog); } catch { /* ignore */ }
}

function refreshAll() { loadSettings(); loadStatus(); loadAlarms(); loadMemory(); loadWeather(); }

// ---------------------------------------------------------------- wiring
function init() {
  $("chat").append(el("li", { class: "empty" }, "Nothing yet. Try: “what's the weather?”, “set a timer for 5 minutes”, “remember that I take my coffee black”."));
  document.addEventListener("pointerdown", () => Audio_.ensure(), { once: true });
  $("orb").onclick = () => { send({ type: "stop_speaking" }); Audio_.stop(); };
  $("stop").onclick = () => { send({ type: "stop_speaking" }); Audio_.stop(); };
  $("handsfree").onclick = () => setHandsFree(!S.handsFree);
  const ptt = $("ptt");
  ptt.addEventListener("pointerdown", (e) => { e.preventDefault(); pttDown(); });
  ptt.addEventListener("pointerup", pttUp);
  ptt.addEventListener("pointerleave", pttUp);
  document.addEventListener("keydown", (e) => {
    if (e.code === "Space" && !e.repeat && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName) && !document.activeElement.isContentEditable) { e.preventDefault(); pttDown(); }
  });
  document.addEventListener("keyup", (e) => { if (e.code === "Space") pttUp(); });
  $("ask-form").onsubmit = (e) => {
    e.preventDefault();
    const t = $("ask").value.trim();
    if (!t) return;
    Audio_.ensure();
    send({ type: "text", text: t });
    $("ask").value = "";
  };
  $("ring-stop").onclick = () => stopRinging("button");
  $("ring-snooze").onclick = () => { send({ type: "text", text: "snooze" }); };

  for (const tab of document.querySelectorAll("[role=tab]")) {
    tab.onclick = () => {
      document.querySelectorAll("[role=tab]").forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
      document.querySelectorAll(".panel").forEach((p) => (p.hidden = p.id !== "panel-" + tab.dataset.tab));
      if (tab.dataset.tab === "weather") loadWeather();
      if (tab.dataset.tab === "activity") loadStatus();
    };
  }
  $("meter-source").onchange = (e) => (S.meterSource = e.target.value);

  $("alarm-kind").onchange = alarmFormMode;
  $("alarm-form").onsubmit = async (e) => {
    e.preventDefault();
    const kind = $("alarm-kind").value;
    const body = { kind, label: $("alarm-label").value.trim() };
    if (kind === "timer") body.seconds = Number($("alarm-minutes").value) * 60;
    else { body.time = $("alarm-time").value; body.repeat = $("alarm-repeat").value; }
    if (kind === "reminder" && !body.label) { toast("A reminder needs something to remind you about."); return; }
    try {
      const r = await api("/api/alarms", { method: "POST", body });
      $("alarm-note").textContent = r.confirm;
      $("alarm-label").value = "";
    } catch (err) { toast(err.message); }
  };

  $("memory-form").onsubmit = async (e) => {
    e.preventDefault();
    const text = $("memory-text").value.trim();
    if (!text) return;
    const r = await api("/api/memory", { method: "POST", body: { text } });
    $("memory-text").value = "";
    toast(r.created ? "Remembered." : "Updated an existing memory.");
  };
  $("memory-compact").onclick = async () => {
    toast("Tidying up memory…");
    const r = await api("/api/memory/compact", { method: "POST" });
    toast(r.error ? `Not changed: ${r.error}` : r.skipped ? r.skipped : `Tidied: ${r.before} → ${r.after}`, 4000);
  };
  $("memory-learn").onclick = async () => {
    const r = await api("/api/memory/learn-now", { method: "POST" });
    toast(r.skipped ? "Nothing new to learn yet." : r.error ? r.error : `Learned ${r.added.length}, updated ${r.updated.length}.`, 4000);
  };

  for (const input of document.querySelectorAll("[data-setting]")) {
    input.addEventListener("change", () => saveSetting(input));
    if (input.type === "range") input.addEventListener("input", () => {
      const out = input.nextElementSibling;
      if (out?.tagName === "OUTPUT") out.textContent = Number(input.value).toFixed(2);
    });
  }
  for (const b of document.querySelectorAll("[data-save-key]")) {
    b.onclick = async (e) => {
      e.preventDefault();
      const input = $(b.dataset.input);
      try {
        const r = await api("/api/secrets", { method: "POST", body: { name: b.dataset.saveKey, value: input.value.trim() } });
        input.value = "";
        showKeys(r.keys);
        toast(r.check?.message || "Key saved.");
        loadStatus();
      } catch (err) { toast(err.message); }
    };
  }
  $("setup-skip-eleven").onclick = (e) => {
    e.preventDefault();
    try { localStorage.setItem("buddy-skip-eleven", "1"); } catch {}
    if (S.lastKeys) showKeys(S.lastKeys);
  };
  $("place-go").onclick = async (e) => {
    e.preventDefault();
    const q = $("place-search").value.trim();
    if (!q) return;
    const list = $("places");
    list.replaceChildren(el("li", { class: "note" }, "Searching…"));
    try {
      const results = await api("/api/geocode?q=" + encodeURIComponent(q));
      list.replaceChildren(...results.map((r) => el("li", {}, el("button", { class: "btn small ghost", onclick: async () => {
        await api("/api/settings", { method: "POST", body: { location_name: r.label, latitude: r.latitude, longitude: r.longitude, timezone: r.timezone } });
        list.replaceChildren();
        toast("Home set to " + r.label);
        loadWeather();
      } }, r.label))));
      if (!results.length) list.replaceChildren(el("li", { class: "note" }, "No matches."));
    } catch (err) { list.replaceChildren(el("li", { class: "note" }, err.message)); }
  };
  $("voice-preview").onclick = async () => {
    try {
      const res = await api("/api/tts/preview", { method: "POST", body: { text: `Hello${S.settings.user_name ? " " + S.settings.user_name : ""}! I'm ${S.settings.assistant_name}. This is how I sound.` } });
      $("voice-engine").textContent = "engine: " + (res.headers.get("X-Engine") || "?");
      const url = URL.createObjectURL(await res.blob());
      new Audio(url).play();
    } catch (err) { toast("Voice preview failed: " + err.message, 4000); }
  };

  alarmFormMode();
  loadLog();
  connect();
  setInterval(loadStatus, 15000);
  setInterval(loadAlarms, 30000);
}
init();
