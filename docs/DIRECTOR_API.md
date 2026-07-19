# Director API

HTTP API for the Ninja Director pipeline (same create / continue / retake / reset flow as the UI).

Base URL (local): `http://127.0.0.1:8765`

Prerequisites:

- CupCut Studio running (`python server.py`)
- Song already loaded (via UI or `POST /api/ninja/song`)
- Director template captured, ComfyUI director host reachable

Long work is async: `generate` / `continue` / `retake` return a `job` id — poll until `status` is `done` or `error`.

---

## Endpoints

| Method | Path | Sync | Description |
|--------|------|------|-------------|
| GET | `/api/director/status` | yes | Phase, actions, project, media URLs |
| POST | `/api/director/reset` | yes | Clear chunks / tail / resolution (song kept) |
| POST | `/api/director/generate` | job | Create a video chunk |
| POST | `/api/director/continue` | job | Approve review chunk, prep next tail |
| POST | `/api/director/retake` | job | Re-run last chunk with a new seed |
| GET | `/api/director/job/{id}` | yes | Poll job (+ `result` when done) |

UI routes under `/api/ninja/*` still work; `/api/director/*` is the stable automation surface.

---

## Status phases

| `phase` | Meaning | Typical next call |
|---------|---------|-------------------|
| `needs_song` | No song loaded | Upload song in UI / `/api/ninja/song` |
| `ready_new` | Can start a new scene (`source: "new"`) | `generate` |
| `review` | Chunk waiting for decision | `continue` or `retake` |
| `ready_extend` | Tail ready for next chunk | `generate` with `source: "extend"` |

`actions` flags: `can_generate`, `can_continue`, `can_retake`, `can_reset`.

Media fields include HTTP paths such as `preview_url`, `final_url`, `pending_tail.url` — prefix with the base URL to open in a browser.

**Resolution:** width/height are snapped to multiples of 64 for LTX (e.g. request `480×640` → actual `512×640`). Check `work_resolution` after generate.

---

## Request bodies

### Generate — `POST /api/director/generate`

```json
{
  "from_sec": 0,
  "to_sec": 8,
  "width": 480,
  "height": 640,
  "source": "new",
  "global_prompt": "cinematic music video",
  "prompts": [{ "text": "a person walking through a quiet city street at dusk" }],
  "image_asset": null
}
```

| Field | Notes |
|-------|--------|
| `from_sec` / `to_sec` | Song window for this chunk |
| `width` / `height` | Requested size (snapped to ÷64) |
| `source` | `"new"` = fresh cut; `"extend"` = use `pending_tail` |
| `prompts` | At least one `{ "text": "..." }` |
| `global_prompt` | Optional style / subject |
| `image_asset` | Optional asset id (start image, `source: "new"` only) |

Response:

```json
{ "ok": true, "job": "3afc9f7af505", "status_url": "/api/job/3afc9f7af505" }
```

### Continue — `POST /api/director/continue`

```json
{
  "upscale_tail": false,
  "accept_sec": 8
}
```

| Field | Notes |
|-------|--------|
| `upscale_tail` | `true` = refresh tail via upscaler host (slower) |
| `accept_sec` | Optional — keep only first N seconds of the chunk |

### Retake — `POST /api/director/retake`

```json
{ "duration_sec": 8 }
```

`duration_sec` optional — change length; seed is always new.

### Reset — `POST /api/director/reset`

Empty body `{}`. Keeps the song; clears chunks, tail, work resolution.

---

## Job poll — `GET /api/director/job/{id}`

| `status` | Meaning |
|----------|---------|
| `running` | In progress (`message`, `progress`) |
| `done` | Finished — see `outputs` and `result` (director status snapshot) |
| `error` | Failed — see `message` |

---

## Examples (PowerShell)

```powershell
$base = "http://127.0.0.1:8765"

# Status
Invoke-RestMethod "$base/api/director/status" | ConvertTo-Json -Depth 8

# Reset
Invoke-RestMethod "$base/api/director/reset" -Method POST -ContentType "application/json" -Body "{}"

# Generate (avoid curl JSON escaping issues in PowerShell)
$body = @{
  from_sec = 0
  to_sec = 8
  width = 480
  height = 640
  source = "new"
  global_prompt = "cinematic music video"
  prompts = @(@{ text = "a person walking through a quiet city street at dusk" })
} | ConvertTo-Json -Depth 5

$gen = Invoke-RestMethod "$base/api/director/generate" -Method POST -ContentType "application/json" -Body $body
$job = $gen.job

# Poll
Invoke-RestMethod "$base/api/director/job/$job" | ConvertTo-Json -Depth 8

# Continue (after status=done and phase=review)
Invoke-RestMethod "$base/api/director/continue" -Method POST -ContentType "application/json" -Body '{"upscale_tail":false}'

# Retake (while still in review, instead of continue)
Invoke-RestMethod "$base/api/director/retake" -Method POST -ContentType "application/json" -Body "{}"
```

Open a preview after generate finishes:

```powershell
$st = Invoke-RestMethod "$base/api/director/status"
Start-Process ($base + $st.chunk.preview_url)
```

---

## Examples (CMD / curl.exe)

Write JSON to a file — do not inline complex JSON with `\"` in PowerShell.

```bat
curl.exe -s http://127.0.0.1:8765/api/director/status

curl.exe -s -X POST http://127.0.0.1:8765/api/director/reset -H "Content-Type: application/json" -d "{}"
```

```powershell
@'
{"from_sec":0,"to_sec":8,"width":480,"height":640,"source":"new","global_prompt":"cinematic music video","prompts":[{"text":"a person walking through a quiet city street at dusk"}]}
'@ | Set-Content $env:TEMP\gen.json -Encoding ascii

curl.exe -s -X POST http://127.0.0.1:8765/api/director/generate -H "Content-Type: application/json" --data-binary "@$env:TEMP\gen.json"

curl.exe -s http://127.0.0.1:8765/api/director/job/PASTE_JOB_ID

curl.exe -s -X POST http://127.0.0.1:8765/api/director/continue -H "Content-Type: application/json" -d "{\"upscale_tail\":false}"
```

---

## Typical loop

```
status (ready_new / ready_extend)
  → generate
  → poll job until done
  → status (review) → open preview_url
  → continue  OR  retake
  → status (ready_extend)
  → generate (source: "extend") …
  → reset when starting over
```

Automated e2e: `scripts/e2e_director_api.py`

```powershell
.\.venv\Scripts\python.exe scripts\e2e_director_api.py --seconds 8
```

---

## Phase 2 (Telegram + n8n)

See `integrations/README.md`.

- Telegram DM bot: `cupcut-telegram-bot` (menu → Director API)
- HTTP tools for n8n: `http://127.0.0.1:8787/tools/*`
- n8n webhook: `POST http://127.0.0.1:5678/webhook/cupcut-director` with `{"action":"status"}`
