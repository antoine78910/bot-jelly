"""One-shot: dump metadata from samples/*.MOV for iPhone signature comparison."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from clip_assembler import resolve_ffprobe_bin
from video_metadata import probe_json

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


def main() -> None:
    samples = sorted({*SAMPLES.glob("*.MOV"), *SAMPLES.glob("*.mov")})
    ffprobe = resolve_ffprobe_bin()
    print("ffprobe:", ffprobe)
    print("files:", [p.name for p in samples])
    print()

    for path in samples:
        print("=" * 70)
        print(path.name, path.stat().st_size, "bytes")
        if ffprobe:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "quiet",
                    "-print_format",
                    "json",
                    "-show_format",
                    "-show_streams",
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            data = json.loads(proc.stdout)
        else:
            data = probe_json(path)

        fmt = data.get("format", {})
        print("format_name:", fmt.get("format_name"))
        print("duration:", fmt.get("duration"), "bitrate:", fmt.get("bit_rate"))
        print("FORMAT TAGS:")
        for key, value in sorted((fmt.get("tags") or {}).items()):
            print(f"  {key} = {value}")

        for stream in data.get("streams") or []:
            print(
                f"STREAM #{stream.get('index')} {stream.get('codec_type')} "
                f"codec={stream.get('codec_name')} profile={stream.get('profile')}"
            )
            if stream.get("codec_type") == "video":
                print(
                    f"  {stream.get('width')}x{stream.get('height')} "
                    f"pix_fmt={stream.get('pix_fmt')} "
                    f"color_space={stream.get('color_space')} "
                    f"color_primaries={stream.get('color_primaries')} "
                    f"color_transfer={stream.get('color_transfer')} "
                    f"color_range={stream.get('color_range')}"
                )
                print(
                    f"  avg_frame_rate={stream.get('avg_frame_rate')} "
                    f"r_frame_rate={stream.get('r_frame_rate')} "
                    f"time_base={stream.get('time_base')} "
                    f"nb_frames={stream.get('nb_frames')}"
                )
                print(
                    f"  bit_rate={stream.get('bit_rate')} "
                    f"bits_per_raw_sample={stream.get('bits_per_raw_sample')}"
                )
            if stream.get("codec_type") == "audio":
                print(
                    f"  sample_rate={stream.get('sample_rate')} "
                    f"channels={stream.get('channels')} "
                    f"channel_layout={stream.get('channel_layout')} "
                    f"bit_rate={stream.get('bit_rate')}"
                )
            tags = stream.get("tags") or {}
            if tags:
                print("  STREAM TAGS:")
                for key, value in sorted(tags.items()):
                    print(f"    {key} = {value}")


if __name__ == "__main__":
    main()
