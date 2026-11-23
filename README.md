# bot-jelly

Discord bot for JobShift creators (registration, payouts, content generator panel, persona, support AI).
Runs on Railway; **`main` auto-deploys**.

## Vidéo (bouton « Vidéo » du générateur de contenu)

Creators with the Creator role click **Vidéo** (`custom_id jelly_content_video`) on the content generator
panel and get one unique video in their private `clips-<username>` thread, as a private download link
`/v/<token>` (7 days). Each video is a source clip chosen by staff, nested in an X-style post frame
(`tweet_light`, `tweet_dark`, `quote_dark`), sped up 1.04 to 1.15x, pitch shifted ±1 to 2 semitones
(never 0), re-encoded (H.264 CRF 20, 60 fps, AAC 48 kHz 192 kb/s, metadata stripped).

No AI call and no third-party upload is involved. Code: `video_generator.py` (button, queue, staff
commands), `video_store.py` (JSON store), `video_hosting.py` (`/v` routes), `video_uniquify/` (frames,
encode, guards; Inter font vendored under SIL OFL 1.1).

### Staff commands (runtime staff check + Manage Server by default)

| Command | What it does |
|---|---|
| `/add-video video:<pièce jointe> titre:<texte>` | Validates the Discord attachment (video + audio streams, finite duration 5 to 180 s, portrait ~9:16, size <= `VIDEO_MAX_UPLOAD_MB`), stores it as `sources/<id>.<ext>` |
| `/list-videos` | Active and disabled sources, deliveries per source, persistence warning |
| `/disable-video video:<id>` | Removes a source from rotation. Queued requests for it are cancelled without counting |

### Rotation and uniqueness

- Among the sources the creator has not had yet in this cycle: fewest retained siblings first (every
  new variant is compared with all of them), then least recently assigned (global). A creator never gets the same source twice
  until they have had every active source once (a cycle), then a new cycle starts.
- Seed per assignment = HMAC-SHA256(salt, clipper, video, cycle). The salt lives in the store and is never printed.
- Pitch slots: siblings (deliveries of the same source) cycle through 4 slots by sibling index:
  `+low, -low, +high, -high` (low 1.00 to 1.30 st, high 1.70 to 2.00 st). The sign still alternates
  every sibling. The slot is stored on the assignment (`pitch_slot`) and on the video (`last_pitch_slot`,
  after delivery), so it survives restarts. Next slot = latest live sibling's slot + 1; a released or
  failed reservation is not live, so the next request reuses that slot instead of burning it.
  Retries keep the slot and only re-jitter inside it: after a vs-original audio failure the draw leans
  away from 1.0 st, after a vs-sibling audio failure it leans toward the band's far edge.
- Templates per attempt: the template used by the fewest retained siblings first, then not the newest
  sibling's; consecutive attempts always change template. The video width is picked away from the
  siblings' widths (still inside the template's range, overall 55 to 78 %).
- Guards on every attempt, means over aligned frames:
  vs original pHash >= 16/64, SSIM <= 0.5, chromaprint <= 0.20;
  vs every retained sibling pHash >= 10 (frames taken at the same source instants), chromaprint <= 0.30.
  Up to 3 retries after the first encode (4 attempts). Rejected renders are deleted immediately;
  if all 4 fail, the creator gets an error and nothing is counted.
- Fail closed: no audio, a fingerprint too short to compare, or a NaN/inf metric is a failed guard.
- An assignment is marked delivered only after the link is published **and** the thread message is sent.
  Any failure before that deletes the link and releases the reservation.

### Structural limit (decision for Antoine)

Measured on the real clip (Linux, real fpcalc): same-sign siblings 0.5 st apart score 0.33 to 0.44 on
chromaprint (limit 0.30); 1.0 st apart, 0.14 to 0.24; opposite signs about 0.0. Speed changes do not
help. Inside ±1 to 2 st this leaves about **4 well-separated audio slots per source**, and visually only
3 templates (a 4th sibling has to share one, often failing pHash ≥ 10 on that pair).

So the 5th and later delivery of the same source will probably fail the sibling guard against slot 1's
delivery, and the creator gets "Impossible de produire une version assez différente", until older
siblings age out (signature housekeeping keeps the newest 50 per source, so in practice they don't).
Preferring sources with fewer retained siblings spreads deliveries, but with N active sources the
limit is about 4×N deliveries before failures start.

Options (not implemented):
1. Compare siblings only within the 7-day link window: older variants have probably been posted
   already, and the guard is about variants live at the same time. Cheapest change (drop expired
   signatures in housekeeping).
2. Cap deliveries per source at 4 per cycle and rotate creators onto other sources; staff add sources as
   needed (`/list-videos` shows deliveries per source).
3. Widen the audio space: a larger pitch range (outside the approved ±2 st) or an extra audio treatment
   (EQ/room tone/background bed) evaluated against the same thresholds.

### Queue

One worker (concurrency 1), at most `VIDEO_QUEUE_MAX` creators waiting (default 10), one request per
creator at a time. The creator sees their position and an ETA of 1 to 3 min per video ahead.
Rendering runs in a thread with ffmpeg capped to `VIDEO_FFMPEG_THREADS` (default 2) and a per-encode
timeout; cancel/shutdown kills ffmpeg. After a restart, interrupted requests are released and the
creator is told in their thread to click again.

### System requirements

`railpack.json` installs `ffmpeg`, `rubberband-cli`, `librubberband2`, `libchromaprint-tools` (Debian
trixie runtime image). At startup the bot logs `[VIDEO] Selfcheck OK` or the missing piece; while
anything is missing (ffmpeg, ffprobe, fpcalc, the ffmpeg `rubberband` filter, libx264, aac) the button
answers with an error and nothing is queued. There is no weaker fallback.

**Builder check before merge:** `nixpacks.toml` is also in the repo. `railpack.json` only applies if the
Railway service builds with Railpack. With Nixpacks, fpcalc and rubberband are likely missing and the
selfcheck will keep the button disabled (the rest of the bot is unaffected).

The `/v` links also need the web server (`PORT`) and a public URL (`PUBLIC_BASE_URL`, or
`RAILWAY_PUBLIC_DOMAIN`). Without them the button refuses before reserving or encoding anything.

### Storage plan (operator)

- Data dir: `DATA_DIR`, default `/data`. Layout: `top-videos.json`, `sources/`, `signatures/`,
  `links/<token>/`, `work/`.
- **Single replica only.** The JSON store and the queue live in one process.
- **Planned: Railway volume mounted at `/data`, 10 GB (provisional).** No volume is attached today
  (coordinator's read-only check of Railway). Create and attach it right before merging, because `main`
  auto-deploys.
- Without a volume, the bot logs `[VIDEO] WARNING: no Railway volume mounted at DATA_DIR` and `/list-videos`
  shows a non-persistent warning: sources, assignments and links are lost on redeploy. If `DATA_DIR` is not
  writable, the bot falls back to `./data-local` (gitignored) with the same warning.
- A store that is corrupt, or missing next to existing sources, is refused (feature disabled, file left
  untouched) rather than silently recreated.

Rough sizing: sources up to 300 MB each (typically 20 to 150 MB); one delivered MP4 per click
(roughly 10 to 60 MB for 30 to 180 s), kept 7 days; signatures about 20 KB each. 10 GB covers about
20 sources plus a few hundred live links.

### Housekeeping (runs at startup and after each job)

- Expired links (7 days) and stale temp folders are deleted.
- Failed assignments are pruned after 7 days.
- Sibling signatures: the newest 50 per source are kept. Older siblings are no longer compared;
  they were already compared with their own neighbours, and keeping all of them would make every
  render slower without bound. This is the main tradeoff.
- Deliveries from a creator's finished cycles are pruned once their link expired (the current cycle,
  which drives rotation, is never pruned).
- Disabled sources: the media file is deleted once no request is in flight; the record stays.

### Preview (Linux box with ffmpeg + fpcalc)

```
python scripts/preview_videos.py --src /workspace/dedup-src/original.MOV --out /workspace/bot-jelly-preview
```

Runs the full button path with simulated Discord (no token, no network besides 127.0.0.1): staff
`/add-video`, 3 creators clicking **Vidéo**, real encodes and guards, links served by the bot's aiohttp
app, downloaded over HTTP. Writes `clipper1..3.mp4`, `contact_sheet.jpg` and `preview.json` (every
guard value, attempts, timings). Exit code 0 only if all three were delivered and every guard passed.

## Tests

```
python -m unittest discover tests        # or: python -m pytest tests
VIDEO_E2E=1 python -m pytest tests/test_video_uniquify.py   # real encode, needs ffmpeg + fpcalc
```
