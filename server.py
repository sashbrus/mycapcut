"""
CupCut Studio — a CapCut-style web video/audio editor.

Backend: FastAPI + ffmpeg.
  - Asset library (upload, probe, waveform peaks, filmstrip, thumbnails)
  - One-shot Tools (trim/split, join, attach audio, image+audio->mp4,
    extract audio, frame grab, convert, speed, volume/fade, inspect)
  - Timeline export: renders the Studio timeline (video track + N audio
    tracks) into a single MP4 via one ffmpeg filter_complex graph.
  - Long operations run as background jobs with real ffmpeg progress.

Run:  python server.py   (serves http://127.0.0.1:8765)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import struct
import subprocess
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# ---------------------------------------------------------------------------
# Paths / setup
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent
DATA = Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "CupCutStudio"
ASSETS_DIR = DATA / "assets"
CACHE_DIR = DATA / "cache"
OUT_DIR = DATA / "outputs"
for d in (ASSETS_DIR, CACHE_DIR, OUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

REGISTRY = DATA / "assets.json"

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

log = logging.getLogger("cupcut-studio")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="CupCut Studio")

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".ts", ".wmv", ".flv"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class ToolError(Exception):
    pass


def run(cmd: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    log.info("RUN %s", " ".join(str(c) for c in cmd[:12]) + (" ..." if len(cmd) > 12 else ""))
    proc = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-12:])
        raise ToolError(f"ffmpeg failed:\n{tail}")
    return proc


def ffprobe(path: str | Path) -> dict:
    proc = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format",
         "-show_streams", str(path)],
        capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise ToolError(f"Cannot read media file: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout or "{}")


def stream_of(info: dict, kind: str) -> dict | None:
    for s in info.get("streams", []):
        if s.get("codec_type") == kind:
            return s
    return None


def media_meta(path: Path) -> dict:
    """Probe a file and return normalized metadata."""
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        info = ffprobe(path)
        v = stream_of(info, "video") or {}
        return {"kind": "image", "duration": 0.0, "width": v.get("width", 0),
                "height": v.get("height", 0), "hasAudio": False}
    info = ffprobe(path)
    v = stream_of(info, "video")
    a = stream_of(info, "audio")
    dur = float(info.get("format", {}).get("duration") or 0.0)
    # some containers report duration only on the stream
    if not dur and v:
        dur = float(v.get("duration") or 0.0)
    if not dur and a:
        dur = float(a.get("duration") or 0.0)
    if v and (v.get("disposition", {}).get("attached_pic") or ext in AUDIO_EXT):
        v = None  # album art inside an mp3 etc.
    kind = "video" if v else ("audio" if a else "unknown")
    if kind == "unknown":
        raise ToolError("No audio or video stream found in file.")
    meta = {"kind": kind, "duration": round(dur, 3), "hasAudio": a is not None,
            "width": (v or {}).get("width", 0), "height": (v or {}).get("height", 0)}
    if v:
        rate = v.get("r_frame_rate", "0/1")
        try:
            n, d = rate.split("/")
            meta["fps"] = round(float(n) / float(d), 3) if float(d) else 0
        except (ValueError, ZeroDivisionError):
            meta["fps"] = 0
    return meta


def extract_frame_png(src: str, t: float, dest: Path):
    """Extract one frame at t. ffmpeg exits 0 with no output when t is past
    the last decodable frame (container duration > video stream duration),
    so retreat until a frame is actually produced."""
    for tt in (t, t - 0.25, t - 1.0, 0.0):
        if tt < 0:
            continue
        run([FFMPEG, "-y", "-ss", f"{tt:.3f}", "-i", str(src),
             "-frames:v", "1", "-q:v", "2", str(dest)])
        if dest.exists() and dest.stat().st_size > 0:
            return
    raise ToolError("Could not extract a video frame at this position.")


def out_path(suffix: str, name: str = "") -> Path:
    stem = (name or "out") + "_" + uuid.uuid4().hex[:8]
    return OUT_DIR / f"{stem}{suffix}"


def fmt_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - h * 3600 - m * 60
    return f"{h:d}:{m:02d}:{s:06.3f}" if h else f"{m:d}:{s:06.3f}"


def parse_ts(value) -> float:
    if value is None or value == "":
        raise ToolError("Missing timestamp.")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    parts = text.split(":")
    if not 1 <= len(parts) <= 3:
        raise ToolError(f"Can't read timestamp '{value}'. Try '90' or '1:30.5'.")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise ToolError(f"Can't read timestamp '{value}'. Try '90' or '1:30.5'.")
    secs = 0.0
    for n in nums:
        secs = secs * 60 + n
    return secs


# ---------------------------------------------------------------------------
# Asset registry
# ---------------------------------------------------------------------------

_assets: dict[str, dict] = {}
_assets_lock = threading.Lock()


def _save_registry():
    REGISTRY.write_text(json.dumps(_assets, indent=1), encoding="utf-8")


def _load_registry():
    if REGISTRY.exists():
        try:
            data = json.loads(REGISTRY.read_text(encoding="utf-8"))
            for aid, a in data.items():
                if Path(a["path"]).exists():
                    _assets[aid] = a
        except Exception:
            log.exception("Failed to load asset registry")


_load_registry()


def register_asset(path: Path, name: str) -> dict:
    meta = media_meta(path)
    aid = uuid.uuid4().hex[:12]
    asset = {"id": aid, "name": name, "path": str(path), **meta}
    with _assets_lock:
        _assets[aid] = asset
        _save_registry()
    return asset


def get_asset(aid: str) -> dict:
    a = _assets.get(aid)
    if not a or not Path(a["path"]).exists():
        raise ToolError(f"Asset {aid} not found.")
    return a


# ---------------------------------------------------------------------------
# Jobs (background ffmpeg with progress)
# ---------------------------------------------------------------------------

_jobs: dict[str, dict] = {}


def new_job(label: str) -> dict:
    job = {"id": uuid.uuid4().hex[:12], "label": label, "status": "running",
           "progress": 0.0, "message": "", "outputs": [], "created": time.time()}
    _jobs[job["id"]] = job
    return job


def run_ffmpeg_progress(cmd: list[str], total_duration: float, job: dict):
    """Run ffmpeg, updating job['progress'] from -progress output."""
    pfile = CACHE_DIR / f"progress_{job['id']}.txt"
    full = [str(c) for c in cmd[:1]] + ["-progress", str(pfile), "-nostats"] + [str(c) for c in cmd[1:]]
    proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    stderr_chunks = []

    def _drain():
        for line in proc.stderr:
            stderr_chunks.append(line)
    t = threading.Thread(target=_drain, daemon=True)
    t.start()

    while proc.poll() is None:
        time.sleep(0.4)
        try:
            txt = pfile.read_text(encoding="utf-8", errors="replace")
            for line in reversed(txt.strip().splitlines()):
                if line.startswith("out_time_ms=") or line.startswith("out_time_us="):
                    us = float(line.split("=")[1])
                    if total_duration > 0:
                        job["progress"] = min(0.99, (us / 1e6) / total_duration)
                    break
        except (OSError, ValueError):
            pass
    t.join(timeout=5)
    pfile.unlink(missing_ok=True)
    if proc.returncode != 0:
        tail = "\n".join("".join(stderr_chunks).strip().splitlines()[-12:])
        raise ToolError(f"ffmpeg failed:\n{tail}")
    job["progress"] = 1.0


def start_job(label: str, fn, *args) -> dict:
    job = new_job(label)

    def runner():
        try:
            outputs = fn(job, *args)
            job["outputs"] = outputs
            job["status"] = "done"
            job["progress"] = 1.0
        except ToolError as e:
            job["status"] = "error"
            job["message"] = str(e)
        except Exception as e:
            log.exception("job %s crashed", job["id"])
            job["status"] = "error"
            job["message"] = f"Internal error: {e}"
    threading.Thread(target=runner, daemon=True).start()
    return job


def output_entry(path: Path, kind: str, name: str | None = None) -> dict:
    return {"name": name or path.name, "kind": kind,
            "url": f"/outputs/{path.name}", "path": str(path)}


# ---------------------------------------------------------------------------
# Waveform / filmstrip / thumbnails
# ---------------------------------------------------------------------------

def waveform_peaks(path: str, buckets: int = 1200) -> list[float]:
    """Decode to mono 8kHz s16le and return per-bucket max amplitude 0..1."""
    proc = subprocess.run(
        [FFMPEG, "-v", "error", "-i", path, "-map", "a:0", "-ac", "1",
         "-ar", "8000", "-f", "s16le", "-"],
        capture_output=True, timeout=600)
    raw = proc.stdout
    n = len(raw) // 2
    if n == 0:
        return []
    samples_per_bucket = max(1, n // buckets)
    peaks = []
    for b in range(0, n, samples_per_bucket):
        end = min(n, b + samples_per_bucket)
        chunk = raw[b * 2:end * 2]
        vals = struct.unpack(f"<{len(chunk)//2}h", chunk)
        peaks.append(round(max(abs(v) for v in vals) / 32768, 3))
        if len(peaks) >= buckets:
            break
    return peaks


FILMSTRIP_FRAMES = 12


def make_filmstrip(asset: dict, dest: Path):
    dur = max(asset["duration"], 0.1)
    fps = FILMSTRIP_FRAMES / dur
    run([FFMPEG, "-y", "-i", asset["path"],
         "-vf", f"fps={fps:.6f},scale=120:68:force_original_aspect_ratio=increase,"
                f"crop=120:68,tile={FILMSTRIP_FRAMES}x1",
         "-frames:v", "1", "-q:v", "4", str(dest)])


def make_thumb(asset: dict, dest: Path):
    if asset["kind"] == "image":
        run([FFMPEG, "-y", "-i", asset["path"],
             "-vf", "scale=320:-1", "-frames:v", "1", "-q:v", "4", str(dest)])
    else:
        t = min(asset["duration"] * 0.1, 3.0)
        run([FFMPEG, "-y", "-ss", f"{t:.2f}", "-i", asset["path"],
             "-vf", "scale=320:-1", "-frames:v", "1", "-q:v", "4", str(dest)])


# ---------------------------------------------------------------------------
# API: assets
# ---------------------------------------------------------------------------

@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    name = Path(file.filename or "file").name
    ext = Path(name).suffix.lower()
    if ext not in VIDEO_EXT | AUDIO_EXT | IMAGE_EXT:
        raise HTTPException(400, f"Unsupported file type: {ext}")
    dest_dir = ASSETS_DIR / uuid.uuid4().hex[:8]
    dest_dir.mkdir(parents=True)
    dest = dest_dir / name
    with open(dest, "wb") as fh:
        while chunk := await file.read(1 << 20):
            fh.write(chunk)
    try:
        asset = register_asset(dest, name)
    except ToolError as e:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise HTTPException(400, str(e))
    return asset


@app.get("/api/assets")
def api_assets():
    return list(_assets.values())


@app.delete("/api/assets/{aid}")
def api_delete_asset(aid: str):
    with _assets_lock:
        a = _assets.pop(aid, None)
        _save_registry()
    if a:
        shutil.rmtree(Path(a["path"]).parent, ignore_errors=True)
        for suffix in ("_wave.json", "_strip.jpg", "_thumb.jpg"):
            (CACHE_DIR / f"{aid}{suffix}").unlink(missing_ok=True)
    return {"ok": True}


@app.get("/media/{aid}")
def api_media(aid: str):
    try:
        a = get_asset(aid)
    except ToolError:
        raise HTTPException(404, "asset not found")
    return FileResponse(a["path"], filename=a["name"])


@app.get("/api/waveform/{aid}")
def api_waveform(aid: str):
    try:
        a = get_asset(aid)
    except ToolError:
        raise HTTPException(404, "asset not found")
    cache = CACHE_DIR / f"{aid}_wave.json"
    if cache.exists():
        return JSONResponse(json.loads(cache.read_text()))
    peaks = waveform_peaks(a["path"]) if a["hasAudio"] else []
    data = {"peaks": peaks, "duration": a["duration"]}
    cache.write_text(json.dumps(data))
    return JSONResponse(data)


@app.get("/api/filmstrip/{aid}")
def api_filmstrip(aid: str):
    try:
        a = get_asset(aid)
    except ToolError:
        raise HTTPException(404, "asset not found")
    cache = CACHE_DIR / f"{aid}_strip.jpg"
    if not cache.exists():
        if a["kind"] == "video":
            make_filmstrip(a, cache)
        else:
            make_thumb(a, cache)
    return FileResponse(cache)


@app.get("/api/thumb/{aid}")
def api_thumb(aid: str):
    try:
        a = get_asset(aid)
    except ToolError:
        raise HTTPException(404, "asset not found")
    cache = CACHE_DIR / f"{aid}_thumb.jpg"
    if not cache.exists():
        make_thumb(a, cache)
    return FileResponse(cache)


PROJECT_FILE = DATA / "project.json"


@app.get("/api/project")
def api_get_project():
    """Return the auto-saved project (timeline + settings), if any."""
    if PROJECT_FILE.exists():
        try:
            return JSONResponse(json.loads(PROJECT_FILE.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return JSONResponse({})


@app.post("/api/project")
async def api_save_project(req: Request):
    body = await req.json()
    PROJECT_FILE.write_text(json.dumps(body), encoding="utf-8")
    return {"ok": True}


def _decode_mono(path: str, start: float, dur: float, rate: int = 8000):
    """Decode an audio segment to mono float32 samples."""
    import numpy as np
    proc = subprocess.run(
        [FFMPEG, "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
         "-i", path, "-map", "a:0", "-ac", "1", "-ar", str(rate),
         "-f", "f32le", "-"],
        capture_output=True, timeout=300)
    return np.frombuffer(proc.stdout, dtype=np.float32).astype(np.float64)


@app.post("/api/match_audio")
async def api_match_audio(req: Request):
    """Find where a clip's audio best matches inside a master audio track.

    Cross-correlates the clip audio against a window of the master around
    the user's approximate placement. Returns the best source offset in the
    master plus a normalized confidence (0..1).
    """
    try:
        import numpy as np
    except ImportError:
        raise HTTPException(500, "numpy is required for audio matching (pip install numpy)")
    body = await req.json()
    try:
        vasset = get_asset(body["videoAssetId"])
        aasset = get_asset(body["audioAssetId"])
    except ToolError as e:
        raise HTTPException(404, str(e))
    if not vasset["hasAudio"]:
        raise HTTPException(400, "This clip has no audio to match with.")
    cin = float(body.get("in", 0.0))
    cout = float(body.get("out", vasset["duration"]))
    approx = float(body.get("approx", 0.0))
    window = min(max(float(body.get("window", 5.0)), 0.5), 30.0)
    rate = 8000

    clip_dur = min(cout - cin, 20.0)  # 20s of audio is plenty for a match
    if clip_dur < 0.3:
        raise HTTPException(400, "Clip is too short to match.")
    seg_start = max(0.0, approx - window)
    seg_end = min(aasset["duration"], approx + window + clip_dur)

    clip_raw = _decode_mono(vasset["path"], cin, clip_dur, rate)
    seg_raw = _decode_mono(aasset["path"], seg_start, seg_end - seg_start, rate)
    if len(clip_raw) < rate * 0.2:
        raise HTTPException(400, "Could not decode enough audio from the clip.")
    if len(seg_raw) <= len(clip_raw):
        raise HTTPException(400, "Search window is smaller than the clip — "
                                 "place the clip further inside the audio track.")

    def xcorr_valid(seg, clip):
        """corr[k] = sum(seg[k+i] * clip[i]) for every full overlap position."""
        N = 1 << (len(seg) + len(clip) - 1).bit_length()
        corr = np.fft.irfft(np.fft.rfft(seg, N) * np.conj(np.fft.rfft(clip, N)), N)
        return corr[: len(seg) - len(clip) + 1]

    def ncc(seg, clip):
        """Normalized cross-correlation array (values in -1..1)."""
        valid = xcorr_valid(seg, clip)
        css = np.concatenate([[0.0], np.cumsum(seg * seg)])
        energies = css[len(clip):len(clip) + len(valid)] - css[: len(valid)]
        return valid / (np.sqrt(energies) * np.linalg.norm(clip) + 1e-9)

    # Stage 1: loudness-envelope correlation (robust to re-encoding / EQ,
    # which raw-waveform correlation is not — AI-generated snippets are
    # re-renders, not bit-copies of the master).
    hop = 80                      # 8000 Hz / 80 = 100 Hz envelope
    erate = rate // hop

    def envelope(x):
        n = len(x) // hop
        e = np.abs(x[: n * hop]).reshape(n, hop).mean(axis=1)
        e = np.log1p(100 * e)     # log compression flattens level differences
        return e - e.mean()

    env_ncc = ncc(envelope(seg_raw), envelope(clip_raw))
    k_env = int(np.argmax(env_ncc))
    confidence = float(max(0.0, env_ncc[k_env]))

    # Stage 2: refine to sample precision with raw waveform, but only
    # within ±0.15 s of the envelope peak.
    clip0 = clip_raw - clip_raw.mean()
    seg0 = seg_raw - seg_raw.mean()
    raw_valid = xcorr_valid(seg0, clip0)
    center = k_env * hop
    span = int(0.15 * rate)
    lo = max(0, center - span)
    hi = min(len(raw_valid), center + span + 1)
    k_raw = lo + int(np.argmax(raw_valid[lo:hi]))

    offset = seg_start + k_raw / rate
    return {"offset": round(offset, 4), "confidence": round(confidence, 4)}


@app.get("/api/frame/{aid}")
def api_frame(aid: str, t: float = 0.0):
    """Extract one frame of a library asset as a downloadable PNG."""
    try:
        a = get_asset(aid)
    except ToolError:
        raise HTTPException(404, "asset not found")
    if a["kind"] == "audio":
        raise HTTPException(400, "audio has no frames")
    if a["kind"] == "video":
        t = min(max(0.0, t), max(0.0, a["duration"] - 0.03))
    out = out_path(".png", "frame")
    try:
        if a["kind"] == "image":
            run([FFMPEG, "-y", "-i", a["path"], "-frames:v", "1", str(out)])
        else:
            extract_frame_png(a["path"], t, out)
    except ToolError as e:
        raise HTTPException(500, str(e))
    stem = Path(a["name"]).stem
    return FileResponse(out, filename=f"{stem}_frame_{t:.2f}s.png",
                        media_type="image/png")


@app.post("/api/freeze_video")
async def api_freeze_video(req: Request):
    """Create a still video from one frame of an asset and add it to the library."""
    body = await req.json()
    try:
        a = get_asset(body["assetId"])
    except ToolError as e:
        raise HTTPException(404, str(e))
    if a["kind"] == "audio":
        raise HTTPException(400, "Audio has no frames.")
    t = float(body.get("t", 0.0))
    dur = min(max(float(body.get("duration", 5)), 0.5), 60.0)
    w = int(body.get("width", 1280))
    h = int(body.get("height", 720))
    fps = int(body.get("fps", 30))
    if a["kind"] == "video":
        t = min(max(0.0, t), max(0.0, a["duration"] - 0.03))
    # optional soundtrack: a slice of another asset's audio (the song under
    # the clip), starting at audioStart inside that asset's source
    audio_asset = None
    audio_start = 0.0
    if body.get("audioAssetId"):
        try:
            audio_asset = get_asset(body["audioAssetId"])
            audio_start = max(0.0, float(body.get("audioStart", 0.0)))
        except ToolError:
            audio_asset = None

    frame = CACHE_DIR / f"freeze_{uuid.uuid4().hex[:8]}.png"
    dest_dir = ASSETS_DIR / uuid.uuid4().hex[:8]
    try:
        extract_frame_png(a["path"], t, frame)
        dest_dir.mkdir(parents=True)
        name = f"{Path(a['name']).stem}_freeze_{dur:g}s.mp4"
        dest = dest_dir / name
        vchain = (f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                  f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1[v]")
        if audio_asset:
            # atrim decodes from 0 and cuts exactly; input-seeking (-ss -i)
            # is imprecise on VBR MP3s and shifted the audio
            cmd = [FFMPEG, "-y",
                   "-loop", "1", "-framerate", str(fps), "-t", f"{dur:.3f}",
                   "-i", str(frame),
                   "-i", audio_asset["path"],
                   "-filter_complex",
                   f"{vchain};"
                   f"[1:a]atrim=start={audio_start:.4f}:end={audio_start + dur:.4f},"
                   f"asetpts=PTS-STARTPTS,aresample=44100,apad[aud]",
                   "-map", "[v]", "-map", "[aud]",
                   "-c:v", "libx264", "-preset", "fast", "-tune", "stillimage",
                   "-crf", "20", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "192k",
                   "-t", f"{dur:.3f}", "-movflags", "+faststart", str(dest)]
        else:
            cmd = [FFMPEG, "-y",
                   "-loop", "1", "-framerate", str(fps), "-t", f"{dur:.3f}",
                   "-i", str(frame),
                   "-filter_complex", vchain, "-map", "[v]",
                   "-c:v", "libx264", "-preset", "fast", "-tune", "stillimage",
                   "-crf", "20", "-pix_fmt", "yuv420p",
                   "-t", f"{dur:.3f}", "-movflags", "+faststart", str(dest)]
        run(cmd)
        return register_asset(dest, name)
    except ToolError as e:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise HTTPException(500, str(e))
    finally:
        frame.unlink(missing_ok=True)


@app.post("/api/import_output")
async def api_import_output(req: Request):
    """Copy a tool/export output into the asset library."""
    body = await req.json()
    src = Path(body.get("path", ""))
    if not src.exists() or src.parent != OUT_DIR:
        raise HTTPException(400, "invalid output path")
    dest_dir = ASSETS_DIR / uuid.uuid4().hex[:8]
    dest_dir.mkdir(parents=True)
    dest = dest_dir / src.name
    shutil.copy2(src, dest)
    return register_asset(dest, src.name)


# ---------------------------------------------------------------------------
# API: jobs
# ---------------------------------------------------------------------------

@app.get("/api/job/{jid}")
def api_job(jid: str):
    job = _jobs.get(jid)
    if not job:
        raise HTTPException(404, "job not found")
    return job


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

async def save_uploads(req: Request) -> tuple[dict, dict[str, list[Path]]]:
    """Parse a multipart form: returns (fields, files-by-field-name)."""
    form = await req.form()
    fields: dict = {}
    files: dict[str, list[Path]] = {}
    tmp = CACHE_DIR / f"tool_{uuid.uuid4().hex[:8]}"
    tmp.mkdir(parents=True)
    for key in form.keys():
        for val in form.getlist(key):
            if hasattr(val, "filename") and val.filename:
                dest = tmp / f"{len(files.get(key, []))}_{Path(val.filename).name}"
                with open(dest, "wb") as fh:
                    while chunk := await val.read(1 << 20):
                        fh.write(chunk)
                files.setdefault(key, []).append(dest)
            else:
                fields[key] = val
    return fields, files


def enc_video(crf: int = 20) -> list[str]:
    return ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart"]


def enc_audio() -> list[str]:
    return ["-c:a", "aac", "-b:a", "192k"]


def _dur(path: Path) -> float:
    return media_meta(path)["duration"]


# ---- individual tool implementations (run inside a job thread) ------------

def tool_trim_split(job, fields, files):
    src = files["file"][0]
    meta = media_meta(src)
    dur = meta["duration"]
    audio_only = meta["kind"] == "audio"
    ext = ".mp3" if audio_only else ".mp4"
    mode = fields.get("mode", "trim")
    acodec = ["-c:a", "libmp3lame", "-b:a", "192k"] if audio_only else enc_audio()

    if mode == "trim":
        s = parse_ts(fields.get("start") or 0)
        e = parse_ts(fields.get("end") or dur)
        if e <= s:
            raise ToolError(f"End ({fmt_ts(e)}) must be after start ({fmt_ts(s)}).")
        out = out_path(ext, "trim")
        if audio_only:
            cmd = [FFMPEG, "-y", "-ss", f"{s:.3f}", "-to", f"{e:.3f}", "-i", src,
                   "-vn", *acodec, out]
        else:
            cmd = [FFMPEG, "-y", "-ss", f"{s:.3f}", "-to", f"{e:.3f}", "-i", src,
                   *enc_video(), *acodec, out]
        run_ffmpeg_progress(cmd, e - s, job)
        return [output_entry(out, "audio" if audio_only else "video")]

    # split at timestamp -> two files
    s = parse_ts(fields.get("start"))
    if s <= 0 or s >= dur:
        raise ToolError(f"Split point must be between 0 and {fmt_ts(dur)}.")
    p1 = out_path(ext, "part1")
    p2 = out_path(ext, "part2")
    enc = ["-vn", *acodec] if audio_only else [*enc_video(), *acodec]
    run_ffmpeg_progress([FFMPEG, "-y", "-i", src, "-to", f"{s:.3f}", *enc, p1], s, job)
    job["progress"] = 0.5
    run_ffmpeg_progress([FFMPEG, "-y", "-ss", f"{s:.3f}", "-i", src, *enc, p2], dur - s, job)
    kind = "audio" if audio_only else "video"
    return [output_entry(p1, kind), output_entry(p2, kind)]


def tool_join_videos(job, fields, files):
    paths = files.get("files", [])
    if len(paths) < 2:
        raise ToolError("Upload at least 2 videos.")
    meta0 = media_meta(paths[0])
    w, h = meta0.get("width") or 1280, meta0.get("height") or 720
    total = sum(_dur(p) for p in paths)

    inputs, extra = [], []
    audio_fallback = {}
    null_idx = len(paths)
    for i, p in enumerate(paths):
        inputs += ["-i", p]
        if not media_meta(p)["hasAudio"]:
            extra += ["-f", "lavfi", "-t", f"{_dur(p):.3f}",
                      "-i", "anullsrc=r=44100:cl=stereo"]
            audio_fallback[i] = null_idx
            null_idx += 1

    parts, cat = [], ""
    for i in range(len(paths)):
        parts.append(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                     f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v{i}];")
        src = audio_fallback.get(i, i)
        parts.append(f"[{src}:a]aresample=44100[a{i}];")
        cat += f"[v{i}][a{i}]"
    fc = "".join(parts) + f"{cat}concat=n={len(paths)}:v=1:a=1[v][a]"

    out = out_path(".mp4", "joined")
    run_ffmpeg_progress([FFMPEG, "-y", *inputs, *extra, "-filter_complex", fc,
                         "-map", "[v]", "-map", "[a]", *enc_video(), *enc_audio(), out],
                        total, job)
    return [output_entry(out, "video")]


def tool_join_audio(job, fields, files):
    paths = files.get("files", [])
    if len(paths) < 2:
        raise ToolError("Upload at least 2 audio files.")
    fmt = fields.get("format", "mp3")
    total = sum(_dur(p) for p in paths)
    inputs, labels = [], ""
    for i, p in enumerate(paths):
        inputs += ["-i", p]
        labels += f"[{i}:a]"
    fc = labels + f"concat=n={len(paths)}:v=0:a=1[a]"
    suffix = "." + fmt
    codec = {"mp3": ["-c:a", "libmp3lame", "-b:a", "192k"],
             "wav": ["-c:a", "pcm_s16le"],
             "m4a": ["-c:a", "aac", "-b:a", "192k"]}[fmt]
    out = out_path(suffix, "joined")
    run_ffmpeg_progress([FFMPEG, "-y", *inputs, "-filter_complex", fc,
                         "-map", "[a]", *codec, out], total, job)
    return [output_entry(out, "audio")]


def tool_attach_audio(job, fields, files):
    vpath = files["video"][0]
    apath = files["audio"][0]
    mode = fields.get("mode", "replace")
    vmeta = media_meta(vpath)
    out = out_path(".mp4", "attached")
    if mode == "mix" and vmeta["hasAudio"]:
        cmd = [FFMPEG, "-y", "-i", vpath, "-i", apath,
               "-filter_complex",
               "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[a]",
               "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", *enc_audio(),
               "-movflags", "+faststart", out]
    else:
        cmd = [FFMPEG, "-y", "-i", vpath, "-i", apath,
               "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", *enc_audio(),
               "-shortest", "-movflags", "+faststart", out]
    run_ffmpeg_progress(cmd, vmeta["duration"], job)
    return [output_entry(out, "video")]


def tool_image_audio(job, fields, files):
    img = files["image"][0]
    aud = files["audio"][0]
    adur = _dur(aud)
    res = fields.get("resolution", "1280x720")
    w, h = (int(x) for x in res.split("x"))
    zoom = fields.get("zoom") == "true"
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1")
    if zoom:
        frames = int(adur * 30)
        vf = (f"scale={w*2}:{h*2}:force_original_aspect_ratio=increase,"
              f"crop={w*2}:{h*2},"
              f"zoompan=z='min(zoom+0.0006,1.4)':d={frames}:x='iw/2-(iw/zoom/2)'"
              f":y='ih/2-(ih/zoom/2)':s={w}x{h}:fps=30,setsar=1")
    out = out_path(".mp4", "slideshow")
    cmd = [FFMPEG, "-y", "-loop", "1", "-framerate", "30", "-i", img, "-i", aud,
           "-vf", vf, "-t", f"{adur:.3f}", *enc_video(), *enc_audio(),
           "-shortest", out]
    run_ffmpeg_progress(cmd, adur, job)
    return [output_entry(out, "video")]


def tool_extract_audio(job, fields, files):
    src = files["file"][0]
    meta = media_meta(src)
    if not meta["hasAudio"]:
        raise ToolError("This video has no audio track.")
    fmt = fields.get("format", "mp3")
    if fmt == "mp3":
        out = out_path(".mp3", "audio")
        cmd = [FFMPEG, "-y", "-i", src, "-vn", "-c:a", "libmp3lame", "-q:a", "2", out]
    elif fmt == "wav":
        out = out_path(".wav", "audio")
        cmd = [FFMPEG, "-y", "-i", src, "-vn", "-c:a", "pcm_s16le", out]
    else:
        codec = (stream_of(ffprobe(src), "audio") or {}).get("codec_name", "aac")
        ext = {"aac": ".m4a", "opus": ".opus", "vorbis": ".ogg",
               "mp3": ".mp3", "flac": ".flac"}.get(codec, ".m4a")
        out = out_path(ext, "audio")
        cmd = [FFMPEG, "-y", "-i", src, "-vn", "-c:a", "copy", out]
    run_ffmpeg_progress(cmd, meta["duration"], job)
    return [output_entry(out, "audio")]


def tool_grab_frame(job, fields, files):
    src = files["file"][0]
    dur = _dur(src)
    which = fields.get("which", "last")
    t = {"first": 0.0, "last": max(0.0, dur - 0.05), "middle": dur / 2}.get(which)
    if t is None:
        t = parse_ts(fields.get("timestamp"))
        if t < 0 or t > dur:
            raise ToolError(f"Timestamp must be within 0 — {fmt_ts(dur)}.")
    out = out_path(".png", "frame")
    run([FFMPEG, "-y", "-ss", f"{t:.3f}", "-i", src, "-frames:v", "1", "-q:v", "2", out])
    return [output_entry(out, "image")]


def tool_convert(job, fields, files):
    src = files["file"][0]
    meta = media_meta(src)
    fmt = fields.get("format", "mp4")
    dur = meta["duration"]
    if fmt == "mp4":
        out = out_path(".mp4", "converted")
        cmd = [FFMPEG, "-y", "-i", src, *enc_video(), *enc_audio(), out]
    elif fmt == "webm":
        out = out_path(".webm", "converted")
        cmd = [FFMPEG, "-y", "-i", src, "-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0",
               "-c:a", "libopus", "-b:a", "128k", out]
    elif fmt == "gif":
        out = out_path(".gif", "converted")
        cmd = [FFMPEG, "-y", "-i", src,
               "-vf", "fps=12,scale=480:-1:flags=lanczos,split[s0][s1];"
                      "[s0]palettegen[p];[s1][p]paletteuse", out]
    elif fmt in ("mp3", "wav", "m4a", "flac"):
        codec = {"mp3": ["-c:a", "libmp3lame", "-b:a", "192k"],
                 "wav": ["-c:a", "pcm_s16le"],
                 "m4a": ["-c:a", "aac", "-b:a", "192k"],
                 "flac": ["-c:a", "flac"]}[fmt]
        out = out_path("." + fmt, "converted")
        cmd = [FFMPEG, "-y", "-i", src, "-vn", *codec, out]
    else:
        raise ToolError(f"Unknown format {fmt}")
    run_ffmpeg_progress(cmd, dur, job)
    kind = "audio" if fmt in ("mp3", "wav", "m4a", "flac") else \
           ("image" if fmt == "gif" else "video")
    return [output_entry(out, kind)]


def _atempo_chain(speed: float) -> str:
    """atempo supports 0.5..100 per instance; chain for slow speeds."""
    parts = []
    s = speed
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    parts.append(f"atempo={s:.4f}")
    return ",".join(parts)


def tool_speed(job, fields, files):
    src = files["file"][0]
    meta = media_meta(src)
    speed = float(fields.get("speed", 1.0))
    if not 0.1 <= speed <= 10:
        raise ToolError("Speed must be between 0.1x and 10x.")
    newdur = meta["duration"] / speed
    if meta["kind"] == "audio":
        out = out_path(".mp3", "speed")
        cmd = [FFMPEG, "-y", "-i", src, "-filter:a", _atempo_chain(speed),
               "-c:a", "libmp3lame", "-b:a", "192k", out]
        run_ffmpeg_progress(cmd, newdur, job)
        return [output_entry(out, "audio")]
    out = out_path(".mp4", "speed")
    if meta["hasAudio"]:
        fc = f"[0:v]setpts=PTS/{speed}[v];[0:a]{_atempo_chain(speed)}[a]"
        cmd = [FFMPEG, "-y", "-i", src, "-filter_complex", fc,
               "-map", "[v]", "-map", "[a]", *enc_video(), *enc_audio(), out]
    else:
        cmd = [FFMPEG, "-y", "-i", src, "-filter:v", f"setpts=PTS/{speed}",
               "-an", *enc_video(), out]
    run_ffmpeg_progress(cmd, newdur, job)
    return [output_entry(out, "video")]


def tool_volume(job, fields, files):
    src = files["file"][0]
    meta = media_meta(src)
    if not meta["hasAudio"]:
        raise ToolError("File has no audio track.")
    gain = float(fields.get("gain", 0))
    fade_in = float(fields.get("fade_in", 0) or 0)
    fade_out = float(fields.get("fade_out", 0) or 0)
    normalize = fields.get("normalize") == "true"
    dur = meta["duration"]
    af = []
    if normalize:
        af.append("loudnorm=I=-14:TP=-1.5:LRA=11")
    if gain:
        af.append(f"volume={gain}dB")
    if fade_in > 0:
        af.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        af.append(f"afade=t=out:st={max(0, dur - fade_out):.3f}:d={fade_out:.3f}")
    if not af:
        raise ToolError("Nothing to do — set a gain, fade, or normalize.")
    if meta["kind"] == "audio":
        out = out_path(".mp3", "volume")
        cmd = [FFMPEG, "-y", "-i", src, "-af", ",".join(af),
               "-c:a", "libmp3lame", "-b:a", "192k", out]
        run_ffmpeg_progress(cmd, dur, job)
        return [output_entry(out, "audio")]
    out = out_path(".mp4", "volume")
    cmd = [FFMPEG, "-y", "-i", src, "-af", ",".join(af),
           "-c:v", "copy", *enc_audio(), "-movflags", "+faststart", out]
    run_ffmpeg_progress(cmd, dur, job)
    return [output_entry(out, "video")]


TOOLS = {
    "trim_split": tool_trim_split,
    "join_videos": tool_join_videos,
    "join_audio": tool_join_audio,
    "attach_audio": tool_attach_audio,
    "image_audio": tool_image_audio,
    "extract_audio": tool_extract_audio,
    "grab_frame": tool_grab_frame,
    "convert": tool_convert,
    "speed": tool_speed,
    "volume": tool_volume,
}


@app.post("/api/tool/{name}")
async def api_tool(name: str, req: Request):
    fn = TOOLS.get(name)
    if not fn:
        raise HTTPException(404, f"Unknown tool {name}")
    fields, files = await save_uploads(req)
    if not files:
        raise HTTPException(400, "No files uploaded.")
    job = start_job(name, fn, fields, files)
    return {"job": job["id"]}


@app.post("/api/inspect")
async def api_inspect(file: UploadFile = File(...)):
    tmp = CACHE_DIR / f"inspect_{uuid.uuid4().hex[:8]}{Path(file.filename or '').suffix}"
    with open(tmp, "wb") as fh:
        while chunk := await file.read(1 << 20):
            fh.write(chunk)
    try:
        info = ffprobe(tmp)
    except ToolError as e:
        raise HTTPException(400, str(e))
    finally:
        size = tmp.stat().st_size if tmp.exists() else 0
    fmt = info.get("format", {})
    v = stream_of(info, "video")
    a = stream_of(info, "audio")
    result = {
        "name": file.filename,
        "container": fmt.get("format_long_name", fmt.get("format_name", "?")),
        "duration": float(fmt.get("duration", 0) or 0),
        "size_mb": round(size / (1024 * 1024), 2),
        "bitrate_kbps": int(fmt.get("bit_rate", 0) or 0) // 1000,
    }
    if v:
        rate = v.get("r_frame_rate", "0/1")
        try:
            n, d = rate.split("/")
            fps = float(n) / float(d) if float(d) else 0
        except (ValueError, ZeroDivisionError):
            fps = 0
        result["video"] = {"codec": v.get("codec_name"), "width": v.get("width"),
                           "height": v.get("height"), "fps": round(fps, 2),
                           "pix_fmt": v.get("pix_fmt")}
    if a:
        result["audio"] = {"codec": a.get("codec_name"),
                           "sample_rate": a.get("sample_rate"),
                           "channels": a.get("channels")}
    tmp.unlink(missing_ok=True)
    return result


# ---------------------------------------------------------------------------
# Timeline export
# ---------------------------------------------------------------------------

def build_export(job: dict, timeline: dict):
    settings = timeline.get("settings", {})
    W = int(settings.get("width", 1280))
    H = int(settings.get("height", 720))
    FPS = int(settings.get("fps", 30))
    vclips = sorted(timeline.get("video", []), key=lambda c: c["start"])
    atracks = timeline.get("audioTracks", [])

    total = 0.0
    for c in vclips:
        total = max(total, c["start"] + (c["out"] - c["in"]))
    for tr in atracks:
        for c in tr:
            total = max(total, c["start"] + (c["out"] - c["in"]))
    if total <= 0:
        raise ToolError("Timeline is empty — add clips first.")

    inputs: list[str] = []
    input_count = 0
    filters: list[str] = []

    def add_input(args: list[str]) -> int:
        nonlocal input_count
        inputs.extend(args)
        idx = input_count
        input_count += 1
        return idx

    asset_input: dict[str, int] = {}  # plain (non-image) assets reuse one input

    def input_for(asset: dict) -> int:
        if asset["id"] not in asset_input:
            asset_input[asset["id"]] = add_input(["-i", asset["path"]])
        return asset_input[asset["id"]]

    # ---------------- video track ----------------
    vsegs: list[str] = []
    seg = 0
    t = 0.0

    def black(d: float):
        nonlocal seg
        idx = add_input(["-f", "lavfi", "-i",
                         f"color=c=black:s={W}x{H}:r={FPS}:d={max(d, 0.04):.3f}"])
        filters.append(f"[{idx}:v]setsar=1[vs{seg}]")
        vsegs.append(f"[vs{seg}]")
        seg += 1

    audio_parts: list[str] = []
    apart = 0

    def add_audio(idx: int, cin: float, cout: float, delay: float, volume: float):
        nonlocal apart
        ms = int(round(delay * 1000))
        vol = f",volume={volume:.3f}" if abs(volume - 1.0) > 0.01 else ""
        filters.append(
            f"[{idx}:a]atrim=start={cin:.3f}:end={cout:.3f},asetpts=PTS-STARTPTS,"
            f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
            f"{vol},adelay={ms}:all=1[ap{apart}]")
        audio_parts.append(f"[ap{apart}]")
        apart += 1

    has_video = bool(vclips)
    if has_video:
        for c in vclips:
            asset = get_asset(c["assetId"])
            cdur = c["out"] - c["in"]
            if cdur <= 0.01:
                continue
            if c["start"] > t + 0.02:
                black(c["start"] - t)
            if asset["kind"] == "image":
                idx = add_input(["-loop", "1", "-t", f"{cdur:.3f}",
                                 "-framerate", str(FPS), "-i", asset["path"]])
                filters.append(
                    f"[{idx}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                    f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS},"
                    f"trim=duration={cdur:.3f},setpts=PTS-STARTPTS[vs{seg}]")
            else:
                idx = input_for(asset)
                filters.append(
                    f"[{idx}:v]trim=start={c['in']:.3f}:end={c['out']:.3f},"
                    f"setpts=PTS-STARTPTS,"
                    f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                    f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS}[vs{seg}]")
                if asset["hasAudio"] and not c.get("muted"):
                    add_audio(idx, c["in"], c["out"], c["start"], c.get("volume", 1.0))
            vsegs.append(f"[vs{seg}]")
            seg += 1
            t = c["start"] + cdur
        if total > t + 0.02:
            black(total - t)
        if len(vsegs) == 1:
            filters.append(f"{vsegs[0]}format=yuv420p[vout]")
        else:
            filters.append("".join(vsegs) + f"concat=n={len(vsegs)}:v=1:a=0,"
                           f"format=yuv420p[vout]")

    # ---------------- audio tracks ----------------
    for tr in atracks:
        for c in tr:
            asset = get_asset(c["assetId"])
            if not asset["hasAudio"] or c.get("muted"):
                continue
            idx = input_for(asset)
            add_audio(idx, c["in"], c["out"], c["start"], c.get("volume", 1.0))

    if audio_parts:
        if len(audio_parts) == 1:
            filters.append(f"{audio_parts[0]}apad=whole_dur={total:.3f},"
                           f"atrim=0:{total:.3f}[aout]")
        else:
            filters.append("".join(audio_parts) +
                           f"amix=inputs={len(audio_parts)}:duration=longest:normalize=0,"
                           f"apad=whole_dur={total:.3f},atrim=0:{total:.3f}[aout]")
    else:
        idx = add_input(["-f", "lavfi", "-t", f"{total:.3f}",
                         "-i", "anullsrc=r=44100:cl=stereo"])
        filters.append(f"[{idx}:a]atrim=0:{total:.3f}[aout]")

    fc = ";".join(filters)

    if has_video:
        out = out_path(".mp4", "export")
        cmd = [FFMPEG, "-y", *inputs, "-filter_complex", fc,
               "-map", "[vout]", "-map", "[aout]", "-r", str(FPS),
               *enc_video(crf=19), *enc_audio(), "-t", f"{total:.3f}", out]
        run_ffmpeg_progress(cmd, total, job)
        return [output_entry(out, "video", "export.mp4")]

    # audio-only timeline -> mp3
    out = out_path(".mp3", "export")
    cmd = [FFMPEG, "-y", *inputs, "-filter_complex", fc, "-map", "[aout]",
           "-c:a", "libmp3lame", "-b:a", "192k", "-t", f"{total:.3f}", out]
    run_ffmpeg_progress(cmd, total, job)
    return [output_entry(out, "audio", "export.mp3")]


@app.post("/api/export")
async def api_export(req: Request):
    timeline = await req.json()
    job = start_job("export", build_export, timeline)
    return {"job": job["id"]}


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------

class NoCacheStatic(StaticFiles):
    """Always revalidate the app's JS/CSS so updates arrive on plain refresh."""
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/outputs", StaticFiles(directory=str(OUT_DIR)), name="outputs")
app.mount("/", NoCacheStatic(directory=str(ROOT / "static"), html=True), name="static")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
