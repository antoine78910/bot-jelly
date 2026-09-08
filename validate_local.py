"""Validate templates and assets without Discord (run before sync_all)."""
from __future__ import annotations

import sys

from embed_utils import build_embeds_from_template
from embeds import MESSAGE_TEMPLATES, get_template
from publisher import load_channel_config


def main() -> int:
    errors: list[str] = []
    mapping = load_channel_config()

    print(f"Channels configured: {len(mapping)}")
    for channel_id, template_name in mapping.items():
        label = f"{channel_id} -> {template_name}"
        try:
            if template_name == "payout_proofs":
                from payout_proofs import list_proof_image_paths

                images = list_proof_image_paths()
                if not images:
                    errors.append(f"{label}: no images in assets/payout_proofs/")
                else:
                    order = " -> ".join(p.name for p in images)
                    print(f"OK   {label} ({len(images)} images, order: {order})")
            elif template_name == "registration_welcome":
                from registration import registration_panel_fingerprint, welcome_embed

                welcome_embed()
                registration_panel_fingerprint()
                print(f"OK   {label} (registration panel)")
            elif template_name == "payout_submission_welcome":
                from payout_submission import (
                    payout_submission_panel_fingerprint,
                    submission_embed,
                )

                submission_embed()
                payout_submission_panel_fingerprint()
                print(f"OK   {label} (payout submission panel)")
            elif template_name == "content_generator_welcome":
                from carousel_assembler import assets_status
                from content_generator import (
                    content_generator_panel_fingerprint,
                    panel_embed,
                )

                panel_embed()
                content_generator_panel_fingerprint()
                st = assets_status()
                clip_note = (
                    f"carousel assets avatars={st['avatars']} photos={st['photos']} "
                    f"captions={st['captions']}"
                )
                if st["avatars"] < 1 or st["photos"] < 1:
                    errors.append(f"{label}: missing files in carousel/assets/")
                elif not st["captions"]:
                    errors.append(f"{label}: missing carousel/carousel_captions.txt")
                print(f"OK   {label} (content generator — {clip_note})")
            else:
                template = get_template(template_name)
                if not template:
                    errors.append(f"{label}: unknown template")
                    continue
                embeds = build_embeds_from_template(template)
                if not embeds:
                    errors.append(f"{label}: no embeds built")
                else:
                    total = sum(len(e.description or "") for e in embeds)
                    print(f"OK   {label} ({len(embeds)} embed(s), {total} chars)")
        except Exception as exc:
            errors.append(f"{label}: {exc}")

    print()
    if errors:
        print("FAILED:")
        for err in errors:
            print(f"  - {err}")
        return 1

    print("All templates OK locally.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
