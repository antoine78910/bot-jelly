# Clip assets — where to put your files

The bot assembles **hook + body + music + optional text hook** with random visual tweaks (color, scale, rotation, end cut) so each export looks slightly different.

## Folder structure

```
assets/clips/
├── hooks/              ← Visual hook clips (3–8 sec b-roll intros)
│   ├── hook_01.mp4
│   └── hook_02.mp4
├── hooks_text/         ← Text-hook overlays (PNG with transparency, or short MP4)
│   └── lines/          ← One PNG per hook line (random pick each export)
│       ├── the_art_of_jelly.png
│       └── ai_on_steroids.png
├── body/               ← Main talking-head / demo footage (your core clip)
│   ├── body_01.mp4
│   └── body_02.mp4
├── music/              ← Background tracks (MP3 or M4A)
│   ├── beat_01.mp3
│   └── beat_02.mp3
└── output/             ← Generated files (temporary, auto-deleted after upload)
```

## Formats

| Folder      | Formats        | Tips |
|------------|----------------|------|
| `hooks/`   | `.mp4`, `.mov` | Vertical 9:16 preferred (1080×1920). 3–8 seconds. |
| `body/`    | `.mp4`, `.mov` | Main content; can be 30–90s. |
| `music/`   | `.mp3`, `.m4a`, `.wav` | Mixed at ~42% volume under your body audio. Hook segments are always silent. |
| `hooks_text/` | `.png` (transparent) or `.mp4` | Shown **upper-mid screen** for the full hook segment only. |

## How assembly works

1. Random **visual hook** + random **body** + random **music**
2. Random **text hook** from any PNG in `hooks_text/` (currently `hooks_text/lines/`)
3. Variations applied on the final video:
   - Color: saturation / brightness / contrast jitter
   - Scale: 100% → up to 105%, then center-crop to 1080×1920
   - Rotation: ±0.5°
   - **Mini cut:** trims 0.08–0.25s off the end
   - Text overlay **upper-mid screen** for the **entire hook** (hidden when the body starts)

## Requirements

- [FFmpeg](https://ffmpeg.org/download.html) installed and on your `PATH`
- At least **1 file** in `hooks/`, **1** in `body/`, **1** in `music/`
- Text hooks are optional

## Discord

Discord **Generate Content** now builds 4-slide carousels from `carousel/`
(hook + Google + Jellyjob + recap), not these video clips.
