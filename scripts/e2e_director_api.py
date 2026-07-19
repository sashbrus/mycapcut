#!/usr/bin/env python3
"""E2E: Director external API — reset → generate 480x640 → continue → status → reset.

Requires CupCut Studio + ComfyUI director host already running, song loaded.
Usage:
  .venv\\Scripts\\python.exe scripts\\e2e_director_api.py
  .venv\\Scripts\\python.exe scripts\\e2e_director_api.py --base http://127.0.0.1:8765 --seconds 8
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

W, H = 480, 640  # requested; LTX snaps to nearest multiple of 64 → 512x640


def snap_ltx(v: int, step: int = 64) -> int:
    return max(step, int(round(v / step) * step))


def api(base: str, method: str, path: str, body=None, timeout: float = 60):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:800]
        raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}") from e


def poll_job(base: str, jid: str, label: str, timeout_sec: float = 7200):
    t0 = time.time()
    last_msg = ""
    while True:
        job = api(base, "GET", f"/api/director/job/{jid}", timeout=30)
        status = job.get("status")
        msg = job.get("message") or ""
        prog = float(job.get("progress") or 0)
        if msg != last_msg or status != "running":
            print(f"  [{label}] {status} {prog:.0%} {msg}".rstrip())
            last_msg = msg
        if status == "done":
            return job
        if status == "error":
            raise RuntimeError(f"{label} failed: {job.get('message')}")
        if time.time() - t0 > timeout_sec:
            raise TimeoutError(f"{label} timed out after {timeout_sec}s")
        time.sleep(5)


def check(cond: bool, msg: str):
    if not cond:
        raise AssertionError(msg)
    print(f"  OK  {msg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--seconds", type=float, default=8.0,
                    help="chunk length in seconds (keep short for e2e)")
    ap.add_argument("--skip-generate", action="store_true",
                    help="only exercise status/reset (no ComfyUI)")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    print(f"E2E Director API @ {base}")

    exp_w, exp_h = snap_ltx(W), snap_ltx(H)
    print(f"requested {W}x{H} -> LTX grid {exp_w}x{exp_h}")

    # 1) status
    st = api(base, "GET", "/api/director/status")
    print(f"phase={st.get('phase')} ready={st.get('ready')} templates={st.get('templates')}")
    check(st.get("ok") is True, "status.ok")
    check(st.get("ready") is True, "director ready (song + template)")
    check(bool(st.get("project", {}).get("song")), "song present")

    if args.skip_generate:
        st = api(base, "POST", "/api/director/reset", {})
        check(st.get("phase") == "ready_new", f"after reset phase=ready_new (got {st.get('phase')})")
        check(st.get("project", {}).get("chunks") == [], "chunks cleared")
        print("SKIP generate/continue (--skip-generate)")
        print("PASS (status/reset only)")
        return 0

    # 2) generate (or resume a matching review chunk left by a prior run)
    can_resume = (
        st.get("phase") == "review"
        and int((st.get("work_resolution") or {}).get("w") or 0) == exp_w
        and int((st.get("work_resolution") or {}).get("h") or 0) == exp_h
    )
    if can_resume:
        print("resume: review chunk already present - skip generate")
        result = st
    else:
        st = api(base, "POST", "/api/director/reset", {})
        check(st.get("phase") == "ready_new", f"after reset phase=ready_new (got {st.get('phase')})")
        check(st.get("project", {}).get("chunks") == [], "chunks cleared")
        check(st.get("pending_tail") is None, "pending_tail cleared")
        check(st.get("work_resolution") is None, "work_resolution cleared")
        check(st.get("actions", {}).get("can_generate") is True, "can_generate")
        body = {
            "from_sec": 0.0,
            "to_sec": float(args.seconds),
            "width": W,
            "height": H,
            "source": "new",
            "global_prompt": "cinematic music video, consistent subject, natural motion",
            "prompts": [{"text": "a person walking through a quiet city street at dusk, "
                                 "soft neon lights, camera slowly tracking forward"}],
        }
        print(f"generate {W}x{H} for {args.seconds}s ...")
        gen = api(base, "POST", "/api/director/generate", body)
        check(bool(gen.get("job")), "generate returned job id")
        check(bool(gen.get("status_url")), "generate returned status_url")
        job = poll_job(base, gen["job"], "generate")
        result = job.get("result") or {}

    check(result.get("phase") == "review", f"phase=review after generate (got {result.get('phase')})")
    chunk = result.get("chunk") or {}
    check(chunk.get("status") == "review", "chunk.status=review")
    check(bool(chunk.get("preview_url") or chunk.get("raw_url")), "chunk has media URL")
    wr = result.get("work_resolution") or {}
    check(int(wr.get("w") or 0) == exp_w, f"work_resolution.w={exp_w} (got {wr.get('w')})")
    check(int(wr.get("h") or 0) == exp_h, f"work_resolution.h={exp_h} (got {wr.get('h')})")
    check(result.get("actions", {}).get("can_continue") is True, "can_continue")

    # media URL fetchable
    url = chunk.get("preview_url") or chunk.get("raw_url")
    with urllib.request.urlopen(base + url, timeout=30) as r:
        check(r.status == 200 and int(r.headers.get("Content-Length") or 1) > 0,
              f"media URL fetchable ({url[:60]}...)")

    # 4) continue (no remote upscale - local tail prep)
    print("continue (upscale_tail=false) ...")
    cont = api(base, "POST", "/api/director/continue", {"upscale_tail": False})
    job = poll_job(base, cont["job"], "continue")
    result = job.get("result") or {}
    check(result.get("phase") == "ready_extend",
          f"phase=ready_extend after continue (got {result.get('phase')})")
    check(result.get("pending_tail") is not None, "pending_tail set")
    check(bool((result.get("pending_tail") or {}).get("url")), "pending_tail.url set")
    check(float(result.get("next_start") or 0) > 0, f"next_start>0 (got {result.get('next_start')})")
    finals = [c for c in (result.get("project") or {}).get("chunks", []) if c.get("final")]
    check(len(finals) == 1, "one finalized chunk")
    check(bool(finals[0].get("final_url")), "final_url present")
    check(result.get("actions", {}).get("can_generate") is True, "can generate next extend")

    # 5) status endpoint agrees
    st = api(base, "GET", "/api/director/status")
    check(st.get("phase") == "ready_extend", "GET /status phase=ready_extend")
    check(int((st.get("work_resolution") or {}).get("w") or 0) == exp_w, "status resolution w")
    check(int((st.get("work_resolution") or {}).get("h") or 0) == exp_h, "status resolution h")

    # 6) reset again
    st = api(base, "POST", "/api/director/reset", {})
    check(st.get("phase") == "ready_new", "final reset -> ready_new")
    check(st.get("project", {}).get("chunks") == [], "final reset cleared chunks")

    print("PASS - director API e2e ok")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
