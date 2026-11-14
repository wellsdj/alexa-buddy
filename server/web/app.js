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
  idle: ["Ready", "Hold the mic or type."],
  listening: ["Listening…", "Go ahead, I'm listening."],
  thinking: ["Thinking…", "Working on it."],
  speaking: ["Speaking", "Press stop to interrupt."],
  ringing: ["Ringing", "Stop or snooze above, or say “hey buddy, stop”."],
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
        if (S.ws?.readyState === 1 && (S.handsFree || S.ptt || S.micTest || S.state === "listening")) S.ws.send(e.data);
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
    case "mic_test": micTestEvent(m); break;
    case "voice_enroll": vidEvent(m); break;
    case "log": addLog(m.entry); if (m.entry.event === "turn") loadStatusSoon();
      if (m.entry.event === "timing" && !$("panel-speed").hidden) loadSpeed(); break;
    case "alarms": loadAlarms(); break;
    case "memory": loadMemory(); break;
    case "settings": loadSettings(); loadStatus(); break;
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
  const li = m.role === "note" ? el("li", { class: "note-line" }, m.text)
    : el("li", { class: `${m.role}${m.session && m.session !== S.sessionId ? " from-device" : ""}` }, m.text, meta);
  chat.append(li);
  while (chat.children.length > 120) chat.firstChild.remove();
  requestAnimationFrame(() => (chat.scrollTop = chat.scrollHeight));
}

async function setHandsFree(on) {
  try {
    if (on) await Mic.start();
    S.handsFree = on;
    $("handsfree")?.setAttribute("aria-pressed", String(on));
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
  renderVoices();
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
  k("ANTHROPIC_API_KEY", "key-claude");
  let skipEleven = false, skipGroq = false;
  try {
    skipEleven = localStorage.getItem("buddy-skip-eleven") === "1";
    skipGroq = localStorage.getItem("buddy-skip-groq") === "1";
  } catch {}
  $("setup-claude").hidden = keys.ANTHROPIC_API_KEY.set;
  $("setup-eleven").hidden = keys.ELEVENLABS_API_KEY.set || skipEleven;
  $("setup-groq").hidden = keys.GROQ_API_KEY.set || skipGroq;
  $("setup-box").hidden = $("setup-claude").hidden && $("setup-eleven").hidden && $("setup-groq").hidden;
  S.lastKeys = keys;
  const pill = $("pill-keys");
  const brain = keys.ANTHROPIC_API_KEY.set, voice = keys.ELEVENLABS_API_KEY.set;
  pill.dataset.ok = brain && voice ? "true" : brain ? "warn" : "false";
  pill.title = !brain ? "Claude key missing: Buddy can't answer questions"
    : voice ? (keys.GROQ_API_KEY.set ? "All keys set" : "No Groq key: phrase rules decide when you're talking to Buddy")
    : "No ElevenLabs key: offline voice will be used";
}
// ---------------------------------------------------------------- voices
function voiceRows() {
  return [...document.querySelectorAll("#voice-list li")].map((li) => ({
    name: li.querySelector(".v-name").value.trim(), id: li.querySelector(".v-id").value.trim() }));
}
function renderVoices() {
  const list = $("voice-list");
  const current = S.settings.elevenlabs_voice_id;
  if (!S.voicesDirty || !list.children.length) {
    let saved = (S.settings.voices || []).slice(0, 8);
    if (!saved.some((v) => v.id)) saved = [{ name: "George", id: current }];
    list.replaceChildren(...Array.from({ length: 8 }, (_, i) => {
      const v = saved[i] || { name: "", id: "" };
      return el("li", {},
        el("button", { type: "button", class: "v-use", title: "Use this voice", "aria-label": `Use voice ${i + 1}`,
                       onclick: () => useVoice(i) }, String(i + 1)),
        el("input", { class: "v-name", type: "text", placeholder: "Name", value: v.name, "aria-label": `Voice ${i + 1} name`,
                      oninput: () => (S.voicesDirty = true) }),
        el("input", { class: "v-id", type: "text", placeholder: "Voice ID", value: v.id, "aria-label": `Voice ${i + 1} ID`,
                      spellcheck: "false", oninput: () => (S.voicesDirty = true) }));
    }));
  }
  const rows = [...list.children];
  const match = rows.findIndex((li) => !!current && li.querySelector(".v-id").value.trim() === current);
  if (match >= 0) S.voiceActive = match;
  rows.forEach((li, i) => li.classList.toggle("active", i === S.voiceActive));
}
async function saveVoices(extra = {}) {
  const rows = voiceRows();
  const voices = rows.filter((v) => v.id);
  // Saving also switches Buddy to the highlighted voice, using whatever ID is in that row now
  // (so editing the ID of the voice in use takes effect straight away).
  let active = S.voiceActive ?? rows.findIndex((v) => v.id);
  if (!rows[active]?.id) active = rows.findIndex((v) => v.id);
  const body = { voices, ...extra };
  if (!("elevenlabs_voice_id" in body) && active >= 0) body.elevenlabs_voice_id = rows[active].id;
  await api("/api/settings", { method: "POST", body });
  S.voicesDirty = false;
  S.voiceActive = active;
  S.settings.voices = voices;
  if (body.elevenlabs_voice_id) S.settings.elevenlabs_voice_id = body.elevenlabs_voice_id;
  renderVoices();
}
async function useVoice(i) {
  const v = voiceRows()[i];
  if (!v.id) { toast("Paste a voice ID into that row first"); return; }
  try {
    S.voiceActive = i;
    await saveVoices({ elevenlabs_voice_id: v.id });
    toast(`Now using ${v.name || "voice " + (i + 1)}`);
  } catch (err) { toast("Couldn't save: " + err.message); }
}

// ---------------------------------------------------------------- mic test
async function micTest(on) {
  const target = $("mt-source").value;
  if (on && target === "me") {
    try { await Mic.start(); } catch (err) { toast("Microphone: " + err.message, 5000); return; }
  }
  S.micTest = on;
  send({ type: "mic_test", on, target });
  $("mt-start").textContent = on ? "Stop" : "Start";
  $("mt-stats").textContent = on ? "Listening… say something." : "Stopped.";
  if (on) { $("mt-lines").replaceChildren(); $("mt-partial").textContent = ""; $("mt-advice").textContent = ""; }
  if (!on && !S.handsFree && !S.ptt) Mic.stop();
  micLive();
}
function micTestEvent(m) {
  if (m.error) { toast(m.error); micTest(false); return; }
  if (m.db != null) {
    $("mt-bar").style.width = Math.max(0, Math.min(100, ((m.db + 70) / 60) * 100)) + "%";
    $("mt-db").textContent = `${Math.round(m.db)} dB`;
    $("mt-stats").textContent = `Loudest recently: ${m.peak_db} dB · distorted samples: ${m.clipped}`;
    $("mt-advice").textContent = m.clipped > 50 ? "Too loud: it's distorting. Lower the desk unit mic boost in Settings (try ×4)."
      : m.peak_db < -32 && m.text ? "Quite quiet: raise the desk unit mic boost in Settings, or move a bit closer."
      : m.peak_db > -32 ? "Level looks good." : "";
  }
  if (m.text == null) return;
  if (m.final) {
    $("mt-partial").textContent = "";
    if (m.text.trim()) {
      const who = m.speaker === undefined ? null
        : m.speaker ? el("span", { class: "who" }, `sounds like ${m.speaker} (${m.voice_score})`)
        : m.learning ? el("span", { class: "who unknown" }, `probably ${m.learning}, still learning their voice (${m.voice_score})`)
        : el("span", { class: "who unknown" }, `voice not recognised (${m.voice_score})`);
      $("mt-lines").append(el("li", {}, m.text, el("span", { class: "meta" }, ` ${m.seconds}s of speech · ${m.ms} ms · ${m.engine}`), ...(who ? [who] : [])));
    } else {
      $("mt-lines").append(el("li", { class: "miss" }, `(heard ${m.seconds}s of sound but no words)`));
    }
  } else {
    $("mt-partial").textContent = m.text ? m.text + " …" : "…";
  }
}

// ---------------------------------------------------------------- speed tab
const SPEED_PARTS = [
  ["wait", "waiting for you to finish", "#6b7280"], ["ears", "writing down your words", "#8b5cf6"],
  ["think", "quick thinking", "#38bdf8"], ["haiku", "Claude answering", "#f59e0b"],
  ["check", "fact-check", "#f43f5e"], ["search", "web search", "#ec4899"], ["reword", "rewording", "#a3e635"],
  ["voice", "voice starting", "#22c55e"], ["hold", "lining up the sound", "#14b8a6"],
];
function secs(ms) { return ms == null ? "–" : (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + " s"; }
function speedClass(ms) { return ms == null ? "" : ms <= 800 ? "good" : ms <= 1500 ? "ok" : "slow"; }
async function loadSpeed() {
  let d;
  try { d = await api("/api/speed?last=20"); } catch { return; }
  const turns = d.turns || [];
  const last = turns[0]?.total, ten = turns.slice(0, 10).map((t) => t.total);
  const avg = ten.length ? Math.round(ten.reduce((a, b) => a + b, 0) / ten.length) : null;
  for (const [id, ms] of [["speed-last", last], ["speed-avg", avg]]) {
    $(id).textContent = secs(ms);
    $(id).className = "speed-big " + speedClass(ms);
  }
  $("speed-key").replaceChildren(...SPEED_PARTS.map(([, label, c]) => el("li", {}, el("i", { style: `background:${c}` }), label)));
  $("speed-list").replaceChildren(...(turns.length ? turns.map((t) => {
    const parts = SPEED_PARTS.filter(([k]) => t[k] > 0);
    const sum = parts.reduce((a, [k]) => a + t[k], 0) || 1;
    return el("li", {},
      el("div", { class: "speed-row" }, el("span", {}, t.text ? `“${t.text}”` : "(spoken turn)"),
        el("b", { class: speedClass(t.total) }, secs(t.total))),
      el("div", { class: "speed-bar", title: parts.map(([k, label]) => `${label}: ${t[k]} ms`).join("\n") },
        ...parts.map(([k, label, c]) => el("i", { style: `width:${(100 * t[k] / sum).toFixed(1)}%;background:${c}`, title: `${label}: ${t[k]} ms` }))));
  }) : [el("li", { class: "speed-empty" }, d.on ? "Talk to Buddy and each answer shows up here." : "Timing is switched off (timing_log in Settings).")]));
}

// One Stop button at the bottom of the controls, whichever page started the mic.
function micLive() {
  const on = S.micTest;
  $("mic-live").hidden = !on;
  if (on) $("mic-live-text").textContent = vidRecording ? "Learning your voice…" : "Mic test is on";
}
function stopMicAll() {
  if (vidRecording) vidRec(false);
  else if (S.micTest) micTest(false);
}

// ---------------------------------------------------------------- your voice (voice recognition)
let vidPoll = null;
async function loadVoiceID() {
  let st;
  try { st = await api("/api/voiceid"); } catch { return; }
  $("vid-setup").hidden = st.installed;
  $("vid-main").hidden = !st.installed;
  $("vid-install").disabled = st.installing;
  $("vid-install").textContent = st.installing ? "Downloading…" : "Download";
  $("vid-install-status").textContent = st.error || (st.installing ? "This takes a minute or so." : "");
  clearTimeout(vidPoll);
  if (st.installing) vidPoll = setTimeout(loadVoiceID, 2000);
  if (!$("vid-name").value) $("vid-name").value = S.settings?.user_name || "";
  $("vid-people").replaceChildren(...(st.people.length ? st.people.map((p) => el("li", {},
    el("div", {}, el("b", {}, p.name),
      el("small", {}, p.trusted ? `Knows this voice ✓ · ${Math.round(p.seconds)} s of speech · ${p.sources.join(", ")}`
        : `Still learning: ${Math.round(p.seconds)} of ${st.trust_seconds} s heard. It learns more each time they talk.`)),
    el("button", { class: "btn small ghost", type: "button", onclick: async () => {
      if (!confirm(`Delete ${p.name}'s saved voice?`)) return;
      try { await api("/api/voiceid/" + encodeURIComponent(p.name), { method: "DELETE" }); loadVoiceID(); toast("Deleted"); }
      catch (err) { toast("Couldn't: " + err.message); }
    } }, "Delete"))) : [el("li", { class: "note" }, "None yet. Do step 1 or 2 above.")]));
}
function vidName() {
  const name = $("vid-name").value.trim();
  if (!name) { toast("Type whose voice it is first"); $("vid-name").focus(); }
  return name;
}
function vidSay(id, text, kind = "") { const p = $(id); p.textContent = text; p.className = "vid-status " + kind; }
async function vidUpload() {
  const name = vidName(), file = $("vid-file").files[0];
  if (!name) return;
  if (!file) { toast("Choose the recording first"); return; }
  $("vid-upload").disabled = true;
  vidSay("vid-upload-status", "Listening to it… (a 5 minute recording takes about 10 seconds)");
  try {
    const res = await fetch(`/api/voiceid/upload?name=${encodeURIComponent(name)}&file=${encodeURIComponent(file.name)}`,
      { method: "POST", body: file, headers: S.token ? { "X-Buddy-Token": S.token } : {} });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.detail || res.statusText);
    vidSay("vid-upload-status", `Done: learnt ${Math.round(j.seconds)} s of ${j.name}'s voice. ` +
      (j.match < 0.5 ? "It sounded quite mixed. Was anyone else talking in it?" : "Sounds consistent ✓"), j.match < 0.5 ? "bad" : "ok");
    $("vid-file").value = "";
    loadVoiceID();
  } catch (err) { vidSay("vid-upload-status", "Couldn't: " + err.message, "bad"); }
  finally { $("vid-upload").disabled = false; }
}
let vidRecording = false;
async function vidRec(on) {
  const name = on ? vidName() : "";
  if (on && !name) return;
  const target = $("vid-source").value;
  if (on && target === "me") {
    try { await Mic.start(); } catch (err) { toast("Microphone: " + err.message, 5000); return; }
  }
  vidRecording = on;
  S.micTest = on;
  send({ type: "mic_test", on, target, enroll: name });
  $("vid-rec").textContent = on ? "Stop" : "Start";
  vidSay("vid-rec-status", on ? "Listening… read the sentences out. Each one is added as you finish it." : "Stopped.");
  if (!on && !S.handsFree && !S.ptt) Mic.stop();
  if (!on) loadVoiceID();
  micLive();
}
function vidEvent(m) {
  if (m.error) { vidSay("vid-rec-status", m.error, "bad"); return; }
  vidSay("vid-rec-status", `Got it ✓ ${m.total_seconds ? Math.round(m.total_seconds) + " s of " + m.name + "'s voice saved so far." : ""}` +
    (m.like_before != null && m.like_before < 0.35 ? " That one didn't sound like the rest. Is it the same person?" : " Keep going."),
    m.like_before != null && m.like_before < 0.35 ? "bad" : "ok");
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
  const mts = $("mt-source"), mcur = mts.value;
  mts.replaceChildren(el("option", { value: "me" }, "this browser"),
    ...devices.map((d) => el("option", { value: d.id }, `desk unit mic (${d.name})`)));
  mts.value = [...mts.options].some((o) => o.value === mcur) ? mcur : (devices[0]?.id || "me");
  const vs = $("vid-source"), vcur = vs.value;
  vs.replaceChildren(...devices.map((d) => el("option", { value: d.id }, `Buddy's mic (${d.name})`)),
    el("option", { value: "me" }, "this browser"));
  vs.value = [...vs.options].some((o) => o.value === vcur) ? vcur : (devices[0]?.id || "me");
  showKeys(st.keys);
  const sp = st.spotify || {};
  $("spotify-status").textContent = sp.connected
    ? `Connected as ${sp.account || "?"}${sp.email ? " (" + sp.email + ")" : ""}. Spotify says this account is: ${sp.plan || "unknown, press Connect again"}.`
      + (sp.last_error ? ` Last Spotify error: ${sp.last_error}.` : "")
    : sp.client_id ? "Client ID saved. Press Connect Spotify." : "Not connected.";
  $("spotify-disconnect").hidden = !sp.connected;
  $("music-login-row").hidden = !(st.music && st.music.login_url);
  if (st.music && st.music.login_url) $("music-login").href = st.music.login_url;
  const mu = st.music || {};
  $("music-status").textContent = !mu.enabled ? ""
    : !mu.installed ? "Needs librespot: run Update Buddy (it installs it if Homebrew is on this Mac, from brew.sh)."
    : mu.running && !mu.logged_in ? (mu.login_url ? "One last step: press the button below and log in to Spotify (once only)."
                                                    : "Starting the Spotify login… (refresh this in a few seconds)")
    : mu.running ? `Ready: “${mu.name}” is your Spotify speaker${mu.playing ? ", playing now" : ""}.`
    : `Not running${mu.error ? ": " + mu.error : ""}.`;
  const dl = $("model-list");
  dl.replaceChildren(...(st.llm.available || []).map((m) => el("option", { value: m })));
  $("model-note").textContent = st.llm.available
    ? `Your key can use: ${st.llm.available.filter((m) => !/whisper|guard|tts|orpheus/.test(m)).join(", ")}`
    : "Add a Groq key to see which listener models your account can use.";
  const q = st.tts.elevenlabs.quota;
  $("eleven-quota").textContent = q && q.limit ? `ElevenLabs credits used this month: ${q.used.toLocaleString()} of ${q.limit.toLocaleString()} (${q.tier})` : "";
  const cool = Object.entries(st.llm.cooldowns || {}).map(([m, s]) => `${m.replace("openai/", "")} ${s}s`).join(", ");
  const stats = [
    ["Buddy is", st.listening.awake ? (st.listening.room ? "on, listening to the room" : "on") : "off (say “hey buddy”)"],
    ["Everyday model", (st.llm.fast || [])[0] || "none"],
    ["Deep-thinking model", (st.llm.smart || [])[0] || "none"],
    ["Ears", st.stt.installed ? `${st.stt.engine}${st.stt.last_ms ? `, last ${st.stt.last_ms} ms` : ""}` : "not downloaded: run Update Buddy"],
    ["Listener", st.keys.GROQ_API_KEY.set ? ((st.llm.listener || [])[0] || "none available") : "phrase rules (no Groq key)"],
    ["Rate-limited", cool || "none"],
    ["Voice", st.tts.last_engine || (st.tts.elevenlabs.available ? "ElevenLabs" : st.tts.piper.available ? "Piper" : "browser only")],
    ["Memory", `${st.memory.count} items`],
    ["Connected", st.sessions.map((s) => `${s.role}:${s.state}`).join(", ") || "–"],
  ];
  stats.push(["Buddy's volume", `${S.settings.volume ?? "?"} of 10`]);
  const dev = devices[0]?.info || {};
  if (dev.rssi) stats.push(["Desk unit Wi-Fi", `${dev.rssi} dBm${dev.rssi < -75 ? " (weak)" : ""} · heap ${Math.round((dev.heap || 0) / 1024)} KB`]);
  if (dev.fw) stats.push(["Desk unit software", `${dev.fw}` + (dev.sd === undefined ? " (older: can't record lessons)"
    : dev.sd ? " · SD card found" + (dev.recording ? " · recording a lesson now" : "")
    : " · SD card NOT found (check the 4 signal wires, 3V, GND and that the card is in)")]);
  if (dev.dropped_spk != null) stats.push(["Desk unit audio gaps", `speaker ${dev.dropped_spk} · mic ${dev.dropped_mic ?? 0}`]);
  const snd = st.alarm_sounds || {};
  stats.push(["Alarm sounds", snd.folder ? (snd.sounds.length ? snd.sounds.map((n, i) => `${i + 1}: ${n}`).join(", ")
    : `no sound files in ${snd.folder}`) : "no “sounds for buddy alarm” folder found (Desktop, Documents or Downloads)"]);
  const mus = st.music || {};
  if (mus.enabled) stats.push(["Music to Buddy's speaker",
    `${mus.running ? "librespot running" : "librespot NOT running"} · in ${mus.kb_in} KB · out ${mus.kb_out} KB` +
    `${mus.rate ? " · " + mus.rate + " Hz" : ""} · dropped (busy ${mus.busy_drops}, no unit ${mus.no_unit_drops})` +
    `${mus.error ? " · " + mus.error : ""}${(mus.librespot_log || []).length ? " · last: " + mus.librespot_log.slice(-1)[0] : ""}`]);
  if (st.llm.last_error) stats.push(["Last model error", st.llm.last_error.slice(0, 140)]);
  if (st.weather_error) stats.push(["Weather error", st.weather_error.slice(0, 140)]);
  $("status-grid").replaceChildren(...stats.map(([k, v]) => el("div", { class: "stat" }, el("b", {}, k), el("span", {}, v))));
}
function addLog(e) {
  const log = $("log");
  let text = "";
  if (e.event === "turn") text = `“${e.heard}” → “${(e.said || "").slice(0, 120)}” [${e.model || e.intent}${e.stt_ms ? `, stt ${e.stt_ms}ms` : ""}, brain ${e.brain_ms}ms]`;
  else if (e.event === "wake_rejected" || e.event === "wake_verified") text = `heard “${e.heard}”`;
  else if (e.event === "heard") text = `“${e.heard}” → ${e.for_buddy ? "for Buddy" : "ignored"} (${e.how})`;
  else if (e.event === "awake" || e.event === "asleep") text = e.event === "awake" ? "Buddy is on" : "Buddy is off until “hey buddy”";
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
  const ask = (t) => { Audio_.ensure(); send({ type: "text", text: t }); };
  const hour = new Date().getHours();
  const hello = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  $("chat").append(el("li", { class: "empty" },
    el("h1", {}, `${hello}.`), el("p", {}, "What can I do for you?"),
    el("div", { class: "suggest" }, ["What's the weather?", "Set a timer for 10 minutes", "Wake me up at 7 on weekdays", "What do you remember about me?"]
      .map((t) => el("button", { type: "button", onclick: () => ask(t) }, t)))));
  const drawer = (open) => {
    if (!open) stopMicAll();  // closing the controls switches the mic test off
    $("drawer").classList.toggle("open", open);
    $("drawer").setAttribute("aria-hidden", String(!open));
    $("menu").setAttribute("aria-expanded", String(open));
    $("scrim").hidden = !open;
    document.body.classList.toggle("drawer-open", open);
  };
  $("menu").onclick = () => drawer(!$("drawer").classList.contains("open"));
  $("drawer-close").onclick = () => drawer(false);
  $("scrim").onclick = () => drawer(false);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") drawer(false); });
  document.addEventListener("pointerdown", () => Audio_.ensure(), { once: true });
  $("orb").onclick = () => { send({ type: "stop_speaking" }); Audio_.stop(); };
  $("stop").onclick = () => { send({ type: "stop_speaking" }); Audio_.stop(); };
  if ($("handsfree")) $("handsfree").onclick = () => setHandsFree(!S.handsFree);
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

  // Study website: connect with the code the website shows.
  {
    const status = $("study-status"), btn = $("study-connect"), off = $("study-disconnect");
    const refresh = async () => {
      try {
        const r = await api("/api/study/cloud");
        status.textContent = r.connected
          ? `Connected. Lesson recordings go to ${r.url}${r.waiting ? ` (${r.waiting} waiting to send)` : ""}.`
          : "Not connected.";
        off.hidden = !r.connected;
      } catch { /* server restarting */ }
    };
    btn.onclick = async () => {
      btn.disabled = true;
      status.textContent = "Connecting…";
      try {
        await api("/api/study/pair", { method: "POST", body: JSON.stringify({ code: $("study-code").value.trim() }) });
        $("study-code").value = "";
      } catch (e) { status.textContent = e.message; btn.disabled = false; return; }
      btn.disabled = false;
      refresh();
    };
    off.onclick = async () => { await api("/api/study/unpair", { method: "POST" }); refresh(); };
    document.querySelector('[data-open="studyweb"]').addEventListener("click", refresh);
  }

  // Settings: a short menu of sections; tap one to see just that section, or search.
  {
    const panel = $("panel-settings"), menu = $("set-menu"), back = $("set-back"), search = $("set-search");
    const studyLink = panel.querySelector(".study-link"), nothing = $("set-nothing");
    const sections = [...panel.querySelectorAll("fieldset[data-section]")];
    const show = (key) => {
      search.value = "";
      sections.forEach((f) => { f.hidden = f.dataset.section !== key; f.removeAttribute("data-match"); });
      menu.hidden = studyLink.hidden = !!key;
      back.hidden = !key; nothing.hidden = true;
      search.hidden = !!key;
      panel.closest(".drawer-body")?.scrollTo?.(0, 0);
    };
    menu.querySelectorAll("[data-open]").forEach((b) => b.onclick = () => show(b.dataset.open));
    back.onclick = () => show(null);
    search.oninput = () => {
      const q = search.value.trim().toLowerCase();
      if (!q) return show(null);
      let any = false;
      sections.forEach((f) => {
        const legend = f.querySelector("legend").textContent.toLowerCase();
        const items = [...f.querySelectorAll(":scope > label, :scope > p, :scope > ol")];
        let hit = legend.includes(q);
        items.forEach((el) => {
          const m = el.textContent.toLowerCase().includes(q) || [...el.querySelectorAll("option")].some((o) => o.textContent.toLowerCase().includes(q));
          el.toggleAttribute("data-hit", m || legend.includes(q));
          hit = hit || m;
        });
        f.hidden = !hit; f.toggleAttribute("data-match", hit);
        any = any || hit;
      });
      menu.hidden = studyLink.hidden = true; back.hidden = false; nothing.hidden = any;
    };
  }
  for (const tab of document.querySelectorAll("[role=tab]")) {
    tab.onclick = () => {
      document.querySelectorAll("[role=tab]").forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
      document.querySelectorAll(".panel").forEach((p) => (p.hidden = p.id !== "panel-" + tab.dataset.tab));
      if (tab.dataset.tab === "weather") loadWeather();
      if (tab.dataset.tab === "activity") loadStatus();
      if (tab.dataset.tab === "yourvoice") loadVoiceID();
      if (tab.dataset.tab === "speed") loadSpeed();
      if (tab.dataset.tab !== "yourvoice" && vidRecording) vidRec(false);
      if (tab.dataset.tab !== "mictest" && S.micTest && !vidRecording) micTest(false);
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
      if (out?.tagName === "OUTPUT") out.textContent = Number(input.value).toFixed(Number(input.step) >= 1 ? 0 : 2);
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
  $("setup-skip-groq").onclick = (e) => {
    e.preventDefault();
    try { localStorage.setItem("buddy-skip-groq", "1"); } catch {}
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
  $("spotify-disconnect").onclick = async () => {
    try { await api("/api/spotify/disconnect", { method: "POST" }); toast("Spotify disconnected"); loadStatus(); }
    catch (err) { toast("Couldn't: " + err.message); }
  };
  $("spotify-redirect").textContent = `http://127.0.0.1:${location.port || 80}/spotify/callback`;
  $("mt-start").onclick = () => micTest(!S.micTest);
  $("vid-install").onclick = async () => { try { await api("/api/voiceid/install", { method: "POST" }); } catch (err) { toast(err.message); } loadVoiceID(); };
  $("vid-upload").onclick = vidUpload;
  $("vid-rec").onclick = () => vidRec(!vidRecording);
  $("mic-live-stop").onclick = stopMicAll;
  $("update-now").onclick = async () => {
    if (!confirm("Update Buddy now? It restarts by itself; give it a minute or two.")) return;
    try {
      await api("/api/update", { method: "POST" });
      $("update-status").textContent = "Updating… Buddy will restart and this page will reconnect. If it takes more than 5 minutes, double-click Update Buddy on the Desktop.";
    } catch (err) { $("update-status").textContent = "Couldn't start the update: " + err.message; }
  };
  $("voices-save").onclick = async () => {
    try {
      await saveVoices();
      const v = voiceRows()[S.voiceActive] || {};
      toast(v.id ? `Saved. Now using ${v.name || "voice " + (S.voiceActive + 1)}` : "Voices saved");
    } catch (err) { toast("Couldn't save: " + err.message); }
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
