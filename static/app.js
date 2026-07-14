/* ================================================================
   CupCut Studio — frontend
   Studio: asset library + preview player + multi-track timeline.
   Tools : one-shot ffmpeg operations with job progress.
   ================================================================ */

"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const uid = () => Math.random().toString(36).slice(2, 10);

function fmtTime(s, withMs = true) {
  if (!isFinite(s) || s < 0) s = 0;
  const m = Math.floor(s / 60);
  const sec = s - m * 60;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  const secStr = withMs ? sec.toFixed(1).padStart(4, "0") : String(Math.floor(sec)).padStart(2, "0");
  return h ? `${h}:${String(mm).padStart(2, "0")}:${secStr}` : `${mm}:${secStr}`;
}

function toast(msg, isError = false, ms = 4200) {
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " error" : "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), ms);
}

async function apiJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return r.json();
}

function pollJob(jobId, onProgress) {
  return new Promise((resolve, reject) => {
    const tick = async () => {
      let job;
      try { job = await apiJSON(`/api/job/${jobId}`); }
      catch (e) { return reject(e); }
      onProgress?.(job);
      if (job.status === "done") return resolve(job);
      if (job.status === "error") return reject(new Error(job.message || "Job failed"));
      setTimeout(tick, 500);
    };
    tick();
  });
}

/* ================================================================
   State
   ================================================================ */

const state = {
  assets: {},                                  // id -> asset
  timeline: { video: [], audioTracks: [[]],    // clips: {id, assetId, start, in, out, volume, muted}
              videoMuted: false, audioMuted: [false] },
  selection: null,                             // clip id
  pps: 12,                                     // pixels per second
  snap: true,
  playhead: 0,
  playing: false,
};

const MAX_AUDIO_TRACKS = 3;
const peaksCache = {};   // assetId -> promise of peaks[]
let undoStack = [], redoStack = [];

function projSettings() {
  const [w, h] = $("#proj-res").value.split("x").map(Number);
  return { width: w, height: h, fps: Number($("#proj-fps").value) };
}

function allClips() {
  return [...state.timeline.video, ...state.timeline.audioTracks.flat()];
}

function findClip(id) {
  let i = state.timeline.video.findIndex(c => c.id === id);
  if (i >= 0) return { clip: state.timeline.video[i], arr: state.timeline.video, trackType: "video", trackIdx: 0 };
  for (let t = 0; t < state.timeline.audioTracks.length; t++) {
    const arr = state.timeline.audioTracks[t];
    i = arr.findIndex(c => c.id === id);
    if (i >= 0) return { clip: arr[i], arr, trackType: "audio", trackIdx: t };
  }
  return null;
}

/** Fill in defaults so older saved projects / undo snapshots stay valid. */
function normalizeTimeline(t) {
  t.video = t.video || [];
  t.audioTracks = (t.audioTracks && t.audioTracks.length) ? t.audioTracks : [[]];
  t.videoMuted = !!t.videoMuted;
  t.audioMuted = Array.isArray(t.audioMuted) ? t.audioMuted : [];
  while (t.audioMuted.length < t.audioTracks.length) t.audioMuted.push(false);
  t.audioMuted.length = t.audioTracks.length;
  return t;
}

function clipDur(c) { return c.out - c.in; }
function clipEnd(c) { return c.start + clipDur(c); }
function timelineDuration() {
  return allClips().reduce((m, c) => Math.max(m, clipEnd(c)), 0);
}

function pushHistory() {
  undoStack.push(JSON.stringify(state.timeline));
  if (undoStack.length > 80) undoStack.shift();
  redoStack = [];
}

function undo() {
  if (!undoStack.length) return;
  redoStack.push(JSON.stringify(state.timeline));
  state.timeline = normalizeTimeline(JSON.parse(undoStack.pop()));
  state.selection = null;
  renderTimeline(); saveLocal();
}

function redo() {
  if (!redoStack.length) return;
  undoStack.push(JSON.stringify(state.timeline));
  state.timeline = normalizeTimeline(JSON.parse(redoStack.pop()));
  state.selection = null;
  renderTimeline(); saveLocal();
}

let projSaveTimer = null;

function projectPayload() {
  return {
    timeline: state.timeline,
    res: $("#proj-res").value, fps: $("#proj-fps").value,
  };
}

/* Saved in two places: localStorage (instant) and the server (survives
   browser data clearing / different browsers). */
function saveLocal() {
  try { localStorage.setItem("cupcut_project", JSON.stringify(projectPayload())); } catch {}
  clearTimeout(projSaveTimer);
  projSaveTimer = setTimeout(() => {
    fetch("/api/project", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(projectPayload()),
    }).catch(() => {});
  }, 700);
}

function applyProject(p) {
  if (!p) return false;
  if (p.res) $("#proj-res").value = p.res;
  if (p.fps) $("#proj-fps").value = p.fps;
  if (!p.timeline) return false;
  // drop clips whose asset disappeared
  p.timeline.video = (p.timeline.video || []).filter(c => state.assets[c.assetId]);
  p.timeline.audioTracks = (p.timeline.audioTracks || [[]])
    .map(tr => tr.filter(c => state.assets[c.assetId]));
  state.timeline = normalizeTimeline(p.timeline);
  return true;
}

async function restoreProject() {
  let server = null;
  try { server = await apiJSON("/api/project"); } catch {}
  if (applyProject(server)) return;
  try { applyProject(JSON.parse(localStorage.getItem("cupcut_project"))); } catch {}
}

/* ================================================================
   Asset library
   ================================================================ */

async function loadAssets() {
  const list = await apiJSON("/api/assets");
  state.assets = {};
  for (const a of list) state.assets[a.id] = a;
  renderLibrary();
}

async function uploadFiles(files) {
  for (const f of files) {
    const card = document.createElement("div");
    card.className = "asset uploading";
    card.innerHTML = `<div class="asset-thumb">⏳</div>
      <div class="asset-meta"><div class="asset-name"></div>
      <div class="asset-sub">uploading…</div></div>`;
    $(".asset-name", card).textContent = f.name;
    $("#asset-list").appendChild(card);
    $("#lib-empty")?.remove();
    try {
      const fd = new FormData();
      fd.append("file", f);
      const asset = await apiJSON("/api/upload", { method: "POST", body: fd });
      state.assets[asset.id] = asset;
    } catch (e) {
      toast(`Upload failed: ${f.name}\n${e.message}`, true);
    }
    card.remove();
    renderLibrary();
  }
}

const KIND_ICON = { video: "🎞", audio: "🎵", image: "🖼" };

async function deleteAsset(a) {
  if (!confirm(`Remove "${a.name}" from the library?\nClips using it will be removed from the timeline.`)) return;
  await fetch(`/api/assets/${a.id}`, { method: "DELETE" });
  delete state.assets[a.id];
  pushHistory();
  state.timeline.video = state.timeline.video.filter(c => c.assetId !== a.id);
  state.timeline.audioTracks = state.timeline.audioTracks.map(tr => tr.filter(c => c.assetId !== a.id));
  renderLibrary(); renderTimeline(); saveLocal();
}

function renderLibrary() {
  const list = $("#asset-list");
  list.innerHTML = "";
  const assets = Object.values(state.assets);
  if (!assets.length) {
    list.innerHTML = `<div class="lib-empty" id="lib-empty">
      <div class="lib-empty-icon">📂</div>
      Drop video, audio or images here<br>or click <b>＋ Import</b></div>`;
    return;
  }
  for (const a of assets) {
    const card = document.createElement("div");
    card.className = "asset";
    card.dataset.id = a.id;
    const thumb = document.createElement("div");
    thumb.className = "asset-thumb" + (a.kind === "audio" ? " audio" : "");
    if (a.kind === "audio") thumb.textContent = "🎵";
    else thumb.style.backgroundImage = `url(/api/thumb/${a.id})`;
    const sub = a.kind === "image"
      ? `${a.width}×${a.height}`
      : `${fmtTime(a.duration)}${a.kind === "video" ? ` · ${a.width}×${a.height}` : ""}`;
    card.appendChild(thumb);
    card.insertAdjacentHTML("beforeend",
      `<div class="asset-meta"><div class="asset-name"></div>
       <div class="asset-sub">${KIND_ICON[a.kind] || ""} ${sub}</div></div>
       <button class="asset-del" title="Remove from library">✕</button>`);
    $(".asset-name", card).textContent = a.name;
    $(".asset-del", card).addEventListener("click", (e) => {
      e.stopPropagation();
      deleteAsset(a);
    });
    card.addEventListener("dblclick", () => addAssetToTimeline(a, null, null));
    card.addEventListener("pointerdown", (e) => beginLibraryDrag(e, a));
    card.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      buildCtxMenu([
        { label: "💾 Save as…", fn: () => saveAssetAs(a) },
        { label: "➕ Add to timeline", fn: () => addAssetToTimeline(a, null, null) },
        "sep",
        { label: "🗑 Remove from library", fn: () => deleteAsset(a), danger: true },
      ], e.clientX, e.clientY);
    });
    list.appendChild(card);
  }
}

/* ---------------- library drag → timeline ---------------- */

function beginLibraryDrag(e, asset) {
  if (e.button !== 0 || e.target.closest(".asset-del")) return;
  const startX = e.clientX, startY = e.clientY;
  const ghost = $("#drag-ghost");
  let dragging = false;

  const move = (ev) => {
    if (!dragging && Math.hypot(ev.clientX - startX, ev.clientY - startY) > 6) {
      dragging = true;
      ghost.hidden = false;
      ghost.textContent = `${KIND_ICON[asset.kind] || ""} ${asset.name}`;
    }
    if (!dragging) return;
    ghost.style.left = ev.clientX + "px";
    ghost.style.top = ev.clientY + "px";
    $$(".track").forEach(t => t.classList.remove("droptarget"));
    const tr = trackUnderPoint(ev.clientX, ev.clientY, asset);
    if (tr) tr.el.classList.add("droptarget");
  };
  const up = (ev) => {
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", up);
    ghost.hidden = true;
    $$(".track").forEach(t => t.classList.remove("droptarget"));
    if (!dragging) return;
    const tr = trackUnderPoint(ev.clientX, ev.clientY, asset);
    if (tr) {
      const time = Math.max(0, (ev.clientX - tr.el.getBoundingClientRect().left) / state.pps);
      addAssetToTimeline(asset, tr, time);
    }
  };
  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", up);
}

/** Which track is under (x,y) and can accept this asset? */
function trackUnderPoint(x, y, asset) {
  const el = document.elementFromPoint(x, y);
  if (!el) return null;
  const trackEl = el.closest(".track");
  if (trackEl) {
    const type = trackEl.dataset.type;
    const idx = Number(trackEl.dataset.idx);
    if (type === "video" && (asset.kind === "video" || asset.kind === "image"))
      return { el: trackEl, type, idx };
    if (type === "audio" && (asset.kind === "audio" || (asset.kind === "video" && asset.hasAudio)))
      return { el: trackEl, type, idx };
    return null;
  }
  // dropped in the timeline area but not on a specific track → default track
  if (el.closest("#timeline-scroll")) {
    const type = (asset.kind === "audio") ? "audio" : "video";
    const q = type === "video" ? '.track[data-type="video"]' : '.track[data-type="audio"]';
    const target = $(q);
    if (target) return { el: target, type, idx: Number(target.dataset.idx) };
  }
  return null;
}

const DEFAULT_IMAGE_DUR = 4;

function addAssetToTimeline(asset, track, time) {
  if (!track) {
    const type = asset.kind === "audio" ? "audio" : "video";
    track = { type, idx: 0 };
    time = null; // append at end of that track
  }
  const arr = track.type === "video" ? state.timeline.video
                                     : state.timeline.audioTracks[track.idx];
  if (!arr) return;
  if (track.type === "video" && asset.kind === "audio") { toast("Audio goes on the audio tracks 🎵"); return; }
  if (track.type === "audio" && asset.kind === "image") { toast("Images go on the video track 🎞"); return; }

  const dur = asset.kind === "image" ? DEFAULT_IMAGE_DUR : asset.duration;
  const clip = {
    id: uid(), assetId: asset.id,
    start: time ?? arr.reduce((m, c) => Math.max(m, clipEnd(c)), 0),
    in: 0, out: dur, volume: 1, muted: false,
  };
  pushHistory();
  placeClip(arr, clip);
  state.selection = clip.id;
  renderTimeline(); saveLocal();
}

/** Insert clip into arr, pushing it right until it doesn't overlap. */
function placeClip(arr, clip) {
  const sorted = [...arr].sort((a, b) => a.start - b.start);
  for (const c of sorted) {
    const overlaps = clip.start < clipEnd(c) && clipEnd(clip) > c.start;
    if (overlaps) clip.start = clipEnd(c);
  }
  arr.push(clip);
}

/* ================================================================
   Timeline rendering
   ================================================================ */

const trackHeads = $("#track-heads");
const tracksEl = $("#tracks");
const rulerEl = $("#ruler");
const tlScroll = $("#timeline-scroll");
const tlContent = $("#timeline-content");

function contentWidth() {
  const visible = tlScroll.clientWidth || 800;
  return Math.max((timelineDuration() + 30) * state.pps, visible);
}

function renderTimeline() {
  const width = contentWidth();
  tlContent.style.width = width + "px";

  /* ---- ruler ---- */
  rulerEl.innerHTML = "";
  const steps = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  const step = steps.find(s => s * state.pps >= 68) || 600;
  const minor = step / 5;
  const frag = document.createDocumentFragment();
  // Compute each tick's time from its index (i * minor accumulates no
  // floating-point drift; "t += minor" does, which mislabeled ticks).
  const tickCount = Math.ceil(width / (minor * state.pps));
  for (let i = 0; i <= tickCount; i++) {
    const isMajor = i % 5 === 0;
    const t = isMajor ? (i / 5) * step : i * minor;
    const tick = document.createElement("div");
    tick.className = "ruler-tick" + (isMajor ? "" : " minor");
    tick.style.left = (t * state.pps) + "px";
    if (isMajor) tick.textContent = fmtTime(t, step < 1);
    frag.appendChild(tick);
  }
  rulerEl.appendChild(frag);

  /* ---- track heads ---- */
  $$(".track-head", trackHeads).forEach(el => el.remove());
  const addHead = (label, cls, extra) => {
    const h = document.createElement("div");
    h.className = "track-head " + cls;
    h.innerHTML = label;
    if (extra) extra(h);
    trackHeads.appendChild(h);
    return h;
  };
  const mkMuteBtn = (muted, onToggle) => {
    const b = document.createElement("button");
    b.className = "th-mute" + (muted ? " on" : "");
    b.textContent = muted ? "🔇" : "🔊";
    b.title = muted ? "Unmute track" : "Mute track (preview & export)";
    b.addEventListener("click", (e) => { e.stopPropagation(); onToggle(); });
    return b;
  };
  const vh = addHead("🎞 Video", "video-head");
  vh.style.height = "64px";
  vh.appendChild(mkMuteBtn(state.timeline.videoMuted, () => {
    pushHistory();
    state.timeline.videoMuted = !state.timeline.videoMuted;
    renderTimeline(); saveLocal();
  }));
  state.timeline.audioTracks.forEach((tr, i) => {
    const h = addHead(`🎵 Audio ${i + 1}`, "audio-head");
    h.style.height = "54px";
    h.appendChild(mkMuteBtn(!!state.timeline.audioMuted[i], () => {
      pushHistory();
      state.timeline.audioMuted[i] = !state.timeline.audioMuted[i];
      renderTimeline(); saveLocal();
    }));
    if (state.timeline.audioTracks.length > 1) {
      const del = document.createElement("button");
      del.className = "th-del"; del.textContent = "✕"; del.title = "Remove track";
      del.addEventListener("click", () => {
        if (tr.length && !confirm(`Audio ${i + 1} has ${tr.length} clip(s). Remove anyway?`)) return;
        pushHistory();
        state.timeline.audioTracks.splice(i, 1);
        state.timeline.audioMuted.splice(i, 1);
        renderTimeline(); saveLocal();
      });
      h.appendChild(del);
    }
  });
  if (state.timeline.audioTracks.length < MAX_AUDIO_TRACKS) {
    const add = addHead("＋ track", "add-track");
    add.addEventListener("click", () => {
      pushHistory();
      state.timeline.audioTracks.push([]);
      state.timeline.audioMuted.push(false);
      renderTimeline(); saveLocal();
    });
  }

  /* ---- tracks & clips ---- */
  tracksEl.innerHTML = "";
  const mkTrack = (type, idx, arr) => {
    const t = document.createElement("div");
    const trackMuted = type === "video" ? state.timeline.videoMuted
                                        : !!state.timeline.audioMuted[idx];
    t.className = `track ${type}-track` + (trackMuted ? " muted" : "");
    t.dataset.type = type;
    t.dataset.idx = idx;
    for (const clip of arr) t.appendChild(buildClipEl(clip, type));
    t.addEventListener("pointerdown", (e) => {
      if (e.target !== t) return;         // clip handles its own events
      state.selection = null;
      updateSelectionUI();
      scrubFromEvent(e);
    });
    tracksEl.appendChild(t);
  };
  mkTrack("video", 0, state.timeline.video);
  state.timeline.audioTracks.forEach((tr, i) => mkTrack("audio", i, tr));

  updatePlayheadEl();
  updateSelectionUI();
  updateTimeDisplay();
}

function buildClipEl(clip, trackType) {
  const asset = state.assets[clip.assetId];
  const el = document.createElement("div");
  const kind = trackType === "audio" ? "audio" : (asset?.kind === "image" ? "image" : "video");
  el.className = `clip ${kind}` + (clip.id === state.selection ? " selected" : "");
  el.dataset.id = clip.id;
  el.style.left = (clip.start * state.pps) + "px";
  el.style.width = Math.max(clipDur(clip) * state.pps, 14) + "px";

  if (kind === "video" && asset) {
    const strip = document.createElement("div");
    strip.className = "clip-strip";
    strip.style.backgroundImage = `url(/api/filmstrip/${asset.id})`;
    // anchor thumbnails to the content, so left-trimming doesn't look like
    // the video is sliding along with the box
    strip.style.backgroundPosition = `${(-clip.in * state.pps).toFixed(1)}px 0`;
    el.appendChild(strip);
  } else if (kind === "image" && asset) {
    const strip = document.createElement("div");
    strip.className = "clip-strip";
    strip.style.backgroundImage = `url(/api/thumb/${asset.id})`;
    strip.style.backgroundSize = "auto 100%";
    el.appendChild(strip);
  } else if (kind === "audio" && asset) {
    const canvas = document.createElement("canvas");
    canvas.className = "clip-wave";
    el.appendChild(canvas);
    drawWave(canvas, clip, asset);
  }

  const label = document.createElement("div");
  label.className = "clip-label";
  label.textContent = asset ? asset.name : "?";
  el.appendChild(label);

  if (clip.muted) {
    const m = document.createElement("div");
    m.className = "clip-muted-ico";
    m.textContent = "🔇";
    el.appendChild(m);
  }

  const hl = document.createElement("div");
  hl.className = "clip-handle left";
  const hr = document.createElement("div");
  hr.className = "clip-handle right";
  el.append(hl, hr);

  el.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.stopPropagation();
    state.selection = clip.id;
    updateSelectionUI();
    if (e.target === hl) beginClipEdit(e, clip, el, "trim-left");
    else if (e.target === hr) beginClipEdit(e, clip, el, "trim-right");
    else beginClipEdit(e, clip, el, "move");
  });
  el.addEventListener("contextmenu", (e) => showClipMenu(e, clip));
  return el;
}

/* ---------------- clip context menu ---------------- */

function hideClipMenu() { $("#ctx-menu")?.remove(); }

function showClipMenu(e, clip) {
  e.preventDefault();
  e.stopPropagation();
  hideClipMenu();
  state.selection = clip.id;
  updateSelectionUI();
  const asset = state.assets[clip.assetId];
  const phInside = state.playhead > clip.start + 0.05 &&
                   state.playhead < clipEnd(clip) - 0.05;

  const items = [
    { label: "✂ Split at playhead", fn: splitSelected, disabled: !phInside },
    { label: "⧉ Duplicate", fn: duplicateSelected },
    { label: clip.muted ? "🔊 Unmute" : "🔇 Mute", fn: toggleMuteSelected },
  ];
  if (asset?.kind === "video") {
    if (asset.hasAudio)
      items.push({ label: "🧲 Match to audio track", fn: () => matchToAudio(clip) });
    items.push("sep",
      { label: "⏸ Last frame → still video… ▸", keepOpen: true,
        fn: (menu) => showFreezeSubmenu(menu, clip) },
      { label: "📸 Save last frame as PNG", fn: () => saveFrame(clip, clip.out - 0.04) },
      { label: "📸 Save first frame as PNG", fn: () => saveFrame(clip, clip.in) });
    if (phInside)
      items.push({ label: "📸 Save frame at playhead",
                   fn: () => saveFrame(clip, clip.in + state.playhead - clip.start) });
  }
  items.push({ label: "💾 Save as… (source file)", fn: () => saveAssetAs(asset) });
  items.push("sep", { label: "🗑 Delete", fn: deleteSelected, danger: true });

  buildCtxMenu(items, e.clientX, e.clientY);
}

function buildCtxMenu(items, x, y) {
  hideClipMenu();
  const menu = document.createElement("div");
  menu.id = "ctx-menu";
  for (const it of items) {
    if (it === "sep") {
      menu.insertAdjacentHTML("beforeend", `<div class="sep"></div>`);
      continue;
    }
    const b = document.createElement("button");
    b.textContent = it.label;
    if (it.danger) b.className = "danger";
    if (it.disabled) b.disabled = true;
    b.addEventListener("click", () => {
      if (it.keepOpen) it.fn(menu);
      else { hideClipMenu(); it.fn(); }
    });
    menu.appendChild(b);
  }
  document.body.appendChild(menu);
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  menu.style.left = Math.min(x, innerWidth - mw - 8) + "px";
  menu.style.top = Math.min(y, innerHeight - mh - 8) + "px";
  return menu;
}

/** "Save as…" with a native file picker (Chrome); falls back to a download. */
async function saveAssetAs(asset) {
  if (!asset) return;
  if (window.showSaveFilePicker) {
    let handle;
    try {
      handle = await showSaveFilePicker({ suggestedName: asset.name });
    } catch {
      return; // user cancelled the dialog
    }
    try {
      toast("💾 Saving…", false, 2000);
      const resp = await fetch(`/media/${asset.id}`, { cache: "no-store" });
      if (!resp.ok) throw new Error(`server returned ${resp.status}`);
      const blob = await resp.blob();
      if (!blob.size) throw new Error("server sent an empty file");
      const writable = await handle.createWritable();
      await writable.write(blob);
      await writable.close();
      toast(`💾 Saved: ${handle.name || asset.name} (${(blob.size / 1048576).toFixed(1)} MB)`);
      return;
    } catch (e) {
      toast("Save-as failed (" + e.message + ") — downloading instead…", true, 6000);
    }
  }
  const aEl = document.createElement("a");
  aEl.href = `/media/${asset.id}`;
  aEl.download = asset.name;
  document.body.appendChild(aEl);
  aEl.click();
  aEl.remove();
}

document.addEventListener("pointerdown", (e) => {
  if (!e.target.closest("#ctx-menu")) hideClipMenu();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideClipMenu(); });

/** Auto-align a video clip to the audio clip beneath it via cross-correlation. */
async function matchToAudio(clip) {
  // find the audio clip whose timeline range overlaps this video clip
  let target = null;
  outer:
  for (const tr of state.timeline.audioTracks) {
    for (const ac of tr) {
      if (clip.start < clipEnd(ac) && clipEnd(clip) > ac.start) { target = ac; break outer; }
    }
  }
  if (!target) {
    toast("Place the video roughly over an audio clip first — then I can calibrate against it.", true);
    return;
  }
  // approximate offset inside the audio SOURCE that corresponds to clip.start
  const approx = clip.start - target.start + target.in;
  toast("🧲 Analyzing audio similarity…", false, 2500);
  try {
    const r = await apiJSON("/api/match_audio", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        videoAssetId: clip.assetId, audioAssetId: target.assetId,
        in: clip.in, out: clip.out, approx, window: 5,
      }),
    });
    const pct = Math.round(r.confidence * 100);
    if (r.confidence < 0.5) {
      toast(`No reliable match near this position (confidence ${pct}%).\nPlace the clip closer to the right spot and try again.`, true, 6000);
      return;
    }
    const newStart = Math.max(0, target.start - target.in + r.offset);
    pushHistory();
    // keep it from overlapping neighbours on the video track
    const info = findClip(clip.id);
    const others = info.arr.filter(c => c.id !== clip.id).sort((a, b) => a.start - b.start);
    const prev = others.filter(c => clipEnd(c) <= clip.start + 0.001).pop();
    const next = others.find(c => c.start >= clipEnd(clip) - 0.001);
    const lo = prev ? clipEnd(prev) : 0;
    const hi = next ? next.start - clipDur(clip) : Infinity;
    clip.start = Math.min(Math.max(newStart, lo), hi);
    renderTimeline(); saveLocal();
    const clamped = Math.abs(clip.start - newStart) > 0.01;
    toast(`🧲 Matched at ${fmtTime(newStart)} — confidence ${pct}%` +
          (clamped ? "\n(shifted to avoid overlapping a neighbouring clip)" : ""), false, 6000);
  } catch (e) {
    toast("Match failed: " + e.message, true, 7000);
  }
}

/** Duration picker (1–20 s) shown inside the context menu. */
function showFreezeSubmenu(menu, clip) {
  menu.innerHTML = `<div class="ctx-head">⏸ Still video from last frame — length:</div>`;
  const grid = document.createElement("div");
  grid.className = "ctx-grid";
  for (let s = 1; s <= 20; s++) {
    const b = document.createElement("button");
    b.textContent = s + "s";
    b.addEventListener("click", () => { hideClipMenu(); freezeLastFrame(clip, s); });
    grid.appendChild(b);
  }
  menu.appendChild(grid);
}

/** Make a still video of the clip's last frame + the song slice that starts
    where the clip ends (like the Image+Audio→MP4 tool, but on the fly).
    Adds it to the library and inserts it right after the clip. */
async function freezeLastFrame(clip, dur) {
  const s = projSettings();
  const at = clipEnd(clip); // timeline point where the freeze begins

  // find the audio clip lying under that timeline point — mute states are
  // ignored on purpose: muted is a playback setting, the content is still there
  let song = null, songAudible = false;
  for (let ti = 0; ti < state.timeline.audioTracks.length && !song; ti++) {
    for (const ac of state.timeline.audioTracks[ti]) {
      if (at >= ac.start - 0.01 && at < clipEnd(ac)) {
        song = ac;
        songAudible = !ac.muted && !state.timeline.audioMuted[ti];
        break;
      }
    }
  }
  toast(`⏸ Creating ${dur}s still video` +
        (song ? " with the song continuing from the cut point…" : " (no audio track under this point — silent)…"));
  try {
    const asset = await apiJSON("/api/freeze_video", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        assetId: clip.assetId, t: clip.out - 0.04, duration: dur,
        width: s.width, height: s.height, fps: s.fps,
        audioAssetId: song ? song.assetId : null,
        audioStart: song ? at - song.start + song.in : 0,
      }),
    });
    state.assets[asset.id] = asset;
    renderLibrary();
    pushHistory();
    for (const c of state.timeline.video)
      if (c.id !== clip.id && c.start >= at - 0.01) c.start += asset.duration;
    // if the song track is audible below, mute the inserted clip so the sound
    // isn't doubled; if the track is muted, keep the clip's own audio audible
    const newClip = { id: uid(), assetId: asset.id, start: at,
                      in: 0, out: asset.duration, volume: 1,
                      muted: !!song && songAudible };
    state.timeline.video.push(newClip);
    state.selection = newClip.id;
    renderTimeline(); saveLocal();
    toast(`⏸ ${dur}s freeze-frame${song ? " + song audio" : ""} inserted after the clip (also in Media)`);
  } catch (e) {
    toast("Freeze failed: " + e.message, true, 7000);
  }
}

function saveFrame(clip, srcTime) {
  const a = document.createElement("a");
  a.href = `/api/frame/${clip.assetId}?t=${Math.max(0, srcTime).toFixed(3)}`;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
  toast("📸 Extracting frame — download starts in a moment…");
}

function drawWave(canvas, clip, asset) {
  const render = (peaks) => {
    if (!peaks?.length) return;
    const w = Math.min(Math.max(clipDur(clip) * state.pps, 14), 3000);
    const h = 54;
    canvas.width = w; canvas.height = h;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#34d399";
    ctx.globalAlpha = 0.8;
    const total = asset.duration || 1;
    const i0 = Math.floor((clip.in / total) * peaks.length);
    const i1 = Math.min(peaks.length, Math.ceil((clip.out / total) * peaks.length));
    const n = Math.max(1, i1 - i0);
    const barW = Math.max(1, w / n);
    for (let i = 0; i < n; i++) {
      const p = peaks[i0 + i] || 0;
      const bh = Math.max(1, p * (h - 8));
      ctx.fillRect(i * barW, (h - bh) / 2, Math.max(1, barW - 0.5), bh);
    }
  };
  if (!peaksCache[asset.id]) {
    peaksCache[asset.id] = apiJSON(`/api/waveform/${asset.id}`).then(d => d.peaks).catch(() => []);
  }
  peaksCache[asset.id].then(render);
}

/* ---------------- clip move / trim ---------------- */

function snapTo(value, exclude, extra = []) {
  if (!state.snap) return value;
  const pts = [0, state.playhead, ...extra];
  for (const c of allClips()) {
    if (c.id === exclude) continue;
    pts.push(c.start, clipEnd(c));
  }
  const tol = 8 / state.pps;
  let best = value, bestD = tol;
  for (const p of pts) {
    const d = Math.abs(p - value);
    if (d < bestD) { best = p; bestD = d; }
  }
  return best;
}

function beginClipEdit(e, clip, el, mode) {
  const info = findClip(clip.id);
  if (!info) return;
  const asset = state.assets[clip.assetId];
  const isImage = asset?.kind === "image";
  const orig = { ...clip };
  const startX = e.clientX;
  let moved = false;

  // neighbours on the same track (for collision clamping)
  const others = info.arr.filter(c => c.id !== clip.id).sort((a, b) => a.start - b.start);
  const prev = others.filter(c => clipEnd(c) <= orig.start + 0.001).pop();
  const next = others.find(c => c.start >= clipEnd(orig) - 0.001);
  const minStart = prev ? clipEnd(prev) : 0;
  const maxEnd = next ? next.start : Infinity;

  const move = (ev) => {
    const dx = (ev.clientX - startX) / state.pps;
    if (!moved && Math.abs(ev.clientX - startX) > 3) { moved = true; pushHistory(); }
    if (!moved) return;

    if (mode === "move") {
      let s = snapTo(orig.start + dx, clip.id, [orig.start]);
      let end = s + clipDur(orig);
      const snappedEnd = snapTo(end, clip.id);
      if (snappedEnd !== end) s = snappedEnd - clipDur(orig);
      s = Math.max(minStart, Math.min(s, maxEnd - clipDur(orig)));
      clip.start = Math.max(0, s);
    } else if (mode === "trim-left") {
      let edge = snapTo(orig.start + dx, clip.id);
      edge = Math.max(minStart, Math.min(edge, clipEnd(orig) - 0.1));
      const delta = edge - orig.start;
      if (isImage) {
        clip.start = edge;
        clip.out = orig.out - delta;
      } else {
        const newIn = Math.max(0, orig.in + delta);
        clip.start = orig.start + (newIn - orig.in);
        clip.in = newIn;
      }
    } else if (mode === "trim-right") {
      let edge = snapTo(clipEnd(orig) + dx, clip.id);
      edge = Math.min(maxEnd, Math.max(edge, orig.start + 0.1));
      if (isImage) {
        clip.out = edge - orig.start;
      } else {
        clip.out = Math.min(asset ? asset.duration : Infinity, orig.in + (edge - orig.start));
      }
    }
    el.style.left = (clip.start * state.pps) + "px";
    el.style.width = Math.max(clipDur(clip) * state.pps, 14) + "px";
    if (mode === "trim-left") {
      const strip = el.querySelector(".clip-strip");
      if (strip) strip.style.backgroundPosition = `${(-clip.in * state.pps).toFixed(1)}px 0`;
    }
    updateTimeDisplay();
  };
  const up = () => {
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", up);
    if (moved) { renderTimeline(); saveLocal(); }
  };
  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", up);
}

/* ---------------- playhead / scrubbing ---------------- */

function updatePlayheadEl() {
  $("#playhead").style.left = (state.playhead * state.pps) + "px";
}

function setPlayhead(t, updateFrame = true) {
  state.playhead = Math.max(0, t);
  updatePlayheadEl();
  updateTimeDisplay();
  if (updateFrame && !state.playing) updateStage(state.playhead, false);
}

function scrubFromEvent(e) {
  const rect = tlContent.getBoundingClientRect();
  const timeAt = (ev) => Math.max(0, (ev.clientX - rect.left) / state.pps);
  setPlayhead(timeAt(e));
  const move = (ev) => setPlayhead(timeAt(ev));
  const up = () => {
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", up);
  };
  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", up);
}

rulerEl.addEventListener("pointerdown", (e) => { if (state.playing) stopPlayback(); scrubFromEvent(e); });

function updateTimeDisplay() {
  $("#time-cur").textContent = fmtTime(state.playhead);
  $("#time-total").textContent = fmtTime(timelineDuration());
}

/* ================================================================
   Preview playback engine
   ================================================================ */

const stageInner = $("#stage-inner");

/* Size the preview box to fit the stage panel at the project aspect ratio.
   (It only has absolutely-positioned children, so it can't auto-size.) */
function fitStage() {
  const stage = $("#stage");
  const s = projSettings();
  const availW = stage.clientWidth - 28;
  const availH = stage.clientHeight - 28;
  if (availW <= 0 || availH <= 0) return;
  const ratio = s.width / s.height;
  let w = availW, h = w / ratio;
  if (h > availH) { h = availH; w = h * ratio; }
  stageInner.style.width = Math.round(w) + "px";
  stageInner.style.height = Math.round(h) + "px";
}

const videoEls = {};   // assetId -> <video>
const imgEls = {};     // assetId -> <img>
const audioEls = {};   // clipId  -> HTMLAudioElement
let rafId = null, playT0 = 0, playWall0 = 0;

function ensureVideoEl(asset) {
  if (!videoEls[asset.id]) {
    const v = document.createElement("video");
    v.src = `/media/${asset.id}`;
    v.preload = "auto";
    v.playsInline = true;
    stageInner.appendChild(v);
    videoEls[asset.id] = v;
  }
  return videoEls[asset.id];
}

function ensureImgEl(asset) {
  if (!imgEls[asset.id]) {
    const im = document.createElement("img");
    im.className = "stage-img";
    im.src = `/media/${asset.id}`;
    stageInner.appendChild(im);
    imgEls[asset.id] = im;
  }
  return imgEls[asset.id];
}

function ensureAudioEl(clip) {
  if (!audioEls[clip.id]) {
    const a = new Audio(`/media/${clip.assetId}`);
    a.preload = "auto";
    audioEls[clip.id] = a;
  }
  return audioEls[clip.id];
}

function updateStage(t, playing) {
  // ---- video track ----
  const vclip = state.timeline.video.find(c => t >= c.start && t < clipEnd(c));
  let activeVisual = null;
  if (vclip) {
    const asset = state.assets[vclip.assetId];
    if (asset?.kind === "image") {
      activeVisual = ensureImgEl(asset);
    } else if (asset) {
      const v = ensureVideoEl(asset);
      const target = t - vclip.start + vclip.in;
      if (Math.abs(v.currentTime - target) > 0.12) v.currentTime = target;
      v.volume = (vclip.muted || state.timeline.videoMuted)
        ? 0 : Math.min(1, vclip.volume ?? 1);
      if (playing && v.paused) v.play().catch(() => {});
      if (!playing && !v.paused) v.pause();
      activeVisual = v;
    }
  }
  for (const el of [...Object.values(videoEls), ...Object.values(imgEls)]) {
    const active = el === activeVisual;
    el.style.display = active ? "block" : "none";
    if (!active && el.tagName === "VIDEO" && !el.paused) el.pause();
  }
  $("#stage-blank").style.display = activeVisual ? "none" : "flex";

  // ---- audio tracks ----
  const activeIds = new Set();
  for (let ti = 0; ti < state.timeline.audioTracks.length; ti++) {
    if (state.timeline.audioMuted[ti]) continue;
    const tr = state.timeline.audioTracks[ti];
    for (const clip of tr) {
      if (t >= clip.start && t < clipEnd(clip) && !clip.muted) {
        activeIds.add(clip.id);
        const a = ensureAudioEl(clip);
        const target = t - clip.start + clip.in;
        if (Math.abs(a.currentTime - target) > 0.12) a.currentTime = target;
        a.volume = Math.min(1, clip.volume ?? 1);
        if (playing && a.paused) a.play().catch(() => {});
        if (!playing && !a.paused) a.pause();
      }
    }
  }
  for (const [cid, a] of Object.entries(audioEls)) {
    if (!activeIds.has(cid) && !a.paused) a.pause();
  }
}

function startPlayback() {
  const dur = timelineDuration();
  if (dur <= 0) { toast("Timeline is empty — drag clips from Media."); return; }
  if (state.playhead >= dur - 0.05) state.playhead = 0;
  state.playing = true;
  $("#btn-play").textContent = "⏸";
  playT0 = state.playhead;
  playWall0 = performance.now();
  const tick = () => {
    if (!state.playing) return;
    const t = playT0 + (performance.now() - playWall0) / 1000;
    if (t >= timelineDuration()) {
      setPlayhead(timelineDuration());
      stopPlayback();
      return;
    }
    state.playhead = t;
    updatePlayheadEl();
    updateTimeDisplay();
    updateStage(t, true);
    rafId = requestAnimationFrame(tick);
  };
  rafId = requestAnimationFrame(tick);
}

function stopPlayback() {
  state.playing = false;
  $("#btn-play").textContent = "▶";
  if (rafId) cancelAnimationFrame(rafId);
  updateStage(state.playhead, false);
}

function togglePlay() { state.playing ? stopPlayback() : startPlayback(); }

/* ================================================================
   Toolbar actions
   ================================================================ */

function selectedInfo() {
  return state.selection ? findClip(state.selection) : null;
}

function updateSelectionUI() {
  $$(".clip").forEach(el =>
    el.classList.toggle("selected", el.dataset.id === state.selection));
  const info = selectedInfo();
  const has = !!info;
  ["#btn-split", "#btn-dup", "#btn-mute", "#btn-delete"].forEach(s => $(s).disabled = !has);
  $("#clip-volume").disabled = !has;
  if (has) {
    $("#clip-volume").value = Math.round((info.clip.volume ?? 1) * 100);
    $("#vol-label").textContent = Math.round((info.clip.volume ?? 1) * 100) + "%";
    $("#btn-mute").classList.toggle("on", !!info.clip.muted);
  } else {
    $("#vol-label").textContent = "—";
    $("#btn-mute").classList.remove("on");
  }
}

function splitSelected() {
  let info = selectedInfo();
  const t = state.playhead;
  if (!info || t <= info.clip.start + 0.05 || t >= clipEnd(info.clip) - 0.05) {
    // fall back: first clip under the playhead on any track
    info = null;
    for (const c of allClips()) {
      if (t > c.start + 0.05 && t < clipEnd(c) - 0.05) { info = findClip(c.id); break; }
    }
  }
  if (!info) { toast("Put the playhead inside a clip to split it ✂"); return; }
  const { clip, arr } = info;
  pushHistory();
  const offset = t - clip.start;
  const right = {
    ...clip, id: uid(),
    start: t,
    in: clip.in + offset,
  };
  clip.out = clip.in + offset;
  arr.push(right);
  state.selection = right.id;
  renderTimeline(); saveLocal();
}

function deleteSelected() {
  const info = selectedInfo();
  if (!info) return;
  pushHistory();
  info.arr.splice(info.arr.indexOf(info.clip), 1);
  state.selection = null;
  renderTimeline(); saveLocal();
}

function duplicateSelected() {
  const info = selectedInfo();
  if (!info) return;
  pushHistory();
  const copy = { ...info.clip, id: uid(), start: clipEnd(info.clip) };
  placeClip(info.arr, copy);
  state.selection = copy.id;
  renderTimeline(); saveLocal();
}

function toggleMuteSelected() {
  const info = selectedInfo();
  if (!info) return;
  pushHistory();
  info.clip.muted = !info.clip.muted;
  renderTimeline(); saveLocal();
}

/* ================================================================
   Export
   ================================================================ */

async function doExport() {
  if (timelineDuration() <= 0) { toast("Timeline is empty — nothing to export.", true); return; }
  stopPlayback();
  const overlay = $("#export-overlay");
  overlay.hidden = false;
  $("#export-title").textContent = "Exporting…";
  $("#export-bar").style.width = "0%";
  $("#export-pct").textContent = "0%";
  $("#export-result").hidden = true;
  $("#export-result").innerHTML = "";

  const payload = {
    settings: projSettings(),
    video: state.timeline.video.map(c => ({
      assetId: c.assetId, start: c.start, in: c.in, out: c.out,
      muted: !!c.muted || state.timeline.videoMuted, volume: c.volume ?? 1,
    })).sort((a, b) => a.start - b.start),
    audioTracks: state.timeline.audioTracks.map((tr, ti) =>
      state.timeline.audioMuted[ti] ? [] : tr.map(c => ({
        assetId: c.assetId, start: c.start, in: c.in, out: c.out,
        muted: !!c.muted, volume: c.volume ?? 1,
      }))),
  };
  try {
    const { job } = await apiJSON("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const done = await pollJob(job, (j) => {
      const pct = Math.round((j.progress || 0) * 100);
      $("#export-bar").style.width = pct + "%";
      $("#export-pct").textContent = pct + "%";
    });
    $("#export-title").textContent = "✅ Export complete";
    const out = done.outputs[0];
    const box = $("#export-result");
    box.hidden = false;
    box.innerHTML = out.kind === "video"
      ? `<video src="${out.url}" controls></video>`
      : `<audio src="${out.url}" controls></audio>`;
    box.insertAdjacentHTML("beforeend",
      `<div class="result-actions">
         <a class="btn-small" href="${out.url}" download="${out.name}">⬇ Download</a>
         <button class="btn-small" data-import="${out.path}">＋ Add to library</button>
       </div>`);
    $("[data-import]", box).addEventListener("click", (e) => importOutput(e.target));
  } catch (e) {
    $("#export-title").textContent = "❌ Export failed";
    $("#export-result").hidden = false;
    $("#export-result").innerHTML = `<pre style="white-space:pre-wrap;color:var(--danger);font-size:12px"></pre>`;
    $("#export-result pre").textContent = e.message;
  }
}

async function importOutput(btn) {
  try {
    const asset = await apiJSON("/api/import_output", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: btn.dataset.import }),
    });
    state.assets[asset.id] = asset;
    renderLibrary();
    btn.textContent = "✓ In library";
    btn.disabled = true;
    toast(`Added "${asset.name}" to Media`);
  } catch (e) { toast(e.message, true); }
}

/* ================================================================
   Tools section
   ================================================================ */

const TOOL_DEFS = [
  {
    id: "trim_split", icon: "✂️", title: "Trim / Split",
    desc: "Cut video or audio — keep a range, or split into two files at a point.",
    files: [{ name: "file", label: "Video or audio file", accept: "video/*,audio/*" }],
    fields: [
      { name: "mode", type: "radio", label: "Mode", options: [["trim", "Trim to range"], ["split", "Split into two"]], value: "trim" },
      { name: "start", type: "text", label: "Start / split point (e.g. 90 or 1:30.5)", placeholder: "0:00" },
      { name: "end", type: "text", label: "End (trim mode only)", placeholder: "leave blank = to the end" },
    ],
  },
  {
    id: "join_videos", icon: "🎬", title: "Join videos",
    desc: "Concatenate 2+ videos into one MP4. Different sizes are normalized automatically.",
    files: [{ name: "files", label: "Videos (in order)", accept: "video/*", multiple: true }],
    fields: [],
  },
  {
    id: "join_audio", icon: "🎵", title: "Join audio",
    desc: "Concatenate 2+ audio files into one track.",
    files: [{ name: "files", label: "Audio files (in order)", accept: "audio/*", multiple: true }],
    fields: [{ name: "format", type: "radio", label: "Output", options: [["mp3", "MP3"], ["wav", "WAV"], ["m4a", "M4A"]], value: "mp3" }],
  },
  {
    id: "attach_audio", icon: "🔊", title: "Audio + Video",
    desc: "Attach an audio track to a video — replace the original sound or mix with it.",
    files: [
      { name: "video", label: "Video", accept: "video/*" },
      { name: "audio", label: "Audio", accept: "audio/*" },
    ],
    fields: [{ name: "mode", type: "radio", label: "Mode", options: [["replace", "Replace original audio"], ["mix", "Mix with original"]], value: "replace" }],
  },
  {
    id: "image_audio", icon: "🖼️", title: "Image + Audio → MP4",
    desc: "Turn a still image and an audio track into a video (great for music uploads).",
    files: [
      { name: "image", label: "Image", accept: "image/*" },
      { name: "audio", label: "Audio", accept: "audio/*" },
    ],
    fields: [
      { name: "resolution", type: "select", label: "Resolution", options: [["1920x1080", "1080p"], ["1280x720", "720p"], ["1080x1920", "Vertical 1080×1920"], ["720x1280", "Vertical 720×1280"], ["1080x1080", "Square"]], value: "1920x1080" },
      { name: "zoom", type: "check", label: "Slow Ken-Burns zoom", value: false },
    ],
  },
  {
    id: "extract_audio", icon: "🎧", title: "Extract audio",
    desc: "Pull the audio track out of a video file.",
    files: [{ name: "file", label: "Video", accept: "video/*" }],
    fields: [{ name: "format", type: "radio", label: "Format", options: [["mp3", "MP3"], ["wav", "WAV"], ["original", "Original (lossless)"]], value: "mp3" }],
  },
  {
    id: "grab_frame", icon: "📸", title: "Grab a frame",
    desc: "Save a single frame of a video as a PNG image.",
    files: [{ name: "file", label: "Video", accept: "video/*" }],
    fields: [
      { name: "which", type: "radio", label: "Which frame", options: [["last", "Last"], ["first", "First"], ["middle", "Middle"], ["at", "At time…"]], value: "last" },
      { name: "timestamp", type: "text", label: "Timestamp (for 'At time')", placeholder: "e.g. 0:30" },
    ],
  },
  {
    id: "resize", icon: "📐", title: "Resize / downscale",
    desc: "Change video or image to any exact size, e.g. 1644×3072 → 704×1280.",
    files: [{ name: "file", label: "Video or image", accept: "video/*,image/*" }],
    fields: [
      { name: "width", type: "number", label: "Target width", value: 704, step: 2 },
      { name: "height", type: "number", label: "Target height", value: 1280, step: 2 },
      { name: "mode", type: "radio", label: "Aspect handling", options: [["crop", "Fill & crop (no bars)"], ["fit", "Fit (black bars)"], ["stretch", "Stretch exactly"]], value: "crop" },
    ],
  },
  {
    id: "convert", icon: "🔄", title: "Convert format",
    desc: "Convert between MP4, WebM, GIF, MP3, WAV, M4A, FLAC.",
    files: [{ name: "file", label: "Video or audio file", accept: "video/*,audio/*" }],
    fields: [{ name: "format", type: "select", label: "Convert to", options: [["mp4", "MP4"], ["webm", "WebM"], ["gif", "GIF"], ["mp3", "MP3"], ["wav", "WAV"], ["m4a", "M4A"], ["flac", "FLAC"]], value: "mp4" }],
  },
  {
    id: "speed", icon: "⏩", title: "Change speed",
    desc: "Speed up or slow down video/audio (pitch-corrected audio).",
    files: [{ name: "file", label: "Video or audio file", accept: "video/*,audio/*" }],
    fields: [{ name: "speed", type: "select", label: "Speed", options: [["0.25", "0.25×"], ["0.5", "0.5×"], ["0.75", "0.75×"], ["1.25", "1.25×"], ["1.5", "1.5×"], ["2", "2×"], ["4", "4×"]], value: "2" }],
  },
  {
    id: "volume", icon: "🎚️", title: "Volume & fades",
    desc: "Boost/cut volume, loudness-normalize, add fade in/out.",
    files: [{ name: "file", label: "Video or audio file", accept: "video/*,audio/*" }],
    fields: [
      { name: "gain", type: "number", label: "Gain (dB, e.g. 6 or -6)", value: 0, step: 1 },
      { name: "fade_in", type: "number", label: "Fade in (seconds)", value: 0, step: 0.5 },
      { name: "fade_out", type: "number", label: "Fade out (seconds)", value: 0, step: 0.5 },
      { name: "normalize", type: "check", label: "Loudness normalize (-14 LUFS)", value: false },
    ],
  },
  {
    id: "inspect", icon: "ℹ️", title: "File info",
    desc: "Inspect resolution, duration, codecs, bitrate of any media file.",
    files: [{ name: "file", label: "Media file", accept: "video/*,audio/*,image/*" }],
    fields: [],
  },
];

function renderToolGrid() {
  const grid = $("#tool-grid");
  grid.innerHTML = "";
  for (const t of TOOL_DEFS) {
    const tile = document.createElement("button");
    tile.className = "tool-tile";
    tile.innerHTML = `<div class="tt-icon">${t.icon}</div><h3>${t.title}</h3><p>${t.desc}</p>`;
    tile.addEventListener("click", () => openTool(t));
    grid.appendChild(tile);
  }
}

function openTool(def) {
  $("#tools-home").hidden = true;
  $("#tool-view").hidden = false;
  $("#tool-title").textContent = `${def.icon} ${def.title}`;
  $("#tool-results").innerHTML = `<div class="results-placeholder">Results appear here</div>`;

  const form = $("#tool-form");
  form.innerHTML = "";
  const picked = {};   // fieldName -> File[]

  for (const f of def.files) {
    const wrap = document.createElement("div");
    wrap.className = "field";
    wrap.innerHTML = `<label class="f-label">${f.label}</label>`;
    const dz = document.createElement("div");
    dz.className = "dropzone";
    dz.innerHTML = `<div>📥 Drop file${f.multiple ? "s" : ""} here or click to browse</div><div class="dz-files"></div>`;
    const input = document.createElement("input");
    input.type = "file";
    input.accept = f.accept;
    input.multiple = !!f.multiple;
    input.hidden = true;
    const setFiles = (files) => {
      picked[f.name] = f.multiple ? [...(picked[f.name] || []), ...files] : [files[0]];
      $(".dz-files", dz).innerHTML = picked[f.name]
        .map((x, i) => `<div>${i + 1}. ${x.name.replace(/</g, "&lt;")}</div>`).join("");
    };
    dz.addEventListener("click", () => input.click());
    input.addEventListener("change", () => { if (input.files.length) setFiles([...input.files]); input.value = ""; });
    dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("dragover"); });
    dz.addEventListener("dragleave", () => dz.classList.remove("dragover"));
    dz.addEventListener("drop", (e) => {
      e.preventDefault(); dz.classList.remove("dragover");
      if (e.dataTransfer.files.length) setFiles([...e.dataTransfer.files]);
    });
    wrap.append(dz, input);
    form.appendChild(wrap);
  }

  for (const f of def.fields) {
    const wrap = document.createElement("div");
    wrap.className = "field";
    if (f.type === "radio") {
      wrap.innerHTML = `<label class="f-label">${f.label}</label><div class="radio-row">` +
        f.options.map(([v, l]) =>
          `<label><input type="radio" name="f_${f.name}" value="${v}" ${v === f.value ? "checked" : ""}>${l}</label>`).join("") +
        `</div>`;
    } else if (f.type === "select") {
      wrap.innerHTML = `<label class="f-label">${f.label}</label><select name="f_${f.name}">` +
        f.options.map(([v, l]) => `<option value="${v}" ${v === f.value ? "selected" : ""}>${l}</option>`).join("") +
        `</select>`;
    } else if (f.type === "check") {
      wrap.innerHTML = `<label class="check-row"><input type="checkbox" name="f_${f.name}" ${f.value ? "checked" : ""}> ${f.label}</label>`;
    } else if (f.type === "number") {
      wrap.innerHTML = `<label class="f-label">${f.label}</label>
        <input type="number" name="f_${f.name}" value="${f.value}" step="${f.step || 1}">`;
    } else {
      wrap.innerHTML = `<label class="f-label">${f.label}</label>
        <input type="text" name="f_${f.name}" placeholder="${f.placeholder || ""}">`;
    }
    form.appendChild(wrap);
  }

  const go = document.createElement("button");
  go.className = "tool-go";
  go.textContent = def.id === "inspect" ? "Inspect" : "Run " + def.title;
  form.appendChild(go);
  const prog = document.createElement("div");
  prog.className = "tool-progress";
  prog.hidden = true;
  prog.innerHTML = `<div class="progress-outer"><div class="progress-inner"></div></div>`;
  form.appendChild(prog);

  go.addEventListener("click", async () => {
    for (const f of def.files) {
      const need = f.multiple ? 2 : 1;
      if ((picked[f.name] || []).length < (f.multiple ? 2 : 1)) {
        toast(`Please add ${need > 1 ? "at least 2 files" : "a file"}: ${f.label}`, true);
        return;
      }
    }
    const fd = new FormData();
    for (const [name, files] of Object.entries(picked))
      for (const file of files) fd.append(name, file);
    for (const f of def.fields) {
      const el = form.querySelector(`[name="f_${f.name}"]${f.type === "radio" ? ":checked" : ""}`);
      if (!el) continue;
      fd.append(f.name, f.type === "check" ? String(el.checked) : el.value);
    }
    go.disabled = true;
    prog.hidden = false;
    const bar = $(".progress-inner", prog);
    bar.style.width = "5%";
    try {
      if (def.id === "inspect") {
        const info = await apiJSON("/api/inspect", { method: "POST", body: fd });
        renderInspect(info);
      } else {
        const { job } = await apiJSON(`/api/tool/${def.id}`, { method: "POST", body: fd });
        const done = await pollJob(job, (j) => {
          bar.style.width = Math.max(5, Math.round((j.progress || 0) * 100)) + "%";
        });
        renderToolResults(done.outputs);
      }
    } catch (e) {
      toast(e.message, true, 8000);
    }
    go.disabled = false;
    prog.hidden = true;
  });
}

function renderToolResults(outputs) {
  const box = $("#tool-results");
  box.innerHTML = "";
  for (const out of outputs) {
    const item = document.createElement("div");
    item.className = "result-item";
    if (out.kind === "video") item.innerHTML = `<video src="${out.url}" controls></video>`;
    else if (out.kind === "audio") item.innerHTML = `<audio src="${out.url}" controls></audio>`;
    else if (out.kind === "image") item.innerHTML = `<img src="${out.url}">`;
    item.insertAdjacentHTML("beforeend",
      `<div class="result-actions">
         <a class="btn-small" href="${out.url}" download="${out.name}">⬇ Download</a>
         <button class="btn-small" data-import="${out.path}">＋ Add to Studio media</button>
       </div>`);
    $("[data-import]", item).addEventListener("click", (e) => importOutput(e.target));
    box.appendChild(item);
  }
}

function renderInspect(info) {
  const rows = [
    ["File", info.name],
    ["Container", info.container],
    ["Duration", `${fmtTime(info.duration)} (${info.duration.toFixed(2)} s)`],
    ["Size", `${info.size_mb} MB`],
    ["Bitrate", info.bitrate_kbps ? `${info.bitrate_kbps} kbps` : "—"],
  ];
  if (info.video) rows.push(
    ["Video codec", info.video.codec],
    ["Resolution", `${info.video.width} × ${info.video.height}`],
    ["FPS", info.video.fps],
    ["Pixel format", info.video.pix_fmt]);
  if (info.audio) rows.push(
    ["Audio codec", info.audio.codec],
    ["Sample rate", `${info.audio.sample_rate} Hz`],
    ["Channels", info.audio.channels]);
  $("#tool-results").innerHTML = `<table class="info-table">` +
    rows.map(([k, v]) => `<tr><td>${k}</td><td>${String(v).replace(/</g, "&lt;")}</td></tr>`).join("") +
    `</table>`;
}

/* ================================================================
   Wiring
   ================================================================ */

/* mode tabs */
$("#tab-studio").addEventListener("click", () => switchMode("studio"));
$("#tab-tools").addEventListener("click", () => switchMode("tools"));
function switchMode(mode) {
  $("#tab-studio").classList.toggle("active", mode === "studio");
  $("#tab-tools").classList.toggle("active", mode === "tools");
  $("#studio").hidden = mode !== "studio";
  $("#tools").hidden = mode !== "tools";
  if (mode === "tools") { stopPlayback(); }
  else { renderTimeline(); fitStage(); }
}
$("#btn-tools-back").addEventListener("click", () => {
  $("#tools-home").hidden = false;
  $("#tool-view").hidden = true;
});

/* library import */
$("#btn-import").addEventListener("click", () => $("#file-input").click());
$("#file-input").addEventListener("change", (e) => {
  uploadFiles([...e.target.files]);
  e.target.value = "";
});
const libList = $("#asset-list");
libList.addEventListener("dragover", (e) => { e.preventDefault(); libList.classList.add("dragover"); });
libList.addEventListener("dragleave", () => libList.classList.remove("dragover"));
libList.addEventListener("drop", (e) => {
  e.preventDefault();
  libList.classList.remove("dragover");
  if (e.dataTransfer.files.length) uploadFiles([...e.dataTransfer.files]);
});

/* transport */
$("#btn-play").addEventListener("click", togglePlay);
$("#btn-seek-start").addEventListener("click", () => { stopPlayback(); setPlayhead(0); });
$("#btn-seek-end").addEventListener("click", () => { stopPlayback(); setPlayhead(timelineDuration()); });

/* timeline toolbar */
$("#btn-split").addEventListener("click", splitSelected);
$("#btn-dup").addEventListener("click", duplicateSelected);
$("#btn-mute").addEventListener("click", toggleMuteSelected);
$("#btn-delete").addEventListener("click", deleteSelected);
$("#btn-undo").addEventListener("click", undo);
$("#btn-redo").addEventListener("click", redo);
$("#snap-toggle").addEventListener("change", (e) => state.snap = e.target.checked);
$("#clip-volume").addEventListener("input", (e) => {
  const info = selectedInfo();
  if (!info) return;
  info.clip.volume = Number(e.target.value) / 100;
  $("#vol-label").textContent = e.target.value + "%";
  saveLocal();
});

/* zoom: slider 0..100 → pps 2..200 (log scale), keeps playhead roughly centered */
$("#zoom-slider").addEventListener("input", (e) => {
  const v = Number(e.target.value);
  const oldPps = state.pps;
  state.pps = 2 * Math.pow(100, v / 100);
  const anchor = state.playhead;
  renderTimeline();
  tlScroll.scrollLeft += anchor * (state.pps - oldPps);
});
tlScroll.addEventListener("wheel", (e) => {
  if (!e.ctrlKey) return;
  e.preventDefault();
  const slider = $("#zoom-slider");
  slider.value = Math.max(0, Math.min(100, Number(slider.value) - Math.sign(e.deltaY) * 6));
  slider.dispatchEvent(new Event("input"));
}, { passive: false });

/* keep track heads vertically aligned with the scrolled tracks */
tlScroll.addEventListener("scroll", () => {
  trackHeads.scrollTop = tlScroll.scrollTop;
});

/* project settings */
$("#proj-res").addEventListener("change", () => {
  fitStage();
  saveLocal();
});
$("#proj-fps").addEventListener("change", saveLocal);

/* export */
$("#btn-export").addEventListener("click", doExport);
$("#btn-export-close").addEventListener("click", () => $("#export-overlay").hidden = true);

/* keyboard */
document.addEventListener("keydown", (e) => {
  const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || "");
  if (typing) return;
  if (e.code === "Space") { e.preventDefault(); if (!$("#studio").hidden) togglePlay(); }
  else if (e.key === "s" || e.key === "S") splitSelected();
  else if (e.key === "m" || e.key === "M") toggleMuteSelected();
  else if (e.key === "Delete" || e.key === "Backspace") deleteSelected();
  else if (e.key === "Home") { stopPlayback(); setPlayhead(0); }
  else if (e.key === "End") { stopPlayback(); setPlayhead(timelineDuration()); }
  else if ((e.ctrlKey || e.metaKey) && e.key === "z") { e.preventDefault(); undo(); }
  else if ((e.ctrlKey || e.metaKey) && (e.key === "y" || (e.shiftKey && e.key === "Z"))) { e.preventDefault(); redo(); }
  else if ((e.ctrlKey || e.metaKey) && e.key === "d") { e.preventDefault(); duplicateSelected(); }
  else if (e.key === "ArrowLeft") setPlayhead(state.playhead - (e.shiftKey ? 1 : 1 / 30));
  else if (e.key === "ArrowRight") setPlayhead(state.playhead + (e.shiftKey ? 1 : 1 / 30));
});

/* ================================================================
   Init
   ================================================================ */

(async function init() {
  renderToolGrid();
  // never start with a half-loaded app: wait for the server, retrying
  for (;;) {
    try { await loadAssets(); break; }
    catch {
      toast("Server not reachable — retrying…", true, 1800);
      await new Promise(r => setTimeout(r, 2000));
    }
  }
  await restoreProject();
  fitStage();
  renderTimeline();
  new ResizeObserver(fitStage).observe($("#stage"));
  window.addEventListener("resize", () => { fitStage(); renderTimeline(); });
})();
