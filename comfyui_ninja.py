"""Ninja Director — LTX music-video pipeline section for CupCut Studio.

Drives ComfyUI (local or remote) over its HTTP API only:
  - Director: chunk-by-chunk generation with chained 5s upscaled tails.
  - Upscaler: Pixel Spatial IC-LoRA ×2 (LoadVideo → ManualSigmas → SaveVideo).
    Chunks are pad/trim joined to the source frame count + original audio.

Templates are frozen snapshots of successful runs captured from /history —
per host, per kind ("director" / "upscaler"). The builder only overrides
inputs (timeline, files, seed); it never edits workflows on the host.

Wired from server.py via register(app, deps).
"""
from __future__ import annotations

import base64
import copy
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, File, Form, Request

# populated by register()
DEPS: dict = {}

NINJA_DIR: Path = None  # DATA/ninja
TPL_DIR: Path = None    # DATA/comfyui_templates
CFG_FILE: Path = None   # DATA/comfyui.json
SONGS_DIR: Path = None  # DATA/ninja/songs — drop tracks here for the library

SONG_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}

DEFAULT_CFG = {
    # role-based hosts: every Director execution goes to "director", every upscale
    # execution goes to "upscaler". Same URL or different machines — user's choice.
    "hosts": {
        "director": {"url": "http://127.0.0.1:8188"},
        "upscaler": {"url": "http://127.0.0.1:8188"},
    },
    "settings": {
        "tail_seconds": 5.0,
        # Legacy VHS upscaler knobs (ignored by Pixel Spatial template).
        "tail_upscale_denoise": 1.0,
        "tail_upscale_multiplier": 1.3,
        # Pixel Spatial: short-edge resize before the fixed ×2 latent upscale.
        "upscale_shorter_size": 416,
        "frame_rate": 24,
        "width": 0,
        "height": 0,
        # Optional override only. Preferred: run Director once with Ingredients
        # IC-LoRA selected → Capture template (Guide nodes keep the LoRA name).
        "ic_lora_name": "",
        "ic_lora_strength": 1.0,
    },
}

# node classes that identify a template kind
KIND_MARKERS = {"director": "LTXDirector",
                "upscaler": "LTXICLoRALoaderModelOnly",
                "h3": "MiniMaxH3ImageToVideo",
                "h3ref": "MiniMaxH3ReferenceToVideo"}

# Pixel Spatial IC-LoRA upscaler (H100 / LoadVideo + ManualSigmas + SaveVideo)
UP_VIDEO = "5001"      # LoadVideo → inputs.file
UP_SHORTER = "5026"    # ResizeImageMaskNode → resize_type.shorter_size
UP_PROMPT = "2483"     # CLIPTextEncode (positive)
UP_SAVE = "4852"       # SaveVideo
# Legacy VHS / RTX template ids (only used if those nodes still exist)
UP_IMAGE, UP_BYPASS, UP_SCHED, UP_MULT = "2004", "5019", "5074", "5093"

# Grok refine for IC-upscale anchors — same API as grokmcp `edit_image`
# (docker container vigilant_gould). No invented n8n webhooks.
DEFAULT_GROK_UPSCALE_PROMPT = (
    "upscale only, realistic skin, keep all details exactly the same"
)
GROK_MCP_CONTAINER = os.environ.get("GROK_MCP_CONTAINER", "vigilant_gould").strip()


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
    # nested settings keys added over time (ic_lora_name, …)
    settings = c.setdefault("settings", {})
    for sk, sv in DEFAULT_CFG["settings"].items():
        settings.setdefault(sk, copy.deepcopy(sv))
    return c


def host_url(role: str) -> str:
    hosts = cfg()["hosts"]
    # h3ref runs on the h3 host; h3 rides the director host unless configured
    if role == "h3ref":
        role = "h3"
    if role == "h3" and not (hosts.get("h3") or {}).get("url"):
        role = "director"
    try:
        return hosts[role]["url"].rstrip("/")
    except KeyError:
        raise HTTPException(400, f"unknown host role '{role}' (use director/upscaler/h3)")


def active_session_file() -> Path:
    return NINJA_DIR / "active_session.json"


def set_active_session_id(sid: str):
    _save_json(active_session_file(), {"session_id": sid})


def session_dir(sid: str | None = None) -> Path:
    """ninja/sessions/{uuid}/ — isolated workspace for one Director session."""
    sid = (sid or get_active_session_id()).strip()
    d = NINJA_DIR / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    for sub in ("work", "parts", "imports", "exports"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def session_work_dir(p: dict | None = None) -> Path:
    return session_dir((p or {}).get("session_id")) / "work"


def session_parts_dir(p: dict | None = None) -> Path:
    return session_dir((p or {}).get("session_id")) / "parts"


def session_imports_dir(p: dict | None = None) -> Path:
    return session_dir((p or {}).get("session_id")) / "imports"


def session_exports_dir(p: dict | None = None) -> Path:
    return session_dir((p or {}).get("session_id")) / "exports"


def begin_session(keep_prompts_from: dict | None = None) -> str:
    """Mint a new session UUID folder and make it active (empty project)."""
    sid = uuid.uuid4().hex
    d = NINJA_DIR / "sessions" / sid
    for sub in ("work", "parts", "imports", "exports"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    try:
        (parts_root() / "sessions" / sid).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    p = {
        "session_id": sid,
        "name": "",
        "song": None,
        "chunks": [],
        "next_start": 0.0,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if keep_prompts_from and isinstance(keep_prompts_from.get("last_prompts"), dict):
        p["last_prompts"] = keep_prompts_from["last_prompts"]
    set_active_session_id(sid)
    _save_json(d / "project.json", p)
    try:
        write_session_metadata(p)
    except Exception:
        pass
    return sid


def get_active_session_id() -> str:
    data = _load_json(active_session_file(), {})
    sid = str(data.get("session_id") or "").strip()
    if sid:
        (NINJA_DIR / "sessions" / sid).mkdir(parents=True, exist_ok=True)
        for sub in ("work", "parts", "imports", "exports"):
            (NINJA_DIR / "sessions" / sid / sub).mkdir(parents=True, exist_ok=True)
        return sid
    # First run / upgrade: adopt legacy flat project.json into a new session once.
    sid = uuid.uuid4().hex
    d = NINJA_DIR / "sessions" / sid
    for sub in ("work", "parts", "imports", "exports"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    legacy = NINJA_DIR / "project.json"
    if legacy.is_file():
        p = _load_json(legacy, {"name": "", "song": None, "chunks": [], "next_start": 0.0})
        p["session_id"] = sid
        _save_json(d / "project.json", p)
        try:
            legacy.rename(NINJA_DIR / "project.json.pre_session.bak")
        except Exception:
            pass
    else:
        _save_json(d / "project.json", {
            "session_id": sid, "name": "", "song": None,
            "chunks": [], "next_start": 0.0,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
    set_active_session_id(sid)
    return sid


def project_file() -> Path:
    return session_dir() / "project.json"


def project() -> dict:
    p = _load_json(project_file(), {"name": "", "song": None, "chunks": [], "next_start": 0.0})
    if not p.get("session_id"):
        p["session_id"] = get_active_session_id()
    return p


def save_project(p: dict):
    if not p.get("session_id"):
        p["session_id"] = get_active_session_id()
    # Always write into the session folder named by the project (or active).
    dest = session_dir(p.get("session_id")) / "project.json"
    _save_json(dest, p)
    if p.get("session_id"):
        set_active_session_id(p["session_id"])
    try:
        write_session_metadata(p)
    except Exception:
        pass


def _list_session_artifact_relpaths(root: Path) -> list[str]:
    """Relative file paths under a session folder (skip project/metadata)."""
    out = []
    if not root.is_dir():
        return out
    skip = {"project.json", "metadata.json", "README.txt", "session.txt"}
    for fp in sorted(root.rglob("*")):
        if not fp.is_file():
            continue
        if fp.name in skip and fp.parent == root:
            continue
        try:
            out.append(fp.relative_to(root).as_posix())
        except Exception:
            out.append(fp.name)
    return out[:400]


def write_session_metadata(p: dict | None = None) -> Path:
    """Write metadata.json (+ short README) into the session UUID folder.

    One place that describes progress, last part, and every artifact path so the
    folder is self-contained and browsable without opening project.json.
    """
    p = p if p is not None else project()
    sid = str(p.get("session_id") or get_active_session_id())
    root = session_dir(sid)
    song = p.get("song") or {}
    chunks = p.get("chunks") or []
    finals = [c for c in chunks if c.get("final")]
    last = chunks[-1] if chunks else None
    prog = song_progress(p)
    phase = director_phase(p)
    parts_summary = []
    for c in chunks:
        req = c.get("request") or {}
        parts_summary.append({
            "id": c.get("id"),
            "part": c.get("part"),
            "from": c.get("from"),
            "to": c.get("to"),
            "status": c.get("status") or ("done" if c.get("final") else "review"),
            "imported": bool(req.get("imported")),
            "final": c.get("final"),
            "preview": c.get("preview"),
            "raw": c.get("raw"),
        })
    last_part = None
    if last:
        req = last.get("request") or {}
        last_part = {
            "id": last.get("id"),
            "part": last.get("part"),
            "from": last.get("from"),
            "to": last.get("to"),
            "status": last.get("status") or ("done" if last.get("final") else "review"),
            "imported": bool(req.get("imported")),
            "final": last.get("final"),
            "preview": last.get("preview"),
            "raw": last.get("raw"),
        }
    export_mirror = str(parts_root() / "sessions" / sid)
    meta = {
        "session_id": sid,
        "created_at": p.get("created_at"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "song": {
            "name": p.get("name") or "",
            "filename": song.get("filename"),
            "duration": song.get("duration"),
            "path": song.get("path"),
        },
        "phase": phase,
        "progress": {
            "covered_sec": prog.get("covered_sec"),
            "song_sec": prog.get("song_sec"),
            "remaining_sec": prog.get("remaining_sec"),
            "percent": prog.get("percent"),
            "parts_done": prog.get("parts_done"),
            "next_start": p.get("next_start", 0.0),
        },
        "last_part": last_part,
        "parts": parts_summary,
        "pending_review": bool(last and not last.get("final")),
        "pending_tail": {
            "from_chunk": (p.get("pending_tail") or {}).get("from_chunk"),
            "seconds": (p.get("pending_tail") or {}).get("seconds"),
            "frames": (p.get("pending_tail") or {}).get("frames"),
            "path": (p.get("pending_tail") or {}).get("path"),
        } if p.get("pending_tail") else None,
        "work_resolution": p.get("work_resolution"),
        "final_video": p.get("final_video"),
        "finished_at": p.get("finished_at"),
        "artifacts": {
            "session_dir": str(root),
            "parts_dir": str(root / "parts"),
            "work_dir": str(root / "work"),
            "imports_dir": str(root / "imports"),
            "exports_dir": str(root / "exports"),
            "export_mirror": export_mirror,
            "files": _list_session_artifact_relpaths(root),
        },
    }
    meta_path = root / "metadata.json"
    _save_json(meta_path, meta)
    # Human-readable one-pager
    lp = last_part or {}
    readme = (
        f"CupCut session {sid}\n"
        f"song: {meta['song'].get('filename') or '(none)'}\n"
        f"phase: {phase}\n"
        f"progress: {prog.get('covered_sec')}s / {prog.get('song_sec')}s "
        f"({prog.get('percent')}%) — next {p.get('next_start')}s\n"
        f"parts_done: {prog.get('parts_done')}\n"
        f"last_part: {lp.get('part') or '-'} "
        f"[{lp.get('from')}–{lp.get('to')}] {lp.get('status') or ''}\n"
        f"updated: {meta['updated_at']}\n"
        f"\nfolders: parts/ work/ imports/ exports/\n"
        f"mirror: {export_mirror}\n"
    )
    try:
        (root / "README.txt").write_text(readme, encoding="utf-8")
    except Exception:
        pass
    # Mirror metadata next to durable D:\parts\sessions\{uuid}\ exports
    try:
        mirror = parts_root() / "sessions" / sid
        mirror.mkdir(parents=True, exist_ok=True)
        _save_json(mirror / "metadata.json", meta)
        (mirror / "README.txt").write_text(readme, encoding="utf-8")
    except Exception:
        pass
    return meta_path


def _prompt_snapshot(req: dict | None) -> dict | None:
    """Normalize global + segment prompts from a chunk request. Skip imports/empty."""
    if not isinstance(req, dict) or req.get("imported"):
        return None
    global_prompt = req.get("global_prompt") or ""
    if not isinstance(global_prompt, str):
        global_prompt = str(global_prompt)
    prompts_out = []
    for item in (req.get("prompts") or []):
        if isinstance(item, dict):
            text = (item.get("text") or "").strip()
            if text:
                prompts_out.append({"text": text})
        else:
            text = str(item).strip()
            if text:
                prompts_out.append({"text": text})
    if not global_prompt.strip() and not prompts_out:
        return None
    return {"global_prompt": global_prompt, "prompts": prompts_out}


def remember_executed_prompts(p: dict, req: dict | None = None) -> bool:
    """Keep latest executed global + segment prompts on the project (survives reset)."""
    snap = _prompt_snapshot(req) if req is not None else None
    if snap is None:
        for c in reversed(p.get("chunks") or []):
            snap = _prompt_snapshot(c.get("request"))
            if snap:
                break
    if snap:
        p["last_prompts"] = snap
        return True
    return False


def songs_dir() -> Path:
    d = SONGS_DIR or (NINJA_DIR / "songs")
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_library_songs() -> list[dict]:
    """Audio files dropped into ninja/songs/ (sorted by name)."""
    out = []
    for p in sorted(songs_dir().iterdir(), key=lambda x: x.name.lower()):
        if not p.is_file() or p.suffix.lower() not in SONG_EXTS:
            continue
        try:
            dur = media_duration(p)
        except Exception:
            dur = 0.0
        out.append({"filename": p.name, "path": str(p), "duration": round(float(dur), 3),
                    "size": p.stat().st_size})
    return out


def apply_song_file(src: Path, display_name: str | None = None) -> dict:
    """Start a brand-new session UUID and activate the song inside it."""
    if not src.is_file():
        raise HTTPException(404, f"Song not found: {src.name}")
    prev = None
    try:
        prev = project()
        remember_executed_prompts(prev)
    except Exception:
        prev = None
    begin_session(keep_prompts_from=prev)
    suffix = src.suffix.lower() or ".wav"
    dest = session_dir() / ("song" + suffix)
    if src.resolve() != dest.resolve():
        dest.write_bytes(src.read_bytes())
    filename = display_name or src.name
    p = project()
    p["name"] = Path(filename).stem
    p["song"] = {"path": str(dest), "filename": filename,
                 "duration": media_duration(dest)}
    p["chunks"], p["next_start"] = [], 0.0
    p.pop("pending_tail", None)
    p.pop("work_resolution", None)
    p.pop("final_video", None)
    p.pop("finished_at", None)
    save_project(p)
    return director_status(p)


def save_to_song_library(data: bytes, filename: str) -> Path:
    """Store a copy under ninja/songs/ (unique name if collision)."""
    safe = Path(filename or "song.wav").name
    if Path(safe).suffix.lower() not in SONG_EXTS:
        safe += ".wav"
    dest = songs_dir() / safe
    if dest.exists():
        dest = songs_dir() / f"{Path(safe).stem}_{uuid.uuid4().hex[:6]}{Path(safe).suffix}"
    dest.write_bytes(data)
    return dest


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


def song_slug(p: dict | None = None) -> str:
    """Filesystem-safe song folder name — keeps songs from overwriting each other."""
    p = p if p is not None else project()
    raw = (p.get("name") or (p.get("song") or {}).get("filename") or "song").strip()
    raw = Path(str(raw)).stem
    slug = re.sub(r"[^\w\-]+", "_", raw, flags=re.UNICODE).strip("_")
    return (slug or "song")[:60]


def parts_root() -> Path:
    """D:\\parts (or CUPCUT_PARTS_DIR)."""
    return Path(os.environ.get("CUPCUT_PARTS_DIR", r"D:\parts"))


def fullvideo_dir() -> Path:
    """D:\\parts\\fullvideo — finished movies for Polish."""
    d = parts_root() / "fullvideo"
    d.mkdir(parents=True, exist_ok=True)
    return d


def parts_export_dir(p: dict | None = None) -> Path:
    """D:\\parts\\sessions\\{session_id}\\ — durable exports for one session only."""
    p = p if p is not None else project()
    sid = str(p.get("session_id") or get_active_session_id())
    d = parts_root() / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    # human-readable marker (song name) — isolation is by UUID, not slug
    meta = d / "session.txt"
    if not meta.is_file():
        try:
            meta.write_text(
                f"session_id={sid}\nsong={p.get('name') or ''}\n"
                f"file={(p.get('song') or {}).get('filename') or ''}\n",
                encoding="utf-8",
            )
        except Exception:
            pass
    return d


def _path_allowed(fp: Path) -> bool:
    """Serve/load only under ninja data or D:\\parts."""
    try:
        rp = fp.resolve()
    except Exception:
        return False
    if not rp.is_file():
        return False
    roots = []
    if NINJA_DIR:
        roots.append(Path(NINJA_DIR).resolve())
    roots.append(parts_root().resolve())
    for root in roots:
        try:
            rp.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def list_fullvideos() -> list[dict]:
    """Videos in D:\\parts\\fullvideo (mp4/mov/webm), newest first."""
    items = []
    root = fullvideo_dir()
    exts = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
    files = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in exts]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files:
        try:
            dur = media_duration(p)
        except Exception:
            dur = 0.0
        items.append({
            "id": f"fullvideo:{p.name}",
            "label": p.name,
            "path": str(p),
            "url": file_url(str(p)),
            "duration": round(float(dur), 3),
            "size": p.stat().st_size,
        })
    return items


def clip_meta_from_path(path: Path, filename: str | None = None) -> dict:
    info = DEPS["ffprobe"](path)
    v = next((st for st in info.get("streams", []) if st.get("codec_type") == "video"), None)
    if not v:
        raise HTTPException(400, "No video stream in file")
    num, _, den = (v.get("avg_frame_rate") or "24/1").partition("/")
    fps = float(num) / float(den or 1)
    dur = float(info.get("format", {}).get("duration") or 0)
    return {
        "path": str(path.resolve()),
        "filename": filename or path.name,
        "duration": round(dur, 3),
        "fps": round(fps, 3),
        "width": v.get("width"),
        "height": v.get("height"),
        "frames": int(v.get("nb_frames") or round(dur * fps)),
        "url": file_url(str(path.resolve())),
    }


def export_part_copy(final_path: Path, p: dict | None = None) -> Path | None:
    """Copy an approved part into the song's export folder."""
    try:
        import shutil
        dest = parts_export_dir(p) / Path(final_path).name
        shutil.copy2(final_path, dest)
        return dest
    except Exception:
        return None


def song_progress(p: dict | None = None) -> dict:
    """Covered timeline vs song length + 15s milestone flags."""
    p = p if p is not None else project()
    song = p.get("song") or {}
    song_sec = float(song.get("duration") or 0.0)
    finals = [c for c in (p.get("chunks") or []) if c.get("final")]
    covered = 0.0
    if finals:
        covered = max(float(c.get("to") or 0) for c in finals)
    else:
        covered = float(p.get("next_start") or 0.0)
    remaining = max(0.0, song_sec - covered) if song_sec else 0.0
    pct = min(100.0, (covered / song_sec) * 100.0) if song_sec > 0 else 0.0
    step = 15.0
    marks = []
    if song_sec > 0:
        t = step
        while t < song_sec + 1e-6:
            marks.append({"at": int(t), "done": covered + 1e-6 >= t})
            t += step
        # always include song end marker
        end_at = round(song_sec, 1)
        if not marks or marks[-1]["at"] != int(song_sec):
            marks.append({"at": end_at, "done": covered + 0.25 >= song_sec, "end": True})
    return {
        "covered_sec": round(covered, 3),
        "song_sec": round(song_sec, 3),
        "remaining_sec": round(remaining, 3),
        "percent": round(pct, 1),
        # near end (~2s left) counts as finishable — short leftover tails are not worth another part
        "past_end": bool(
            song_sec > 0 and (covered + 0.25 >= song_sec or remaining <= 2.0 or pct >= 98.0)
        ),
        "milestones": marks,
        "parts_done": len(finals),
    }


def stitch_parts_to_song(
    part_paths: list[Path],
    audio_path: Path,
    out_path: Path,
    job: dict | None = None,
) -> Path:
    """Concat silent video parts (in order) and mux full song audio → final mp4."""
    if not part_paths:
        raise DEPS["ToolError"]("No part videos to stitch")
    if not audio_path.is_file():
        raise DEPS["ToolError"](f"Song audio missing: {audio_path}")
    for vp in part_paths:
        if not Path(vp).is_file():
            raise DEPS["ToolError"](f"Part missing: {vp}")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = out_path.parent / "_stitch_work"
    work.mkdir(parents=True, exist_ok=True)
    lst = work / "concat.txt"
    # forward slashes avoid ffmpeg concat quoting issues on Windows
    lines = []
    for vp in part_paths:
        pth = Path(vp).resolve().as_posix().replace("'", r"'\''")
        lines.append(f"file '{pth}'\n")
    lst.write_text("".join(lines), encoding="utf-8")
    joined = work / "joined_v.mp4"
    if job is not None:
        job["message"] = f"concat {len(part_paths)} parts"
        job["progress"] = 0.3
    DEPS["run"]([
        DEPS["FFMPEG"], "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(lst),
        "-an", "-c:v", "libx264", "-crf", "14", "-preset", "fast",
        "-pix_fmt", "yuv420p", str(joined),
    ])
    if job is not None:
        job["message"] = "muxing song audio"
        job["progress"] = 0.7
    DEPS["run"]([
        DEPS["FFMPEG"], "-y", "-loglevel", "error",
        "-i", str(joined), "-i", str(audio_path),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", str(out_path),
    ])
    if job is not None:
        job["progress"] = 1.0
        job["message"] = "finish ready"
    return out_path


def collect_final_part_paths(p: dict | None = None) -> list[Path]:
    p = p if p is not None else project()
    finals = [c for c in (p.get("chunks") or []) if c.get("final")]
    finals.sort(key=lambda c: float(c.get("from") or 0))
    return [Path(c["final"]) for c in finals]


def job_finish(job, _host: str, p: dict, req: dict | None = None):
    """Stitch all approved parts + song → one final MP4."""
    req = req or {}
    parts = collect_final_part_paths(p)
    if not parts:
        raise DEPS["ToolError"]("No approved parts — Continue at least one chunk first")
    song = Path((p.get("song") or {}).get("path") or "")
    exports = session_exports_dir(p)
    stem = (p.get("name") or "song").strip() or "song"
    stem = re.sub(r'[<>:"/\\|?*]+', "_", stem)[:80]
    out = exports / f"{stem}_full.mp4"
    stitch_parts_to_song(parts, song, out, job=job)
    p["final_video"] = str(out)
    p["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_project(p)
    import shutil
    # Always mirror parts + full into D:\parts\{song_slug}\
    exp = parts_export_dir(p)
    for part in parts:
        try:
            shutil.copy2(part, exp / Path(part).name)
        except Exception:
            pass
    song_full = exp / "full.mp4"
    try:
        shutil.copy2(out, song_full)
    except Exception:
        song_full = None
    copy_to = (req.get("copy_to") or "").strip()
    if copy_to:
        dest = Path(copy_to)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out, dest)
    outs = [DEPS["output_entry_abs"](out, "video", f"{stem} FULL")]
    if song_full and song_full.is_file():
        outs.append(DEPS["output_entry_abs"](song_full, "video", f"{song_slug(p)}/full.mp4"))
    return outs


def randomize_seeds(graph: dict):
    for node in graph.values():
        if node["class_type"] == "RandomNoise":
            node["inputs"]["noise_seed"] = random.randint(0, 2**48)


def set_filename_prefix(graph: dict, prefix: str):
    for node in graph.values():
        if "filename_prefix" in node.get("inputs", {}):
            node["inputs"]["filename_prefix"] = prefix


# ---------------------------------------------------------------- director build

def template_ic_lora(kind: str = "director") -> tuple[str, float]:
    """IC-LoRA name/strength baked into the captured template (LTXDirectorGuide)."""
    try:
        graph = load_template(kind)
    except Exception:
        return "", 1.0
    name, strength = "", 1.0
    for node in graph.values():
        if node.get("class_type") != "LTXDirectorGuide":
            continue
        inp = node.get("inputs") or {}
        n = str(inp.get("ic_lora_name") or "").strip()
        if n and n != "None":
            name = n
            try:
                strength = float(inp.get("ic_lora_strength", 1.0))
            except (TypeError, ValueError):
                strength = 1.0
            break
    return name, strength


def build_director_graph(host: str, *, prompts: list, from_sec: float, to_sec: float,
                         fps: int, width: int, height: int, tail_file: str | None,
                         tail_seconds: float, image_file: str | None,
                         global_prompt: str, part_name: str, song_file: str,
                         end_image_file: str | None = None,
                         audio_tracks: list | None = None,
                         end_zone_frames: int = 6,
                         image_zone_frames: int | None = None,
                         total_frames: int | None = None,
                         motion_files: list | None = None,
                         ic_lora_name: str | None = None,
                         ic_lora_strength: float | None = None) -> dict:
    """Build a Director graph for one chunk.

    Timeline (all in frames, starting at 0):
      [tail video (tail_seconds)] or [image] or nothing
      [text prompt segments...] filling the regenerated window
      [end-frame image] (optional, ONE frame immediately after the window,
                         isEndFrame — native node feature)
      [trailing text padding] (only with an end-frame image; fills the 8n+1
                         grid, generated past the landing point, trimmed away)
      audio: one segment = song slice covering the whole window.
      IC-LoRA track (motionSegments): optional reference videos (ingredients /
                         character sheet / singer clip) when ic_lora_name is set.
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
        # img2 = ONE-frame keyframe exactly at the window end. Proven both
        # ways: a long isEndFrame segment makes the model settle onto the
        # image ~10 frames EARLY (frozen dupes at the end of the window);
        # the 1-frame pin + trailing text keeps it moving right up to the
        # landing point. The trailing region is trimmed away.
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

    # IC-LoRA / Ingredients track — ComfyUI Guide loads these as motion videos
    motion_segments = []
    for i, vf in enumerate(motion_files or []):
        if not vf:
            continue
        motion_segments.append({
            "id": uuid.uuid4().hex[:13] + f"_m{i}",
            "type": "motion_video",
            "start": 0,
            "length": total_frames,
            "trimStart": 0,
            "videoDurationFrames": total_frames,
            "videoFile": vf,
            "fileName": str(vf).split("/")[-1],
            "videoStrength": 1.0,
            "videoAttentionStrength": 0.65,
            "resampleMode": "nearest",
        })

    # Prefer LoRA already captured in the template Guides; optional override from
    # request/settings only when explicitly provided.
    tpl_lora, tpl_strength = "", 1.0
    for node in graph.values():
        if node.get("class_type") != "LTXDirectorGuide":
            continue
        n = str((node.get("inputs") or {}).get("ic_lora_name") or "").strip()
        if n and n != "None":
            tpl_lora = n
            try:
                tpl_strength = float((node.get("inputs") or {}).get("ic_lora_strength", 1.0))
            except (TypeError, ValueError):
                tpl_strength = 1.0
            break
    override = (ic_lora_name or "").strip()
    if override and override != "None":
        active_lora, active_strength = override, float(
            ic_lora_strength if ic_lora_strength is not None else tpl_strength)
    else:
        active_lora, active_strength = tpl_lora, tpl_strength
    use_ic = bool(motion_segments) and bool(active_lora) and active_lora != "None"
    tl = {
        "mainTrackEnabled": True, "audioTrackEnabled": True, "motionTrackEnabled": True,
        "propHeight": 90, "globalPropHeight": 60, "showFilenames": True,
        "overrideAudio": False, "inpaint_audio": False,
        "global_prompt": global_prompt, "retake_global_prompt": "",
        "retakeMode": False, "retakeStart": 0, "retakeLength": 0, "retakePrompt": "",
        "retakeStrength": 1, "retakeVideo": None,
        "normalStartFrame": 0, "normalDurationFrames": total_frames,
        "segments": segments,
        "motionSegments": motion_segments if use_ic else [],
        "audioSegments": audio_segments,
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
        "use_custom_motion": use_ic,
        "frame_rate": float(fps), "custom_width": width, "custom_height": height,
        "resize_method": "crop", "display_mode": "seconds",
    })
    # Only rewrite Guide LoRA when caller overrides; otherwise keep Captured values
    if use_ic and override and override != "None":
        for _nid, node in graph.items():
            if node.get("class_type") == "LTXDirectorGuide":
                node["inputs"]["ic_lora_name"] = active_lora
                node["inputs"]["ic_lora_strength"] = active_strength
                node["inputs"]["auto_snap_ic_grid"] = True
    randomize_seeds(graph)
    set_filename_prefix(graph, f"ninja/{part_name}")
    return graph


def upscale_template_is_pixel(graph: dict | None = None) -> bool:
    """True for the Pixel Spatial IC-LoRA template (LoadVideo, no VHS skip)."""
    g = graph if graph is not None else load_template("upscaler")
    return any(n.get("class_type") == "LTXICLoRALoaderModelOnly" for n in g.values())


def build_upscale_graph(host: str, *, video_file: str, anchor_file: str | None,
                        denoise: float | None, multiplier: float | None,
                        part_name: str, skip_first_frames: int = 0,
                        shorter_size: int | None = None,
                        prompt: str | None = None) -> dict:
    graph = copy.deepcopy(load_template("upscaler"))
    vin = graph[UP_VIDEO]["inputs"]
    if "file" in vin or graph[UP_VIDEO].get("class_type") == "LoadVideo":
        # Pixel Spatial / core LoadVideo — no skip_first_frames; caller must
        # pre-trim overlap frames before upload when needed.
        vin["file"] = video_file
    else:
        # Legacy VHS_LoadVideo path
        vin["video"] = video_file
        vin["frame_load_cap"] = 0
        vin["skip_first_frames"] = int(skip_first_frames)

    ss = shorter_size
    if ss is None and multiplier is not None:
        try:
            m = float(multiplier)
            if m >= 64:  # treat as short-edge pixels (UI "Short edge")
                ss = int(m)
        except (TypeError, ValueError):
            pass
    if ss is None:
        try:
            ss = int((cfg().get("settings") or {}).get("upscale_shorter_size") or 0) or None
        except (TypeError, ValueError):
            ss = None
    if ss is not None and UP_SHORTER in graph:
        graph[UP_SHORTER]["inputs"]["resize_type.shorter_size"] = int(ss)

    if prompt is not None and UP_PROMPT in graph:
        graph[UP_PROMPT]["inputs"]["text"] = prompt

    # Legacy RTX / VHS nodes — no-op on Pixel Spatial template
    if UP_BYPASS in graph:
        graph[UP_BYPASS]["inputs"]["value"] = anchor_file is None
    if anchor_file and UP_IMAGE in graph:
        graph[UP_IMAGE]["inputs"]["image"] = anchor_file
    if denoise is not None and UP_SCHED in graph:
        graph[UP_SCHED]["inputs"]["denoise"] = denoise
    if (multiplier is not None and UP_MULT in graph
            and (shorter_size is None)
            and not (isinstance(multiplier, (int, float)) and float(multiplier) >= 64)):
        graph[UP_MULT]["inputs"]["resize_type.multiplier"] = multiplier

    set_filename_prefix(graph, f"ninja/{part_name}_up")
    return graph


# ---------------------------------------------------------------- minimax h3

H3_I2V_NODE = "MiniMaxH3ImageToVideo"
H3_FPS = 24


def h3_dir() -> Path:
    d = NINJA_DIR / "h3"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _h3_frames(duration_sec: float) -> int:
    """Seconds → valid H3 frame length: 24fps snapped up to the 17k+5 grid
    (same expression as the workflow's Math node)."""
    f = max(5, round(float(duration_sec) * H3_FPS))
    return f + (5 - f % 17) % 17


def _snap_h3_dim(v: int) -> int:
    """H3 canvas sizes are multiples of 32."""
    return max(32, int(round(v / 32) * 32))


def _h3_size_from_image(path: Path) -> tuple[int, int]:
    """Native H3 canvas from the start image: 768 short edge, long edge ≤1344."""
    try:
        info = DEPS["ffprobe"](path)
        st = next((s for s in info.get("streams", []) if s.get("width")), {})
        w, h = int(st.get("width") or 0), int(st.get("height") or 0)
    except Exception:
        w = h = 0
    if w <= 0 or h <= 0:
        return 768, 1344
    if w >= h:
        return min(1344, round(768 * w / h)), 768
    return 768, min(1344, round(768 * h / w))


def _h3_find_upstream(graph: dict, ref, class_type: str, max_hops: int = 6) -> str | None:
    """Follow a ["node_id", slot] link upstream to the first node of class_type."""
    for _ in range(max_hops):
        if not (isinstance(ref, list) and len(ref) == 2):
            return None
        node = graph.get(str(ref[0]))
        if not node:
            return None
        if node.get("class_type") == class_type:
            return str(ref[0])
        ins = node.get("inputs") or {}
        ref = next((v for v in ins.values()
                    if isinstance(v, list) and len(v) == 2), None)
    return None


def build_h3_i2v_graph(*, prompt: str, image_file: str, width: int, height: int,
                       duration_sec: float, seed: int | None, part_name: str) -> dict:
    graph = copy.deepcopy(load_template("h3"))
    nid = next((k for k, n in graph.items()
                if n.get("class_type") == H3_I2V_NODE), None)
    if not nid:
        raise DEPS["ToolError"](
            "Captured 'h3' template has no MiniMaxH3ImageToVideo node — "
            "run the H3 i2v workflow once on the h3 host, then Capture h3.")
    ins = graph[nid]["inputs"]
    ins["prompt"] = prompt
    # literals override ResolutionSelector / Math node links from the template
    ins["width"] = int(width)
    ins["height"] = int(height)
    ins["length"] = _h3_frames(duration_sec)
    load_nid = _h3_find_upstream(graph, ins.get("first_frame"), "LoadImage")
    if not load_nid:
        raise DEPS["ToolError"](
            "h3 template has no LoadImage feeding first_frame — run the i2v "
            "workflow once WITH a start image connected, then re-capture h3.")
    graph[load_nid]["inputs"]["image"] = image_file
    randomize_seeds(graph)
    if seed is not None:
        for node in graph.values():
            if node.get("class_type") == "RandomNoise":
                node["inputs"]["noise_seed"] = int(seed)
    set_filename_prefix(graph, f"ninja/{part_name}")
    return graph


def job_h3_generate(job, host: str, req: dict):
    """One MiniMax H3 i2v shot: upload start image -> queue -> download video."""
    prompt = str(req.get("prompt") or "").strip()
    dur = max(1.0, min(15.0, float(req.get("duration_sec") or 5.0)))
    a = Path(DEPS["asset_path"](req["image_asset"]))
    width = int(req.get("width") or 0)
    height = int(req.get("height") or 0)
    if not (width and height):
        width, height = _h3_size_from_image(a)
    width, height = _snap_h3_dim(width), _snap_h3_dim(height)
    frames = _h3_frames(dur)
    part_name = f"h3_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    # center-crop + scale the start image to the exact canvas — H3 squeezes
    # mismatched aspect ratios instead of cropping, breaking proportions
    fitted = h3_dir() / f"{part_name}_start.png"
    ffmpeg(job, [
        "-i", str(a),
        "-vf", f"crop=w='min(iw,ih*{width}/{height})':h='min(ih,iw*{height}/{width})',"
               f"scale={width}:{height}",
        "-frames:v", "1", str(fitted),
    ], "fitting start image to canvas")
    job["message"] = "uploading start image"
    image_file = comfy_upload(host, fitted, fitted.name)
    seed = req.get("seed")
    graph = build_h3_i2v_graph(
        prompt=prompt, image_file=image_file, width=width, height=height,
        duration_sec=dur, seed=int(seed) if seed is not None else None,
        part_name=part_name)
    job["progress"] = 0.15
    run = queue_and_wait(host, graph, job, part_name)
    job["progress"] = 0.9
    fn, sub = first_video_output(run)
    dest = h3_dir() / f"{part_name}.mp4"
    comfy_download(host, fn, sub, dest)
    result = {"ok": True, "name": part_name, "video": str(dest),
              "video_url": file_url(dest), "width": width, "height": height,
              "frames": frames, "duration_sec": round(frames / H3_FPS, 3)}
    job["result"] = result
    job["message"] = f"h3 done: {dest.name}"
    return [result]


H3_REF_NODE = "MiniMaxH3ReferenceToVideo"
H3_REF_PREFIXES = ("ref_images.", "ref_videos.", "ref_video_audios.", "ref_audios.")


def _prune_unreachable(graph: dict):
    """Drop nodes that no longer feed any Save* node (orphaned ref loaders
    would still be validated by ComfyUI and fail on missing files)."""
    sinks = [k for k, n in graph.items() if "Save" in (n.get("class_type") or "")]
    if not sinks:
        return
    keep: set[str] = set()
    stack = list(sinks)
    while stack:
        nid = stack.pop()
        if nid in keep:
            continue
        keep.add(nid)
        for v in (graph[nid].get("inputs") or {}).values():
            if isinstance(v, list) and len(v) == 2 and str(v[0]) in graph:
                stack.append(str(v[0]))
    for k in list(graph):
        if k not in keep:
            del graph[k]


def build_h3_ref_graph(*, prompt: str, image_files: list[str], audio_files: list[str],
                       width: int, height: int, duration_sec: float,
                       seed: int | None, ref_image_size: str | None,
                       part_name: str) -> dict:
    """Rewire the captured ref2va template: strip whatever refs it was captured
    with, inject one loader per requested reference (prompt tags follow the
    injection order: <Picture N> / <Audio N>)."""
    graph = copy.deepcopy(load_template("h3ref"))
    nid = next((k for k, n in graph.items()
                if n.get("class_type") == H3_REF_NODE), None)
    if not nid:
        raise DEPS["ToolError"](
            "Captured 'h3ref' template has no MiniMaxH3ReferenceToVideo node — "
            "run the H3 ref workflow once on the h3 host, then Capture h3ref.")
    ins = graph[nid]["inputs"]
    for k in [k for k in ins if k.startswith(H3_REF_PREFIXES)]:
        ins.pop(k)
    _prune_unreachable(graph)
    ins["prompt"] = prompt
    ins["width"] = int(width)
    ins["height"] = int(height)
    ins["length"] = _h3_frames(duration_sec)
    if ref_image_size in ("match", "max"):
        ins["ref_image_size"] = ref_image_size
    for i, f in enumerate(image_files[:9]):
        gid = f"cc_ref_img{i}"
        graph[gid] = {"class_type": "LoadImage", "inputs": {"image": f}}
        ins[f"ref_images.ref_image_{i}"] = [gid, 0]
    for i, f in enumerate(audio_files[:3]):
        gid = f"cc_ref_aud{i}"
        graph[gid] = {"class_type": "LoadAudio", "inputs": {"audio": f}}
        ins[f"ref_audios.ref_audio_{i}"] = [gid, 0]
    randomize_seeds(graph)
    if seed is not None:
        for node in graph.values():
            if node.get("class_type") == "RandomNoise":
                node["inputs"]["noise_seed"] = int(seed)
    set_filename_prefix(graph, f"ninja/{part_name}")
    return graph


def job_h3_ref_generate(job, host: str, req: dict):
    """One H3 ref2va shot: upload image/audio refs (audio cropped to the
    requested range) -> queue -> download video."""
    prompt = str(req.get("prompt") or "").strip()
    dur = max(1.0, min(15.0, float(req.get("duration_sec") or 5.0)))
    width = _snap_h3_dim(int(req.get("width") or 768))
    height = _snap_h3_dim(int(req.get("height") or 1344))
    part_name = f"h3ref_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    image_files: list[str] = []
    for i, aid in enumerate(list(req.get("image_assets") or [])[:9]):
        a = Path(DEPS["asset_path"](aid))
        job["message"] = f"uploading ref image {i + 1}"
        image_files.append(comfy_upload(host, a, a.name))
    audio_files: list[str] = []
    for i, spec in enumerate(list(req.get("audio_assets") or [])[:3]):
        if isinstance(spec, str):
            spec = {"asset": spec}
        a = Path(DEPS["asset_path"](spec["asset"]))
        f0 = spec.get("from_sec")
        t0 = spec.get("to_sec")
        if f0 is not None and t0 is not None and float(t0) > float(f0):
            cut = h3_dir() / f"{part_name}_aud{i}.wav"
            ffmpeg(job, ["-i", str(a), "-ss", f"{float(f0):.3f}",
                         "-to", f"{float(t0):.3f}", "-ar", "44100", "-ac", "2",
                         str(cut)], f"cropping ref audio {i + 1}")
            a = cut
        job["message"] = f"uploading ref audio {i + 1}"
        audio_files.append(comfy_upload(host, a, a.name))
    seed = req.get("seed")
    graph = build_h3_ref_graph(
        prompt=prompt, image_files=image_files, audio_files=audio_files,
        width=width, height=height, duration_sec=dur,
        seed=int(seed) if seed is not None else None,
        ref_image_size=req.get("ref_image_size"), part_name=part_name)
    job["progress"] = 0.15
    run = queue_and_wait(host, graph, job, part_name)
    job["progress"] = 0.9
    fn, sub = first_video_output(run)
    dest = h3_dir() / f"{part_name}.mp4"
    comfy_download(host, fn, sub, dest)
    frames = _h3_frames(dur)
    result = {"ok": True, "name": part_name, "video": str(dest),
              "video_url": file_url(dest), "width": width, "height": height,
              "frames": frames, "duration_sec": round(frames / H3_FPS, 3),
              "refs": {"images": len(image_files), "audios": len(audio_files)}}
    job["result"] = result
    job["message"] = f"h3ref done: {dest.name}"
    return [result]


def job_h3_chunk(job, host: str, p: dict, req: dict):
    """One Director part generated by MiniMax H3 ref2va.

    Assistant work is automatic: song slice for the part window -> <Audio 1>,
    last frame of the previous part -> <Picture 1> (when start=last_frame),
    resolution locked to the project grid, duration = window length.
    Lands as a normal review chunk — Commit / Retake / Finish unchanged."""
    from_sec, to_sec = float(req["from_sec"]), float(req["to_sec"])
    song = p.get("song") or {}
    song_dur = float(song.get("duration") or 0.0)
    if song_dur > 0 and to_sec > song_dur:
        to_sec = round(song_dur, 3)
        req = dict(req)
        req["to_sec"] = to_sec
    win = to_sec - from_sec
    if win <= 0.05:
        raise DEPS["ToolError"](
            f"Empty window {from_sec:.1f}-{to_sec:.1f}s — already past song end?")
    if win > 15.2:
        raise DEPS["ToolError"](f"H3 max part length is 15s (asked {win:.1f}s)")
    dur = min(15.0, win)

    finals = [c for c in p["chunks"] if c.get("final")]
    if not p.get("work_resolution") and finals:
        finfo = DEPS["ffprobe"](Path(p["chunks"][0]["raw"]))
        fst = next((st for st in finfo.get("streams", [])
                    if st.get("codec_type") == "video"), {})
        p["work_resolution"] = {"w": int(fst.get("width", 0)),
                                "h": int(fst.get("height", 0))}
        save_project(p)
    if p.get("work_resolution"):
        width = _snap_h3_dim(int(p["work_resolution"]["w"]))
        height = _snap_h3_dim(int(p["work_resolution"]["h"]))
    else:
        # first part fixes the project canvas for every later part
        width = _snap_h3_dim(int(req.get("width") or 768))
        height = _snap_h3_dim(int(req.get("height") or 1344))
        p["work_resolution"] = {"w": width, "h": height}
        save_project(p)

    n = len(finals) + 1
    part_name = f"part{n}-{int(from_sec)}-{int(to_sec)}"
    work = session_work_dir(p)

    # <Picture 1>: last frame of the previous part, cropped to the canvas
    image_files: list[str] = []
    if req.get("start") == "last_frame":
        if not finals:
            raise DEPS["ToolError"](
                "No previous part yet — use start=none for the first part")
        prev = Path(finals[-1].get("final") or finals[-1].get("raw"))
        # lossless extract, NO crop/scale/re-fit — the last frame IS the
        # continuation anchor and must reach H3 byte-identical in content
        last_png = work / f"{part_name}_h3_last.png"
        ffmpeg(job, ["-sseof", "-0.05", "-i", str(prev), "-frames:v", "1",
                     "-update", "1", str(last_png)], "extracting last frame")
        image_files.append(comfy_upload(host, last_png, last_png.name))
    for i, aid in enumerate(list(req.get("image_assets") or [])[:8]):
        a = Path(DEPS["asset_path"](aid))
        job["message"] = f"uploading ref image {i + 1}"
        image_files.append(comfy_upload(host, a, a.name))

    # <Audio 1>: the song slice for exactly this window
    audio_files: list[str] = []
    if (req.get("audio") or "song") == "song":
        song_local = Path(song["path"])
        slice_path = work / f"{part_name}_h3_audio.wav"
        ffmpeg(job, ["-i", str(song_local), "-ss", f"{from_sec:.6f}",
                     "-to", f"{to_sec:.6f}", "-ar", "44100", "-ac", "2",
                     str(slice_path)], "slicing song for window")
        audio_files.append(comfy_upload(host, slice_path, slice_path.name))
    job["progress"] = 0.15

    seed = req.get("seed")
    graph = build_h3_ref_graph(
        prompt=str(req.get("prompt") or "").strip(),
        image_files=image_files, audio_files=audio_files,
        width=width, height=height, duration_sec=dur,
        seed=int(seed) if seed is not None else None,
        ref_image_size=req.get("ref_image_size"), part_name=part_name)
    run = queue_and_wait(host, graph, job, part_name)
    job["progress"] = 0.9
    fn, sub = first_video_output(run)
    raw = work / f"{part_name}_raw.mp4"
    comfy_download(host, fn, sub, raw)

    chunk = {"id": uuid.uuid4().hex[:8], "part": part_name,
             "from": from_sec, "to": to_sec,
             "tail_used": False, "tail_seconds": 0.0,
             "raw": str(raw), "preview": str(raw), "final": None,
             "status": "review", "request": {**req, "engine": "h3"},
             "host": "h3ref"}

    # cumulative PROGRESS preview — same behavior as the LTX flow: committed
    # parts + this candidate + the ORIGINAL song audio (take-only when the
    # window does not abut the covered end)
    job["message"] = "building cumulative preview"
    try:
        cscale = f"scale={width}:{height}:flags=lanczos"
        cvf = ["-vf", cscale, "-pix_fmt", "yuv420p"]
        cand = work / f"{part_name}_candidate.mp4"
        ffmpeg(job, ["-i", str(raw), "-t", f"{to_sec - from_sec:.6f}", *cvf,
                     "-c:v", "libx264", "-crf", "12", "-preset", "fast", "-an",
                     str(cand)], "trimming candidate")
        preview = work / f"{part_name}_preview.mp4"
        covered = max([float(c.get("to") or 0) for c in finals], default=0.0)
        abutting = abs(from_sec - covered) <= 0.35
        if finals and abutting:
            seq = [Path(c["final"]) for c in finals] + [cand]
            lst = work / f"{part_name}_concat.txt"
            lst.write_text("".join(f"file '{x}'\n" for x in seq), encoding="utf-8")
            joined = work / f"{part_name}_joined_v.mp4"
            DEPS["run"]([DEPS["FFMPEG"], "-y", "-loglevel", "error", "-f", "concat",
                         "-safe", "0", "-i", str(lst), "-an", "-vf", cscale,
                         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "14",
                         "-preset", "fast", str(joined)])
            first_from = min(float(c["from"]) for c in finals)
            DEPS["run"]([DEPS["FFMPEG"], "-y", "-loglevel", "error", "-i", str(joined),
                         "-ss", f"{first_from:.6f}", "-i", str(Path(p['song']['path'])),
                         "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                         "-c:a", "aac", "-b:a", "192k", "-shortest", str(preview)])
            chunk["preview_kind"] = "cumulative"
        else:
            DEPS["run"]([DEPS["FFMPEG"], "-y", "-loglevel", "error", "-i", str(cand),
                         "-ss", f"{from_sec:.6f}", "-i", str(Path(p['song']['path'])),
                         "-map", "0:v", "-map", "1:a",
                         "-t", f"{max(0.1, to_sec - from_sec):.6f}",
                         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                         "-shortest", str(preview)])
            chunk["preview_kind"] = "take_only"
        chunk["preview"] = str(preview)
    except Exception:
        chunk["preview"] = str(raw)  # fallback: at least the raw clip

    p["chunks"] = finals + [chunk]
    p.pop("pending_tail", None)
    p.pop("final_video", None)
    p.pop("finished_at", None)
    save_project(p)
    job["message"] = f"h3 part ready: {part_name}"
    return [str(raw)]


# ---------------------------------------------------------------- pipeline jobs

def job_generate(job, host: str, p: dict, req: dict):
    """One Director chunk: slice song -> upload inputs -> queue -> download result."""
    s = cfg()["settings"]
    fps = int(s["frame_rate"])
    from_sec, to_sec = float(req["from_sec"]), float(req["to_sec"])
    song_dur = float((p.get("song") or {}).get("duration") or 0.0)
    next_start = float(p.get("next_start") or 0.0)
    # Honor the caller's window — never silently rewrite from_sec → next_start.
    # clamp to song end — past-end work belongs to Finish, not another Extend
    if song_dur > 0 and to_sec > song_dur:
        to_sec = round(song_dur, 3)
        req = dict(req)
        req["to_sec"] = to_sec
    if to_sec <= from_sec + 0.05:
        raise DEPS["ToolError"](
            f"Already at/past song end ({song_dur:.1f}s, requested "
            f"{from_sec:.1f}–{to_sec:.1f}s). Use Finish to stitch the final MP4.")
    tail = p.get("pending_tail")            # set by 'continue' of previous chunk
    # source: "extend" = ALWAYS chain previous part's tail (unless Start-frame image).
    # source: "new" / image_asset = fresh scene — no tail.
    want_extend = (req.get("source") or "extend") != "new" and not req.get("image_asset")
    if req.get("image_asset"):
        if tail:
            job["message"] = "start frame set — ignoring extend tail"
        tail = None
    elif req.get("source") == "new":
        tail = None
    elif want_extend:
        # Honor user's window exactly (e.g. 38–58). ALWAYS keep/rebuild tail.
        # Never convert Extend into a no-tail "new scene".
        if not tail or not Path(str((tail or {}).get("path") or "")).is_file():
            tail = ensure_pending_tail(job, p)
            if not tail:
                raise DEPS["ToolError"](
                    "EXTEND needs a previous part tail — Commit a part first, then Extend."
                )
        if next_start > 0 and from_sec < next_start - 0.05:
            job["message"] = (
                f"EXTEND +tail {from_sec:.0f}–{to_sec:.0f}s "
                f"(window before covered {next_start:.0f}s — tail still used)"
            )
    # exact tail length measured from the actual tail file (LTX snaps to 8n+1
    # frames, so the nominal 5s tail is really e.g. 113 frames = 4.7083s)
    tail_seconds = float(tail["seconds"]) if tail else 0.0
    n = len([c for c in p["chunks"] if c.get("final")]) + 1
    part_name = f"part{n}-{int(from_sec)}-{int(to_sec)}"
    work = session_work_dir(p)

    # 1. song slice covering [from - tail, to] — sample-accurate (-ss after -i)
    song_local = Path(p["song"]["path"])
    slice_from = max(0.0, from_sec - tail_seconds)
    slice_path = work / f"{part_name}_audio.wav"
    ffmpeg(job, ["-i", str(song_local), "-ss", f"{slice_from:.6f}", "-to", f"{to_sec:.6f}",
                 "-ar", "44100", "-ac", "1", str(slice_path)], "slicing song")
    job["progress"] = 0.1
    song_file = comfy_upload(host, slice_path, slice_path.name)

    # 2. tail video / first image / optional end (FLF) image
    tail_file = image_file = end_image_file = None
    if tail:
        tail_file = comfy_upload(host, Path(tail["path"]), Path(tail["path"]).name)
    if not tail and req.get("image_asset"):
        a = DEPS["asset_path"](req["image_asset"])
        image_file = comfy_upload(host, a, a.name)
    if req.get("end_image_asset"):
        a = DEPS["asset_path"](req["end_image_asset"])
        end_image_file = comfy_upload(host, a, a.name)
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
        # LTX latents need multiples of 64 (e.g. 480 → 512). Older note said /32
        # (720 → 704); /64 is the real silent snap — do it up-front so the
        # project grid matches Comfy output.
        width = _snap_ltx_dim(width)
        height = _snap_ltx_dim(height)

    # IC-LoRA Ingredients: reference images/videos on Director motion track
    # (character sheet / singer clip). Images → short still videos (Guide needs videoFile).
    motion_files: list[str] = []
    # Override only if request/settings set; else use LoRA from Captured template
    ic_override = (req.get("ic_lora_name") or s.get("ic_lora_name") or "").strip()
    ic_strength = float(req.get("ic_lora_strength", s.get("ic_lora_strength", 1.0)))
    tpl_lora, _ = template_ic_lora("director")
    ic_name = ic_override if ic_override and ic_override != "None" else tpl_lora
    ic_imgs = list(req.get("ic_image_assets") or [])
    ic_vids = list(req.get("ic_video_assets") or [])
    if req.get("ic_video_asset"):
        ic_vids.append(req["ic_video_asset"])
    win_sec = max(0.5, float(to_sec - from_sec + (tail_seconds if tail else 0.0)))
    if ic_imgs or ic_vids:
        if not ic_name or ic_name == "None":
            raise DEPS["ToolError"](
                "IC-LoRA refs provided, but the Captured Director template has "
                "ic_lora_name=None. In ComfyUI: select Ingredients IC-LoRA on "
                "LTX Director Guide → run once → CupCut Hosts & templates → Capture Director.")
        job["message"] = "preparing IC-LoRA ingredients"
        for i, aid in enumerate(ic_imgs):
            src = Path(DEPS["asset_path"](aid))
            still = work / f"{part_name}_ic_img{i}.mp4"
            ffmpeg(job, [
                "-loop", "1", "-i", str(src),
                "-t", f"{win_sec:.3f}", "-r", str(fps),
                "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                       f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
                "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "14", "-an",
                str(still),
            ], f"IC still → video ({src.name})")
            motion_files.append(comfy_upload(host, still, still.name))
        for i, aid in enumerate(ic_vids):
            src = Path(DEPS["asset_path"](aid))
            motion_files.append(comfy_upload(host, src, src.name))

    # FLF / retake-tab principles: ~1s start still zone + ~1s end still zone
    # inside the requested duration (img1 | txt | img2).
    img_zone = fps if image_file else None
    end_zone = fps if end_image_file else 6
    graph = build_director_graph(
        host, prompts=req["prompts"], from_sec=from_sec, to_sec=to_sec, fps=fps,
        width=width, height=height, tail_file=tail_file,
        tail_seconds=tail_seconds, image_file=image_file,
        global_prompt=req.get("global_prompt", ""), part_name=part_name,
        song_file=song_file, end_image_file=end_image_file,
        end_zone_frames=end_zone, image_zone_frames=img_zone,
        motion_files=motion_files,
        ic_lora_name=ic_name or None,
        ic_lora_strength=ic_strength)

    run = queue_and_wait(host, graph, job, part_name)
    job["progress"] = 0.9
    fn, sub = first_video_output(run)
    raw = work / f"{part_name}_raw.mp4"
    comfy_download(host, fn, sub, raw)

    # brightness calibration vs the previous part (same fix as the retake):
    # the raw's opening frames REPRODUCE the pinned tail video — known content —
    # so the VAE luma shift is measurable and baked into every cut of this part
    luma_offset = 0.0
    if tail:
        o = _yavg(Path(tail["path"]), 0, 5)
        g = _yavg(raw, 0, 5)
        if o and g and len(o) == len(g):
            luma_offset = max(-8.0, min(8.0, sum(a - b for a, b in zip(o, g)) / len(o)))

    chunk = {"id": uuid.uuid4().hex[:8], "part": part_name, "from": from_sec, "to": to_sec,
             "tail_used": bool(tail), "tail_seconds": tail_seconds,
             "luma_offset": round(luma_offset, 2),
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
        cscale = f"scale={wr['w']}:{wr['h']}:flags=lanczos"
        if abs(luma_offset) >= 0.3:
            cscale += f",lutyuv=y='clip(val{luma_offset:+.2f},0,255)'"
        cvf = ["-vf", cscale, "-pix_fmt", "yuv420p"]
        cand = work / f"{part_name}_candidate.mp4"
        if chunk["tail_used"] and tail_seconds > 0:
            ffmpeg(job, ["-ss", f"{tail_seconds:.6f}", "-i", str(raw),
                         "-t", f"{to_sec - from_sec:.6f}", *cvf, "-c:v", "libx264", "-crf", "12",
                         "-preset", "fast", "-an", str(cand)], "trimming candidate")
        else:
            ffmpeg(job, ["-i", str(raw), "-t", f"{to_sec - from_sec:.6f}", *cvf,
                         "-c:v", "libx264", "-crf", "12", "-preset", "fast", "-an",
                         str(cand)], "trimming candidate")
        preview = work / f"{part_name}_preview.mp4"
        covered = 0.0
        finals = [c for c in p["chunks"] if c.get("final")]
        if finals:
            covered = max(float(c.get("to") or 0) for c in finals)
        # Abutting extend (from ≈ covered): cumulative session preview.
        # Overlap / gap / earlier window (e.g. 23–45 after 0–40): show ONLY the
        # new take — concat would look like "61s stuck" (40+21) and confuse review.
        abutting = abs(from_sec - covered) <= 0.35
        if finals and abutting:
            seq = [Path(c["final"]) for c in finals] + [cand]
            lst = work / f"{part_name}_concat.txt"
            lst.write_text("".join(f"file '{x}'\n" for x in seq), encoding="utf-8")
            joined = work / f"{part_name}_joined_v.mp4"
            DEPS["run"]([DEPS["FFMPEG"], "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                         "-i", str(lst), "-an", "-vf", f"scale={wr['w']}:{wr['h']}:flags=lanczos",
                         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "14", "-preset", "fast",
                         str(joined)])
            first_from = min(float(c["from"]) for c in finals)
            DEPS["run"]([DEPS["FFMPEG"], "-y", "-loglevel", "error", "-i", str(joined),
                         "-ss", f"{first_from:.6f}", "-i", str(Path(p['song']['path'])),
                         "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                         "-shortest", str(preview)])
            chunk["preview_kind"] = "cumulative"
        else:
            # New take only + song audio for this window
            DEPS["run"]([DEPS["FFMPEG"], "-y", "-loglevel", "error",
                         "-i", str(cand),
                         "-ss", f"{from_sec:.6f}", "-i", str(Path(p['song']['path'])),
                         "-map", "0:v", "-map", "1:a",
                         "-t", f"{max(0.1, to_sec - from_sec):.6f}",
                         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                         "-shortest", str(preview)])
            chunk["preview_kind"] = "take_only"
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
    remember_executed_prompts(p, req)
    save_project(p)
    outs = [DEPS["output_entry_abs"](raw, "video", f"{part_name} (raw)")]
    if chunk.get("preview"):
        outs.insert(0, DEPS["output_entry_abs"](Path(chunk["preview"]), "video",
                                                f"CUMULATIVE preview + original audio"))
    return outs


def ensure_pending_tail(job, p: dict) -> dict | None:
    """Rebuild pending_tail from the last finalized part if missing (import/commit gaps)."""
    existing = p.get("pending_tail")
    if existing and Path(str(existing.get("path") or "")).is_file():
        return existing
    finals = [c for c in (p.get("chunks") or []) if c.get("final") and Path(str(c["final"])).is_file()]
    if not finals:
        return None
    chunk = finals[-1]
    s = cfg()["settings"]
    fps = int(s["frame_rate"])
    want = int(float(s["tail_seconds"]) * fps)
    tail_frames_cut = max(9, ((want - 1) // 8) * 8 + 1)
    final = Path(chunk["final"])
    dur = media_duration(final)
    if dur <= 0.2:
        return None
    work = session_work_dir(p)
    wr = p.get("work_resolution") or {}
    if not wr.get("w") or not wr.get("h"):
        info0 = DEPS["ffprobe"](final)
        fst = next((st for st in info0.get("streams", []) if st.get("codec_type") == "video"), {})
        wr = {"w": int(fst.get("width") or 704), "h": int(fst.get("height") or 1280)}
        p["work_resolution"] = wr
    tail_start = max(0.0, dur - tail_frames_cut / fps)
    tail_up = work / f"{chunk['part']}_tail_rebuilt.mp4"
    ffmpeg(job, ["-ss", f"{tail_start:.6f}", "-i", str(final),
                 "-frames:v", str(tail_frames_cut),
                 "-vf", f"scale={wr['w']}:{wr['h']}:flags=lanczos",
                 "-c:v", "libx264", "-crf", "12", "-preset", "fast", "-c:a", "aac",
                 str(tail_up)], "rebuilding missing extend tail")
    info = DEPS["ffprobe"](tail_up)
    vstream = next((st for st in info.get("streams", []) if st.get("codec_type") == "video"), {})
    tail_frames = int(vstream.get("nb_frames") or 0) or int(round(float(info["format"]["duration"]) * fps))
    tail = {"path": str(tail_up), "from_chunk": chunk["part"],
            "frames": tail_frames, "seconds": tail_frames / fps}
    p["pending_tail"] = tail
    p["next_start"] = float(chunk.get("to") or p.get("next_start") or 0.0)
    save_project(p)
    job["message"] = f"rebuilt extend tail from {chunk['part']}"
    return tail


def job_continue(job, host: str, p: dict, accept_sec: float | None = None,
                 upscale_tail: bool = False):
    """Approve last chunk: trim guidance overlap -> save final part -> build next tail.
    accept_sec: optionally accept only the FIRST N seconds of the generated chunk.
    upscale_tail: refresh the tail through the upscale workflow before pinning."""
    s = cfg()["settings"]
    chunk = p["chunks"][-1]
    raw = Path(chunk["raw"])
    parts_dir = session_parts_dir(p)
    work = session_work_dir(p)

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
    # apply the luma offset measured at generation time (vs the previous part's
    # tail) to EVERY cut of this raw: the final part and the next tail
    off = float(chunk.get("luma_offset") or 0.0)
    vf_expr = f"scale={wr['w']}:{wr['h']}:flags=lanczos"
    if abs(off) >= 0.3:
        vf_expr += f",lutyuv=y='clip(val{off:+.2f},0,255)'"
    vf = ["-vf", vf_expr]
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
    tail_vf = ["-vf", f"lutyuv=y='clip(val{off:+.2f},0,255)'"] if abs(off) >= 0.3 else []
    ffmpeg(job, ["-ss", f"{tail_start:.6f}", "-i", str(raw),
                 "-frames:v", str(tail_frames_cut), *tail_vf,
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
        # tail must re-enter the Director at exactly the working size.
        # The upscaler is its own LTX pass with its own luma shift — measure it
        # against tail_cut (known, already-corrected content) and correct here,
        # so the tail pinned into the next part carries TRUE brightness
        up_off = 0.0
        o = _yavg(tail_cut, 0, 5)
        g = _yavg(tail_up_big, 0, 5)
        if o and g and len(o) == len(g):
            up_off = max(-8.0, min(8.0, sum(a - b for a, b in zip(o, g)) / len(o)))
        wr = p["work_resolution"]
        dvf = f"scale={wr['w']}:{wr['h']}:flags=lanczos"
        if abs(up_off) >= 0.3:
            dvf += f",lutyuv=y='clip(val{up_off:+.2f},0,255)'"
        tail_up = work / f"{chunk['part']}_tail_up.mp4"
        ffmpeg(job, ["-i", str(tail_up_big), "-vf", dvf,
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
    # Committing parts invalidates any previous Finish export (often another song).
    p.pop("final_video", None)
    p.pop("finished_at", None)
    save_project(p)
    # Per-song export: D:\parts\{song}\partN-from-to.mp4 (won't clobber other songs)
    export_part_copy(final, p)
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

    Mode 1: still at From + text + end still at To (same as FLF-style parts).
    Mode 2: SAME AS CONTINUE — tail before From + text + end still at To.
      Example window 8–13: tail 3–8, generate 8–13, end at 13.
      Splice into clip: keep original frame at From and at To; replace only
      interior (From+1frame .. To−1frame) so the 13.00 seam does not glitch."""
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
    # Mode 2 lead length = same 8n+1 snap as Continue tails (settings.tail_seconds)
    if mode == 2:
        want = int(round(float(cfg()["settings"].get("tail_seconds", 5.0)) * fps))
        lead = max(9, ((want - 1) // 8) * 8 + 1)
        if S < lead:
            raise DEPS["ToolError"](
                f"Mode 2 needs ≥{lead / fps:.2f}s of video before From "
                f"(same as Continue tail). From={start_sec:.2f}s is too early.")
    else:
        lead = 0
    # Patch starts AFTER the full lead — same as Continue `-ss tail_seconds`
    head = lead
    img_frames = fps if mode == 1 else 0
    pin2 = head + win                     # end-image pin at window end
    total = ((pin2 + fps) // 8) * 8 + 1
    end_frames = total - pin2
    tail_seconds = lead / fps if lead else 0.0

    work = NINJA_DIR / "retake" / "work"
    work.mkdir(parents=True, exist_ok=True)
    tag = f"{S}_{E}_{uuid.uuid4().hex[:6]}"

    # 1. end still at To + (mode1 start still | mode2 Continue-style tail before From)
    # Optional user frames (assets) replace extracted stills.
    import shutil
    end_png = work / f"{tag}_end.png"
    if req.get("end_image_asset"):
        src = Path(DEPS["asset_path"](req["end_image_asset"]))
        if not src.is_file():
            raise DEPS["ToolError"](f"End frame asset missing: {req['end_image_asset']}")
        shutil.copy2(src, end_png)
    else:
        ffmpeg(job, ["-i", str(clip), "-vf", f"select='eq(n\\,{E})'", "-vsync", "0",
                     "-frames:v", "1", str(end_png)], "extracting end anchor")
    _guard_black_anchor(end_png, "End ('To')", end_sec)
    start_png = lead_mp4 = None
    if mode == 1:
        start_png = work / f"{tag}_start.png"
        if req.get("start_image_asset"):
            src = Path(DEPS["asset_path"](req["start_image_asset"]))
            if not src.is_file():
                raise DEPS["ToolError"](f"Start frame asset missing: {req['start_image_asset']}")
            shutil.copy2(src, start_png)
        else:
            ffmpeg(job, ["-i", str(clip), "-vf", f"select='eq(n\\,{S})'", "-vsync", "0",
                         "-frames:v", "1", str(start_png)], "extracting start anchor")
        _guard_black_anchor(start_png, "Start ('From')", start_sec)
    else:
        # Tail = last `lead` frames BEFORE window start S (e.g. 3–8 for window 8–13)
        lead_mp4 = work / f"{tag}_lead.mp4"
        ffmpeg(job, ["-i", str(clip),
                     "-vf", f"trim=start_frame={S - lead}:end_frame={S},setpts=PTS-STARTPTS",
                     "-an", "-r", str(fps), "-fps_mode", "cfr",
                     "-c:v", "libx264", "-crf", "12", "-preset", "fast",
                     str(lead_mp4)], "extracting Continue-style tail")
        lead_n, lead_fps = _count_frames(lead_mp4, fps)
        if lead_n != lead or abs(lead_fps - fps) > 0.01:
            raise DEPS["ToolError"](
                f"Mode-2 tail broken: got {lead_n} frames @ {lead_fps:.3f}fps, "
                f"expected {lead} @ {fps}.")
    # Audio = Continue: [From − tail, To]
    win_wav = work / f"{tag}_audio_win.wav"
    ffmpeg(job, ["-i", str(clip), "-ss", f"{(S - head) / fps:.6f}",
                 "-to", f"{E / fps:.6f}",
                 "-vn", "-ar", "44100", "-ac", "1", str(win_wav)], "slicing window audio")
    job["progress"] = 0.15

    # 2. upload + generate — same builder as Director parts
    end_up = comfy_upload(host, end_png, end_png.name)
    start_up = comfy_upload(host, start_png, start_png.name) if start_png else None
    lead_up = comfy_upload(host, lead_mp4, lead_mp4.name) if lead_mp4 else None
    win_wav_up = comfy_upload(host, win_wav, win_wav.name)
    tracks = [(win_wav_up, 0, head + win)]
    graph = build_director_graph(
        host, prompts=req["prompts"],
        from_sec=start_sec, to_sec=end_sec + end_frames / fps, fps=fps,
        width=int(sess["clip"]["width"]), height=int(sess["clip"]["height"]),
        tail_file=lead_up, tail_seconds=tail_seconds,
        image_file=start_up, global_prompt=req.get("global_prompt", ""),
        part_name=f"retake_{tag}", song_file=win_wav_up, end_image_file=end_up,
        audio_tracks=tracks, end_zone_frames=end_frames,
        image_zone_frames=img_frames or None, total_frames=total)
    run = queue_and_wait(host, graph, job, f"retake {start_sec}-{end_sec}")
    job["progress"] = 0.75
    fn, sub = first_video_output(run)
    raw = work / f"{tag}_raw.mp4"
    comfy_download(host, fn, sub, raw)

    vinfo = DEPS["ffprobe"](raw)
    vstream = next((st for st in vinfo.get("streams", []) if st.get("codec_type") == "video"), {})
    raw_frames = int(vstream.get("nb_frames") or 0) or \
        int(round(float(vinfo.get("format", {}).get("duration") or 0) * fps))
    if raw_frames != total:
        raise DEPS["ToolError"](
            f"Director returned {raw_frames} frames, expected {total} — the patch "
            f"would be shifted. Aborting instead of splicing misaligned frames.")

    # 3. Interior patch only — keep original seam frames at From (S) and To (E).
    # Generated span after lead is [S .. E); we take (S+1 .. E-1) so the cut at
    # 13.00 / 18.00 stays the original frame (no 1-frame overlap/glitch).
    if win < 3:
        raise DEPS["ToolError"]("Window too short to keep From/To seam frames")
    patch_start = head + 1          # skip boundary frame at From
    patch_end = head + win          # exclusive; last kept = E-1; E stays original
    samples = []
    o = _yavg(clip, E, E + 1)
    g = _yavg(raw, pin2, pin2 + 1)
    if o and g:
        samples.append(o[0] - g[0])
    if mode == 1:
        o = _yavg(clip, S, S + 1)
        g = _yavg(raw, 0, 1)
        if o and g:
            samples.append(o[0] - g[0])
    else:
        o = _yavg(clip, S - lead, S - lead + 5)
        g = _yavg(raw, 0, 5)
        if o and g and len(o) == len(g):
            samples.append(sum(a - b for a, b in zip(o, g)) / len(o))
    offset = max(-8.0, min(8.0, sum(samples) / len(samples))) if samples else 0.0
    vf = (f"trim=start_frame={patch_start}:end_frame={patch_end},setpts=PTS-STARTPTS,"
          f"scale={sess['clip']['width']}:{sess['clip']['height']}:flags=lanczos")
    if abs(offset) >= 0.3:
        vf += f",lutyuv=y='clip(val{offset:+.2f},0,255)'"
    patch = work / f"{tag}_patch.mp4"
    ffmpeg(job, ["-i", str(raw), "-vf", vf,
                 "-pix_fmt", "yuv420p", "-an", "-r", str(fps),
                 "-c:v", "libx264", "-crf", "12", "-preset", "fast",
                 str(patch)], "trimming interior patch (keep From/To seams)")

    # 4. splice: original[0..S] + interior patch + original[E..)
    #    end_frame=S+1 keeps the exact From frame; start_frame=E keeps To.
    preview = work / f"{tag}_preview.mp4"
    ffmpeg(job, ["-i", str(clip), "-i", str(patch), "-filter_complex",
                 f"[0:v]trim=end_frame={S + 1},setpts=PTS-STARTPTS[a];"
                 f"[1:v]setpts=PTS-STARTPTS[b];"
                 f"[0:v]trim=start_frame={E},setpts=PTS-STARTPTS[c];"
                 f"[a][b][c]concat=n=3:v=1:a=0[v]",
                 "-map", "[v]", "-map", "0:a", "-r", str(fps), "-pix_fmt", "yuv420p",
                 "-c:v", "libx264", "-crf", "12", "-preset", "fast", "-c:a", "copy",
                 str(preview)], "splicing preview (seam-safe)")
    job["progress"] = 0.98

    sess["pending"] = {"preview": str(preview), "patch": str(patch), "raw": str(raw),
                       "start_sec": start_sec, "end_sec": end_sec, "mode": mode,
                       "request": req, "tag": tag}
    save_retake_session(sess)
    return [DEPS["output_entry_abs"](preview, "video",
                                     f"retake {start_sec:.1f}-{end_sec:.1f} spliced preview")]


def _count_frames(path: Path, fps_hint: float | None = None) -> tuple[int, float]:
    """Return (frame_count, fps) from ffprobe — prefer nb_frames over duration*fps."""
    info = DEPS["ffprobe"](path)
    vs = next((st for st in info.get("streams", []) if st.get("codec_type") == "video"), {})
    fr = (vs.get("avg_frame_rate") or vs.get("r_frame_rate") or "24/1").split("/")
    fps = float(fr[0]) / float(fr[1] if len(fr) > 1 and fr[1] else 1)
    if fps <= 0 and fps_hint:
        fps = fps_hint
    n = int(vs.get("nb_frames") or 0)
    if n <= 0:
        n = int(round(float(info.get("format", {}).get("duration") or 0) * fps))
    return n, fps


def _ltx_nearest(n: int) -> int:
    """Nearest LTX-legal length (8k+1), minimum 9."""
    n = max(1, int(n))
    if n <= 9:
        return 9
    down = ((n - 1) // 8) * 8 + 1
    up = down + 8
    return up if (up - n) < (n - down) else down


def _ltx_ceil(n: int) -> int:
    """Smallest LTX-legal length (8k+1) >= n, minimum 9."""
    n = max(1, int(n))
    if n <= 9:
        return 9
    return ((n - 1 + 7) // 8) * 8 + 1


def _grok_xai_api_key() -> str:
    """Same key grokmcp uses inside docker container vigilant_gould."""
    key = (os.environ.get("XAI_API_KEY") or "").strip()
    if key:
        return key
    try:
        out = subprocess.check_output(
            ["docker", "exec", GROK_MCP_CONTAINER, "printenv", "XAI_API_KEY"],
            text=True, timeout=15, stderr=subprocess.DEVNULL)
        key = (out or "").strip()
    except Exception as e:
        raise DEPS["ToolError"](
            f"No XAI_API_KEY and cannot read it from docker {GROK_MCP_CONTAINER}: {e}"
        ) from e
    if not key:
        raise DEPS["ToolError"](
            f"XAI_API_KEY empty in docker container {GROK_MCP_CONTAINER}")
    return key


def _download_bytes(url: str, *, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def refine_anchor_image(src_png: Path, out_png: Path, job: dict, *,
                        image_type: str = "upscale",
                        prompt: str | None = None) -> Path:
    """Grok HQ image edit — same call as grokmcp tool `edit_image` (vigilant_gould)."""
    prompt = (prompt or DEFAULT_GROK_UPSCALE_PROMPT).strip()
    key = _grok_xai_api_key()
    job["message"] = f"Grok MCP edit_image ({image_type}): {src_png.name}"
    b64 = base64.b64encode(src_png.read_bytes()).decode("ascii")
    payload = {
        "model": "grok-imagine-image-quality",
        "prompt": prompt,
        "image": {"url": f"data:image/png;base64,{b64}"},
        "resolution": "2k",
        "response_format": "b64_json",
    }
    req = urllib.request.Request(
        "https://api.x.ai/v1/images/edits",
        data=json.dumps(payload).encode(),
        headers={**UA, "Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:1500]
        raise DEPS["ToolError"](f"Grok edit_image HTTP {e.code}: {body}") from e
    except Exception as e:
        raise DEPS["ToolError"](f"Grok edit_image failed: {e}") from e
    d0 = (data.get("data") or [{}])[0]
    out_b64 = d0.get("b64_json")
    if out_b64:
        out_png.write_bytes(base64.b64decode(out_b64))
        return out_png
    if d0.get("url"):
        out_png.write_bytes(_download_bytes(d0["url"]))
        return out_png
    raise DEPS["ToolError"](f"Grok edit_image returned no image: {list(data.keys())}")


def job_standalone_upscale(job, host: str, req: dict):
    """Upscaler tab — manual IC-upscale recipe, frame-exact:

      Chunk length: user seconds → frames, snapped to LTX 8k+1
        e.g. 10s @24fps = 240 → 241 frames (~10.04s)

      part1:     [0, step)           skip=0  → feeds `step` frames (8k+1)
      middle:    [prev−1, next)      skip=1  → feeds `step` frames (8k+1)
                 overlap trimmed before LoadVideo (or VHS skip_first_frames)
      last part: [prev−1, n_src)     skip=1  → to end; input padded to 8k+1
                 for Comfy, output trimmed back to exact leftover frames
      join:      total == n_src + original audio
    """
    src = Path(DEPS["asset_path"](req["asset"]))
    chunk_len = float(req.get("chunk_seconds", 15))
    denoise = req.get("denoise")
    mult = req.get("multiplier")
    shorter_size = req.get("shorter_size")
    if shorter_size is None and mult is not None:
        try:
            if float(mult) >= 64:
                shorter_size = int(float(mult))
        except (TypeError, ValueError):
            pass
    grok_anchor = bool(req.get("grok_anchor", False))
    grok_prompt = req.get("grok_prompt") or DEFAULT_GROK_UPSCALE_PROMPT
    q_lo = float(req.get("_q_lo", 0.0))
    q_hi = float(req.get("_q_hi", 1.0))
    q_label = req.get("_q_label") or ""
    pixel_up = upscale_template_is_pixel()

    def _prog(local: float, msg: str):
        job["message"] = f"{q_label} {msg}".strip() if q_label else msg
        job["progress"] = q_lo + max(0.0, min(1.0, local)) * (q_hi - q_lo)

    work = NINJA_DIR / "upscaler" / uuid.uuid4().hex[:8]
    work.mkdir(parents=True, exist_ok=True)

    info = DEPS["ffprobe"](src)
    n_src, fps = _count_frames(src)
    if n_src <= 0 or fps <= 0:
        raise DEPS["ToolError"]("Could not read source frame count / fps")
    has_audio = any(st.get("codec_type") == "audio" for st in info.get("streams", []))
    # 10s @24fps → 240 → nearest LTX legal 241 (8*30+1)
    raw_step = max(9, int(round(chunk_len * fps)))
    step = _ltx_nearest(raw_step)
    _prog(0.0, f"chunk {chunk_len:g}s → {raw_step}f → LTX {step}f ({step / fps:.3f}s)"
                f"{' [pixel IC]' if pixel_up else ''}")
    # nominal chunk ends on the LTX grid; last cut always n_src
    nominal = list(range(step, n_src, step))
    cuts = nominal + [n_src]

    # Legacy VHS template used a still image as IC anchor. Pixel Spatial guides
    # from the input video frames themselves — skip anchor extract/upload.
    anchor_remote = None
    if not pixel_up:
        if req.get("anchor_asset"):
            a0 = Path(DEPS["asset_path"](req["anchor_asset"]))
            if grok_anchor:
                refined = work / "anchor_000_grok.png"
                a0 = refine_anchor_image(a0, refined, job, prompt=grok_prompt)
            anchor_remote = comfy_upload(host, a0, a0.name)
        else:
            a0 = work / "anchor_000.png"
            ffmpeg(job, ["-i", str(src), "-vf", "select='eq(n\\,0)'", "-vsync", "0",
                         "-frames:v", "1", str(a0)], "extracting first-frame anchor")
            if grok_anchor:
                refined = work / "anchor_000_grok.png"
                a0 = refine_anchor_image(a0, refined, job, prompt=grok_prompt)
            anchor_remote = comfy_upload(host, a0, a0.name)

    pieces = []
    prev_end = 0
    n_parts = len(cuts)
    for i, nominal_b in enumerate(cuts):
        is_last = i == n_parts - 1
        if i == 0:
            s, b, skip = 0, (n_src if is_last else nominal_b), 0
        elif is_last:
            s, b, skip = prev_end - 1, n_src, 1
        else:
            # start one frame early (already upscaled); end on LTX grid boundary
            # so after skip_first_frames=1 we still feed exactly `step` (8k+1)
            s, b, skip = prev_end - 1, nominal_b, 1
        cut_frames = b - s
        exp_out = cut_frames - skip
        if cut_frames <= skip or exp_out <= 0:
            raise DEPS["ToolError"](
                f"part {i + 1}: empty cut [{s},{b}) skip={skip} (n_src={n_src})")
        _prog(i / max(1, n_parts),
              f"part {i + 1}/{n_parts} cut [{s},{b}) skip={skip} "
              f"expect_out={exp_out}{' LAST→end' if is_last else ''}")
        c = work / f"c{i:03d}.mp4"
        ffmpeg(job, ["-i", str(src),
                     "-vf", f"trim=start_frame={s}:end_frame={b},setpts=PTS-STARTPTS,fps={fps}",
                     "-an", "-c:v", "libx264", "-crf", "12", "-preset", "fast",
                     str(c)], f"cutting part {i + 1}")
        cut_n, _ = _count_frames(c, fps)
        if cut_n != cut_frames:
            raise DEPS["ToolError"](
                f"part {i + 1}: cut produced {cut_n} frames, expected {cut_frames}")

        # Frames fed to Comfy must be 8k+1 or LTX will snap (e.g. 240→233).
        load_n = cut_frames - skip
        comfy_n = _ltx_ceil(load_n)
        upload = c
        if comfy_n > load_n:
            file_need = skip + comfy_n
            pad = file_need - cut_frames
            c_ltx = work / f"c{i:03d}_ltx.mp4"
            _prog((i + 0.15) / max(1, n_parts),
                  f"part {i + 1}: pad cut {cut_frames}→{file_need} "
                  f"(load {load_n}→{comfy_n} LTX)")
            ffmpeg(job, ["-i", str(c),
                         "-vf", (f"tpad=stop_mode=clone:stop={pad},"
                                 f"fps={fps}"),
                         "-an", "-c:v", "libx264", "-crf", "12", "-preset", "fast",
                         str(c_ltx)], f"LTX-pad part {i + 1}")
            upload = c_ltx

        # LoadVideo has no skip_first_frames — drop overlap before upload.
        graph_skip = skip
        if pixel_up and skip > 0:
            c_feed = work / f"c{i:03d}_feed.mp4"
            _prog((i + 0.2) / max(1, n_parts),
                  f"part {i + 1}: trim skip={skip} for LoadVideo")
            ffmpeg(job, ["-i", str(upload),
                         "-vf", (f"trim=start_frame={skip},"
                                 f"setpts=PTS-STARTPTS,fps={fps}"),
                         "-an", "-c:v", "libx264", "-crf", "12", "-preset", "fast",
                         str(c_feed)], f"trim skip part {i + 1}")
            upload = c_feed
            graph_skip = 0

        remote_v = comfy_upload(host, upload, upload.name)
        graph = build_upscale_graph(
            host, video_file=remote_v, anchor_file=anchor_remote,
            denoise=denoise, multiplier=mult,
            part_name=f"upsc_{src.stem}_{i:03d}",
            skip_first_frames=graph_skip,
            shorter_size=int(shorter_size) if shorter_size is not None else None,
        )
        run = queue_and_wait(host, graph, job, f"part {i + 1}/{n_parts}")
        fn, sub = first_video_output(run)
        lp = work / f"up_{i:03d}_raw.mp4"
        comfy_download(host, fn, sub, lp)

        # Keep exactly exp_out for the source timeline (drop any LTX pad frames)
        got, _ = _count_frames(lp, fps)
        piece = work / f"j_{i:03d}.mp4"
        if got < exp_out:
            pad = exp_out - got
            _prog((i + 0.5) / max(1, n_parts),
                  f"part {i + 1}: Comfy gave {got}/{exp_out} — padding {pad}")
            ffmpeg(job, ["-i", str(lp),
                         "-vf", (f"tpad=stop_mode=clone:stop={pad},"
                                 f"trim=start_frame=0:end_frame={exp_out},"
                                 f"setpts=PTS-STARTPTS,fps={fps}"),
                         "-pix_fmt", "yuv420p",
                         "-an", "-c:v", "libx264", "-crf", "12", "-preset", "fast",
                         str(piece)], f"padding part {i + 1} {got}→{exp_out}")
        else:
            if got != exp_out:
                _prog((i + 0.5) / max(1, n_parts),
                      f"part {i + 1}: Comfy gave {got}, trim to {exp_out}")
            ffmpeg(job, ["-i", str(lp),
                         "-vf", f"trim=start_frame=0:end_frame={exp_out},setpts=PTS-STARTPTS,fps={fps}",
                         "-pix_fmt", "yuv420p",
                         "-an", "-c:v", "libx264", "-crf", "12", "-preset", "fast",
                         str(piece)], f"normalizing part {i + 1} to {exp_out} frames")
        got2, _ = _count_frames(piece, fps)
        if got2 != exp_out:
            raise DEPS["ToolError"](
                f"part {i + 1}: after normalize got {got2} frames, expected {exp_out}")

        pieces.append(piece)
        prev_end = b

        # next anchor = last frame of THIS upscaled part (legacy VHS template only)
        if not is_last and not pixel_up:
            anchor_png = work / f"anchor_{i + 1:03d}.png"
            ffmpeg(job, ["-i", str(piece),
                         "-vf", f"select='eq(n\\,{exp_out - 1})'", "-vsync", "0",
                         "-frames:v", "1", str(anchor_png)], "extracting next anchor")
            if grok_anchor:
                refined = work / f"anchor_{i + 1:03d}_grok.png"
                anchor_png = refine_anchor_image(
                    anchor_png, refined, job, prompt=grok_prompt)
            anchor_remote = comfy_upload(host, anchor_png, anchor_png.name)

    # concat — keep every frame from every part (shift1 already handled in loader)
    lst = work / "concat.txt"
    lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in pieces),
                   encoding="utf-8")
    joined = work / "joined.mp4"
    ffmpeg(job, ["-f", "concat", "-safe", "0", "-i", str(lst),
                 "-vf", f"fps={fps}", "-pix_fmt", "yuv420p",
                 "-c:v", "libx264", "-crf", "12", "-preset", "fast", "-an",
                 str(joined)], "joining parts")
    joined_n, _ = _count_frames(joined, fps)
    if joined_n != n_src:
        raise DEPS["ToolError"](
            f"join produced {joined_n} frames, source has {n_src} — "
            f"refusing to deliver a wrong-length timeline")

    out = DEPS["OUT_DIR"] / f"{src.stem}_upscaled_{int(time.time())}.mp4"
    exact_dur = n_src / fps
    _prog(0.95, "muxing original audio")
    if has_audio:
        ffmpeg(job, ["-i", str(joined), "-i", str(src),
                     "-map", "0:v:0", "-map", "1:a:0",
                     "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                     "-t", f"{exact_dur:.6f}", str(out)], "muxing original audio")
    else:
        ffmpeg(job, ["-i", str(joined), "-c", "copy", str(out)], "saving (no source audio)")

    return [DEPS["output_entry"](out, "video", f"{src.stem} upscaled")]


def job_upscale_queue(job, host: str, req: dict):
    """Run several standalone upscales back-to-back (walk-away queue).

    req = {
      items: [{ asset, name?, anchor_asset? }, ...],
      chunk_seconds, shorter_size, denoise, multiplier, grok_anchor, grok_prompt?
    }
    On item failure: record error and continue with the next video (unless
    stop_on_error=true).
    """
    items = list(req.get("items") or [])
    if not items:
        raise DEPS["ToolError"]("Upscale queue is empty")
    stop_on_error = bool(req.get("stop_on_error", False))
    shared = {
        "chunk_seconds": req.get("chunk_seconds", 15),
        "denoise": req.get("denoise"),
        "multiplier": req.get("multiplier"),
        "shorter_size": req.get("shorter_size"),
        "grok_anchor": bool(req.get("grok_anchor", False)),
    }
    if req.get("grok_prompt"):
        shared["grok_prompt"] = req["grok_prompt"]

    all_outs = []
    errors = []
    n = len(items)
    for i, it in enumerate(items):
        asset = it.get("asset")
        if not asset:
            err = f"queue item {i + 1}: missing asset"
            if stop_on_error:
                raise DEPS["ToolError"](err)
            errors.append(err)
            continue
        label = it.get("name") or str(asset)
        job["message"] = f"queue {i + 1}/{n}: {label}"
        job["progress"] = i / n
        sub = {
            **shared,
            "asset": asset,
            "_q_lo": i / n,
            "_q_hi": (i + 1) / n,
            "_q_label": f"[{i + 1}/{n}] {label}",
        }
        if it.get("anchor_asset"):
            sub["anchor_asset"] = it["anchor_asset"]
        try:
            outs = job_standalone_upscale(job, host, sub)
            all_outs.extend(outs or [])
        except Exception as e:
            msg = f"[{i + 1}/{n}] {label} FAILED: {e}"
            errors.append(msg)
            job["message"] = msg
            if stop_on_error:
                raise
            # keep going so a walk-away queue doesn't die on one bad clip
            continue
    job["progress"] = 1.0
    if errors and not all_outs:
        raise DEPS["ToolError"](
            "Queue finished with 0 successes:\n" + "\n".join(errors))
    if errors:
        job["message"] = (
            f"queue done with {len(all_outs)} ok, {len(errors)} failed — "
            + "; ".join(errors)[:500])
    else:
        job["message"] = f"queue done ({n} video{'s' if n != 1 else ''})"
    return all_outs


def _snap_ltx_dim(v: int, step: int = 64) -> int:
    """Round to nearest LTX-safe size (multiples of 64). 0 means 'unset'."""
    if v <= 0:
        return v
    return max(step, int(round(v / step) * step))


# ---------------------------------------------------------------- public API helpers

def file_url(path) -> str | None:
    """HTTP URL for a file under the ninja data dir (same as UI /api/ninja/file)."""
    if not path:
        return None
    return "/api/ninja/file?path=" + urllib.parse.quote(str(path))


def public_project(p: dict | None = None) -> dict:
    """Project snapshot with stable HTTP media URLs for external clients."""
    out = copy.deepcopy(p if p is not None else project())
    song = out.get("song")
    if song and song.get("path"):
        song["url"] = file_url(song["path"])
    for c in out.get("chunks") or []:
        for key in ("raw", "preview", "final"):
            if c.get(key):
                c[f"{key}_url"] = file_url(c[key])
    tail = out.get("pending_tail")
    if tail and tail.get("path"):
        tail["url"] = file_url(tail["path"])
    if out.get("final_video"):
        out["final_video_url"] = file_url(out["final_video"])
    return out


def director_phase(p: dict | None = None) -> str:
    p = p if p is not None else project()
    if not p.get("song"):
        return "needs_song"
    chunks = p.get("chunks") or []
    last = chunks[-1] if chunks else None
    if last and not last.get("final"):
        return "review"
    prog = song_progress(p)
    if prog.get("past_end") and prog.get("parts_done", 0) > 0:
        return "ready_finish"
    if p.get("pending_tail"):
        return "ready_extend"
    return "ready_new"


def director_status(p: dict | None = None) -> dict:
    """Automation-friendly director status (phase, actions, URLs)."""
    p = p if p is not None else project()
    phase = director_phase(p)
    templates = template_status()
    pub = public_project(p)
    last = (pub.get("chunks") or [None])[-1]
    prog = song_progress(p)
    has_finals = prog.get("parts_done", 0) > 0
    # Drop Finish exports that are not inside this session folder.
    final_video = pub.get("final_video")
    final_url = pub.get("final_video_url")
    sid = str(p.get("session_id") or get_active_session_id())
    if final_video and sid:
        fp = str(final_video).replace("\\", "/").lower()
        if sid.lower() not in fp:
            final_video = None
            final_url = None
            pub.pop("final_video", None)
            pub.pop("final_video_url", None)
            if p.get("final_video"):
                p.pop("final_video", None)
                p.pop("finished_at", None)
                try:
                    save_project(p)
                except Exception:
                    pass
    return {
        "ok": True,
        "phase": phase,
        "session_id": sid,
        "session_dir": str(session_dir(sid)),
        "ready": bool(p.get("song")) and bool(templates.get("director")),
        "templates": templates,
        "project": pub,
        "chunk": last,
        "next_start": pub.get("next_start", 0.0),
        "work_resolution": pub.get("work_resolution"),
        "pending_tail": pub.get("pending_tail"),
        "progress": prog,
        "final_video": final_video,
        "final_video_url": final_url,
        "actions": {
            "can_generate": phase in ("ready_new", "ready_extend"),
            "can_continue": phase == "review",
            "can_retake": phase == "review" and not bool(
                ((last or {}).get("request") or {}).get("imported")),
            # Finish stitches approved finals only (ignores unfinished review)
            "can_finish": has_finals,
            "can_reset": bool(p.get("song")),
        },
        "config": cfg(),
    }


def start_ninja_job(label: str, fn, *args) -> dict:
    """Background job that attaches director_status() on success for API clients."""
    def runner(job, *a):
        outs = fn(job, *a)
        job["result"] = director_status()
        return outs
    return DEPS["start_job"](label, runner, *args)


def _job_response(job: dict) -> dict:
    return {"ok": True, "job": job["id"], "status_url": f"/api/job/{job['id']}"}


# ---------------------------------------------------------------- registration

def register(app, deps: dict):
    global DEPS, NINJA_DIR, TPL_DIR, CFG_FILE, SONGS_DIR
    DEPS = deps
    NINJA_DIR = deps["DATA"] / "ninja"
    TPL_DIR = deps["DATA"] / "comfyui_templates"
    CFG_FILE = deps["DATA"] / "comfyui.json"
    NINJA_DIR.mkdir(parents=True, exist_ok=True)
    SONGS_DIR = NINJA_DIR / "songs"
    SONGS_DIR.mkdir(parents=True, exist_ok=True)
    readme = SONGS_DIR / "README.txt"
    if not readme.is_file():
        readme.write_text(
            "Drop audio files here (mp3/wav/m4a/flac/ogg).\n"
            "They appear in Telegram New video → From song list,\n"
            "and via GET /api/director/songs.\n",
            encoding="utf-8",
        )

    @app.get("/api/ninja/state")
    def ninja_state():
        p = project()
        status = director_status(p)
        rt = copy.deepcopy(retake_session())
        pend = rt.get("pending") or {}
        if pend.get("preview"):
            pend["preview_url"] = file_url(pend["preview"])
        if pend.get("patch"):
            pend["patch_url"] = file_url(pend["patch"])
        if pend.get("raw"):
            pend["raw_url"] = file_url(pend["raw"])
        if pend:
            rt["pending"] = pend
        if (rt.get("clip") or {}).get("path"):
            rt["clip"]["url"] = file_url(rt["clip"]["path"])
        return {"config": status["config"], "templates": status["templates"],
                "project": status["project"], "retake": rt,
                "phase": status["phase"], "actions": status["actions"],
                "ready": status["ready"],
                "progress": status.get("progress"),
                "final_video": status.get("final_video"),
                "final_video_url": status.get("final_video_url")}

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

    @app.post("/api/ninja/comfy/restart")
    def ninja_comfy_restart():
        """Kill + restart local ComfyUI Desktop backend (VRAM flush).
        Only affects the Windows host install — not a remote RunPod URL."""
        script = Path(__file__).resolve().parent / "scripts" / "restart_comfyui.py"
        if not script.is_file():
            raise HTTPException(500, f"restart script missing: {script}")
        try:
            proc = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True, text=True, timeout=120,
            )
        except Exception as e:
            raise HTTPException(500, f"restart failed to launch: {e}")
        msg = (proc.stdout or proc.stderr or "").strip() or f"exit {proc.returncode}"
        return {"ok": proc.returncode == 0, "message": msg[:800],
                "returncode": proc.returncode}

    @app.post("/api/director/comfy/restart")
    def director_comfy_restart():
        return ninja_comfy_restart()

    @app.post("/api/ninja/capture/{kind}")
    def ninja_capture(kind: str):
        return capture_template(kind)

    @app.post("/api/ninja/song")
    async def ninja_song(file: UploadFile = File(...), name: str = Form("")):
        raw = await file.read()
        filename = file.filename or "song.wav"
        # keep a library copy for later "pick from list"
        try:
            lib = save_to_song_library(raw, filename)
        except Exception:
            lib = None
        src = lib if lib and Path(lib).is_file() else None
        if src is None:
            # temp write then apply (apply mints a new session)
            tmp = NINJA_DIR / ("_upload_" + uuid.uuid4().hex[:8] + Path(filename).suffix)
            tmp.write_bytes(raw)
            src = tmp
        return apply_song_file(Path(src), display_name=name or Path(filename).name)

    @app.get("/api/ninja/songs")
    def ninja_songs():
        songs = list_library_songs()
        return {"ok": True, "dir": str(songs_dir()), "songs": songs, "count": len(songs)}

    @app.post("/api/ninja/songs/select")
    async def ninja_songs_select(req: dict):
        """Activate a library song by filename. Body: {"filename": "track.mp3"}."""
        filename = (req or {}).get("filename") or (req or {}).get("name")
        if not filename:
            raise HTTPException(400, "filename required")
        src = songs_dir() / Path(str(filename)).name
        return apply_song_file(src, display_name=src.name)

    @app.post("/api/ninja/import_part")
    async def ninja_import_part(
        file: UploadFile = File(...),
        from_sec: float | None = Form(None),
    ):
        """Inject an external video as the current review part (Seedance/Grok/etc).
        Omit from_sec to use project next_start. Press Continue to finalize + prep tail."""
        p = project()
        if not p.get("song"):
            raise HTTPException(400, "Load the song first")
        if from_sec is None:
            from_sec = float(p.get("next_start") or 0.0)
        else:
            from_sec = float(from_sec)
        imports = session_imports_dir(p)
        dest = imports / (uuid.uuid4().hex[:8] + Path(file.filename or "part.mp4").suffix)
        dest.write_bytes(await file.read())
        dur = media_duration(dest)
        if dur <= 0:
            raise HTTPException(400, "Could not read video duration")
        to_sec = round(from_sec + dur, 3)
        n = len([c for c in p["chunks"] if c.get("final")]) + 1
        chunk = {"id": uuid.uuid4().hex[:8],
                 "part": f"part{n}-{int(from_sec)}-{int(to_sec)}",
                 "from": from_sec, "to": to_sec,
                 "tail_used": False, "tail_seconds": 0.0,
                 "raw": str(dest), "preview": str(dest), "final": None,
                 "status": "review",
                 "request": {"from_sec": from_sec, "to_sec": to_sec,
                              "prompts": [], "imported": True},
                 "host": "director"}
        p["chunks"] = [c for c in p["chunks"] if c.get("final")] + [chunk]
        # imported review replaces any pending extend tail until Continue
        p.pop("pending_tail", None)
        p.pop("final_video", None)  # don't keep another song's Finish export
        p.pop("finished_at", None)
        save_project(p)
        return director_status(p)

    @app.post("/api/ninja/reset")
    async def ninja_reset(request: Request):
        """Start a fresh session UUID. Keeps song by default; {"clear_song": true} drops it."""
        try:
            req = await request.json()
            if not isinstance(req, dict):
                req = {}
        except Exception:
            req = {}
        p = project()
        remember_executed_prompts(p)
        song = p.get("song") or {}
        song_path = Path(song["path"]) if song.get("path") else None
        song_name = song.get("filename")
        clear_song = bool(req.get("clear_song"))
        if not clear_song and song_name:
            # Prefer library copy so we don't depend on old session song path.
            lib = songs_dir() / Path(str(song_name)).name
            src = lib if lib.is_file() else song_path
            if src and Path(src).is_file():
                return apply_song_file(Path(src), display_name=song_name)
        begin_session(keep_prompts_from=p)
        return director_status()

    @app.post("/api/ninja/generate")
    async def ninja_generate(req: dict):
        p = project()
        if not p.get("song"):
            raise HTTPException(400, "Upload a song first")
        if not req.get("prompts"):
            raise HTTPException(400, "At least one text prompt segment required")
        load_template("director")  # fail fast if missing
        job = start_ninja_job("ninja-generate", job_generate, "director", p, req)
        return _job_response(job)

    @app.post("/api/ninja/retake")
    async def ninja_retake(req: dict = None):
        p = project()
        if not p["chunks"] or p["chunks"][-1].get("final"):
            raise HTTPException(400, "Nothing to retake — generate first")
        chunk = p["chunks"][-1]
        # retake = same request, NEW seed; optional duration / prompt overrides
        req = req or {}
        new_req = dict(chunk["request"])
        dur = req.get("duration_sec")
        if dur:
            new_req["to_sec"] = round(float(new_req["from_sec"]) + float(dur), 3)
        if new_req.get("engine") == "h3":
            # H3 part: single prompt — take the scene prompt override if given
            load_template("h3ref")
            if req.get("prompts"):
                txt = (req["prompts"][0] or {}).get("text")
                if txt:
                    new_req["prompt"] = txt
            new_req.pop("seed", None)  # always reroll on retake
            job = start_ninja_job("ninja-retake", job_h3_chunk, "h3ref", p, new_req)
            return _job_response(job)
        load_template("director")
        if "global_prompt" in req:
            new_req["global_prompt"] = req.get("global_prompt") or ""
        if req.get("prompts"):
            new_req["prompts"] = req["prompts"]
        job = start_ninja_job("ninja-retake", job_generate, "director", p, new_req)
        return _job_response(job)

    @app.post("/api/ninja/discard_review")
    async def ninja_discard_review():
        """Drop the uncommitted review chunk so the user can Generate again."""
        p = project()
        chunks = p.get("chunks") or []
        if not chunks or chunks[-1].get("final"):
            raise HTTPException(400, "Nothing to discard — no pending review")
        dropped = chunks[-1]
        p["chunks"] = [c for c in chunks if c.get("final")]
        # next_start stays at last committed end; clear any stale final export pointer
        if p["chunks"]:
            p["next_start"] = float(p["chunks"][-1].get("to") or p.get("next_start") or 0)
        save_project(p)
        st = director_status(p)
        st["discarded"] = {
            "part": dropped.get("part"),
            "from": dropped.get("from"),
            "to": dropped.get("to"),
        }
        return st

    @app.post("/api/ninja/continue")
    async def ninja_continue(req: dict = None):
        p = project()
        if not p["chunks"] or p["chunks"][-1].get("final"):
            raise HTTPException(400, "Nothing to continue — generate first")
        upscale_tail = bool((req or {}).get("upscale_tail", False))
        if upscale_tail:
            load_template("upscaler")  # fail fast with a clear message if missing
        accept = (req or {}).get("accept_sec")
        job = start_ninja_job("ninja-continue", job_continue, "upscaler", p,
                              accept, upscale_tail)
        return _job_response(job)

    @app.post("/api/ninja/finish")
    async def ninja_finish(req: dict = None):
        """Stitch all approved parts + song audio → final MP4.
        Optional body: {"copy_to": "D:\\\\parts\\\\full.mp4"}"""
        p = project()
        if not collect_final_part_paths(p):
            raise HTTPException(400, "No approved parts to finish")
        req = req or {}
        # default: D:\parts\{song_slug}\full.mp4 (not a shared flat full.mp4)
        if not req.get("copy_to"):
            req = {**(req or {}), "copy_to": str(parts_export_dir(p) / "full.mp4")}
        job = start_ninja_job("ninja-finish", job_finish, "director", p, req)
        return _job_response(job)

    @app.post("/api/ninja/stitch_folder")
    async def ninja_stitch_folder(req: dict):
        """Stitch partN-from-to.mp4 files from a folder + audio → one mp4.
        Body: {"parts_dir": "D:\\\\parts", "audio": "...optional...", "out": "...optional..."}"""
        parts_dir = Path((req or {}).get("parts_dir") or r"D:\parts")
        if not parts_dir.is_dir():
            raise HTTPException(400, f"parts_dir not found: {parts_dir}")
        files = sorted(
            parts_dir.glob("part*.mp4"),
            key=lambda f: (
                int(re.search(r"part(\d+)", f.name, re.I).group(1))
                if re.search(r"part(\d+)", f.name, re.I) else 999,
                f.name,
            ),
        )
        files = [f for f in files if f.name.lower() != "full.mp4"]
        if not files:
            raise HTTPException(400, f"No part*.mp4 in {parts_dir}")
        p = project()
        audio = Path((req or {}).get("audio") or ((p.get("song") or {}).get("path") or ""))
        if not audio.is_file():
            raise HTTPException(400, "audio path required (or load a song in the project)")
        out = Path((req or {}).get("out") or (parts_dir / "full.mp4"))

        def _run(job, *_a):
            stitch_parts_to_song(files, audio, out, job=job)
            return [DEPS["output_entry_abs"](out, "video", "stitched full")]

        job = start_ninja_job("ninja-stitch-folder", _run, "director")
        return _job_response(job)

    @app.post("/api/ninja/upscale")
    async def ninja_upscale(req: dict):
        load_template("upscaler")
        job = start_ninja_job("ninja-upscale", job_standalone_upscale, "upscaler", req)
        return _job_response(job)

    @app.post("/api/ninja/upscale_queue")
    async def ninja_upscale_queue(req: dict):
        """Walk-away queue: upscale several videos one after another."""
        load_template("upscaler")
        items = (req or {}).get("items") or []
        if not items:
            raise HTTPException(400, "items[] required — add at least one video")
        job = start_ninja_job("ninja-upscale-queue", job_upscale_queue, "upscaler", req)
        return _job_response(job)

    # ---- External Director API (same pipeline; stable contract for automation) ----

    @app.get("/api/director/status")
    def director_api_status():
        return director_status()

    @app.get("/api/director/songs")
    def director_api_songs():
        return ninja_songs()

    @app.post("/api/director/songs/select")
    async def director_api_songs_select(req: dict):
        return await ninja_songs_select(req)

    @app.get("/api/director/finished")
    def director_api_finished():
        """Finished full songs: D:\\parts\\{song}\\full.mp4 + ninja/exports/*_full.mp4."""
        items = []
        seen = set()
        root = parts_root()
        if root.is_dir():
            for d in sorted(root.iterdir(), key=lambda x: x.name.lower()):
                if not d.is_dir() or d.name.lower() == "fullvideo":
                    continue
                full = d / "full.mp4"
                if not full.is_file():
                    continue
                try:
                    dur = media_duration(full)
                except Exception:
                    dur = 0.0
                key = str(full.resolve())
                seen.add(key)
                items.append({
                    "id": f"parts:{d.name}",
                    "label": d.name,
                    "path": str(full),
                    "url": file_url(str(full)),
                    "duration": round(float(dur), 3),
                    "size": full.stat().st_size,
                })
        # Legacy flat exports + per-session exports
        export_roots = [NINJA_DIR / "exports"]
        sess_root = NINJA_DIR / "sessions"
        if sess_root.is_dir():
            export_roots.extend(sorted(sess_root.glob("*/exports")))
        parts_sess = parts_root() / "sessions"
        if parts_sess.is_dir():
            for d in sorted(parts_sess.iterdir(), key=lambda x: x.name.lower()):
                if not d.is_dir():
                    continue
                full = d / "full.mp4"
                if full.is_file():
                    key = str(full.resolve())
                    if key not in seen:
                        try:
                            dur = media_duration(full)
                        except Exception:
                            dur = 0.0
                        seen.add(key)
                        items.append({
                            "id": f"session:{d.name}",
                            "label": f"session/{d.name[:8]}",
                            "path": str(full),
                            "url": file_url(str(full)),
                            "duration": round(float(dur), 3),
                            "size": full.stat().st_size,
                        })
        for exp in export_roots:
            if not exp.is_dir():
                continue
            for pth in sorted(exp.glob("*_full.mp4"), key=lambda x: x.name.lower()):
                if not pth.is_file():
                    continue
                key = str(pth.resolve())
                if key in seen:
                    continue
                try:
                    dur = media_duration(pth)
                except Exception:
                    dur = 0.0
                items.append({
                    "id": f"exports:{pth.name}",
                    "label": pth.stem,
                    "path": str(pth),
                    "url": file_url(str(pth)),
                    "duration": round(float(dur), 3),
                    "size": pth.stat().st_size,
                })
        return {"ok": True, "dir": str(root), "items": items, "count": len(items)}

    @app.post("/api/director/reset")
    async def director_api_reset(request: Request):
        return await ninja_reset(request)

    @app.post("/api/director/generate")
    async def director_api_generate(req: dict):
        return await ninja_generate(req)

    @app.post("/api/director/continue")
    async def director_api_continue(req: dict = None):
        return await ninja_continue(req)

    @app.post("/api/director/finish")
    async def director_api_finish(req: dict = None):
        return await ninja_finish(req)

    @app.post("/api/director/stitch_folder")
    async def director_api_stitch_folder(req: dict):
        return await ninja_stitch_folder(req)

    @app.post("/api/director/retake")
    async def director_api_retake(req: dict = None):
        return await ninja_retake(req)

    @app.post("/api/director/discard_review")
    async def director_api_discard_review():
        return await ninja_discard_review()

    @app.post("/api/director/import_part")
    async def director_api_import_part(
        file: UploadFile = File(...),
        from_sec: float | None = Form(None),
    ):
        return await ninja_import_part(file=file, from_sec=from_sec)

    @app.get("/api/director/job/{jid}")
    def director_api_job(jid: str):
        job = DEPS.get("get_job", lambda _jid: None)(jid)
        if job is None:
            # fall back: jobs live in server._jobs; injected below when available
            raise HTTPException(404, "job not found")
        return job

    @app.get("/api/director/jobs")
    def director_api_jobs(status: str | None = None):
        jobs = list(DEPS.get("list_jobs", lambda: [])() or [])
        jobs.sort(key=lambda j: float(j.get("created") or 0), reverse=True)
        if status:
            jobs = [j for j in jobs if j.get("status") == status]
        return {"ok": True, "jobs": jobs[:40], "count": len(jobs)}

    # ---------------- MiniMax H3 (i2v smoke) ----------------

    @app.get("/api/h3/status")
    def h3_status():
        return {"ok": True, "host": host_url("h3"),
                "template": tpl_path("h3").is_file(),
                "template_ref": tpl_path("h3ref").is_file(),
                "capture_url": "/api/ninja/capture/h3"}

    @app.post("/api/h3/generate")
    async def h3_generate(req: dict):
        req = req or {}
        if not str(req.get("prompt") or "").strip():
            raise HTTPException(400, "prompt required")
        if not req.get("image_asset"):
            raise HTTPException(
                400, "image_asset required (i2v smoke — upload the start "
                     "image via /api/upload first)")
        load_template("h3")  # fail fast with the capture hint
        job = DEPS["start_job"]("h3-generate", job_h3_generate, "h3", req)
        return {"ok": True, "job": job["id"], "status_url": f"/api/job/{job['id']}"}

    @app.get("/api/director/projects")
    def director_projects():
        """Unfinished (and finished) projects across sessions, newest first."""
        root = NINJA_DIR / "sessions"
        active = get_active_session_id()
        items = []
        dirs = sorted([d for d in root.iterdir() if d.is_dir()],
                      key=lambda x: x.stat().st_mtime, reverse=True) if root.is_dir() else []
        for d in dirs:
            pj = d / "project.json"
            if not pj.is_file():
                continue
            data = _load_json(pj, {})
            song = data.get("song") or {}
            if not song:
                continue
            finals = [c for c in data.get("chunks") or [] if c.get("final")]
            covered = max([float(c.get("to") or 0) for c in finals], default=0.0)
            items.append({
                "session_id": data.get("session_id") or d.name,
                "song": data.get("name") or song.get("filename") or "?",
                "parts": len(finals),
                "covered": round(covered, 1),
                "duration": round(float(song.get("duration") or 0), 1),
                "finished": bool(data.get("final_video")),
                "active": d.name == active,
            })
            if len(items) >= 20:
                break
        return {"ok": True, "projects": items}

    @app.post("/api/director/projects/open")
    async def director_projects_open(req: dict):
        sid = str((req or {}).get("session_id") or "").strip()
        d = NINJA_DIR / "sessions" / sid
        if not (d / "project.json").is_file():
            raise HTTPException(404, f"project {sid} not found")
        set_active_session_id(sid)
        return director_status(project())

    @app.post("/api/director/canvas")
    async def director_canvas(req: dict):
        """Lock the project canvas (before the first part is generated)."""
        w = int((req or {}).get("width") or 0)
        h = int((req or {}).get("height") or 0)
        if w <= 0 or h <= 0:
            raise HTTPException(400, "width/height required")
        p = project()
        p["work_resolution"] = {"w": _snap_h3_dim(w), "h": _snap_h3_dim(h)}
        save_project(p)
        return {"ok": True, "work_resolution": p["work_resolution"]}

    @app.post("/api/director/h3_generate")
    async def director_h3_generate(req: dict):
        """H3-engine Director part: window + prompt (+ optional start=last_frame,
        extra image_assets, audio="song"|"none"). Lands as a review chunk."""
        req = req or {}
        p = project()
        if not p.get("song"):
            raise HTTPException(400, "Upload a song first")
        if not str(req.get("prompt") or "").strip():
            raise HTTPException(400, "prompt required")
        if req.get("from_sec") is None or req.get("to_sec") is None:
            raise HTTPException(400, "from_sec / to_sec required")
        if p["chunks"] and not p["chunks"][-1].get("final"):
            raise HTTPException(400, "Commit or Retake the current review first")
        load_template("h3ref")
        req["engine"] = "h3"
        job = start_ninja_job("h3-part", job_h3_chunk, "h3ref", p, req)
        return _job_response(job)

    @app.post("/api/h3/ref_generate")
    async def h3_ref_generate(req: dict):
        req = req or {}
        if not str(req.get("prompt") or "").strip():
            raise HTTPException(400, "prompt required")
        if not (req.get("image_assets") or req.get("audio_assets")):
            raise HTTPException(
                400, "at least one reference required (image_assets / "
                     "audio_assets — upload via /api/upload)")
        load_template("h3ref")  # fail fast with the capture hint
        job = DEPS["start_job"]("h3-ref-generate", job_h3_ref_generate, "h3ref", req)
        return {"ok": True, "job": job["id"], "status_url": f"/api/job/{job['id']}"}

    @app.get("/api/h3/job/{jid}")
    def h3_job(jid: str):
        job = DEPS.get("get_job", lambda _jid: None)(jid)
        if job is None:
            raise HTTPException(404, "job not found")
        return job

    # ---------------- Retake (clip window patching) ----------------

    @app.get("/api/director/fullvideos")
    def director_api_fullvideos():
        """List D:\\parts\\fullvideo for mobile Polish picker."""
        items = list_fullvideos()
        return {"ok": True, "dir": str(fullvideo_dir()), "items": items, "count": len(items)}

    @app.post("/api/ninja/retake_clip/upload")
    async def retake_upload(file: UploadFile = File(...)):
        rt_dir = NINJA_DIR / "retake"
        rt_dir.mkdir(parents=True, exist_ok=True)
        dest = rt_dir / (uuid.uuid4().hex[:8] + Path(file.filename or "clip.mp4").suffix)
        dest.write_bytes(await file.read())
        clip = clip_meta_from_path(dest, file.filename)
        # No song master — after Apply this upload becomes the chain base.
        save_retake_session({"clip": clip, "pending": None, "master_path": str(dest.resolve())})
        return clip

    @app.post("/api/ninja/retake_clip/load")
    async def retake_load(req: dict):
        """Load an on-disk movie by path (keeps that path — do not re-upload)."""
        path = Path(str((req or {}).get("path") or ""))
        if not path.is_file() or not _path_allowed(path):
            raise HTTPException(404, f"Video not found or not allowed: {path}")
        path = path.resolve()
        clip = clip_meta_from_path(path)
        # Master = this file. Apply overwrites it so Part2 continues from Part1.
        save_retake_session({
            "clip": clip,
            "pending": None,
            "master_path": str(path),
        })
        return {"ok": True, "clip": clip}

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
        if int(req.get("mode", 1)) == 2:
            want = int(round(float(cfg()["settings"].get("tail_seconds", 5.0)) * fps))
            lead_need = max(9, ((want - 1) // 8) * 8 + 1)
            if start_sec * fps < lead_need:
                raise HTTPException(
                    400,
                    f"Mode 2 needs ≥{lead_need / fps:.2f}s before From "
                    f"(Continue-style tail) — use mode 1 here")
        job = DEPS["start_job"]("ninja-retake-clip", job_retake_generate, "director", req)
        return {"job": job["id"]}

    @app.post("/api/ninja/retake_clip/apply")
    def retake_apply():
        """Bake pending polish into the master clip so the next window continues from it.

        Overwrites master_path (usually D:\\parts\\{song}\\full.mp4). Keeps a one-time
        .bak and a timestamped archive copy. Session clip always points at master.
        """
        sess = retake_session()
        pend = sess.get("pending")
        if not pend:
            raise HTTPException(400, "Nothing to apply — generate a retake first")
        preview = Path(pend["preview"])
        if not preview.is_file():
            raise HTTPException(400, "Pending preview missing — regenerate polish")
        clip = Path((sess.get("clip") or {}).get("path") or "")
        master = Path(str(sess.get("master_path") or ""))
        if not master.is_file():
            if clip.is_file() and clip.name.lower() == "full.mp4":
                master = clip.resolve()
            elif clip.is_file():
                master = clip.resolve()
            else:
                raise HTTPException(400, "No master clip path to apply into")
        else:
            master = master.resolve()
        tag = str(pend.get("tag") or uuid.uuid4().hex[:6])[:12]
        import shutil
        # One-time backup of the first original before any polish
        bak = master.with_name(master.stem + ".mp4.bak")
        if master.is_file() and not bak.is_file():
            try:
                shutil.copy2(master, bak)
            except Exception:
                pass
        # Archive each apply next to master (history), then overwrite master
        archive = master.with_name(f"{master.stem}_polish_{tag}.mp4")
        try:
            shutil.copy2(preview, archive)
        except Exception:
            archive = preview
        tmp = master.with_name(master.stem + f".polish_tmp_{tag}.mp4")
        shutil.copy2(preview, tmp)
        os.replace(str(tmp), str(master))
        clip_meta = clip_meta_from_path(master)
        sess["clip"] = clip_meta
        sess["master_path"] = str(master)
        sess["pending"] = None
        save_retake_session(sess)
        return {
            "ok": True,
            "path": str(master),
            "master_path": str(master),
            "export_path": str(archive),
            "url": clip_meta["url"],
            "clip": clip_meta,
            "window": {
                "start_sec": pend.get("start_sec"),
                "end_sec": pend.get("end_sec"),
            },
        }

    @app.post("/api/director/retake_clip/upload")
    async def director_retake_upload(file: UploadFile = File(...)):
        return await retake_upload(file)

    @app.post("/api/director/retake_clip/load")
    async def director_retake_load(req: dict):
        return await retake_load(req)

    @app.post("/api/director/retake_clip/generate")
    async def director_retake_generate(req: dict):
        return await retake_generate(req)

    @app.post("/api/director/retake_clip/apply")
    def director_retake_apply():
        return retake_apply()

    @app.post("/api/director/retake_clip/discard")
    def director_retake_discard():
        return retake_discard()

    @app.post("/api/ninja/retake_clip/discard")
    def retake_discard():
        sess = retake_session()
        sess["pending"] = None
        save_retake_session(sess)
        return {"ok": True}

    @app.get("/api/ninja/file")
    def ninja_file(path: str):
        fp = Path(path)
        if not _path_allowed(fp):
            raise HTTPException(404, "not found")
        from fastapi.responses import FileResponse
        return FileResponse(fp.resolve())
