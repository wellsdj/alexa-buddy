// Study Buddy: pick who you are, add lessons (a recording or pasted text), then read the notes and
// practise the questions Buddy made from them.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const view = $("#view");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const today = () => new Date().toLocaleDateString("en-CA");  // YYYY-MM-DD
const niceDate = (d) => d ? new Date(d + "T12:00").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" }) : "";
const longDate = (d) => d ? new Date(d + "T12:00").toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", year: "numeric" }) : "";
const isoDay = (ms) => new Date(ms).toLocaleDateString("en-CA");
// What a lesson is called: your title, or "Maths lesson" once Buddy has worked out the subject.
const lessonName = (x) => x.title || (x.subject ? `${x.subject} lesson` : (x.status === "ready" ? x.auto_title || "Lesson" : "New lesson"));
const aboutLine = (x) => x.about ? `About ${x.about}` : "";

const S = { token: load("studyToken"), me: null, poll: null };

function load(k) { try { return localStorage.getItem(k) || ""; } catch { return ""; } }
function store(k, v) { try { v ? localStorage.setItem(k, v) : localStorage.removeItem(k); } catch { /* private window */ } }

async function api(path, opts = {}) {
  const headers = { ...(opts.body && !(opts.body instanceof Blob) ? { "Content-Type": "application/json" } : {}),
                    ...(S.token ? { "X-Study-Token": S.token } : {}), ...(opts.headers || {}) };
  const res = await fetch("/study/api" + path, { ...opts, headers });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) { signedOut(); throw new Error(data.detail || "Sign in again."); }
  if (!res.ok) throw new Error(data.detail || "Something went wrong.");
  return data;
}

function signedOut() { S.token = ""; S.me = null; store("studyToken", ""); showMe(); location.hash = "#/"; }

// ---------- who's revising ----------
async function whoView() {
  const { profiles } = await api("/profiles");
  document.body.classList.add("signin");
  view.innerHTML = `<section class="hero">
    <h1>Study<br>Buddy</h1>
    <p class="lead">Your lessons, turned into notes and practice questions. Who's revising?</p>
    <div class="people">
      ${profiles.map((p) => `<button class="person" data-id="${esc(p.id)}" data-name="${esc(p.name)}">
        <span class="avatar c${p.colour}">${esc(p.name[0].toUpperCase())}</span>${esc(p.name)}</button>`).join("")}
      <button class="person add" data-add><span class="avatar">+</span>Add person</button>
    </div>
    <p class="foot"><a href="/">← Back to Buddy</a></p></section>`;
  view.querySelectorAll(".person[data-id]").forEach((b) => b.onclick = async () => {
    const pin = await askPin(`Hi ${b.dataset.name}`, "Type your PIN", async (pin) => {
      const r = await api("/login", { method: "POST", body: JSON.stringify({ profile: b.dataset.id, pin }) });
      signedIn(r.token, r.profile);
    });
    if (pin) route();
  });
  $("[data-add]", view).onclick = addPersonView;
}

function addPersonView() {
  document.body.classList.remove("signin");
  view.innerHTML = `
    <a class="back" href="#/">← Back</a>
    <h1>Add a person</h1>
    <p class="muted">Pick a 4-number PIN so nobody else sees your lessons.</p>
    <form id="f">
      <label class="field">Name <input name="name" maxlength="30" autocomplete="off" required></label>
      <button class="btn wide">Next: choose a PIN</button>
      <p class="error" id="err"></p>
    </form>`;
  $("#f").onsubmit = async (e) => {
    e.preventDefault();
    const name = e.target.name.value.trim();
    if (!name) return;
    const first = await askPin(`PIN for ${name}`, "Choose 4 numbers");
    if (!first) return;
    await askPin("Type it again", "Just to be sure", async (pin) => {
      if (pin !== first) throw new Error("Those didn't match. Try again.");
      const r = await api("/profiles", { method: "POST", body: JSON.stringify({ name, pin }) });
      signedIn(r.token, r.profile);
    }).catch((err) => { $("#err").textContent = err.message; });
    if (S.token) { location.hash = "#/"; route(); }
  };
  $("[name=name]", view).focus();
}

function signedIn(token, me) { S.token = token; S.me = me; store("studyToken", token); showMe(); }

function showMe() {
  const b = $("#me");
  b.hidden = !S.me;
  if (S.me) b.innerHTML = `<span class="avatar c${S.me.colour}">${esc(S.me.name[0].toUpperCase())}</span>${esc(S.me.name)}`;
}

// PIN pad. With check(pin): keeps asking until check succeeds. Resolves to the PIN, or "" if cancelled.
function askPin(title, hint, check) {
  const sheet = $("#pin"), dots = [...sheet.querySelectorAll(".pin-dots i")], err = $("#pin-error");
  $("#pin-title").textContent = title;
  $("#pin-hint").textContent = hint;
  err.textContent = "";
  let pin = "", busy = false;
  const draw = () => dots.forEach((d, i) => d.classList.toggle("on", i < pin.length));
  draw();
  sheet.hidden = false;
  return new Promise((resolve) => {
    const done = (v) => { sheet.hidden = true; sheet.onclick = null; document.onkeydown = null; resolve(v); };
    const press = async (k) => {
      if (busy) return;
      if (k === "cancel") return done("");
      if (k === "back") { pin = pin.slice(0, -1); return draw(); }
      if (!/^\d$/.test(k) || pin.length >= 4) return;
      pin += k; draw();
      if (pin.length < 4) return;
      if (!check) return done(pin);
      busy = true;
      try { await check(pin); done(pin); }
      catch (e) { err.textContent = e.message; pin = ""; setTimeout(draw, 250); }
      finally { busy = false; }
    };
    sheet.onclick = (e) => {
      const b = e.target.closest(".pad button");
      if (b) press(b.dataset.k || b.textContent.trim());
      else if (e.target === sheet) done("");
    };
    document.onkeydown = (e) => {
      if (/^\d$/.test(e.key)) press(e.key);
      else if (e.key === "Backspace") press("back");
      else if (e.key === "Escape") press("cancel");
    };
  });
}

// ---------- lessons ----------
const STATUS = {
  waiting: ["busy", "Waiting…"], listening: ["busy", "Listening to the recording"], writing: ["busy", "Writing your notes…"],
  ready: ["ok", "Ready"], failed: ["bad", "Didn't work"],
};
const busy = (x) => ["waiting", "listening", "writing"].includes(x.status);

async function homeView() {
  const { lessons } = await api("/lessons");
  const first = S.me.name.split(" ")[0];
  view.innerHTML = `
    <h1>Hi ${esc(first)}</h1>
    <p class="muted">${lessons.length ? "Your lessons, newest first." : "Add your first lesson to get notes and practice questions."}</p>
    <a class="btn wide" href="#/add">+ Add a lesson</a>
    ${lessons.length ? `<div class="lessons">${lessons.map(lessonCard).join("")}</div>`
                     : `<div class="empty">No lessons yet.<br>Upload a recording of a lesson, or paste in some notes.</div>`}`;
  if (lessons.some(busy)) S.poll = setTimeout(() => location.hash === "#/" && homeView().catch(() => {}), 3000);
}

function lessonCard(x) {
  const [cls, label] = STATUS[x.status] || ["", x.status];
  const progress = x.status === "listening" ? ` ${x.progress}%` : "";
  return `<a class="lesson" href="#/lesson/${esc(x.id)}">
    <div class="t">${esc(lessonName(x))}</div>
    <div class="when">${esc(longDate(x.date))}</div>
    ${x.status === "ready" ? `<div class="about">${esc(aboutLine(x))}</div>
      <div class="meta" style="margin-top:8px">${x.questions} practice questions</div>`
      : `<div class="meta" style="margin-top:8px"><span class="tag ${cls}">${esc(label + progress)}</span></div>`}
    ${x.status === "listening" ? `<div class="bar"><i style="width:${x.progress}%"></i></div>` : ""}
    ${x.status === "failed" ? `<div class="meta">${esc(x.error)}</div>` : ""}
  </a>`;
}

async function addView() {
  view.innerHTML = `
    <a class="back" href="#/">← Lessons</a>
    <h1>Add a lesson</h1>
    <p class="muted">Choose the recording. Buddy works out the subject, the date and what it was about.</p>
    <form id="f">
      <label class="drop" id="drop">
        <input type="file" name="file" accept="audio/*,.m4a,.mp3,.wav,.aac,.caf">
        <b id="drop-name">Choose a recording</b>
        <span class="muted small">A voice memo from your phone works. The recording is deleted once it's turned into text.</span>
      </label>
      <p class="picked" id="picked" hidden></p>
      <label class="field">Title (optional) <input name="title" maxlength="80" placeholder="Leave empty and it's called e.g. “Maths lesson”"></label>
      <p class="note small">Ask your teacher before recording a lesson, and don't share recordings of other people.</p>
      <button class="btn wide" id="go" style="margin-top:14px">Make my notes</button>
      <div class="bar" id="up" hidden><i style="width:0"></i></div>
      <p class="error" id="err"></p>
    </form>`;
  const f = $("#f"), file = f.file;
  file.onchange = () => {
    const x = file.files[0];
    $("#drop-name").textContent = x ? x.name : "Choose a recording";
    $("#drop").classList.toggle("has", !!x);
    const picked = $("#picked");
    picked.hidden = !x;
    if (x) picked.textContent = `Recorded ${longDate(isoDay(x.lastModified))} · ${Math.round(x.size / 1e5) / 10} MB`;
  };
  f.onsubmit = async (e) => {
    e.preventDefault();
    const err = $("#err"), go = $("#go");
    err.textContent = "";
    const x = file.files[0];
    if (!x) { err.textContent = "Choose a recording first."; return; }
    go.disabled = true;
    try {
      // The recording's own date is the lesson's date (phones keep it on voice memos).
      const lesson = await upload(x, { title: f.title.value.trim(), date: isoDay(x.lastModified) });
      location.hash = `#/lesson/${lesson.id}`;
    } catch (ex) { err.textContent = ex.message; }
    finally { go.disabled = false; }
  };
}

// Sends the recording with a progress bar (fetch can't show upload progress).
function upload(blob, details) {
  return new Promise((resolve, reject) => {
    const bar = $("#up"), fill = $("#up i");
    bar.hidden = false;
    const x = new XMLHttpRequest();
    x.open("POST", "/study/api/lessons/audio?" + new URLSearchParams(details));
    x.setRequestHeader("X-Study-Token", S.token);
    x.upload.onprogress = (e) => { if (e.lengthComputable) fill.style.width = `${Math.round(100 * e.loaded / e.total)}%`; };
    x.onload = () => {
      let data = {};
      try { data = JSON.parse(x.responseText); } catch { /* not JSON */ }
      if (x.status === 401) { signedOut(); reject(new Error("Sign in again.")); }
      else if (x.status >= 400) reject(new Error(data.detail || "Upload failed."));
      else resolve(data);
    };
    x.onerror = () => reject(new Error("Upload failed. Is Buddy still running on the computer?"));
    x.send(blob);
  });
}

// ---------- one lesson ----------
async function lessonView(id, tab) {
  const x = await api(`/lessons/${encodeURIComponent(id)}`);
  const [cls, label] = STATUS[x.status] || ["", x.status];
  const head = `<a class="back" href="#/">← Lessons</a>
    <h1 style="margin-top:14px">${esc(lessonName(x))}</h1>
    <div class="meta"><span>${esc(longDate(x.date))}</span>${x.duration_s ? `<span>· ${Math.round(x.duration_s / 60)} min recording</span>` : ""}</div>`;
  if (x.status !== "ready") {
    const working = busy(x);
    view.innerHTML = `${head}
      <p><span class="tag ${cls}">${esc(label)}${x.status === "listening" ? ` ${x.progress}%` : ""}</span></p>
      ${x.status === "listening" ? `<div class="bar"><i style="width:${x.progress}%"></i></div>
        <p class="muted small">Turning the recording into text on the computer. A one-hour lesson takes a few minutes.</p>` : ""}
      ${x.status === "writing" ? `<p class="muted small">Buddy is reading the lesson and writing notes and questions. About a minute.</p>` : ""}
      ${x.status === "failed" ? `<p class="error">${esc(x.error)}</p><button class="btn secondary" id="retry">Try again</button>` : ""}
      <p><button class="link" id="del">Delete this lesson</button></p>`;
    if (working) S.poll = setTimeout(() => location.hash.startsWith(`#/lesson/${id}`) && lessonView(id, tab).catch(() => {}), 2500);
    $("#retry")?.addEventListener("click", async () => { await api(`/lessons/${id}/retry`, { method: "POST" }); lessonView(id); });
    $("#del").onclick = () => deleteLesson(id);
    return;
  }
  tab = tab || "notes";
  const tabs = [["notes", "Notes"], ["practice", `Practice (${x.questions.length})`], ["transcript", x.source === "audio" ? "Transcript" : "Text"]];
  view.innerHTML = `${head}
    ${x.about ? `<p class="summary" style="margin-top:14px"><b>${esc(aboutLine(x))}.</b> ${esc(x.summary)}</p>` : `<p class="summary">${esc(x.summary)}</p>`}
    <div class="chips">${x.topics.map((t) => `<span class="tag" title="${esc(t.summary)}">${esc(t.name)}</span>`).join("")}</div>
    <div class="tabs" role="tablist">${tabs.map(([k, n]) => `<button role="tab" data-tab="${k}" aria-selected="${k === tab}">${n}</button>`).join("")}</div>
    <section id="tab"></section>
    <p><button class="link" id="del">Delete this lesson</button></p>`;
  view.querySelectorAll("[data-tab]").forEach((b) => b.onclick = () => { history.replaceState(null, "", `#/lesson/${id}/${b.dataset.tab}`); lessonView(id, b.dataset.tab); });
  $("#del").onclick = () => deleteLesson(id);
  const box = $("#tab");
  if (tab === "notes") {
    box.className = "notes";
    box.innerHTML = x.notes.map((n) => `<h3>${esc(n.heading)}</h3><ul>${n.points.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>`).join("");
  } else if (tab === "transcript") {
    box.className = "transcript";
    box.textContent = x.transcript;
  } else {
    practice(box, x);
  }
}

// Practice: answer each question, then see the answer and say whether you got it.
// (Remembering what you're good at across lessons comes next; for now marks stay on this device.)
function practice(box, x) {
  const key = `studyMarks:${S.me.id}:${x.id}`;
  let marks = {};
  try { marks = JSON.parse(load(key) || "{}"); } catch { /* fresh */ }
  const levels = ["", "Easy", "Medium", "Exam-style"];
  const score = () => {
    const done = Object.values(marks), right = done.filter(Boolean).length;
    return `<div class="score"><span>${right} of ${x.questions.length} right</span>
      <span class="dots">${x.questions.map((q) => `<i class="${marks[q.id] ? "on" : ""}"></i>`).join("")}</span>
      ${done.length ? `<button class="link" id="reset" style="margin-left:auto">Start again</button>` : ""}</div>`;
  };
  box.className = "";
  box.innerHTML = `<div id="sc">${score()}</div>` + x.questions.map((q) => `
    <div class="q" data-q="${esc(q.id)}">
      <div class="qh"><span>${esc(q.topic)}</span><span>${levels[q.level] || ""}</span></div>
      <div class="qt">${esc(q.question)}</div>
      ${q.choices.length ? `<div class="choices">${q.choices.map((c) => `<button class="choice">${esc(c)}</button>`).join("")}</div>`
        : `<label class="field" style="margin:0">Your answer <textarea rows="2" style="min-height:60px"></textarea></label>
           <button class="btn secondary" data-show style="margin-top:10px">Show answer</button>`}
      <div class="reveal" hidden><b>Answer:</b> ${esc(q.answer)}${q.why ? `<div class="muted small" style="margin-top:6px">${esc(q.why)}</div>` : ""}
        ${q.choices.length ? "" : `<div class="marks"><button class="btn secondary" data-mark="1">I got it</button>
          <button class="btn secondary" data-mark="0">Not yet</button></div>`}</div>
    </div>`).join("");
  const save = (id, ok) => { marks[id] = ok; store(key, JSON.stringify(marks)); $("#sc").innerHTML = score(); bindReset(); };
  const bindReset = () => { const r = $("#reset"); if (r) r.onclick = () => { marks = {}; store(key, ""); practice(box, x); }; };
  bindReset();
  box.querySelectorAll(".q").forEach((el) => {
    const q = x.questions.find((y) => y.id === el.dataset.q), reveal = $(".reveal", el);
    el.querySelectorAll(".choice").forEach((c) => c.onclick = () => {
      if (!reveal.hidden) return;
      const right = c.textContent === q.answer;
      el.querySelectorAll(".choice").forEach((b) => { if (b.textContent === q.answer) b.classList.add("right"); });
      if (!right) c.classList.add("wrong");
      reveal.hidden = false;
      save(q.id, right);
    });
    const show = $("[data-show]", el);
    if (show) show.onclick = () => { reveal.hidden = false; show.hidden = true; };
    el.querySelectorAll("[data-mark]").forEach((b) => b.onclick = () => {
      save(q.id, b.dataset.mark === "1");
      el.querySelectorAll("[data-mark]").forEach((y) => y.disabled = y !== b);
    });
  });
}

async function deleteLesson(id) {
  if (!confirm("Delete this lesson and its notes?")) return;
  await api(`/lessons/${id}`, { method: "DELETE" });
  location.hash = "#/";
}

// ---------- menu ----------
$("#me").onclick = (e) => { e.stopPropagation(); $("#me-menu").hidden = !$("#me-menu").hidden; };
document.addEventListener("click", () => { $("#me-menu").hidden = true; });
$("#me-menu").onclick = async (e) => {
  const act = e.target.dataset.act;
  if (act === "switch") { await api("/logout", { method: "POST" }).catch(() => {}); signedOut(); route(); }
  if (act === "delete") {
    if (!confirm(`Delete ${S.me.name}'s profile, with all their lessons and notes? This can't be undone.`)) return;
    const ok = await askPin("Type your PIN", "To delete your profile", (pin) =>
      api("/me/delete", { method: "POST", body: JSON.stringify({ pin }) }));
    if (ok) { signedOut(); route(); }
  }
};

// ---------- pages ----------
async function route() {
  clearTimeout(S.poll);
  document.body.classList.remove("signin");
  try {
    if (S.token && !S.me) S.me = (await api("/me")).profile;
  } catch { /* signed out */ }
  showMe();
  const h = location.hash || "#/";
  try {
    if (!S.me) return await whoView();
    if (h === "#/add") return await addView();
    const m = h.match(/^#\/lesson\/([0-9a-f]+)(?:\/(\w+))?/);
    if (m) return await lessonView(m[1], m[2]);
    return await homeView();
  } catch (e) {
    view.innerHTML = `<p class="error">${esc(e.message)}</p><a class="btn secondary" href="#/">Back to lessons</a>`;
  }
}
window.addEventListener("hashchange", route);
route();
