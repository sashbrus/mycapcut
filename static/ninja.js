/* ================================================================
   Ninja Director — ComfyUI pipeline UI (Director + Upscaler tabs)
   Uses helpers from app.js: $, $$ (if present), apiJSON, pollJob, toast.
   ================================================================ */
(function () {
  const $n = (sel) => document.querySelector(sel);
  let ninjaState = null;

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

  $n("#nj-tab-director").addEventListener("click", () => switchNinjaTab("director"));
  $n("#nj-tab-upscaler").addEventListener("click", () => switchNinjaTab("upscaler"));
  $n("#nj-tab-settings").addEventListener("click", () => switchNinjaTab("settings"));
  function switchNinjaTab(t) {
    ["director", "upscaler", "settings"].forEach(x => {
      $n(`#nj-tab-${x}`).classList.toggle("active", x === t);
      $n(`#nj-${x}`).hidden = x !== t;
    });
  }

  /* ---------- state ---------- */
  async function refreshState() {
    try {
      ninjaState = await apiJSON("/api/ninja/state");
      renderSettings();
      renderDirector();
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
  let promptCount = 0;

  function renderDirector() {
    const p = ninjaState.project;
    const songEl = $n("#nj-song-info");
    if (p.song) {
      songEl.textContent = `♪ ${p.song.filename} — ${fmtSec(p.song.duration)} | next chunk starts at ${fmtSec(p.next_start)}`;
    } else {
      songEl.textContent = "No song loaded — upload the track first.";
    }
    $n("#nj-from").value = (p.next_start || 0).toFixed(1);
    // dimension fields: initialize from Settings once, then respect user edits
    if ($n("#nj-gen-w").value === "") $n("#nj-gen-w").value = ninjaState.config.settings.width;
    if ($n("#nj-gen-h").value === "") $n("#nj-gen-h").value = ninjaState.config.settings.height;
    const tail = p.pending_tail;
    $n("#nj-tail-info").textContent = tail
      ? `pinned start: upscaled 5s tail from ${tail.from_chunk}`
      : "first chunk — starts from image (optional) or pure text";
    $n("#nj-img-row").style.display = tail ? "none" : "flex";

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
      $n("#nj-review-title").textContent = imported
        ? `${last.part} (imported) — press Continue to prep its tail`
        : (last.preview
            ? `${last.part} — cumulative + original audio. Retake or Continue?`
            : `${last.part} — Retake or Continue?`);
    }
    if (promptCount === 0) addPrompt();
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
      await pollJob(r.job, (j) => {
        bar.style.width = Math.round((j.progress || 0) * 100) + "%";
        txt.textContent = `${label}: ${j.message || j.status}`;
      });
      toast(`${label} done`);
    } finally {
      $n("#nj-progress-wrap").hidden = true;
      refreshState();
    }
  }

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
    };
    const imgInput = $n("#nj-img-file");
    if (!ninjaState.project.pending_tail && imgInput.files[0]) {
      const fd = new FormData();
      fd.append("file", imgInput.files[0]);
      const asset = await apiJSON("/api/upload", { method: "POST", body: fd });
      body.image_asset = asset.id;
    }
    try { await runJob("/api/ninja/generate", body, "Generate"); }
    catch (e) { toast(e.message, true); }
  });

  $n("#nj-retake").addEventListener("click", async () => {
    const len = +$n("#nj-retake-len").value;
    try { await runJob("/api/ninja/retake", len > 0 ? { duration_sec: len } : {}, "Retake"); }
    catch (e) { toast(e.message, true); }
  });

  $n("#nj-continue").addEventListener("click", async () => {
    try { await runJob("/api/ninja/continue", {}, "Continue (finalize + tail upscale)"); }
    catch (e) { toast(e.message, true); }
  });

  $n("#nj-reset").addEventListener("click", async () => {
    if (!confirm("Reset project? Finished part files stay on disk.")) return;
    await apiJSON("/api/ninja/reset", { method: "POST" });
    refreshState();
  });

  /* ---------- upscaler tab ---------- */
  $n("#nj-up-run").addEventListener("click", async () => {
    const fileInput = $n("#nj-up-file");
    if (!fileInput.files[0]) return toast("Choose a video", true);
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    let asset;
    try { asset = await apiJSON("/api/upload", { method: "POST", body: fd }); }
    catch (e) { return toast(e.message, true); }
    const body = {
      asset: asset.id,
      chunk_seconds: +$n("#nj-up-chunk").value,
      denoise: +$n("#nj-up-den").value,
      multiplier: +$n("#nj-up-mult").value,
    };
    const anchorInput = $n("#nj-up-anchor");
    if (anchorInput.files[0]) {
      const fd2 = new FormData();
      fd2.append("file", anchorInput.files[0]);
      const a2 = await apiJSON("/api/upload", { method: "POST", body: fd2 });
      body.anchor_asset = a2.id;
    }
    try { await runJob("/api/ninja/upscale", body, "Upscale"); }
    catch (e) { toast(e.message, true); }
  });
})();
