"""Ninja Director — LTX music-video pipeline section for CupCut Studio.

Drives ComfyUI (local or remote) over its HTTP API only:
  - Director: chunk-by-chunk generation with chained 5s upscaled tails.
  - Upscaler: chained IC-upscale of any video (anchor = prev chunk's last frame).

Templates are frozen snapshots of successful runs captured from /history —
per host, per kind ("director" / "upscaler"). The builder only overrides
inputs (timeline, files, seed); it never edits workflows on the host.

Wired from server.py via register(app, deps).
"""
from __future__ import annotations

import copy
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, File, Form

# populated by register()
DEPS: dict = {}

NINJA_DIR: Path = None  # DATA/ninja
TPL_DIR: Path = None    # DATA/comfyui_templates
CFG_FILE: Path = None   # DATA/comfyui.json

DEFAULT_CFG = {
    # role-based hosts: every Director execution goes to "director", every upscale
    # execution goes to "upscaler". Same URL or different machines — user's choice.
    "hosts": {
        "director": {"url": "http://127.0.0.1:8188"},
        "upscaler": {"url": "http://127.0.0.1:8188"},
    },
    "settings": {
        "tail_seconds": 5.0,
        # workflow defaults — the user's proven manual values. NOTE: multiplier 1.3
        # rounds 768x512 to 960x640 (clean 1.5 ratio); 1.1 rounds to 832x512 and
        # DISTORTS — do not lower without checking the ratio survives rounding.
        "tail_upscale_denoise": 1.0,
        "tail_upscale_multiplier": 1.3,
        "frame_rate": 24,
        "width": 0,
        "height": 0,
    },
}

# node classes that identify a template kind
KIND_MARKERS = {"director": "LTXDirector", "upscaler": "RTXVideoSuperResolution"}

# upscaler template node ids (from the proven "Ltx 2.3 IC Upscale" workflow)
UP_VIDEO, UP_IMAGE, UP_BYPASS, UP_SCHED, UP_MULT = "5070", "2004", "5019", "5074", "5093"
UP_SAVE = "5071"


# ---------------------------------------------------------------- config/state

def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return copy.deepcopy(default)


def _save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1), encoding="utf-8")


def _migrate_templates(legacy_host: str):
    """Old layout stored templates per named host (local_/remote_); the role layout
    uses one template per role (director.json / upscaler.json)."""
    for kind in KIND_MARKERS:
        new = TPL_DIR / f"{kind}.json"
        if new.is_file():
            continue
        for cand in (TPL_DIR / f"{legacy_host}_{kind}.json",
                     TPL_DIR / f"local_{kind}.json",
                     TPL_DIR / f"remote_{kind}.json"):
            if cand.is_file():
                new.write_text(cand.read_text(encoding="utf-8"), encoding="utf-8")
                break


def cfg() -> dict:
    c = _load_json(CFG_FILE, DEFAULT_CFG)
    hosts = c.get("hosts") or {}
    if "director" not in hosts or "upscaler" not in hosts:
        # migrate legacy local/remote + active_host layout to role-based hosts
        legacy_active = c.get("active_host") or "local"
        url = ((hosts.get(legacy_active) or hosts.get("local") or {}).get("url")
               or DEFAULT_CFG["hosts"]["director"]["url"])
        c["hosts"] = {"director": {"url": url}, "upscaler": {"url": url}}
        c.pop("active_host", None)
        _save_json(CFG_FILE, c)
        _migrate_templates(legacy_active)
    for k, v in DEFAULT_CFG.items():  # forward-fill new keys
        c.setdefault(k, copy.deepcopy(v))
    return c


def host_url(role: str) -> str:
    try:
        return cfg()["hosts"][role]["url"].rstrip("/")
    except KeyError:
        raise HTTPException(400, f"unknown host role '{role}' (use director/upscaler)")


def project_file() -> Path:
    return NINJA_DIR / "project.json"


def project() -> dict:
    return _load_json(project_file(), {"name": "", "song": None, "chunks": [], "next_start": 0.0})


def save_project(p: dict):
    _save_json(project_file(), p)


# ---------------------------------------------------------------- comfy client

UA = {"User-Agent": "Mozilla/5.0 (CupCut NinjaDirector)"}  # runpod proxy 403s python-urllib


def comfy(host: str, path: str, payload=None, timeout=60):
    url = host_url(host) + path
    headers = dict(UA)
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="replace")[:1500]
            det = json.loads(body)
            # surface ComfyUI validation errors (e.g. model file renamed on host)
            msgs = []
            for nid, ne in (det.get("node_errors") or {}).items():
                for err in ne.get("errors", []):
                    msgs.append(f"node {nid} ({ne.get('class_type')}): {err.get('details') or err.get('message')}")
            body = "; ".join(msgs) or det.get("error", {}).get("message") or body
        except Exception:
            pass
        raise DEPS["ToolError"](f"ComfyUI ({host_url(host)}) HTTP {e.code}: {body} — "
                                f"if a model file was renamed on the host, re-capture the template.")
    except Exception as e:
        raise DEPS["ToolError"](f"ComfyUI ({host_url(host)}) unreachable: {e}")


def comfy_upload(host: str, local_path: Path, name: str, subfolder: str = "whatdreamscost") -> str:
    """Upload a file to the ComfyUI input dir via /upload/image. Returns 'subfolder/name'."""
    boundary = uuid.uuid4().hex
    data = local_path.read_bytes()
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + (
        f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"subfolder\"\r\n\r\n{subfolder}"
        f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue"
        f"\r\n--{boundary}--\r\n").encode()
    req = urllib.request.Request(host_url(host) + "/upload/image", data=body,
        headers={**UA, "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            resp = json.loads(r.read())
    except Exception as e:
        raise DEPS["ToolError"](f"Upload to ComfyUI ({host_url(host)}) failed: {e} — "
                                f"is that host running and reachable?")
    sub = resp.get("subfolder", subfolder)
    return f"{sub}/{resp['name']}" if sub else resp["name"]


def comfy_download(host: str, filename: str, subfolder: str, dest: Path):
    q = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": "output"})
    req = urllib.request.Request(host_url(host) + "/view?" + q, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.read())
    except Exception as e:
        raise DEPS["ToolError"](f"Download from ComfyUI ({host_url(host)}) failed: {e} — "
                                f"is that host running and reachable?")


def queue_and_wait(host: str, graph: dict, job: dict, label: str, timeout=7200) -> dict:
    pid = comfy(host, "/prompt", {"prompt": graph})["prompt_id"]
    job["message"] = f"{label}: running on ComfyUI ({pid[:8]})"
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(5)
        try:
            h = comfy(host, f"/history/{pid}", timeout=30)
        except Exception:
            continue
        if pid in h:
            run = h[pid]
            if run["status"].get("status_str") == "success":
                return run
            raise DEPS["ToolError"](f"{label} failed on ComfyUI (status="
                                    f"{run['status'].get('status_str')}) — check its log")
        job["message"] = f"{label}: running {int(time.time() - t0)}s"
    raise DEPS["ToolError"](f"{label} timed out")


def first_video_output(run: dict) -> tuple[str, str]:
    for out in run["outputs"].values():
        for key in ("gifs", "video", "images"):
            for f in out.get(key, []) or []:
                if f.get("filename", "").lower().endswith((".mp4", ".webm", ".mov")):
                    return f["filename"], f.get("subfolder", "")
    raise DEPS["ToolError"]("run produced no video output")


# ---------------------------------------------------------------- templates

def tpl_path(kind: str) -> Path:
    return TPL_DIR / f"{kind}.json"


def load_template(kind: str) -> dict:
    p = tpl_path(kind)
    if not p.is_file():
        raise HTTPException(400, f"No '{kind}' template captured yet. Run the {kind} "
                                 f"workflow once manually on its host, then click Capture.")
    return json.loads(p.read_text(encoding="utf-8"))


def capture_template(kind: str) -> dict:
    marker = KIND_MARKERS.get(kind)
    if not marker:
        raise HTTPException(400, f"kind must be one of {list(KIND_MARKERS)}")
    hist = comfy(kind, "/history", timeout=60)  # the role IS the host to capture from
    for rid, run in reversed(list(hist.items())):
        graph = run["prompt"][2]
        if run["status"].get("status_str") == "success" and \
           any(n["class_type"] == marker for n in graph.values()):
            _save_json(tpl_path(kind), graph)
            return {"captured_from": rid, "nodes": len(graph)}
    raise HTTPException(404, f"No successful '{kind}' run found in the {kind} host's "
                             f"history. Run it once manually there first.")


def template_status() -> dict:
    return {k: tpl_path(k).is_file() for k in KIND_MARKERS}


# ---------------------------------------------------------------- media helpers

def ffmpeg(job, args: list, label: str):
    job["message"] = label
    DEPS["run"]([DEPS["FFMPEG"], "-y", "-loglevel", "error"] + args)


def _yavg(path: Path, start_frame: int, end_frame: int) -> list[float]:
    """Mean luma (YAVG, 0-255) of decoded frames [start_frame, end_frame).

    NOTE: trim ranges (no commas in expressions) and explicit mode=print —
    the `metadata=print` positional shorthand mis-parses inside longer
    filter chains on this ffmpeg build ("Option not found").
    """
    proc = DEPS["run"]([DEPS["FFMPEG"], "-y", "-loglevel", "error", "-i", str(path),
                        "-vf", f"trim=start_frame={start_frame}:end_frame={end_frame},"
                               f"signalstats,"
                               f"metadata=mode=print:key=lavfi.signalstats.YAVG:file=-",
                        "-f", "null", "-"])
    vals = []
    for line in (proc.stdout or "").splitlines():
        if "YAVG=" in line:
            try:
                vals.append(float(line.rsplit("=", 1)[1].strip()))
            except ValueError:
                pass
    return vals


def media_duration(path: Path) -> float:
    info = DEPS["ffprobe"](path)
    return float(info.get("format", {}).get("duration") or 0.0)


def randomize_seeds(graph: dict):
    for node in graph.values():
        if node["class_type"] == "RandomNoise":
            node["inputs"]["noise_seed"] = random.randint(0, 2**48)


def set_filename_prefix(graph: dict, prefix: str):
    for node in graph.values():
        if "filename_prefix" in node.get("inputs", {}):
            node["inputs"]["filename_prefix"] = prefix


# ---------------------------------------------------------------- director build

def build_director_graph(host: str, *, prompts: list, from_sec: float, to_sec: float,
                         fps: int, width: int, height: int, tail_file: str | None,
                         tail_seconds: float, image_file: str | None,
                         global_prompt: str, part_name: str, song_file: str,
                         end_image_file: str | None = None,
                         audio_tracks: list | None = None,
                         end_zone_frames: int = 6,
                         image_zone_frames: int | None = None,
                         total_frames: int | None = None) -> dict:
    """Build a Director graph for one chunk.

    Timeline (all in frames, starting at 0):
      [tail video (tail_seconds)] or [image] or nothing
      [text prompt segments...] filling the regenerated window
      [end-frame image] (optional, ONE frame immediately after the window,
                         isEndFrame — native node feature)
      [trailing text padding] (only with an end-frame image; fills the 8n+1
                         grid, generated past the landing point, trimmed away)
      audio: one segment = song slice covering the whole window.
    """
    graph = copy.deepcopy(load_template("director"))
    d_id = next(nid for nid, n in graph.items() if n["class_type"] == "LTXDirector")
    d = graph[d_id]["inputs"]

    if total_frames is None:
        total_frames = int(round((to_sec - from_sec + (tail_seconds if tail_file else 0)) * fps))
    tail_frames = int(round(tail_seconds * fps)) if tail_file else 0

    segments = []
    if tail_file:
        segments.append({
            "id": uuid.uuid4().hex[:13] + "_v", "type": "video", "start": 0,
            "length": tail_frames, "trimStart": 0, "videoDurationFrames": tail_frames,
            "imageFile": tail_file, "fileName": tail_file.split("/")[-1],
            "prompt": "", "fileSize": 0,
        })
    elif image_file:
        img_frames = image_zone_frames or int(round(prompts[0].get("image_seconds", 0.25) * fps)) or 6
        segments.append({
            "id": uuid.uuid4().hex[:13] + "_i", "type": "image", "start": 0,
            "length": img_frames, "imageFile": image_file,
            "fileName": image_file.split("/")[-1], "prompt": "", "isEndFrame": False,
        })

    # head = everything already placed (video tail OR start image) — text begins
    # AFTER it, like in the real Director UI (overlap = broken prompt mapping)
    used = sum(s["length"] for s in segments)
    end_frames = end_zone_frames if end_image_file else 0
    text_start = used
    text_total = total_frames - text_start - end_frames
    n_txt = max(1, len(prompts))
    per = text_total // n_txt
    for i, p in enumerate(prompts):
        ln = text_total - per * (n_txt - 1) if i == n_txt - 1 else per
        segments.append({"id": uuid.uuid4().hex[:13], "type": "text", "start": text_start,
                         "length": ln, "prompt": p["text"], "isEndFrame": False})
        text_start += ln
    if end_image_file:
        # the end keyframe sits IMMEDIATELY after the last regenerated frame —
        # a 1-frame segment, so wherever the node pins it (segment start or
        # end) it lands exactly at the window boundary. The rest of the end
        # zone becomes trailing text padding AFTER the keyframe: it keeps the
        # 8n+1 grid, is generated past the landing point and trimmed away.
        # Before this, the keyframe sat at the far end of the padding, so the
        # last kept frame was still mid-flight toward it (visible end jump).
        segments.append({
            "id": uuid.uuid4().hex[:13] + "_e", "type": "image",
            "start": text_start, "length": 1,
            "imageFile": end_image_file, "fileName": end_image_file.split("/")[-1],
            "prompt": "", "isEndFrame": True,
        })
        if end_frames > 1:
            segments.append({
                "id": uuid.uuid4().hex[:13] + "_p", "type": "text",
                "start": text_start + 1, "length": end_frames - 1,
                "prompt": prompts[-1]["text"] if prompts else "",
                "isEndFrame": False,
            })

    if audio_tracks:
        # explicit audio layout: list of (file, start_frame, num_frames) — the UI
        # pattern: video segment's own audio first, then the new-range slice
        audio_segments = [{
            "id": uuid.uuid4().hex[:13] + f"_a{i}", "type": "audio", "start": int(st),
            "length": int(ln), "trimStart": 0, "audioDurationFrames": int(ln),
            "audioFile": f, "fileName": f.split("/")[-1], "waveformPeaks": [],
        } for i, (f, st, ln) in enumerate(audio_tracks)]
    else:
        audio_segments = [{
            "id": uuid.uuid4().hex[:13] + "_a", "type": "audio", "start": 0,
            "length": total_frames, "trimStart": 0, "audioDurationFrames": total_frames,
            "audioFile": song_file, "fileName": song_file.split("/")[-1], "waveformPeaks": [],
        }]

    tl = {
        "mainTrackEnabled": True, "audioTrackEnabled": True, "motionTrackEnabled": True,
        "propHeight": 90, "globalPropHeight": 60, "showFilenames": True,
        "overrideAudio": False, "inpaint_audio": False,
        "global_prompt": global_prompt, "retake_global_prompt": "",
        "retakeMode": False, "retakeStart": 0, "retakeLength": 0, "retakePrompt": "",
        "retakeStrength": 1, "retakeVideo": None,
        "normalStartFrame": 0, "normalDurationFrames": total_frames,
        "segments": segments, "motionSegments": [], "audioSegments": audio_segments,
    }

    dur_sec = total_frames / fps
    seg_lengths = [s["length"] for s in segments]
    d.update({
        "timeline_data": json.dumps(tl),
        "start_second": 0.0, "end_second": round(dur_sec, 3),
        "duration_seconds": round(dur_sec, 3),
        "start_frame": 0, "end_frame": total_frames, "duration_frames": total_frames,
        "global_prompt": global_prompt,
        "local_prompts": " | ".join(s.get("prompt", "") for s in segments),
        "segment_lengths": ",".join(str(x) for x in seg_lengths),
        "use_custom_audio": True, "inpaint_audio": False, "override_audio": False,
        "frame_rate": float(fps), "custom_width": width, "custom_height": height,
        "resize_method": "crop", "display_mode": "seconds",
    })
    randomize_seeds(graph)
    set_filename_prefix(graph, f"ninja/{part_name}")
    return graph


def build_upscale_graph(host: str, *, video_file: str, anchor_file: str | None,
                        denoise: float | None, multiplier: float | None,
                        part_name: str) -> dict:
    graph = copy.deepcopy(load_template("upscaler"))
    graph[UP_VIDEO]["inputs"]["video"] = video_file
    graph[UP_VIDEO]["inputs"]["frame_load_cap"] = 0
    graph[UP_BYPASS]["inputs"]["value"] = anchor_file is None
    if anchor_file:
        graph[UP_IMAGE]["inputs"]["image"] = anchor_file
    if denoise is not None:
        graph[UP_SCHED]["inputs"]["denoise"] = denoise
    if multiplier is not None:
        graph[UP_MULT]["inputs"]["resize_type.multiplier"] = multiplier
    set_filename_prefix(graph, f"ninja/{part_name}_up")
    return graph


# ---------------------------------------------------------------- pipeline jobs

def job_generate(job, host: str, p: dict, req: dict):
    """One Director chunk: slice song -> upload inputs -> queue -> download result."""
    s = cfg()["settings"]
    fps = int(s["frame_rate"])
    from_sec, to_sec = float(req["from_sec"]), float(req["to_sec"])
    tail = p.get("pending_tail")            # set by 'continue' of previous chunk
    # source: "extend" = continue from the previous part's tail (default);
    # "new" = fresh scene (a cut) — text/image start, tail ignored
    if req.get("source") == "new":
        tail = None
    # exact tail length measured from the actual tail file (LTX snaps to 8n+1
    # frames, so the nominal 5s tail is really e.g. 113 frames = 4.7083s)
    tail_seconds = float(tail["seconds"]) if tail else 0.0
    n = len([c for c in p["chunks"] if c.get("final")]) + 1
    part_name = f"part{n}-{int(from_sec)}-{int(to_sec)}"
    work = NINJA_DIR / "work"; work.mkdir(parents=True, exist_ok=True)

    # 1. song slice covering [from - tail, to] — sample-accurate (-ss after -i)
    song_local = Path(p["song"]["path"])
    slice_from = max(0.0, from_sec - tail_seconds)
    slice_path = work / f"{part_name}_audio.wav"
    ffmpeg(job, ["-i", str(song_local), "-ss", f"{slice_from:.6f}", "-to", f"{to_sec:.6f}",
                 "-ar", "44100", "-ac", "1", str(slice_path)], "slicing song")
    job["progress"] = 0.1
    song_file = comfy_upload(host, slice_path, slice_path.name)

    # 2. tail video / first image upload
    tail_file = image_file = None
    if tail:
        tail_file = comfy_upload(host, Path(tail["path"]), Path(tail["path"]).name)
    if not tail and req.get("image_asset"):
        a = DEPS["asset_path"](req["image_asset"])
        image_file = comfy_upload(host, a, a.name)
    job["progress"] = 0.2

    # Dimensions law: extensions ALWAYS use part1's ACTUAL dimensions, explicitly
    # (measured from the first part's file — never user fields, never node defaults).
    if tail:
        if not p.get("work_resolution"):
            finfo = DEPS["ffprobe"](Path(p["chunks"][0]["raw"]))
            fst = next((st for st in finfo.get("streams", []) if st.get("codec_type") == "video"), {})
            p["work_resolution"] = {"w": int(fst.get("width", 0)), "h": int(fst.get("height", 0))}
            save_project(p)
        width = int(p["work_resolution"]["w"])
        height = int(p["work_resolution"]["h"])
    else:
        width = int(req["width"]) if req.get("width") is not None else int(s["width"])
        height = int(req["height"]) if req.get("height") is not None else int(s["height"])
        # the node snaps to /32 silently (720 -> 704) which desyncs the project
        # grid — snap up-front so what you ask for is what everything else sees
        width -= width % 32
        height -= height % 32
    graph = build_director_graph(
        host, prompts=req["prompts"], from_sec=from_sec, to_sec=to_sec, fps=fps,
        width=width, height=height, tail_file=tail_file,
        tail_seconds=tail_seconds, image_file=image_file,
        global_prompt=req.get("global_prompt", ""), part_name=part_name,
        song_file=song_file)

    run = queue_and_wait(host, graph, job, part_name)
    job["progress"] = 0.9
    fn, sub = first_video_output(run)
    raw = work / f"{part_name}_raw.mp4"
    comfy_download(host, fn, sub, raw)

    chunk = {"id": uuid.uuid4().hex[:8], "part": part_name, "from": from_sec, "to": to_sec,
             "tail_used": bool(tail), "tail_seconds": tail_seconds,
             "raw": str(raw), "final": None, "status": "review",
             "request": req, "host": host}

    # cumulative review preview: all approved parts + this candidate (exact-trimmed)
    # + ORIGINAL song audio — the seams are what the user judges, not the part alone.
    # ALWAYS 8-bit yuv420p: LTX outputs 10-bit which browsers cannot play.
    # Preview scales to the PROJECT grid (part1's real size — never stale, never default).
    job["message"] = "building cumulative preview"
    try:
        if not p.get("work_resolution"):
            src0 = Path(p["chunks"][0]["raw"]) if p["chunks"] else raw
            finfo = DEPS["ffprobe"](src0)
            fst = next((st for st in finfo.get("streams", []) if st.get("codec_type") == "video"), {})
            p["work_resolution"] = {"w": int(fst.get("width", 0)), "h": int(fst.get("height", 0))}
            save_project(p)
        wr = p["work_resolution"]
        cvf = ["-vf", f"scale={wr['w']}:{wr['h']}:flags=lanczos", "-pix_fmt", "yuv420p"]
        cand = work / f"{part_name}_candidate.mp4"
        if chunk["tail_used"] and tail_seconds > 0:
            ffmpeg(job, ["-ss", f"{tail_seconds:.6f}", "-i", str(raw),
                         "-t", f"{to_sec - from_sec:.6f}", *cvf, "-c:v", "libx264", "-crf", "12",
                         "-preset", "fast", "-an", str(cand)], "trimming candidate")
        else:
            ffmpeg(job, ["-i", str(raw), "-t", f"{to_sec - from_sec:.6f}", *cvf,
                         "-c:v", "libx264", "-crf", "12", "-preset", "fast", "-an",
                         str(cand)], "trimming candidate")
        seq = [Path(c["final"]) for c in p["chunks"] if c.get("final")] + [cand]
        lst = work / f"{part_name}_concat.txt"
        lst.write_text("".join(f"file '{x}'\n" for x in seq), encoding="utf-8")
        joined = work / f"{part_name}_joined_v.mp4"
        DEPS["run"]([DEPS["FFMPEG"], "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                     "-i", str(lst), "-an", "-vf", f"scale={wr['w']}:{wr['h']}:flags=lanczos",
                     "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "14", "-preset", "fast",
                     str(joined)])
        preview = work / f"{part_name}_preview.mp4"
        first_from = min(float(c["from"]) for c in p["chunks"] if c.get("final")) if \
            any(c.get("final") for c in p["chunks"]) else from_sec
        DEPS["run"]([DEPS["FFMPEG"], "-y", "-loglevel", "error", "-i", str(joined),
                     "-ss", f"{first_from:.6f}", "-i", str(Path(p['song']['path'])),
                     "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                     "-shortest", str(preview)])
        chunk["preview"] = str(preview)
    except Exception:
        # fallback: at least a browser-safe re-encode of the raw chunk alone
        try:
            safe = work / f"{part_name}_review.mp4"
            ffmpeg(job, ["-i", str(raw), "-pix_fmt", "yuv420p", "-c:v", "libx264",
                         "-crf", "14", "-preset", "fast", "-c:a", "aac", str(safe)],
                   "building safe review file")
            chunk["preview"] = str(safe)
        except Exception:
            chunk["preview"] = None
    p["chunks"] = [c for c in p["chunks"] if c.get("final")] + [chunk]
    save_project(p)
    outs = [DEPS["output_entry_abs"](raw, "video", f"{part_name} (raw)")]
    if chunk.get("preview"):
        outs.insert(0, DEPS["output_entry_abs"](Path(chunk["preview"]), "video",
                                                f"CUMULATIVE preview + original audio"))
    return outs


def job_continue(job, host: str, p: dict, accept_sec: float | None = None,
                 upscale_tail: bool = False):
    """Approve last chunk: trim guidance overlap -> save final part -> build next tail.
    accept_sec: optionally accept only the FIRST N seconds of the generated chunk.
    upscale_tail: refresh the tail through the upscale workflow before pinning."""
    s = cfg()["settings"]
    chunk = p["chunks"][-1]
    raw = Path(chunk["raw"])
    parts_dir = NINJA_DIR / "parts"; parts_dir.mkdir(parents=True, exist_ok=True)
    work = NINJA_DIR / "work"; work.mkdir(parents=True, exist_ok=True)

    # quantum accept: keep only the first N seconds of the generation
    full_len = float(chunk["to"]) - float(chunk["from"])
    if accept_sec:
        accept_sec = float(accept_sec)
        if 0 < accept_sec < full_len:
            chunk["to"] = round(float(chunk["from"]) + accept_sec, 3)
            prefix = chunk["part"].split("-")[0]
            chunk["part"] = f"{prefix}-{int(chunk['from'])}-{int(chunk['to'])}"

    # 1. final part = raw minus the tail overlap at the start, EXACTLY (to-from) long
    #    (LTX outputs 8n+1 frames — the dangling extra frame is dropped at the end
    #    so parts abut sample-precisely on the song grid: 20.000 / 40.000 / ...)
    final = parts_dir / f"{chunk['part']}.mp4"
    exact_len = float(chunk["to"]) - float(chunk["from"])
    # normalize EVERY final part to the project work resolution (part1's grid):
    # the 2-stage workflow outputs Director-res x scale_by x2 (e.g. x1.5 at 0.75),
    # so generated parts come back larger — downscale is supersampling, free quality
    if not p.get("work_resolution"):
        finfo = DEPS["ffprobe"](Path(p["chunks"][0]["raw"]))
        fst = next((st for st in finfo.get("streams", []) if st.get("codec_type") == "video"), {})
        p["work_resolution"] = {"w": int(fst.get("width", 0)), "h": int(fst.get("height", 0))}
        save_project(p)
    wr = p["work_resolution"]
    vf = ["-vf", f"scale={wr['w']}:{wr['h']}:flags=lanczos"]
    if chunk["tail_used"] and chunk["tail_seconds"] > 0:
        ffmpeg(job, ["-ss", f"{chunk['tail_seconds']:.6f}", "-i", str(raw),
                     "-t", f"{exact_len:.6f}", *vf, "-pix_fmt", "yuv420p",
                     "-c:v", "libx264", "-crf", "12", "-preset", "fast", "-c:a", "aac",
                     str(final)], "trimming guidance overlap")
    else:
        ffmpeg(job, ["-i", str(raw), "-t", f"{exact_len:.6f}", *vf, "-pix_fmt", "yuv420p",
                     "-c:v", "libx264", "-crf", "12", "-preset", "fast", "-c:a", "aac",
                     str(final)], "saving part (exact length)")
    job["progress"] = 0.2

    # 2. cut last ~N seconds of the raw chunk, then MANDATORY quality refresh:
    #    tail -> UPSCALE WORKFLOW (this is THE anti-degradation key: each cycle
    #    re-injects detail, otherwise quality decays copy-over-copy) -> downscale
    #    back to the project resolution -> pin to the next chunk.
    fps = int(s["frame_rate"])
    want = int(float(s["tail_seconds"]) * fps)
    tail_frames_cut = max(9, ((want - 1) // 8) * 8 + 1)
    # the tail must end exactly where the ACCEPTED content ends
    tail_start = max(0.0, float(chunk["tail_seconds"]) + exact_len - tail_frames_cut / fps)
    tail_cut = work / f"{chunk['part']}_tailcut.mp4"
    ffmpeg(job, ["-ss", f"{tail_start:.6f}", "-i", str(raw),
                 "-frames:v", str(tail_frames_cut),
                 "-c:v", "libx264", "-crf", "12", "-preset", "fast", "-c:a", "aac",
                 str(tail_cut)], "cutting tail")
    job["progress"] = 0.3

    if upscale_tail:
        tail_file = comfy_upload(host, tail_cut, tail_cut.name)
        graph = build_upscale_graph(host, video_file=tail_file, anchor_file=None,
                                    denoise=float(s["tail_upscale_denoise"]),
                                    multiplier=float(s["tail_upscale_multiplier"]),
                                    part_name=chunk["part"])
        run = queue_and_wait(host, graph, job, f"{chunk['part']} tail upscale")
        job["progress"] = 0.8
        fn, sub = first_video_output(run)
        tail_up_big = work / f"{chunk['part']}_tail_up_big.mp4"
        comfy_download(host, fn, sub, tail_up_big)

        # downscale back to the project resolution (part1's grid) — the upscaled
        # tail must re-enter the Director at exactly the working size
        wr = p["work_resolution"]
        tail_up = work / f"{chunk['part']}_tail_up.mp4"
        ffmpeg(job, ["-i", str(tail_up_big), "-vf", f"scale={wr['w']}:{wr['h']}:flags=lanczos",
                     "-c:v", "libx264", "-crf", "12", "-preset", "fast", "-c:a", "aac",
                     str(tail_up)], "downscaling tail back to project resolution")
    else:
        # raw tail, no refresh — user's choice per Continue (checkbox)
        wr = p["work_resolution"]
        tail_up = work / f"{chunk['part']}_tail_raw.mp4"
        ffmpeg(job, ["-i", str(tail_cut), "-vf", f"scale={wr['w']}:{wr['h']}:flags=lanczos",
                     "-c:v", "libx264", "-crf", "12", "-preset", "fast", "-c:a", "aac",
                     str(tail_up)], "normalizing raw tail (no upscale)")

    # measure the ACTUAL frame count (upscaler snaps to 8n+1) — the next chunk's
    # timeline and audio slice must use this exact length
    info = DEPS["ffprobe"](tail_up)
    vstream = next((st for st in info.get("streams", []) if st.get("codec_type") == "video"), {})
    tail_frames = int(vstream.get("nb_frames") or 0)
    if not tail_frames:
        tail_frames = int(round(float(info["format"]["duration"]) * fps))
    tail_secs_exact = tail_frames / fps

    chunk["final"] = str(final)
    chunk["status"] = "done"
    p["pending_tail"] = {"path": str(tail_up), "from_chunk": chunk["part"],
                         "frames": tail_frames, "seconds": tail_secs_exact}
    p["next_start"] = chunk["to"]
    save_project(p)
    return [DEPS["output_entry_abs"](final, "video", f"{chunk['part']} FINAL"),
            DEPS["output_entry_abs"](tail_up, "video", "next tail (upscaled, back to project res)")]


# ---------------------------------------------------------------- retake (clip patching)

RETAKE_MIN_S, RETAKE_MAX_S = 2.0, 20.0


def retake_session_file() -> Path:
    return NINJA_DIR / "retake" / "session.json"


def retake_session() -> dict:
    return _load_json(retake_session_file(), {"clip": None, "pending": None})


def save_retake_session(sess: dict):
    _save_json(retake_session_file(), sess)


def _guard_black_anchor(png: Path, label: str, at_sec: float):
    """A black anchor frame forces the model to fade the whole window to black —
    refuse loudly instead of generating a doomed run."""
    try:
        from PIL import Image as _Img
        import numpy as _np
        b = float(_np.array(_Img.open(png).convert("L")).mean())
    except Exception:
        return  # can't measure — don't block
    if b < 5.0:
        raise DEPS["ToolError"](
            f"{label} anchor at {at_sec:.2f}s is a BLACK frame (brightness {b:.1f}). "
            f"The model would fade the window into black. Move that boundary onto "
            f"visible content (e.g. past the dark gap).")


def job_retake_generate(job, host: str, req: dict):
    """Extract anchors + audio from the clip, generate the window, splice a preview.
    Uses the EXACT SAME builder as normal Director parts (proven quality) —
    mode 1 = the proven [image + txt] shape, mode 2 = the proven [tail video + txt]
    shape, plus the node's native end-frame keyframe to land on the window end."""
    sess = retake_session()
    if not sess.get("clip"):
        raise DEPS["ToolError"]("Load a clip first")
    clip = Path(sess["clip"]["path"])
    fps = int(round(float(sess["clip"]["fps"])))
    start_sec, end_sec = float(req["start_sec"]), float(req["end_sec"])
    mode = int(req.get("mode", 1))

    S = int(round(start_sec * fps))
    E = int(round(end_sec * fps))
    win = E - S
    lead = 121 if mode == 2 else 0        # same 121-frame tail as the Director loop
    if mode == 1 and S < 1:
        raise DEPS["ToolError"]("Mode 1 needs at least 1 frame before the window "
                                "(the start anchor is the frame just BEFORE it)")
    img_frames = min(6, S) if mode == 1 else 0   # start-image zone, trimmed like the lead
    head = lead if mode == 2 else img_frames
    # the head/end zones are trimmed away afterwards — pad the END zone so the
    # TOTAL lands exactly on the LTX frame grid (8n+1). Otherwise the node
    # silently resizes to the nearest 8n+1 and every boundary drifts 2-3 frames.
    # (the end keyframe itself occupies only the FIRST frame of this zone —
    # right after the last regenerated frame; the rest is trailing padding)
    end_frames = 6 + (1 - (head + win + 6)) % 8  # end zone (trimmed away)
    total = head + win + end_frames              # always 8n+1

    work = NINJA_DIR / "retake" / "work"
    work.mkdir(parents=True, exist_ok=True)
    tag = f"{S}_{E}_{uuid.uuid4().hex[:6]}"

    # 1. anchors + lead + audio, all cut from the clip itself on its true frame grid
    end_png = work / f"{tag}_end.png"
    ffmpeg(job, ["-i", str(clip), "-vf", f"select='eq(n\\,{E})'", "-vsync", "0",
                 "-frames:v", "1", str(end_png)], "extracting end anchor")
    _guard_black_anchor(end_png, "End ('To')", end_sec)
    start_png = lead_mp4 = None
    if mode == 1:
        start_png = work / f"{tag}_start.png"
        # anchor = the frame just BEFORE the window: every frame of [S, E) gets
        # regenerated; frame S-1 is the last original frame we continue from
        ffmpeg(job, ["-i", str(clip), "-vf", f"select='eq(n\\,{S - 1})'", "-vsync", "0",
                     "-frames:v", "1", str(start_png)], "extracting start anchor")
        _guard_black_anchor(start_png, "Start ('From')", (S - 1) / fps)
    else:
        lead_mp4 = work / f"{tag}_lead.mp4"
        ffmpeg(job, ["-i", str(clip),
                     "-vf", f"trim=start_frame={S - lead}:end_frame={S},setpts=PTS-STARTPTS",
                     "-an", "-c:v", "libx264", "-crf", "12", "-preset", "fast",
                     str(lead_mp4)], "extracting lead-in video")
    # audio, the UI pattern: the lead video brings ITS OWN correlated audio;
    # the new slice covers ONLY the regenerated window (+ end zone)
    win_wav = work / f"{tag}_audio_win.wav"
    win_wav_from = S if mode == 2 else S - img_frames   # mode 1: cover the image zone
    ffmpeg(job, ["-i", str(clip), "-ss", f"{win_wav_from / fps:.6f}",
                 "-to", f"{(E + end_frames) / fps:.6f}",
                 "-vn", "-ar", "44100", "-ac", "1", str(win_wav)], "slicing window audio")
    lead_wav = None
    if lead:
        lead_wav = work / f"{tag}_audio_lead.wav"
        ffmpeg(job, ["-i", str(clip), "-ss", f"{(S - lead) / fps:.6f}", "-to", f"{S / fps:.6f}",
                     "-vn", "-ar", "44100", "-ac", "1", str(lead_wav)], "slicing lead audio")
    job["progress"] = 0.15

    # 2. upload + generate on the Director host — THE SAME BUILDER AS PARTS
    end_up = comfy_upload(host, end_png, end_png.name)
    start_up = comfy_upload(host, start_png, start_png.name) if start_png else None
    lead_up = comfy_upload(host, lead_mp4, lead_mp4.name) if lead_mp4 else None
    win_wav_up = comfy_upload(host, win_wav, win_wav.name)
    tracks = [(win_wav_up, 0, img_frames + win + end_frames)]
    if lead_wav:
        lead_wav_up = comfy_upload(host, lead_wav, lead_wav.name)
        tracks = [(lead_wav_up, 0, lead), (win_wav_up, lead, win + end_frames)]
    graph = build_director_graph(
        host, prompts=req["prompts"],
        from_sec=start_sec, to_sec=end_sec + end_frames / fps, fps=fps,
        width=int(sess["clip"]["width"]), height=int(sess["clip"]["height"]),
        tail_file=lead_up, tail_seconds=lead / fps if lead_up else 0.0,
        image_file=start_up, global_prompt=req.get("global_prompt", ""),
        part_name=f"retake_{tag}", song_file=win_wav_up, end_image_file=end_up,
        audio_tracks=tracks, end_zone_frames=end_frames,
        image_zone_frames=img_frames or None, total_frames=total)
    run = queue_and_wait(host, graph, job, f"retake {start_sec}-{end_sec}")
    job["progress"] = 0.75
    fn, sub = first_video_output(run)
    raw = work / f"{tag}_raw.mp4"
    comfy_download(host, fn, sub, raw)

    # the whole alignment rests on the node honoring our 8n+1 total — verify,
    # never splice shifted frames silently
    vinfo = DEPS["ffprobe"](raw)
    vstream = next((st for st in vinfo.get("streams", []) if st.get("codec_type") == "video"), {})
    raw_frames = int(vstream.get("nb_frames") or 0) or \
        int(round(float(vinfo.get("format", {}).get("duration") or 0) * fps))
    if raw_frames != total:
        raise DEPS["ToolError"](
            f"Director returned {raw_frames} frames, expected {total} — the patch "
            f"would be shifted. Aborting instead of splicing misaligned frames.")

    # 3. patch = generated frames [head, head+win) — exactly `win` new frames,
    # the head zone (lead video / start image) is trimmed away like in parts.
    # Luma fix: the LTX VAE decode shifts brightness (~2-3 units darker) on
    # everything it outputs. Measure the shift on frames whose content is
    # KNOWN — the end keyframe IS original frame E (pinned at the last frame
    # of the end zone); the start zone reproduces the start anchor / lead
    # video — and correct the patch by that offset.
    kf = head + win  # end keyframe position in the generation
    samples = []
    o = _yavg(clip, E, E + 1)
    g = _yavg(raw, kf, kf + 1)
    if o and g:
        samples.append(o[0] - g[0])
    if mode == 1 and img_frames:
        o = _yavg(clip, S - 1, S)
        g = _yavg(raw, img_frames // 2, img_frames // 2 + 1)
        if o and g:
            samples.append(o[0] - g[0])
    elif mode == 2:
        o = _yavg(clip, S - lead, S - lead + 5)
        g = _yavg(raw, 0, 5)
        if o and g and len(o) == len(g):
            samples.append(sum(a - b for a, b in zip(o, g)) / len(o))
    offset = max(-8.0, min(8.0, sum(samples) / len(samples))) if samples else 0.0
    vf = (f"trim=start_frame={head}:end_frame={head + win},setpts=PTS-STARTPTS,"
          f"scale={sess['clip']['width']}:{sess['clip']['height']}:flags=lanczos")
    if abs(offset) >= 0.3:
        vf += f",lutyuv=y='clip(val{offset:+.2f},0,255)'"
    patch = work / f"{tag}_patch.mp4"
    ffmpeg(job, ["-i", str(raw),
                 "-vf", vf,
                 "-pix_fmt", "yuv420p", "-an", "-r", str(fps),
                 "-c:v", "libx264", "-crf", "12", "-preset", "fast",
                 str(patch)], "trimming patch")

    # 4. splice preview: original[0..S) + patch + original[E..), original audio untouched
    preview = work / f"{tag}_preview.mp4"
    ffmpeg(job, ["-i", str(clip), "-i", str(patch), "-filter_complex",
                 f"[0:v]trim=end_frame={S},setpts=PTS-STARTPTS[a];"
                 f"[1:v]setpts=PTS-STARTPTS[b];"
                 f"[0:v]trim=start_frame={E},setpts=PTS-STARTPTS[c];"
                 f"[a][b][c]concat=n=3:v=1:a=0[v]",
                 "-map", "[v]", "-map", "0:a", "-r", str(fps), "-pix_fmt", "yuv420p",
                 "-c:v", "libx264", "-crf", "12", "-preset", "fast", "-c:a", "copy",
                 str(preview)], "splicing preview")
    job["progress"] = 0.98

    sess["pending"] = {"preview": str(preview), "patch": str(patch), "raw": str(raw),
                       "start_sec": start_sec, "end_sec": end_sec, "mode": mode,
                       "request": req, "tag": tag}
    save_retake_session(sess)
    return [DEPS["output_entry_abs"](preview, "video",
                                     f"retake {start_sec:.1f}-{end_sec:.1f} spliced preview")]


def job_standalone_upscale(job, host: str, req: dict):
    """Upscaler tab: chained IC-upscale of a full video, chunked."""
    src = Path(DEPS["asset_path"](req["asset"]))
    chunk_len = float(req.get("chunk_seconds", 10))
    denoise = req.get("denoise")
    mult = req.get("multiplier")
    work = NINJA_DIR / "upscaler" / uuid.uuid4().hex[:8]
    work.mkdir(parents=True, exist_ok=True)

    ffmpeg(job, ["-i", str(src), "-c:v", "libx264", "-crf", "12", "-preset", "fast",
                 "-c:a", "aac", "-force_key_frames", f"expr:gte(t,n_forced*{chunk_len})",
                 "-f", "segment", "-segment_time", str(chunk_len), "-reset_timestamps", "1",
                 str(work / "c%03d.mp4")], "splitting")
    chunks = sorted(work.glob("c*.mp4"))
    anchor_remote = None
    if req.get("anchor_asset"):
        a = Path(DEPS["asset_path"](req["anchor_asset"]))
        anchor_remote = comfy_upload(host, a, a.name)
    results = []
    for i, c in enumerate(chunks):
        job["message"] = f"chunk {i+1}/{len(chunks)}"
        job["progress"] = i / max(1, len(chunks))
        remote_v = comfy_upload(host, c, c.name)
        graph = build_upscale_graph(host, video_file=remote_v, anchor_file=anchor_remote,
                                    denoise=denoise, multiplier=mult,
                                    part_name=f"upsc_{src.stem}_{i:03d}")
        run = queue_and_wait(host, graph, job, f"chunk {i+1}/{len(chunks)}")
        fn, sub = first_video_output(run)
        lp = work / f"up_{i:03d}.mp4"
        comfy_download(host, fn, sub, lp)
        results.append(lp)
        # next anchor = last frame of this upscaled chunk
        anchor_png = work / f"anchor_{i+1:03d}.png"
        ffmpeg(job, ["-sseof", "-0.5", "-i", str(lp), "-update", "1", "-frames:v", "1",
                     str(anchor_png)], "extracting anchor")
        anchor_remote = comfy_upload(host, anchor_png, anchor_png.name)

    lst = work / "concat.txt"
    lst.write_text("".join(f"file '{r}'\n" for r in results), encoding="utf-8")
    joined = work / "joined.mp4"
    ffmpeg(job, ["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)], "joining")
    out = DEPS["OUT_DIR"] / f"{src.stem}_upscaled_{int(time.time())}.mp4"
    ffmpeg(job, ["-i", str(joined), "-i", str(src), "-map", "0:v", "-map", "1:a?",
                 "-c", "copy", "-shortest", str(out)], "muxing original audio")
    return [DEPS["output_entry"](out, "video")]


# ---------------------------------------------------------------- registration

def register(app, deps: dict):
    global DEPS, NINJA_DIR, TPL_DIR, CFG_FILE
    DEPS = deps
    NINJA_DIR = deps["DATA"] / "ninja"
    TPL_DIR = deps["DATA"] / "comfyui_templates"
    CFG_FILE = deps["DATA"] / "comfyui.json"
    NINJA_DIR.mkdir(parents=True, exist_ok=True)

    @app.get("/api/ninja/state")
    def ninja_state():
        p = project()
        return {"config": cfg(), "templates": template_status(), "project": p,
                "retake": retake_session()}

    @app.post("/api/ninja/config")
    async def ninja_config(req: dict):
        c = cfg()
        for k in ("hosts", "active_host", "settings"):
            if k in req:
                c[k] = req[k] if not isinstance(c.get(k), dict) or not isinstance(req[k], dict) \
                    else {**c[k], **req[k]}
        _save_json(CFG_FILE, c)
        return {"ok": True, "config": c}

    @app.post("/api/ninja/ping/{host}")
    def ninja_ping(host: str):
        try:
            stats = comfy(host, "/system_stats", timeout=10)
            dev = (stats.get("devices") or [{}])[0]
            return {"ok": True, "device": dev.get("name", "?")}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    @app.post("/api/ninja/capture/{kind}")
    def ninja_capture(kind: str):
        return capture_template(kind)

    @app.post("/api/ninja/song")
    async def ninja_song(file: UploadFile = File(...), name: str = Form("")):
        dest = NINJA_DIR / ("song" + Path(file.filename).suffix)
        dest.write_bytes(await file.read())
        p = project()
        p["name"] = name or Path(file.filename).stem
        p["song"] = {"path": str(dest), "filename": file.filename,
                     "duration": media_duration(dest)}
        p["chunks"], p["next_start"] = [], 0.0
        p.pop("pending_tail", None)
        p.pop("work_resolution", None)  # new project = new grid, from ITS part1
        save_project(p)
        return p

    @app.post("/api/ninja/import_part")
    async def ninja_import_part(file: UploadFile = File(...), from_sec: float = Form(0.0)):
        """Start/continue a project from an existing video (e.g. a ready part1.mp4).
        The imported video becomes the current chunk in review state — press
        Continue to finalize it and prepare its tail for the next generation."""
        p = project()
        if not p.get("song"):
            raise HTTPException(400, "Load the song first")
        imports = NINJA_DIR / "imports"
        imports.mkdir(parents=True, exist_ok=True)
        dest = imports / (uuid.uuid4().hex[:8] + Path(file.filename or "part.mp4").suffix)
        dest.write_bytes(await file.read())
        dur = media_duration(dest)
        if dur <= 0:
            raise HTTPException(400, "Could not read video duration")
        to_sec = round(float(from_sec) + dur, 3)
        n = len([c for c in p["chunks"] if c.get("final")]) + 1
        chunk = {"id": uuid.uuid4().hex[:8],
                 "part": f"part{n}-{int(from_sec)}-{int(to_sec)}",
                 "from": float(from_sec), "to": to_sec,
                 "tail_used": False, "tail_seconds": 0.0,
                 "raw": str(dest), "final": None, "status": "review",
                 "request": {"from_sec": float(from_sec), "to_sec": to_sec,
                              "prompts": [], "imported": True},
                 "host": "director"}
        p["chunks"] = [c for c in p["chunks"] if c.get("final")] + [chunk]
        save_project(p)
        return p

    @app.post("/api/ninja/reset")
    def ninja_reset():
        p = project()
        p["chunks"], p["next_start"] = [], 0.0
        p.pop("pending_tail", None)
        p.pop("work_resolution", None)  # new project = new grid, from ITS part1
        save_project(p)
        return p

    @app.post("/api/ninja/generate")
    async def ninja_generate(req: dict):
        p = project()
        if not p.get("song"):
            raise HTTPException(400, "Upload a song first")
        if not req.get("prompts"):
            raise HTTPException(400, "At least one text prompt segment required")
        load_template("director")  # fail fast if missing
        job = DEPS["start_job"]("ninja-generate", job_generate, "director", p, req)
        return {"job": job["id"]}

    @app.post("/api/ninja/retake")
    async def ninja_retake(req: dict = None):
        p = project()
        if not p["chunks"] or p["chunks"][-1].get("final"):
            raise HTTPException(400, "Nothing to retake — generate first")
        chunk = p["chunks"][-1]
        load_template("director")
        # retake = same request, NEW seed (always randomized), optional NEW duration
        new_req = dict(chunk["request"])
        dur = (req or {}).get("duration_sec")
        if dur:
            new_req["to_sec"] = round(float(new_req["from_sec"]) + float(dur), 3)
        job = DEPS["start_job"]("ninja-retake", job_generate, "director", p, new_req)
        return {"job": job["id"]}

    @app.post("/api/ninja/continue")
    async def ninja_continue(req: dict = None):
        p = project()
        if not p["chunks"] or p["chunks"][-1].get("final"):
            raise HTTPException(400, "Nothing to continue — generate first")
        upscale_tail = bool((req or {}).get("upscale_tail", False))
        if upscale_tail:
            load_template("upscaler")  # fail fast with a clear message if missing
        accept = (req or {}).get("accept_sec")
        job = DEPS["start_job"]("ninja-continue", job_continue, "upscaler", p,
                                accept, upscale_tail)
        return {"job": job["id"]}

    @app.post("/api/ninja/upscale")
    async def ninja_upscale(req: dict):
        load_template("upscaler")
        job = DEPS["start_job"]("ninja-upscale", job_standalone_upscale, "upscaler", req)
        return {"job": job["id"]}

    # ---------------- Retake (clip window patching) ----------------

    @app.post("/api/ninja/retake_clip/upload")
    async def retake_upload(file: UploadFile = File(...)):
        rt_dir = NINJA_DIR / "retake"
        rt_dir.mkdir(parents=True, exist_ok=True)
        dest = rt_dir / (uuid.uuid4().hex[:8] + Path(file.filename or "clip.mp4").suffix)
        dest.write_bytes(await file.read())
        info = DEPS["ffprobe"](dest)
        v = next((st for st in info.get("streams", []) if st.get("codec_type") == "video"), None)
        if not v:
            raise HTTPException(400, "No video stream in file")
        num, _, den = (v.get("avg_frame_rate") or "24/1").partition("/")
        fps = float(num) / float(den or 1)
        dur = float(info.get("format", {}).get("duration") or 0)
        clip = {"path": str(dest), "filename": file.filename, "duration": round(dur, 3),
                "fps": round(fps, 3), "width": v.get("width"), "height": v.get("height"),
                "frames": int(v.get("nb_frames") or round(dur * fps))}
        save_retake_session({"clip": clip, "pending": None})
        return clip

    @app.post("/api/ninja/retake_clip/generate")
    async def retake_generate(req: dict):
        sess = retake_session()
        if not sess.get("clip"):
            raise HTTPException(400, "Upload a clip first")
        if not req.get("prompts"):
            raise HTTPException(400, "At least one text prompt segment required")
        load_template("director")
        start_sec, end_sec = float(req.get("start_sec", -1)), float(req.get("end_sec", -1))
        win = end_sec - start_sec
        clip = sess["clip"]
        fps = float(clip["fps"])
        if not (RETAKE_MIN_S <= win <= RETAKE_MAX_S):
            raise HTTPException(400, f"Window must be {RETAKE_MIN_S:.0f}-{RETAKE_MAX_S:.0f}s (got {win:.1f}s)")
        if start_sec < 0 or end_sec > float(clip["duration"]) - 0.5:
            raise HTTPException(400, "Window must be inside the clip (and end ≥0.5s before its end)")
        if int(req.get("mode", 1)) == 2 and start_sec * fps < 121:
            raise HTTPException(400, "Mode 2 needs ≥5s of video before the window — use mode 1 here")
        job = DEPS["start_job"]("ninja-retake-clip", job_retake_generate, "director", req)
        return {"job": job["id"]}

    @app.post("/api/ninja/retake_clip/apply")
    def retake_apply():
        sess = retake_session()
        pend = sess.get("pending")
        if not pend:
            raise HTTPException(400, "Nothing to apply — generate a retake first")
        clip = Path(sess["clip"]["path"])
        out = clip.with_name(clip.stem + f"_retake_{pend['tag'][:6]}.mp4")
        Path(pend["preview"]).replace(out)
        # the patched clip becomes the session clip, so retakes can be chained
        sess["clip"]["path"] = str(out)
        sess["clip"]["filename"] = out.name
        sess["pending"] = None
        save_retake_session(sess)
        return {"ok": True, "path": str(out)}

    @app.post("/api/ninja/retake_clip/discard")
    def retake_discard():
        sess = retake_session()
        sess["pending"] = None
        save_retake_session(sess)
        return {"ok": True}

    @app.get("/api/ninja/file")
    def ninja_file(path: str):
        fp = Path(path)
        if not fp.is_file() or NINJA_DIR not in fp.parents:
            raise HTTPException(404, "not found")
        from fastapi.responses import FileResponse
        return FileResponse(fp)
