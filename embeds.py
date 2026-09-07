"""Templates de messages — ajoute/modifie les entrées ici."""

import json
from pathlib import Path

EMBED_COLOR = 0x9B59B6
CONFIG_PATH = Path(__file__).parent / "channel_config.json"

# Labels affichés si l'ID du salon n'est pas dans channel_config.json
CHANNEL_LABELS = {
    "account_setup": "#👤-account-setup",
    "apply": "#🚀-apply",
    "warmup": "#🔥-warmup",
    "content_bot": "#🎬-content-bot",
    "dm_automation": "#📩-dm-automation",
    "payout_submission": "#💳-payout-submission",
}


def _load_channel_links() -> dict[str, str]:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    return {str(k): str(v) for k, v in data.get("channel_links", {}).items() if v}


def channel_mention(link_key: str) -> str:
    """Mention cliquable <#id> ou label #salon si ID manquant."""
    cid = _load_channel_links().get(link_key, "").strip()
    if cid.isdigit():
        return f"<#{cid}>"
    return CHANNEL_LABELS.get(link_key, f"#{link_key}")


def start_here_description() -> str:
    acc = channel_mention("account_setup")
    apply = channel_mention("apply")
    warmup = channel_mention("warmup")
    content = channel_mention("content_bot")

    return (
        "Welcome to **Jelly** 👋\n"
        "You're just a few steps away from getting paid to post content.\n\n"
        "💰 **What You'll Earn**\n"
        "• $300 per 1M views\n\n"
        "🎯 **Your First 4 Actions**\n\n"
        f"Create a brand new Instagram account following {acc}\n\n"
        f"Register your account and enter payout details at {apply}\n\n"
        f"Warm up your account for 3 days {warmup}\n\n"
        f"Start posting using {content}"
    )


def payouts_description() -> str:
    payout_ch = channel_mention("payout_submission")

    return (
        "$300 per 1M views\n"
        "Minimum views for payout: 100k views\n"
        "Minimum T1 audience for payout: 20%\n\n"
        "Payouts are made by Bank Transfer, Paypal, or Cryptocurrency.\n"
        "Payouts are made bi-weekly.\n\n"
        f"Request payout? Go to {payout_ch}"
    )


MESSAGE_TEMPLATES: dict[str, dict] = {
    "start_here": {
        "title": "💸 JELLY CLIPPING — START HERE",
        "description": "",  # rempli dynamiquement via get_template()
        "color": EMBED_COLOR,
    },
    "payouts": {
        "title": "💸 JELLY CLIPPING — PAYOUTS",
        "description": "",
        "color": EMBED_COLOR,
    },
    "account_setup": {
        "title": "💸 JELLY CLIPPING — ACCOUNT SETUP",
        "description": (
            "Create a brand new Instagram account.\n\n"
            "**Username:** ai + American male name\n"
            "Examples: aibynick, aimoneynick, nickaiworkflow, nickusesai, "
            "nickteachesai, nickcreatesai, brysondoesai, aibrysonnnnn\n\n"
            "**Name:** Your American male name. Example: Nick\n\n"
            "**Profile Picture:**\n"
            "https://drive.google.com/drive/folders/1JBwgJrwDsonIXMvyC_LXXJqInpHchuID?usp=sharing\n\n"
            "**Bio:**\n"
            "Helping you scale your brand with AI👀\n"
            "Start generating now with @jelly.ai\n\n"
            "**Bio link:**\n"
            "https://jelly.io/\n\n"
            "Only add the link once you have earned over 30k views on your account.\n\n"
            "**Post frequency:**\n\n"
            "Post 1-3 times a day. Once you unlock trials reels, post 1-3 times a day there too.\n\n"
            "**⏰ Posting time:**\n\n"
            "In order to have a majority US audience, you must post at US peak time. "
            "Avoid posting at Asian evenings at all costs.\n\n"
            "For the 1-3 posts per day, post between 6pm-12am ET time. "
            "ET time is American Eastern Time.\n\n"
            "If you are based in Asia, post in your morning. The earlier the better.\n\n"
            "**✉️ Caption & Hashtags**\n\n"
            "Use one of these opening lines (first line of the caption):\n\n"
            "**Option 1 — UGC**\n"
            "Comment \"UGC\" to try → send the tool link\n\n"
            "Hashtags (use 3–5): #socialmediamarketing #marketingtool #aitoolsforbusiness "
            "#moneymindset #growyourbusiness #marketingtips #adcreative #marketingstrategy\n\n"
            "**Option 2 — Workflow**\n"
            "Comment \"Workflow\" to get the full workflow → send the public workflow link"
        ),
        "color": EMBED_COLOR,
    },
    "warmup": {
        "title": "💸 JELLY CLIPPING — WARM UP",
        "description": (
            "💡 **Warm-Up Guide**\n\n"
            "A cold account = zero views. Warm up properly and you'll go viral "
            "within a week of consistent posting.\n\n"
            "The goal: make Instagram see you as a real person, not a bot, AND match "
            "you with the right audience by engaging with content similar to what "
            "YOU'LL be posting.\n\n"
            "Important rule: only engage with content that's the SAME style as what "
            "you'll be posting in this campaign. If your future posts won't match "
            "the content you watch, Instagram gets confused and won't push your reels.\n\n"
            "────────\n\n"
            "**DAY 1 — Account setup (10–15 min)**\n\n"
            "• Create the account using Gmail or iCloud email\n"
            "• Add phone number + enable 2FA + complete the verification selfie in Settings\n"
            "• Add a profile picture, name, and bio\n"
            "• Go to the search bar on Instagram, search any of the following: "
            "Online Business, Online Money, Claude, Claude Higgsfield, Entrepreneur\n"
            "• One best account to interact with that posts exactly what you need to "
            "be posting: @idanbuild\n"
            "• Open Instagram and scroll the Reels feed for 10–15 min (related to what "
            "you are posting only, only engage with **AMERICAN CONTENT**)\n"
            "• Like a few reels, follow 1–3 accounts in that style\n"
            "• Act like a normal person — don't spam like\n\n"
            "────────\n\n"
            "**DAY 2 — Light engagement (30 min total)**\n\n"
            "Can be one 30-min session or two 15-min sessions.\n\n"
            "• Scroll more reels in your campaign's content style\n"
            "• Like more reels\n"
            "• Drop 2–3 genuine comments on reels you actually like\n"
            "• Follow 3–5 more accounts in your style\n"
            "• Post 1 story (anything — a photo, a quote, a sticker)\n\n"
            "Your reels feed should now mostly show content similar to what you'll be posting.\n\n"
            "────────\n\n"
            "**DAY 3 — Final warm-up (45–60 min total)**\n\n"
            "Spread across one or multiple sessions.\n\n"
            "• Same actions as Day 2 — scroll, like, comment, follow\n"
            "• Keep engaging with content in your campaign's style only\n"
            "• Account is now ready to post\n\n"
            "────────\n\n"
            "**DAY 4 — First post**\n\n"
            "• Spend 15 min using IG normally first (scroll + like a few)\n"
            "• Post your first reel\n"
            "• Keep doing 10–15 min of warm-up scrolling every day from now on\n\n"
            "Important: Instagram rewards accounts with a high \"Trust Score.\" That comes "
            "from using the app like a real human every single day — not just posting and "
            "disappearing.\n\n"
            "────────\n\n"
            "**DAY 5 & BEYOND**\n\n"
            "• 10–15 min warm-up\n"
            "• Post 1-3 reels with minimum 2 hours in between posts\n\n"
            "That's it. Stick to this and your account will start pushing views fast."
        ),
        "color": EMBED_COLOR,
    },
    "dm_automation": {
        "title": "💸 JELLY CLIPPING — DM AUTOMATION",
        "description": (
            "DM automation is required to be set up on every video for payout.\n\n"
            "**What is DM automation?**\n\n"
            "When people comment a keyword in comments on an Instagram post, they "
            "automatically receive a link in their DMs.\n\n"
            "In order to use DM automation, you must set your account to a professional "
            "account. Go to settings on Instagram, search account type, change to "
            "business account.\n\n"
            "Go to Superprofile, sign up for free, only use the free plan.\n\n"
            "On every post you must use DM automation to send out links.\n\n"
            "For keywords, select **AI**, **workflow**, or **ugc** depending on the video.\n\n"
            "**Caption:**\n"
            "Comment \"AI\" to try it out!\n"
            "Comment \"UGC\" to try\n"
            "Comment \"Workflow\" for the full workflow\n"
            "(match the keyword to the links you set up below)\n\n"
            "**The \"Open Link\" button** will send out this link:\n"
            "https://jelly.io\n\n"
            "**The \"Workflow\" button** will send out this link:\n"
            "https://www.jelly.io/workflow/public/community%3Ae7db5a09-667d-4cda-913c-68904f581780"
        ),
        "color": EMBED_COLOR,
    },
    "welcome": {
        "title": "👋 Bienvenue",
        "description": (
            "Bienvenue sur le serveur.\n\n"
            "Lis les règles et présente-toi dans le salon dédié."
        ),
        "color": EMBED_COLOR,
    },
    "rules": {
        "title": "📋 Règles",
        "fields": [
            {"name": "1. Respect", "value": "Sois respectueux envers tout le monde."},
            {"name": "2. Pas de spam", "value": "Pas de pub ni de spam."},
            {"name": "3. Contenu", "value": "Pas de contenu illégal ou NSFW."},
        ],
        "color": EMBED_COLOR,
    },
    "bot_online": {
        "title": "✅ Jelly est en ligne",
        "description": (
            "Bot online. Use `!refresh` in a channel to update its message "
            "(does not run automatically on restart)."
        ),
        "color": EMBED_COLOR,
    },
}


def get_template(name: str) -> dict | None:
    """Retourne une copie du template (start_here = mentions à jour)."""
    template = MESSAGE_TEMPLATES.get(name)
    if not template:
        return None
    result = dict(template)
    if name == "start_here":
        result["description"] = start_here_description()
    elif name == "payouts":
        result["description"] = payouts_description()
    return result
