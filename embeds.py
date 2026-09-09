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
    "payout_proofs": "#💰-payment-proof",
    "music": "#🎵-sounds",
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
    music = channel_mention("music")

    return (
        "Bienvenue sur **Jellyjob** 👋\n"
        "Tu es à quelques étapes d’être payé pour poster du contenu.\n\n"
        "💰 **Ce que tu gagnes**\n"
        "• 1 $ / 1k vues\n\n"
        "🎯 **Tes 4 premières actions**\n\n"
        f"Crée un nouveau compte Instagram en suivant {acc}\n\n"
        f"Inscris-toi et renseigne tes infos de paiement sur {apply}\n\n"
        f"Fais chauffer ton compte pendant 3 jours {warmup}\n\n"
        f"Commence à poster avec {content}\n\n"
        f"⭐ Ajoute tous les sons en favoris dans {music} "
        "pour les retrouver au moment de poster tes carrousels."
    )


def payouts_description() -> str:
    payout_ch = channel_mention("payout_submission")

    return (
        "1 $ / 1k vues\n"
        "Minimum pour un paiement : 100k vues\n"
        "Minimum d’audience française : 20 %\n\n"
        "Les paiements se font par virement bancaire, PayPal ou crypto.\n"
        "Les paiements sont effectués toutes les deux semaines.\n\n"
        f"Demander un paiement ? Va sur {payout_ch}"
    )


MESSAGE_TEMPLATES: dict[str, dict] = {
    "start_here": {
        "title": "💸 JELLY POSTING — COMMENCE ICI",
        "description": "",  # rempli dynamiquement via get_template()
        "color": EMBED_COLOR,
    },
    "payouts": {
        "title": "💸 JELLY POSTING — PAIEMENTS",
        "description": "",
        "color": EMBED_COLOR,
    },
    "account_setup": {
        "title": "💸 JELLY POSTING — CRÉATION DE COMPTE",
        "description": (
            "Crée un tout nouveau compte Instagram.\n\n"
            "**Pseudo :** job / carrière / étudiant + prénom français\n"
            "Exemples : careerwithmarc, careerwithlea, marc.jobs, marc.jobtips, "
            "lea.jobtips, studentwork.lea, jobs.etudiant, camille.carriere, "
            "lucas.jobs, jobsearch.lea\n\n"
            "**Nom :** un prénom français. Exemples : Marc, Léa, Camille, Lucas\n\n"
            "**Photo de profil :**\n"
            "https://drive.google.com/drive/folders/1JBwgJrwDsonIXMvyC_LXXJqInpHchuID?usp=sharing\n\n"
            "**Bio :**\n"
            "Je t’aide à trouver un job plus vite avec l’IA 👀\n"
            "Commence sur jellyjob.co\n\n"
            "**Lien en bio :**\n"
            "https://jellyjob.co\n\n"
            "Ajoute le lien seulement une fois que tu as dépassé 30k vues sur le compte.\n\n"
            "**Fréquence de posts :**\n\n"
            "Poste 1 à 3 fois par jour. Une fois les essais Reels débloqués, "
            "poste 1 à 3 fois par jour là aussi.\n\n"
            "**⏰ Horaires de publication :**\n\n"
            "On cible la **France**. Pour une audience FR, poste aux heures de pointe "
            "françaises. Évite les soirées Asie.\n\n"
            "Pour tes 1 à 3 posts par jour, publie entre **18h et minuit (heure de Paris)**.\n\n"
            "Si tu es en Asie, ça correspond souvent à la fin de soirée / nuit chez toi.\n\n"
            "**✉️ Légende & hashtags**\n\n"
            "Utilise une de ces premières lignes (première ligne de la légende) :\n\n"
            "**Option 1 — Job**\n"
            "Commente « JOB » pour essayer → envoie le lien jellyjob.co\n\n"
            "Hashtags (3 à 5) : #rechercheemploi #jobetudiant #alternance #cv "
            "#premieremploi #conseilscv #linkedin #intelligenceartificielle\n\n"
            "**Option 2 — CV**\n"
            "Commente « CV » pour la méthode → envoie le lien jellyjob.co"
        ),
        "color": EMBED_COLOR,
    },
    "warmup": {
        "title": "💸 JELLY POSTING — CHAUFFE DU COMPTE",
        "description": (
            "💡 **Guide de chauffe**\n\n"
            "Un compte froid = zéro vues. Une bonne chauffe et tu peux décoller "
            "en une semaine de posts réguliers.\n\n"
            "Le but : qu’Instagram te voie comme une vraie personne, pas un bot, "
            "ET te match avec la bonne audience en interagissant avec du contenu "
            "similaire à ce que TU vas poster.\n\n"
            "Règle importante : n’interagis qu’avec du contenu du MÊME style que "
            "cette campagne. Si tes futurs posts ne correspondent pas à ce que tu "
            "regardes, Instagram se trompe d’audience et ne pousse pas tes reels.\n\n"
            "────────\n\n"
            "**JOUR 1 — Création du compte (10–15 min)**\n\n"
            "• Crée le compte avec Gmail ou iCloud\n"
            "• Ajoute un numéro + active la 2FA + fais le selfie de vérif dans Réglages\n"
            "• Ajoute une photo de profil, un nom et une bio\n"
            "• Dans la barre de recherche Instagram, cherche : Recherche emploi, "
            "Job étudiant, CV, Alternance, LinkedIn, Premier job, IA emploi\n"
            "• Scroll le feed Reels 10–15 min (uniquement lié à ce que tu vas poster, "
            "uniquement du **CONTENU FRANÇAIS**)\n"
            "• Like quelques reels, follow 1 à 3 comptes dans ce style\n"
            "• Comporte-toi comme une vraie personne — ne spam pas les likes\n\n"
            "────────\n\n"
            "**JOUR 2 — Engagement léger (30 min au total)**\n\n"
            "Une session de 30 min ou deux sessions de 15 min.\n\n"
            "• Scroll plus de reels dans le style de la campagne\n"
            "• Like plus de reels\n"
            "• Laisse 2–3 vrais commentaires sur des reels que tu aimes\n"
            "• Follow 3 à 5 comptes de plus dans ce style\n"
            "• Poste 1 story (photo, citation, sticker…)\n\n"
            "Ton feed Reels doit maintenant montrer surtout du contenu proche de ce que tu vas poster.\n\n"
            "────────\n\n"
            "**JOUR 3 — Chauffe finale (45–60 min au total)**\n\n"
            "Répartis sur une ou plusieurs sessions.\n\n"
            "• Mêmes actions que le jour 2 — scroll, like, commente, follow\n"
            "• Reste uniquement sur le style de la campagne\n"
            "• Le compte est prêt à poster\n\n"
            "────────\n\n"
            "**JOUR 4 — Premier post**\n\n"
            "• Passe 15 min sur IG normalement d’abord (scroll + quelques likes)\n"
            "• Poste ton premier reel / carrousel\n"
            "• Continue 10–15 min de scroll de chauffe chaque jour ensuite\n\n"
            "Important : Instagram récompense un bon « Trust Score ». Ça vient "
            "d’une utilisation humaine tous les jours — pas juste poster et disparaître.\n\n"
            "────────\n\n"
            "**JOUR 5 ET APRÈS**\n\n"
            "• 10–15 min de chauffe\n"
            "• Poste 1 à 3 reels / carrousels, avec au moins 2 heures entre chaque post\n\n"
            "C’est tout. Tiens ce rythme et les vues arrivent."
        ),
        "color": EMBED_COLOR,
    },
    "dm_automation": {
        "title": "💸 JELLY POSTING — AUTOMATION DM",
        "description": (
            "L’automation DM est obligatoire sur chaque post pour être payé.\n\n"
            "**C’est quoi l’automation DM ?**\n\n"
            "Quand quelqu’un commente un mot-clé sous un post Instagram, "
            "il reçoit automatiquement un lien en DM.\n\n"
            "Pour l’utiliser, passe le compte en compte professionnel. "
            "Réglages Instagram → type de compte → compte professionnel / entreprise.\n\n"
            "Va sur Superprofile, inscris-toi gratuitement, reste sur l’offre free.\n\n"
            "Sur chaque post, tu dois envoyer les liens via l’automation DM.\n\n"
            "Mots-clés : **JOB**, **CV** ou **IA** selon le post.\n\n"
            "**Légende :**\n"
            "Commente « JOB » pour essayer !\n"
            "Commente « CV » pour la méthode\n"
            "Commente « IA » pour le lien\n"
            "(le mot-clé doit matcher les liens configurés ci-dessous)\n\n"
            "**Le bouton « Open Link »** envoie ce lien :\n"
            "https://jellyjob.co\n\n"
            "**Le bouton « CV » / méthode** envoie aussi :\n"
            "https://jellyjob.co"
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
            "Bot en ligne. Utilise `!refresh` dans un salon pour mettre à jour "
            "son message (pas automatique au redémarrage)."
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
