/* ================================================================
   Ninja Director — ComfyUI pipeline UI (Director + Upscaler tabs)
   Uses helpers from app.js: $, $$ (if present), apiJSON, pollJob, toast.
   ================================================================ */
(function () {
  const $n = (sel) => document.querySelector(sel);
  let ninjaState = null;
  let promptCount = 0;
  let promptsHydrated = false;

  /* ---------- tab wiring ---------- */
  const tabBtn = $n("#tab-ninja");
  tabBtn.addEventListener("click", () => {
    document.querySelectorAll(".mode-tab").forEach(b => b.classList.toggle("active", b === tabBtn));
    $n("#studio").hidden = true;
    $n("#tools").hidden = true;
    $n("#ninja").hidden = false;
    refreshState();
  });
  ["#tab-studio", "#tab-tools"].forEach(sel =>
    $n(sel).addEventListener("click", () => { $n("#ninja").hidden = true; }));

  const NJ_SECTIONS = { director: "#nj-director", upscaler: "#nj-upscaler",
                        retake: "#nj-retake-tab", settings: "#nj-settings" };
  Object.keys(NJ_SECTIONS).forEach(x =>
    $n(`#nj-tab-${x}`).addEventListener("click", () => switchNinjaTab(x)));
  function switchNinjaTab(t) {
    Object.entries(NJ_SECTIONS).forEach(([x, sel]) => {
      $n(`#nj-tab-${x}`).classList.toggle("active", x === t);
      $n(sel).hidden = x !== t;
    });
  }

  /* ---------- state ---------- */
  async function refreshState() {
    try {
      ninjaState = await apiJSON("/api/ninja/state");
      renderSettings();
      renderDirector();
      renderRetake();
    } catch (e) {
      const stale = /Method Not Allowed|Not Found/i.test(e.message);
      toast(stale
        ? "Old server code is running — close CupCut and start it again (a fresh start now auto-kills stale instances)."
        : "Ninja: " + e.message, true);
    }
  }

  /* ---------- settings tab ---------- */
  function renderSettings() {
    const c = ninjaState.config, t = ninjaState.templates;
    $n("#nj-host-director").value = c.hosts.director?.url || "";
    $n("#nj-host-upscaler").value = c.hosts.upscaler?.url || "";
    const s = c.settings;
    $n("#nj-set-tail").value = s.tail_seconds;
    $n("#nj-set-den").value = s.tail_upscale_denoise;
    $n("#nj-set-mult").value = s.tail_upscale_multiplier;
    $n("#nj-set-w").value = s.width;
    $n("#nj-set-h").value = s.height;
    const icEl = $n("#nj-set-ic-lora");
    if (icEl) icEl.value = s.ic_lora_name || "";
    for (const kind of ["director", "upscaler"]) {
      const el = $n(`#nj-tpl-${kind}`);
      el.textContent = t[kind] ? "✔ captured" : "✖ missing";
      el.className = "nj-tpl-status " + (t[kind] ? "ok" : "miss");
    }
  }

  $n("#nj-save-config").addEventListener("click", async () => {
    try {
      await apiJSON("/api/ninja/config", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          hosts: { director: { url: $n("#nj-host-director").value.trim() },
                   upscaler: { url: $n("#nj-host-upscaler").value.trim() } },
          settings: {
            tail_seconds: +$n("#nj-set-tail").value,
            tail_upscale_denoise: +$n("#nj-set-den").value,
            tail_upscale_multiplier: +$n("#nj-set-mult").value,
            width: +$n("#nj-set-w").value, height: +$n("#nj-set-h").value,
            frame_rate: 24,
            ic_lora_name: ($n("#nj-set-ic-lora")?.value || "").trim(),
            ic_lora_strength: 1.0,
          },
        }) });
      toast("Saved");
      refreshState();
    } catch (e) { toast(e.message, true); }
  });

  document.querySelectorAll("[data-ping]").forEach(btn => btn.addEventListener("click", async () => {
    btn.textContent = "…";
    const r = await apiJSON(`/api/ninja/ping/${btn.dataset.ping}`, { method: "POST" });
    btn.textContent = "Ping";
    toast(r.ok ? `OK: ${r.device}` : r.error, !r.ok);
  }));

  document.querySelectorAll("[data-capture]").forEach(btn => btn.addEventListener("click", async () => {
    const kind = btn.dataset.capture;
    btn.disabled = true; btn.textContent = "…";
    try {
      const r = await apiJSON(`/api/ninja/capture/${kind}`, { method: "POST" });
      toast(`Captured ${kind} template from its host (${r.nodes} nodes)`);
      refreshState();
    } catch (e) { toast(e.message, true); }
    btn.disabled = false; btn.textContent = "Capture";
  }));

  /* ---------- director tab ---------- */

  function latestExecutedPrompts() {
    const p = ninjaState.project || {};
    if (p.last_prompts && (p.last_prompts.global_prompt || (p.last_prompts.prompts || []).length))
      return p.last_prompts;
    const chunks = p.chunks || [];
    for (let i = chunks.length - 1; i >= 0; i--) {
      const req = chunks[i].request || {};
      if (req.imported) continue;
      if ((req.global_prompt || "").trim() || (req.prompts || []).length)
        return { global_prompt: req.global_prompt || "", prompts: req.prompts || [] };
    }
    return null;
  }

  function hydrateComposerPrompts(force) {
    const lp = latestExecutedPrompts();
    const g = $n("#nj-global");
    const boxes = [...document.querySelectorAll("#nj-prompts textarea")];
    const hasTyped = (g.value || "").trim() || boxes.some(t => (t.value || "").trim());
    // After reset/song wipe (no chunks) always restore last executed
    const noChunks = !((ninjaState.project || {}).chunks || []).length;
    if (!lp) {
      if (promptCount === 0) addPrompt();
      return;
    }
    if (!force && promptsHydrated && hasTyped && !noChunks) return;

    g.value = lp.global_prompt || "";
    $n("#nj-prompts").innerHTML = "";
    promptCount = 0;
    const segs = lp.prompts || [];
    if (!segs.length) addPrompt();
    else {
      for (const seg of segs) {
        addPrompt();
        const ta = $n("#nj-prompts").lastElementChild.querySelector("textarea");
        ta.value = typeof seg === "string" ? seg : (seg.text || "");
      }
    }
    promptsHydrated = true;
  }

  function renderDirector() {
    const p = ninjaState.project;
    const songEl = $n("#nj-song-info");
    const prog = ninjaState.progress || {};
    if (p.song) {
      const marks = (prog.milestones || [])
        .map(m => `${m.done ? "✓" : "·"}${m.at}s`)
        .join(" ");
      songEl.textContent =
        `♪ ${p.song.filename} — ${fmtSec(p.song.duration)} | next ${fmtSec(p.next_start)}` +
        (prog.song_sec
          ? ` | covered ${fmtSec(prog.covered_sec)} (${prog.percent || 0}%)`
          : "");
      const pe = $n("#nj-song-progress");
      if (pe) {
        pe.textContent = marks
          ? `Progress: ${marks}${prog.past_end ? " — past song end → Finish" : ""}`
          : "";
      }
    } else {
      songEl.textContent = "No song loaded — upload the track first.";
    }
    const finBtn = $n("#nj-finish");
    if (finBtn) {
      const can = !!(ninjaState.actions || {}).can_finish;
      finBtn.disabled = !can;
      finBtn.title = can
        ? "Stitch all approved parts + song audio into one MP4"
        : "Need at least one approved part (Continue)";
    }
    $n("#nj-from").value = (p.next_start || 0).toFixed(1);
    // dimension fields: initialize from Settings once, then respect user edits
    if ($n("#nj-gen-w").value === "") $n("#nj-gen-w").value = ninjaState.config.settings.width;
    if ($n("#nj-gen-h").value === "") $n("#nj-gen-h").value = ninjaState.config.settings.height;
    const tail = p.pending_tail;
    $n("#nj-tail-info").textContent = tail
      ? `tail available from ${tail.from_chunk} (${(tail.seconds || 0).toFixed(2)}s)`
      : "no tail yet — first chunk or after reset";
    const srcSel = $n("#nj-source");
    srcSel.querySelector('option[value="extend"]').disabled = !tail;
    if (!tail) {
      srcSel.value = "new";               // nothing to extend from
      srcSel.dataset.userTouched = "";    // forget stale user choice
    } else if (srcSel.dataset.userTouched !== "1") {
      srcSel.value = "extend";            // tail exists -> extending is the default
    }
    updateSourceUI();

    const list = $n("#nj-parts");
    list.innerHTML = "";
    for (const c of p.chunks) {
      const div = document.createElement("div");
      div.className = "nj-part " + (c.final ? "done" : "review");
      div.innerHTML = `<b>${c.part}</b> <span>${c.final ? "✔ final" : "⏳ awaiting decision"}</span>`;
      if (c.raw) {
        const a = document.createElement("a");
        a.href = "/api/ninja/file?path=" + encodeURIComponent(c.final || c.raw);
        a.target = "_blank"; a.textContent = "▶ open";
        div.appendChild(a);
      }
      list.appendChild(div);
    }
    const last = p.chunks[p.chunks.length - 1];
    const inReview = last && !last.final;
    $n("#nj-review").hidden = !inReview;
    $n("#nj-compose").hidden = false;  // composer always available, like a fresh start
    if (inReview) {
      const v = $n("#nj-review-video");
      v.src = "/api/ninja/file?path=" + encodeURIComponent(last.preview || last.raw);
      const imported = last.request && last.request.imported;
      $n("#nj-retake").style.display = imported ? "none" : "";
      $n("#nj-retake-len").style.display = imported ? "none" : "";
      $n("#nj-retake-len").value = (last.to - last.from).toFixed(1);
      $n("#nj-accept-len").value = (last.to - last.from).toFixed(1);
      $n("#nj-review-title").textContent = imported
        ? `${last.part} (imported) — press Continue to prep its tail`
        : (last.preview
            ? `${last.part} — cumulative + original audio. Retake or Continue?`
            : `${last.part} — Retake or Continue?`);
    }
    hydrateComposerPrompts(false);
  }

  function fmtSec(s) { const m = Math.floor(s / 60); return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`; }

  $n("#nj-part-file").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    fd.append("from_sec", $n("#nj-from").value || "0");
    try {
      await apiJSON("/api/ninja/import_part", { method: "POST", body: fd });
      toast("Part imported — press ✔ Continue to prep its tail");
      refreshState();
    } catch (err) { toast(err.message, true); }
    e.target.value = "";
  });

  $n("#nj-song-file").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    try {
      await apiJSON("/api/ninja/song", { method: "POST", body: fd });
      toast("Song loaded — project reset");
      refreshState();
    } catch (err) { toast(err.message, true); }
    e.target.value = "";
  });

  function updateSourceUI() {
    // image start only makes sense for a fresh scene; extend pins the tail video
    $n("#nj-img-row").style.display = $n("#nj-source").value === "new" ? "flex" : "none";
  }
  $n("#nj-source").addEventListener("change", () => {
    $n("#nj-source").dataset.userTouched = "1";
    updateSourceUI();
  });

  $n("#nj-add-prompt").addEventListener("click", addPrompt);
  function addPrompt() {
    promptCount++;
    const wrap = document.createElement("div");
    wrap.className = "nj-prompt";
    wrap.innerHTML = `<span class="nj-prompt-n">[txt]</span>
      <textarea rows="2" placeholder="segment prompt…"></textarea>
      <button class="btn-small nj-del">✕</button>`;
    wrap.querySelector(".nj-del").addEventListener("click", () => { wrap.remove(); promptCount--; });
    $n("#nj-prompts").appendChild(wrap);
  }

  async function runJob(url, body, label) {
    const r = await apiJSON(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                   body: JSON.stringify(body || {}) });
    const bar = $n("#nj-progress"), txt = $n("#nj-progress-text");
    $n("#nj-progress-wrap").hidden = false;
    try {
      const done = await pollJob(r.job, (j) => {
        bar.style.width = Math.round((j.progress || 0) * 100) + "%";
        txt.textContent = `${label}: ${j.message || j.status}`;
      });
      toast(`${label} done`);
      return done;
    } finally {
      $n("#nj-progress-wrap").hidden = true;
      refreshState();
    }
  }

  $n("#nj-size-preset")?.addEventListener("change", () => {
    const v = $n("#nj-size-preset").value;
    if (!v) return;
    const [w, h] = v.split("x").map(Number);
    $n("#nj-gen-w").value = w;
    $n("#nj-gen-h").value = h;
  });

  $n("#nj-generate").addEventListener("click", async () => {
    const prompts = [...document.querySelectorAll("#nj-prompts textarea")]
      .map(t => ({ text: t.value.trim() })).filter(p => p.text);
    if (!prompts.length) return toast("Write at least one prompt", true);
    const from = +$n("#nj-from").value, len = +$n("#nj-len").value;
    const body = {
      from_sec: from, to_sec: from + len, prompts,
      global_prompt: $n("#nj-global").value.trim(),
      width: +$n("#nj-gen-w").value || 0,
      height: +$n("#nj-gen-h").value || 0,
      source: $n("#nj-source").value,
    };
    const imgInput = $n("#nj-img-file");
    if ($n("#nj-source").value === "new" && imgInput.files[0]) {
      const fd = new FormData();
      fd.append("file", imgInput.files[0]);
      const asset = await apiJSON("/api/upload", { method: "POST", body: fd });
      body.image_asset = asset.id;
    }
    const endInp = $n("#nj-end-img-file");
    if (endInp?.files?.[0]) {
      const fd = new FormData();
      fd.append("file", endInp.files[0]);
      const asset = await apiJSON("/api/upload", { method: "POST", body: fd });
      body.end_image_asset = asset.id;
    }
    const icImgs = $n("#nj-ic-imgs");
    if (icImgs?.files?.length) {
      body.ic_image_assets = [];
      for (const f of icImgs.files) {
        const fd = new FormData();
        fd.append("file", f);
        const asset = await apiJSON("/api/upload", { method: "POST", body: fd });
        body.ic_image_assets.push(asset.id);
      }
    }
    const icVid = $n("#nj-ic-vid");
    if (icVid?.files?.[0]) {
      const fd = new FormData();
      fd.append("file", icVid.files[0]);
      const asset = await apiJSON("/api/upload", { method: "POST", body: fd });
      body.ic_video_assets = [asset.id];
    }
    try { await runJob("/api/ninja/generate", body, "Generate"); }
    catch (e) { toast(e.message, true); }
  });

  $n("#nj-retake").addEventListener("click", async () => {
    const len = +$n("#nj-retake-len").value;
    try { await runJob("/api/ninja/retake", len > 0 ? { duration_sec: len } : {}, "Retake"); }
    catch (e) { toast(e.message, true); }
  });

  $n("#nj-finish")?.addEventListener("click", async () => {
    if (!confirm("Stitch all approved parts + song into one final MP4?")) return;
    try {
      await runJob("/api/ninja/finish", { copy_to: "D:\\\\parts\\\\full.mp4" }, "Finish song");
      toast("Full song ready (exports + D:\\\\parts\\\\full.mp4)");
    } catch (e) { toast(e.message, true); }
  });

  $n("#nj-continue").addEventListener("click", async () => {
    const acc = +$n("#nj-accept-len").value;
    const body = { upscale_tail: $n("#nj-tail-upscale").checked };
    if (acc > 0) body.accept_sec = acc;
    try { await runJob("/api/ninja/continue", body, "Continue (finalize + prep tail)"); }
    catch (e) { toast(e.message, true); }
  });

  $n("#nj-reset").addEventListener("click", async () => {
    if (!confirm("Reset project? Finished part files stay on disk.")) return;
    await apiJSON("/api/ninja/reset", { method: "POST" });
    promptsHydrated = false;  // re-fill composer from last_prompts
    await refreshState();
    hydrateComposerPrompts(true);
  });

  /* ---------- retake tab (clip window patching) ---------- */
  let rtPromptCount = 0;

  function renderRetake() {
    const rt = ninjaState.retake || {};
    const info = $n("#nj-rt-info");
    if (rt.clip) {
      info.textContent = `🎞 ${rt.clip.filename} — ${fmtSec(rt.clip.duration)} | ` +
        `${rt.clip.width}×${rt.clip.height} @ ${rt.clip.fps}fps`;
    } else {
      info.textContent = "No clip loaded.";
    }
    const pend = rt.pending;
    $n("#nj-rt-review").hidden = !pend;
    if (pend) {
      const v = $n("#nj-rt-video");
      if (!v.src.includes(encodeURIComponent(pend.preview))) {
        v.src = "/api/ninja/file?path=" + encodeURIComponent(pend.preview);
        v.addEventListener("loadedmetadata", () => {
          v.currentTime = Math.max(0, pend.start_sec - 3);  // land just before the patch
        }, { once: true });
      }
      $n("#nj-rt-review-title").textContent =
        `Spliced preview — patch at ${pend.start_sec.toFixed(1)}–${pend.end_sec.toFixed(1)}s (mode ${pend.mode})`;
    }
    if (rtPromptCount === 0) rtAddPrompt();
  }

  $n("#nj-rt-add-prompt").addEventListener("click", rtAddPrompt);
  function rtAddPrompt() {
    rtPromptCount++;
    const wrap = document.createElement("div");
    wrap.className = "nj-prompt";
    wrap.innerHTML = `<span class="nj-prompt-n">[txt]</span>
      <textarea rows="2" placeholder="what happens in the retaken window…"></textarea>
      <button class="btn-small nj-del">✕</button>`;
    wrap.querySelector(".nj-del").addEventListener("click", () => { wrap.remove(); rtPromptCount--; });
    $n("#nj-rt-prompts").appendChild(wrap);
  }

  $n("#nj-rt-file").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    try {
      await apiJSON("/api/ninja/retake_clip/upload", { method: "POST", body: fd });
      toast("Clip loaded");
      refreshState();
    } catch (err) { toast(err.message, true); }
    e.target.value = "";
  });

  async function rtGenerate(body) {
    try { await runJob("/api/ninja/retake_clip/generate", body, "Retake window"); }
    catch (e) { toast(e.message, true); }
  }

  // parse "82", "1:22", "00:01:22" or "00:01:22:12" (last part = frames at clip fps)
  function parseTimecode(str) {
    const parts = String(str).trim().split(":").map(x => x.trim());
    if (parts.some(x => x === "" || isNaN(+x))) return NaN;
    const fps = ((ninjaState.retake || {}).clip || {}).fps || 24;
    if (parts.length === 1) return +parts[0];                                  // seconds
    if (parts.length === 2) return +parts[0] * 60 + +parts[1];                 // m:ss
    if (parts.length === 3) return +parts[0] * 3600 + +parts[1] * 60 + +parts[2];  // h:mm:ss
    if (parts.length === 4) return +parts[0] * 3600 + +parts[1] * 60 + +parts[2] + (+parts[3]) / fps;
    return NaN;
  }

  $n("#nj-rt-generate").addEventListener("click", () => {
    const prompts = [...document.querySelectorAll("#nj-rt-prompts textarea")]
      .map(t => ({ text: t.value.trim() })).filter(p => p.text);
    if (!prompts.length) return toast("Write at least one prompt", true);
    const from = parseTimecode($n("#nj-rt-from").value);
    const to = parseTimecode($n("#nj-rt-to").value);
    if (isNaN(from) || isNaN(to)) return toast("Bad time format — use 00:01:22 or 82 (s)", true);
    rtGenerate({
      start_sec: from,
      end_sec: to,
      mode: +$n("#nj-rt-mode").value,
      prompts,
      global_prompt: $n("#nj-rt-global").value.trim(),
    });
  });

  $n("#nj-rt-again").addEventListener("click", () => {
    const pend = (ninjaState.retake || {}).pending;
    if (!pend) return;
    rtGenerate(pend.request);  // same request, new seed
  });

  $n("#nj-rt-apply").addEventListener("click", async () => {
    try {
      const r = await apiJSON("/api/ninja/retake_clip/apply", { method: "POST" });
      toast("Applied → " + r.path);
      refreshState();
    } catch (e) { toast(e.message, true); }
  });

  $n("#nj-rt-discard").addEventListener("click", async () => {
    await apiJSON("/api/ninja/retake_clip/discard", { method: "POST" });
    refreshState();
  });

  /* ---------- upscaler tab (multi-video queue) ---------- */
  const upQueue = []; // { asset, name, file? }

  function renderUpQueue() {
    const box = $n("#nj-up-queue");
    if (!upQueue.length) {
      box.textContent = "Queue empty.";
      box.className = "nj-muted";
      return;
    }
    box.className = "";
    box.innerHTML = upQueue.map((it, i) =>
      `<div class="nj-row" data-qi="${i}">
        <span>${i + 1}. ${it.name}</span>
        <label class="btn-small nj-file-btn" title="Optional first-chunk anchor image">🖼 anchor
          <input type="file" accept="image/*" hidden data-anchor="${i}"></label>
        <span class="nj-muted" data-anchor-label="${i}">${it.anchorName ? "✓ " + it.anchorName : ""}</span>
        <button type="button" class="btn-small" data-rm="${i}">✕</button>
      </div>`).join("");
    box.querySelectorAll("[data-rm]").forEach(btn => {
      btn.addEventListener("click", () => {
        upQueue.splice(+btn.dataset.rm, 1);
        renderUpQueue();
      });
    });
    box.querySelectorAll("input[data-anchor]").forEach(inp => {
      inp.addEventListener("change", async () => {
        const i = +inp.dataset.anchor;
        if (!inp.files[0]) return;
        const fd = new FormData();
        fd.append("file", inp.files[0]);
        try {
          const a = await apiJSON("/api/upload", { method: "POST", body: fd });
          upQueue[i].anchor_asset = a.id;
          upQueue[i].anchorName = inp.files[0].name;
          renderUpQueue();
        } catch (e) { toast(e.message, true); }
      });
    });
  }

  $n("#nj-up-file").addEventListener("change", async () => {
    const files = [...($n("#nj-up-file").files || [])];
    $n("#nj-up-file").value = "";
    if (!files.length) return;
    for (const file of files) {
      const fd = new FormData();
      fd.append("file", file);
      try {
        const asset = await apiJSON("/api/upload", { method: "POST", body: fd });
        upQueue.push({ asset: asset.id, name: file.name });
      } catch (e) {
        toast(`${file.name}: ${e.message}`, true);
      }
    }
    renderUpQueue();
    toast(`Queue: ${upQueue.length} video(s)`);
  });

  $n("#nj-up-clear").addEventListener("click", () => {
    upQueue.length = 0;
    renderUpQueue();
    $n("#nj-up-results").innerHTML = "";
  });

  $n("#nj-up-run").addEventListener("click", async () => {
    if (!upQueue.length) return toast("Add at least one video to the queue", true);
    const body = {
      chunk_seconds: +$n("#nj-up-chunk").value,
      shorter_size: +$n("#nj-up-mult").value,
      multiplier: +$n("#nj-up-mult").value,
      denoise: +($n("#nj-up-den")?.value || 1),
      grok_anchor: !!$n("#nj-up-grok")?.checked,
      items: upQueue.map(it => ({
        asset: it.asset,
        name: it.name,
        ...(it.anchor_asset ? { anchor_asset: it.anchor_asset } : {}),
      })),
    };
    try {
      const done = await runJob("/api/ninja/upscale_queue", body, "Upscale queue");
      const box = $n("#nj-up-results");
      box.innerHTML = "";
      for (const o of (done && done.outputs) || []) {
        if (o.kind !== "video") continue;
        box.insertAdjacentHTML("beforeend",
          `<div class="nj-row"><video src="${o.url}" controls style="width:100%;max-height:320px"></video></div>
           <div class="nj-row"><a class="btn-small" href="${o.url}" download="${o.name}">⬇ ${o.name}</a></div>`);
      }
    } catch (e) { toast(e.message, true); }
  });
})();
