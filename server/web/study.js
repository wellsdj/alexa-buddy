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
// On the website (no port) there's no Buddy page to go back to; on the Mac there is.
const CLOUD = location.port === "";
if (CLOUD) document.querySelectorAll(".to-buddy").forEach((a) => a.remove());

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
    <h1 class="fade">Study<br>Buddy</h1>
    <p class="lead">Who's revising?</p>
    <div class="people">
      ${profiles.map((p) => `<button class="person" data-id="${esc(p.id)}" data-name="${esc(p.name)}">
        <span class="avatar c${p.colour}">${esc(p.name[0].toUpperCase())}</span>${esc(p.name)}</button>`).join("")}
      <button class="person add" data-add><span class="avatar">+</span>Add person</button>
    </div>
    ${CLOUD ? "" : `<p class="foot"><a href="/">← Back to Buddy</a></p>`}</section>`;
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
    <h1 class="fade" style="margin-top:20px">Add a person</h1>
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
  const [{ lessons }, prog] = await Promise.all([api("/lessons"), api("/progress").catch(() => null)]);
  const first = S.me.name.split(" ")[0];
  const ready = lessons.filter((x) => x.status === "ready");
  const week = prog?.week || { answered: 0, right: 0, days: 0, streak: 0 };
  const skills = prog?.skills || [];
  const today = new Date().toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
  const pct = week.answered ? Math.round(100 * week.right / week.answered) : null;
  const line = week.streak >= 2 ? `${week.streak} days in a row. Keep it going.`
    : ready.length ? "Ready when you are." : "Record a lesson and Buddy turns it into questions.";
  const startHref = skills.length ? "#/revise/all" : ready[0] ? `#/revise/${ready[0].id}` : "";
  view.classList.add("wide");
  view.innerHTML = `
    <div class="home">
      <section class="home-head">
        <p class="date">${esc(today)}</p>
        <h1 class="fade hello">Hi ${esc(first)}</h1>
        <p class="lead">${esc(line)}</p>
        <div class="actions">
          ${startHref ? `<a class="btn big" href="${startHref}">${ICON_PLAY} Start revising</a>` : ""}
          <a class="btn ghost big" href="#/add">${ICON_PLUS} Add a lesson</a>
        </div>
      </section>
      <section class="home-list">
        <h2 class="sec">Your lessons</h2>
        ${lessons.length ? `<div class="lesson-list">${lessons.map(lessonCard).join("")}</div>` : howItWorks()}
      </section>
      <aside class="home-side">
        <div class="card stats">
          <div><b>${week.streak || 0}</b><span>day streak</span></div>
          <div><b>${week.answered}</b><span>this week</span></div>
          <div><b>${pct === null ? "–" : pct + "%"}</b><span>right</span></div>
        </div>
        <div class="card">
          <h3>Work on these</h3>
          ${skills.length ? skills.slice(0, 4).map((k) => `<div class="skill"><div class="skill-top"><span>${esc(k.name)}</span><span class="muted">Level ${k.level}</span></div>
              <div class="bar thin"><i style="width:${k.level * 20}%"></i></div></div>`).join("")
            + `<a class="more" href="#/progress">All my progress →</a>`
            : `<p class="muted small">Your weakest topics show up here once you've answered some questions.</p>`}
        </div>
      </aside>
    </div>`;
  if (lessons.some(busy)) S.poll = setTimeout(() => location.hash === "#/" && homeView().catch(() => {}), 3000);
}

const ICON_PLAY = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>`;
const ICON_PLUS = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>`;

function howItWorks() {
  const step = (n, icon, t, d) => `<div class="step"><span class="step-icon">${icon}</span><div><b>${n}. ${t}</b><p class="muted small">${d}</p></div></div>`;
  return `<div class="steps-card">
    ${step(1, `<svg viewBox="0 0 24 24"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>`, "Record a lesson",
      "Use Voice Memos on your phone (ask your teacher first), or the pocket Buddy. Longer than 10 seconds.")}
    ${step(2, `<svg viewBox="0 0 24 24"><path d="M3 7h6l2 2h10v10H3z"/></svg>`, "Put it in “Buddy lessons”",
      "AirDrop it to the Mac and drop it in the Buddy lessons folder on the Desktop. Buddy checks it's a lesson and names it.")}
    ${step(3, `<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>`, "Start revising",
      "It appears here. Buddy writes notes and questions that fit your level, and gets harder as you improve.")}
  </div>`;
}

function lessonCard(x) {
  const [cls, label] = STATUS[x.status] || ["", x.status];
  const progress = x.status === "listening" ? ` ${x.progress}%` : "";
  return `<a class="lesson row" href="#/lesson/${esc(x.id)}">
    <span class="rec-icon">${iconFor(x.subject)}</span><span class="row-text">
    <div class="t">${esc(lessonName(x))}</div>
    <div class="when">${esc(longDate(x.date))}</div>
    ${x.status === "ready" ? `<div class="about">${esc(aboutLine(x))}</div>
`
      : `<div class="meta" style="margin-top:6px"><span class="tag ${cls}">${esc(label + progress)}</span></div>`}</span>
    ${x.status === "listening" ? `<div class="bar"><i style="width:${x.progress}%"></i></div>` : ""}
    ${x.status === "failed" ? `<div class="meta">${esc(x.error)}</div>` : ""}
  </a>`;
}

// Little line icons for each subject (the rest get a sound wave).
const ICONS = {
  maths: '<path d="M5 7h5M7.5 4.5v5M14 7h5M5 17h5M14 15l5 4M19 15l-5 4"/>',
  science: '<path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-9V3"/><path d="M7.5 15h9"/>',
  book: '<path d="M2 5h7a3 3 0 0 1 3 3v12a2 2 0 0 0-2-2H2z"/><path d="M22 5h-7a3 3 0 0 0-3 3v12a2 2 0 0 1 2-2h8z"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  talk: '<path d="M4 5h16v11H9l-5 4z"/>',
  music: '<path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>',
  computer: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
  wave: '<path d="M3 12h2M7 8v8M11 5v14M15 9v6M19 7v10M21 12h0"/>',
};
function iconFor(subject) {
  const s = (subject || "").toLowerCase();
  const k = /math|algebra|geometry|statistic/.test(s) ? "maths"
    : /science|chem|bio|physic/.test(s) ? "science"
    : /english|literature|reading|drama|re\b|religio/.test(s) ? "book"
    : /history|geograph/.test(s) ? "globe"
    : /french|spanish|german|italian|latin|mandarin|language/.test(s) ? "talk"
    : /music/.test(s) ? "music"
    : /computing|computer|ict/.test(s) ? "computer" : "wave";
  return `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[k]}</svg>`;
}
const recDay = (t) => new Date(t * 1000).toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
const mins = (s) => s >= 60 ? `${Math.round(s / 60)} min` : `${s} s`;

async function addView() {
  const data = await api("/recordings");
  const recs = data.recordings;
  let chosen = null;
  view.innerHTML = `
    <a class="back" href="#/">← Lessons</a>
    <h1 class="fade" style="margin-top:20px">Choose a recording</h1>
    <p class="muted small">${data.mine
      ? `Recordings from your pocket Buddy appear here by themselves${data.folder ? `, and so do voice memos you put in the “${esc(data.folder_name)}” folder` : ""}. Anything over 10 seconds is kept.`
      : `Recordings from the pocket Buddy go to someone else. <button class="link" id="mine">Send them to me instead</button>`}</p>
    <div class="recs" id="recs">${recs.length ? recs.map(recRow).join("")
      : `<p class="empty">No recordings yet.${data.mine && !data.folder ? ` To add voice memos from your phone, make a folder called “${esc(data.folder_name)}” on this Mac's Desktop and put them in it.` : ""}</p>`}</div>
    <div id="use" hidden>
      <label class="field">Title (optional) <input name="title" maxlength="80" placeholder="Leave empty and it's called e.g. “Maths lesson”"></label>
      <button class="btn wide" id="go">Start revising</button>
    </div>
    <p class="error" id="err"></p>
    ${data.can_upload === false ? `<p style="margin-top:28px"><button class="link" id="pair">Connect your Buddy</button></p><p class="muted small" id="pair-box" hidden></p>`
      : `<p style="margin-top:28px"><button class="link" id="other">Or upload a file from this computer</button></p>`}
    <form id="f" hidden>
      <label class="drop" id="drop">
        <input type="file" name="file" accept="audio/*,.m4a,.mp3,.wav,.aac,.caf">
        <b id="drop-name">Choose a file</b>
        <span class="muted small">The recording is deleted once it's turned into text.</span>
      </label>
      <p class="picked" id="picked" hidden></p>
      <button class="btn wide" id="go2" style="margin-top:18px">Start revising</button>
      <div class="bar" id="up" hidden><i style="width:0"></i></div>
    </form>
    <p class="note small" style="margin-top:28px">Ask your teacher before recording a lesson, and don't share recordings of other people.</p>`;
  $("#mine")?.addEventListener("click", async () => { await api("/recordings/mine", { method: "POST" }); addView(); });
  view.querySelectorAll(".rec[data-ready]").forEach((row) => row.onclick = () => {
    chosen = row.dataset.id;
    view.querySelectorAll(".rec").forEach((r) => r.classList.toggle("on", r === row));
    $("#use").hidden = false;
  });
  $("#go").onclick = async () => {
    const err = $("#err");
    err.textContent = "";
    try {
      const rec = recs.find((r) => r.id === chosen);
      const lesson = await api(`/recordings/${chosen}/use`, { method: "POST", body: JSON.stringify({ title: $("[name=title]").value, date: isoDay(rec.recorded_at * 1000) }) });
      location.hash = `#/revise/${lesson.id}`;
    } catch (ex) { err.textContent = ex.message; }
  };
  if ($("#other")) $("#other").onclick = () => { $("#f").hidden = false; $("#other").hidden = true; };
  $("#pair")?.addEventListener("click", async () => {
    const r = await api("/pair-code", { method: "POST" });
    const box = $("#pair-box");
    box.hidden = false;
    box.innerHTML = `Your code: <b style="font-size:22px;letter-spacing:3px;color:#fff">${esc(r.code)}</b><br>On the Mac, open Buddy → Settings → Study website, type this code and press Connect. It works for ${r.minutes} minutes. Lesson recordings then come here, to you.`;
  });
  const f = $("#f"), file = f.file;
  file.onchange = () => {
    const x = file.files[0];
    $("#drop-name").textContent = x ? x.name : "Choose a file";
    $("#drop").classList.toggle("has", !!x);
    $("#picked").hidden = !x;
    if (x) $("#picked").textContent = `Recorded ${longDate(isoDay(x.lastModified))} · ${Math.round(x.size / 1e5) / 10} MB`;
  };
  f.onsubmit = async (e) => {
    e.preventDefault();
    const x = file.files[0], err = $("#err");
    if (!x) { err.textContent = "Choose a file first."; return; }
    $("#go2").disabled = true;
    try {
      const lesson = await upload(x, { date: isoDay(x.lastModified) });
      location.hash = `#/revise/${lesson.id}`;
    } catch (ex) { err.textContent = ex.message; }
    finally { $("#go2").disabled = false; }
  };
  if (recs.some((r) => r.status === "checking"))
    S.poll = setTimeout(() => { if (location.hash === "#/add" && chosen === null && file.files.length === 0) addView().catch(() => {}); }, 4000);
}

function recRow(r) {
  const ready = r.status === "ready";
  return `<button type="button" class="rec" data-id="${esc(r.id)}" ${ready ? "data-ready" : "disabled"}>
    <span class="rec-icon">${iconFor(r.subject)}</span>
    <span class="rec-text">
      <b>${esc(ready ? r.name : "New recording")}</b>
      <span class="when">${esc(recDay(r.recorded_at))}${r.duration_s ? ` · ${mins(r.duration_s)}` : ""}</span>
      <span class="about">${esc(ready ? (r.about ? `About ${r.about}` : "") : "Checking it's a lesson…")}</span>
    </span>
  </button>`;
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
function teacherForm(id, tab) {
  const f = $("#teacher-form");
  if (!f) return;
  f.onsubmit = async (e) => {
    e.preventDefault();
    const name = $("#teacher-name").value.trim();
    if (!name) return;
    try { await api(`/lessons/${encodeURIComponent(id)}/teacher`, { method: "POST", body: { name } }); lessonView(id, tab); }
    catch (err) { alert(err.message); }
  };
}

async function lessonView(id, tab) {
  const x = await api(`/lessons/${encodeURIComponent(id)}`);
  const [cls, label] = STATUS[x.status] || ["", x.status];
  const head = `<a class="back" href="#/">← Lessons</a>
    <h1 class="fade" style="margin-top:20px">${esc(lessonName(x))}</h1>
    <div class="meta"><span>${esc(longDate(x.date))}</span>${x.duration_s ? `<span>· ${Math.round(x.duration_s / 60)} min recording</span>` : ""}${x.teacher ? `<span class="teacher">· ${esc(x.teacher)}${x.teacher_guessed ? "?" : ""}</span>` : ""}</div>
    ${x.teacher_can_name && (!x.teacher || x.teacher_guessed) ? `<form class="teacher-ask" id="teacher-form">
      <label for="teacher-name">${x.teacher_guessed ? `Was the teacher ${esc(x.teacher)}?` : "Who was the teacher?"} Buddy will know their voice next time.</label>
      <div class="row"><input id="teacher-name" maxlength="40" placeholder="e.g. Mr Smith" value="${esc(x.teacher || "")}" autocomplete="off"><button class="btn secondary" type="submit">Save</button></div>
    </form>` : ""}`;
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
    teacherForm(id, tab);
    $("#del").onclick = () => deleteLesson(id);
    return;
  }
  tab = tab || "notes";
  const tabs = [["notes", "Notes"], ["practice", `Quick questions (${x.questions.length})`], ["transcript", x.source === "audio" ? "Transcript" : "Text"]];
  view.innerHTML = `${head}
    <p class="summary">${esc(x.about ? aboutLine(x) + "." : x.summary)}</p>
    <a class="btn wide" href="#/revise/${esc(x.id)}" style="margin-top:24px">Start revising</a>
    <div class="tabs" role="tablist">${tabs.map(([k, n]) => `<button role="tab" data-tab="${k}" aria-selected="${k === tab}">${n}</button>`).join("")}</div>
    <section id="tab"></section>
    <p><button class="link" id="del">Delete this lesson</button></p>`;
  view.querySelectorAll("[data-tab]").forEach((b) => b.onclick = () => { history.replaceState(null, "", `#/lesson/${id}/${b.dataset.tab}`); lessonView(id, b.dataset.tab); });
  $("#del").onclick = () => deleteLesson(id);
  teacherForm(id, tab);
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

// ---------- Start revising: questions that fit you ----------
const R = { round: null, q: null, tries: 0 };

async function reviseView(id) {
  if (id !== "all") {
    if (!view.querySelector(".getting-ready")) view.innerHTML = `<h1 class="fade getting-ready" style="margin-top:20px">Getting ready…</h1>
      <p class="muted">Buddy is reading the lesson and setting up your questions. This takes about a minute.</p>`;
    const x = await api(`/lessons/${encodeURIComponent(id)}`);
    if (x.status !== "ready") {
      const [cls, label] = STATUS[x.status] || ["", x.status];
      view.innerHTML = `<a class="back" href="#/">← Lessons</a>
        <h1 class="fade getting-ready" style="margin-top:20px">Getting ready…</h1>
        <p class="muted">${x.status === "failed" ? esc(x.error) : "Buddy is reading the lesson and setting up your questions. This takes about a minute."}</p>
        <p><span class="tag ${cls}">${esc(label)}${x.status === "listening" ? ` ${x.progress}%` : ""}</span></p>`;
      if (busy(x)) S.poll = setTimeout(() => location.hash === `#/revise/${id}` && reviseView(id).catch(() => {}), 2500);
      return;
    }
  }
  const r = await api("/revise/start", { method: "POST", body: JSON.stringify({ lesson: id }) });
  if (r.cards) { location.hash = `#/lesson/${id}/practice`; return; }
  R.lesson = id;
  showRound(r);
}

function showRound(r) {
  R.round = r.round;
  if (r.finished) return roundDone(r);
  R.q = r.question; R.tries = 0; R.helped = false;
  const q = r.question;
  view.innerHTML = `
    <div class="rv-top"><a class="back" href="${R.lesson === "all" ? "#/progress" : `#/lesson/${esc(R.lesson)}`}">← Stop</a>
      <span class="muted small">Question ${r.number} of ${r.of}</span></div>
    <div class="bar thin"><i style="width:${100 * (r.number - 1) / r.of}%"></i></div>
    <p class="rv-skill">${esc(q.skill)} <span class="lvl">${"●".repeat(q.level)}${"○".repeat(5 - q.level)}</span></p>
    <div class="rv-q">${esc(q.text).replace(/\n/g, "<br>")}</div>
    ${q.diagram ? drawDiagram(q.diagram) : ""}
    <form id="ans" class="answers" autocomplete="off">
      ${q.fields.map(fieldHTML).join("")}
      <p class="rv-msg" id="msg" role="status"></p>
      <button class="btn wide" id="check">Check</button>
    </form>
    <div class="dock-bar">
      <button type="button" class="help-btn" id="help">Help</button>
      <button type="button" class="calc-btn" id="calc-toggle" aria-label="Calculator" aria-expanded="false">${CALC_ICON}</button>
    </div>
    <div class="help-sheet" id="help-sheet" hidden></div>`;
  $("#ans input")?.focus();
  $("#ans").onsubmit = (e) => { e.preventDefault(); check(); };
  $("#help").onclick = help;
  $("#calc-toggle").onclick = toggleCalc;
  view.querySelectorAll("input[data-expr]").forEach((i) => i.addEventListener("input", () => {
    const p = i.parentElement.querySelector(".preview");
    if (p) p.textContent = prettyExpr(i.value);
  }));
}

function fieldHTML(f, i) {
  const label = f.label ? `<span class="f-label">${esc(f.label)}</span>` : "";
  const pre = f.prefix ? `<span class="f-fix">${esc(f.prefix)}</span>` : "";
  const suf = f.suffix ? `<span class="f-fix">${esc(f.suffix)}</span>` : "";
  if (f.kind === "fraction" || f.kind === "mixed") {
    return `<div class="f-row" data-i="${i}">${label}
      ${f.kind === "mixed" ? `<input class="f-in whole" inputmode="numeric" data-part="w" aria-label="whole number">` : ""}
      <span class="frac"><input class="f-in" inputmode="numeric" data-part="n" aria-label="top number"><i></i>
        <input class="f-in" inputmode="numeric" data-part="d" aria-label="bottom number"></span>${suf}</div>`;
  }
  if (f.kind === "expr") {
    return `<div class="f-row col" data-i="${i}">${label}<input class="f-in wide" data-expr placeholder="e.g. x^2 + 3x - 4" spellcheck="false" autocapitalize="off">
      <span class="preview muted small"></span><span class="muted small">Type ^2 for squared.</span></div>`;
  }
  return `<div class="f-row" data-i="${i}">${label}${pre}<input class="f-in" inputmode="decimal" aria-label="${esc(f.label || "answer")}">${suf}</div>`;
}

const prettyExpr = (v) => v.replace(/\^2/g, "²").replace(/\^3/g, "³").replace(/\*/g, "×").replace(/-/g, " − ").replace(/\+/g, " + ").replace(/\s+/g, " ").trim();

function readValues() {
  return R.q.fields.map((f, i) => {
    const row = view.querySelector(`.f-row[data-i="${i}"]`);
    if (f.kind === "fraction") return [row.querySelector("[data-part=n]").value, row.querySelector("[data-part=d]").value];
    if (f.kind === "mixed") return [row.querySelector("[data-part=w]").value, row.querySelector("[data-part=n]").value, row.querySelector("[data-part=d]").value];
    return row.querySelector("input").value;
  });
}

async function check() {
  const msg = $("#msg"), btn = $("#check");
  const values = readValues();
  if (values.flat().every((v) => !String(v).trim())) { msg.textContent = "Type your answer in the box first."; return; }
  btn.disabled = true;
  try {
    const r = await api("/revise/answer", { method: "POST", body: JSON.stringify({ round: R.round, question: R.q.id, values, tries: R.tries, helped: R.helped }) });
    if (r.round) R.round = r.round;
    if (r.try_again) {
      R.tries++;
      msg.className = "rv-msg bad"; msg.textContent = "Not quite. Have another go, or press Help.";
      btn.disabled = false;
      return;
    }
    view.querySelectorAll("#ans input").forEach((i) => i.disabled = true);
    msg.className = "rv-msg " + (r.right ? "good" : "bad");
    const ans = R.q.fields.map((f, i) => `${f.label ? f.label + " " : ""}${f.prefix || ""}${r.answer[i]}${f.suffix ? (f.suffix === "°" ? "°" : " " + f.suffix) : ""}`).join(", ");
    msg.innerHTML = r.right ? "Correct!" : `The answer was <b>${esc(ans)}</b>.`;
    btn.textContent = "Next question";
    btn.disabled = false;
    $("#ans").onsubmit = async (e) => {
      e.preventDefault();
      btn.disabled = true;
      showRound(await api("/revise/next", { method: "POST", body: JSON.stringify({ round: R.round }) }));
    };
    btn.focus();
  } catch (e) { msg.className = "rv-msg bad"; msg.textContent = e.message; btn.disabled = false; }
}

async function help() {
  R.helped = true;
  const sheet = $("#help-sheet");
  sheet.hidden = false;
  sheet.innerHTML = `<div class="help-head"><b>Help</b><button class="link" id="help-x">Close</button></div><p class="muted">Thinking…</p>`;
  $("#help-x").onclick = () => { sheet.hidden = true; };
  try {
    const { steps } = await api("/revise/help", { method: "POST", body: JSON.stringify({ question: R.q.id }) });
    let shown = 1;
    const draw = () => {
      sheet.innerHTML = `<div class="help-head"><b>Help</b><button class="link" id="help-x">Close</button></div>
        <ol class="steps">${steps.slice(0, shown).map((t) => `<li>${esc(t)}</li>`).join("")}</ol>
        ${shown < steps.length ? `<button class="btn secondary" id="more">Next step</button>` : `<p class="muted small">Now try typing the answer.</p>`}`;
      $("#help-x").onclick = () => { sheet.hidden = true; };
      $("#more")?.addEventListener("click", () => { shown++; draw(); });
    };
    draw();
  } catch (e) { sheet.querySelector("p").textContent = e.message; }
}

function roundDone(r) {
  view.innerHTML = `
    <h1 class="fade" style="margin-top:20px">${r.right} out of ${r.total}</h1>
    <p class="muted">${r.right >= r.total * 0.8 ? "Brilliant. The next questions will be a bit harder." : r.right >= r.total * 0.5 ? "Good work. Keep going to push your level up." : "Tricky ones. The next round will build you back up."}</p>
    <div class="dots big">${r.results.map((ok) => `<i class="${ok ? "on" : "off"}"></i>`).join("")}</div>
    <div class="lessons">${r.skills.map((k) => `<div class="lesson"><div class="t">${esc(k.name)}</div>
      <div class="when">Level ${k.level} of 5</div><div class="bar thin"><i style="width:${k.level * 20}%"></i></div></div>`).join("")}</div>
    <a class="btn wide" href="#/revise/${esc(R.lesson)}" id="again" style="margin-top:28px">Keep going</a>
    <p><a class="back" href="#/">Back to lessons</a></p>`;
  $("#again").onclick = (e) => { e.preventDefault(); reviseView(R.lesson); };
}

async function progressView() {
  const p = await api("/progress");
  view.innerHTML = `<a class="back" href="#/">← Lessons</a>
    <h1 class="fade" style="margin-top:20px">My progress</h1>
    <p class="muted">${p.week.answered ? `This week: ${p.week.answered} questions, ${p.week.right} right, on ${p.week.days} day${p.week.days === 1 ? "" : "s"}.` : "No questions answered this week yet."}</p>
    ${p.skills.length ? `<div class="lessons">${p.skills.map((k) => `<div class="lesson"><div class="t">${esc(k.name)}</div>
      <div class="when">Level ${k.level} of 5 · ${k.right} of ${k.seen} right</div><div class="bar thin"><i style="width:${k.level * 20}%"></i></div></div>`).join("")}</div>
      <a class="btn wide" href="#/revise/all" style="margin-top:28px">Revise everything</a>`
    : `<p class="empty">Start revising a maths lesson and your levels show up here. You can also ask Buddy “how am I doing?”</p>`}`;
}

// A triangle drawn to shape, with its labels.
function drawDiagram(d) {
  if (d.kind !== "triangle") return "";
  const P = d.pts, cx = (P[0][0] + P[1][0] + P[2][0]) / 3, cy = (P[0][1] + P[1][1] + P[2][1]) / 3;
  const away = (x, y, k) => { const dx = x - cx, dy = y - cy, l = Math.hypot(dx, dy) || 1; return [x + dx / l * k, y + dy / l * k]; };
  const toward = (x, y, k) => away(x, y, -k);
  let out = `<polygon points="${P.map((p) => p.join(",")).join(" ")}" class="tri"/>`;
  if (d.right != null) {
    const v = P[d.right], a = P[(d.right + 1) % 3], b = P[(d.right + 2) % 3], s = 14;
    const u = (p) => { const dx = p[0] - v[0], dy = p[1] - v[1], l = Math.hypot(dx, dy); return [dx / l * s, dy / l * s]; };
    const [ax, ay] = u(a), [bx, by] = u(b);
    out += `<polyline points="${v[0] + ax},${v[1] + ay} ${v[0] + ax + bx},${v[1] + ay + by} ${v[0] + bx},${v[1] + by}" class="ra"/>`;
  }
  d.vertices.forEach((name, i) => { const [x, y] = away(...P[i], 16); out += `<text x="${x}" y="${y}" class="vx">${esc(name)}</text>`; });
  d.angles.forEach((t, i) => {
    if (!t) return;
    // Sit the label inside the corner: further in for narrow corners so it clears the lines.
    const v = P[i], a = P[(i + 1) % 3], b = P[(i + 2) % 3];
    const ang = Math.acos(((a[0] - v[0]) * (b[0] - v[0]) + (a[1] - v[1]) * (b[1] - v[1])) / (Math.hypot(a[0] - v[0], a[1] - v[1]) * Math.hypot(b[0] - v[0], b[1] - v[1])));
    const [x, y] = toward(...v, Math.min(70, 16 + 14 / Math.sin(ang / 2)));
    out += `<text x="${x}" y="${y}" class="lab">${esc(t)}</text>`;
  });
  d.sides.forEach((t, i) => {
    if (!t) return;
    const a = P[(i + 1) % 3], b = P[(i + 2) % 3], [x, y] = away((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, 18);
    out += `<text x="${x}" y="${y}" class="lab">${esc(t)}</text>`;
  });
  return `<svg class="diagram" viewBox="-20 -10 300 290" role="img" aria-label="Triangle diagram">${out}</svg>`;
}

// ---------- scientific calculator ----------
const CALC_ICON = `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="3" width="14" height="18" rx="2.5"/><path d="M8 7h8M8.5 11h0M12 11h0M15.5 11h0M8.5 14.5h0M12 14.5h0M15.5 14.5h0M8.5 18h0M12 18h0M15.5 18h0"/></svg>`;
const C = { expr: "", ans: 0, deg: true, shown: false };
const KEYS = [["sin", "cos", "tan", "DEG"], ["x²", "√", "^", "π"], ["(", ")", "AC", "⌫"], ["7", "8", "9", "÷"], ["4", "5", "6", "×"],
              ["1", "2", "3", "−"], ["0", ".", "Ans", "+"], ["sin⁻¹", "cos⁻¹", "tan⁻¹", "="]];

function toggleCalc() {
  let panel = $("#calc");
  if (!panel) {
    panel = document.createElement("div");
    panel.id = "calc"; panel.className = "calc"; panel.hidden = true;
    panel.innerHTML = `<div class="calc-screen"><div id="calc-expr"></div><div id="calc-out">0</div></div>
      <div class="calc-keys">${KEYS.flat().map((k) => `<button type="button" data-k="${k}" class="${/^[0-9.]$/.test(k) ? "d" : k === "=" ? "eq" : ""}">${k}</button>`).join("")}</div>`;
    document.body.append(panel);
    panel.onclick = (e) => { const b = e.target.closest("button"); if (b) calcKey(b.dataset.k); };
  }
  panel.hidden = !panel.hidden;
  $("#calc-toggle")?.setAttribute("aria-expanded", String(!panel.hidden));
  calcDraw();
}

function calcKey(k) {
  const map = { "x²": "^2", "√": "√(", "÷": "÷", "×": "×", "−": "−", "sin": "sin(", "cos": "cos(", "tan": "tan(",
                "sin⁻¹": "asin(", "cos⁻¹": "acos(", "tan⁻¹": "atan(", "π": "π", "Ans": "Ans" };
  if (k === "AC") { C.expr = ""; C.result = ""; }
  else if (k === "⌫") C.expr = C.expr.replace(/(asin\(|acos\(|atan\(|sin\(|cos\(|tan\(|√\(|Ans|.)$/, "");
  else if (k === "DEG") C.deg = !C.deg;
  else if (k === "=") {
    try { C.ans = calcEval(C.expr); C.result = fmt(C.ans); C.expr = ""; } catch { C.result = "Error"; }
  } else C.expr += map[k] ?? k;
  calcDraw();
}
document.addEventListener("keydown", (e) => {
  const panel = $("#calc");
  if (!panel || panel.hidden || e.target.tagName === "INPUT") return;
  if (/^[0-9.+()^]$/.test(e.key)) calcKey(e.key);
  else if (e.key === "-") calcKey("−"); else if (e.key === "*") calcKey("×"); else if (e.key === "/") calcKey("÷");
  else if (e.key === "Enter" || e.key === "=") { e.preventDefault(); calcKey("="); }
  else if (e.key === "Backspace") calcKey("⌫"); else if (e.key === "Escape") calcKey("AC");
});
const fmt = (v) => Number.isFinite(v) ? String(+v.toPrecision(12)) : "Error";
function calcDraw() {
  const e = $("#calc-expr"), o = $("#calc-out");
  if (!e) return;
  e.textContent = C.expr || " ";
  o.textContent = C.result || "0";
  const deg = $('#calc [data-k="DEG"]');
  if (deg) deg.textContent = C.deg ? "DEG" : "RAD";
}

// A small, safe expression reader (no eval): + − × ÷ ^ ( ) √ sin cos tan and inverses, π, Ans.
function calcEval(src) {
  const toks = src.replace(/\s+/g, "").match(/asin|acos|atan|sin|cos|tan|Ans|π|√|\d*\.?\d+|[()+−×÷^-]/g) || [];
  let i = 0;
  const peek = () => toks[i], take = () => toks[i++];
  const rad = (x) => C.deg ? x * Math.PI / 180 : x, unrad = (x) => C.deg ? x * 180 / Math.PI : x;
  const FN = { sin: (x) => Math.sin(rad(x)), cos: (x) => Math.cos(rad(x)), tan: (x) => Math.tan(rad(x)),
               asin: (x) => unrad(Math.asin(x)), acos: (x) => unrad(Math.acos(x)), atan: (x) => unrad(Math.atan(x)), "√": Math.sqrt };
  const fix = (x) => Math.abs(x - Math.round(x)) < 1e-12 ? Math.round(x) : x;
  function expr() { let v = term(); while (peek() === "+" || peek() === "−" || peek() === "-") v = take() === "+" ? v + term() : v - term(); return v; }
  function term() {
    let v = power();
    for (;;) {
      if (peek() === "×") { take(); v *= power(); }
      else if (peek() === "÷") { take(); v /= power(); }
      else if (peek() && /^(\d|\.|\(|π|Ans|√|sin|cos|tan|asin|acos|atan)/.test(peek())) v *= power();  // 2π, 3(4)
      else return v;
    }
  }
  function power() { const b = unary(); if (peek() === "^") { take(); return Math.pow(b, power()); } return b; }
  function unary() { if (peek() === "−" || peek() === "-") { take(); return -unary(); } return atom(); }
  function atom() {
    const t = take();
    if (t === undefined) throw new Error("end");
    if (t === "(") { const v = expr(); if (peek() === ")") take(); return v; }
    if (t === "π") return Math.PI;
    if (t === "Ans") return C.ans;
    if (FN[t]) { const v = unary(); return fix(FN[t](v)); }
    if (/^\d*\.?\d+$/.test(t)) return parseFloat(t);
    throw new Error("bad");
  }
  const v = expr();
  if (i < toks.length) throw new Error("left over");
  return v;
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
  view.classList.remove("wide");
  document.body.classList.remove("signin");
  const calc = $("#calc");
  if (calc) calc.hidden = true;
  try {
    if (S.token && !S.me) S.me = (await api("/me")).profile;
  } catch { /* signed out */ }
  showMe();
  const h = location.hash || "#/";
  try {
    if (!S.me) return await whoView();
    if (h === "#/add") return await addView();
    if (h === "#/progress") return await progressView();
    const rv = h.match(/^#\/revise\/(\w+)/);
    if (rv) return await reviseView(rv[1]);
    const m = h.match(/^#\/lesson\/([0-9a-f]+)(?:\/(\w+))?/);
    if (m) return await lessonView(m[1], m[2]);
    return await homeView();
  } catch (e) {
    view.innerHTML = `<p class="error">${esc(e.message)}</p><a class="btn secondary" href="#/">Back to lessons</a>`;
  }
}
window.addEventListener("hashchange", route);
route();
