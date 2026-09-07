import json
from pathlib import Path

import discord

from embed_utils import build_embeds_from_template
from embeds import MESSAGE_TEMPLATES, get_template
from publish_sync import (
    compute_fingerprint,
    save_channel_state,
    should_skip_publish,
    sync_embed_messages,
)

CONFIG_PATH = Path(__file__).parent / "channel_config.json"


def load_channel_config() -> dict[str, str]:
    """Retourne {channel_id: template_name}."""
    if not CONFIG_PATH.exists():
        return {}

    with CONFIG_PATH.open(encoding="utf-8") as f:
        data = json.load(f)

    return {str(k): v for k, v in data.get("channels", {}).items()}


def _load_special_publishers() -> dict:
    from content_generator import (
        CONTENT_GENERATOR_TEMPLATE,
        publish_content_generator_welcome,
    )
    from payout_proofs import PAYOUT_PROOFS_TEMPLATE, publish_payout_proofs
    from payout_submission import (
        PAYOUT_SUBMISSION_TEMPLATE,
        publish_payout_submission_welcome,
    )
    from registration import REGISTRATION_TEMPLATE, publish_registration_welcome

    return {
        REGISTRATION_TEMPLATE: publish_registration_welcome,
        PAYOUT_SUBMISSION_TEMPLATE: publish_payout_submission_welcome,
        PAYOUT_PROOFS_TEMPLATE: publish_payout_proofs,
        CONTENT_GENERATOR_TEMPLATE: publish_content_generator_welcome,
    }


async def publish_channel(
    channel: discord.TextChannel,
    template_name: str,
    bot_user: discord.ClientUser,
    *,
    force: bool = False,
) -> bool:
    """
    Publish or update channel content.
    Returns True if messages were sent/updated, False if skipped (unchanged).
    """
    fingerprint = compute_fingerprint(template_name)

    if await should_skip_publish(
        channel, template_name, fingerprint, bot_user, force=force
    ):
        return False

    special = _load_special_publishers()
    if template_name in special:
        message_ids = await special[template_name](channel, bot_user, force=force)
    else:
        template = get_template(template_name)
        if not template:
            raise ValueError(f"Template inconnu : {template_name}")

        embeds = build_embeds_from_template(template)
        message_ids = await sync_embed_messages(channel, bot_user, embeds)

    save_channel_state(channel.id, template_name, fingerprint, message_ids)
    return True


async def publish_template(
    channel: discord.TextChannel,
    template_name: str,
    bot_user: discord.ClientUser,
    *,
    force: bool = False,
) -> bool:
    """Alias pour compatibilité."""
    return await publish_channel(channel, template_name, bot_user, force=force)
