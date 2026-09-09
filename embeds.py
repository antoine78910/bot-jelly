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
            "⚠️ Quand tu cherches des mots-clés pour chauffer tes comptes, cherche "
            "**dans la langue dans laquelle tu vas poster** (ici : **français**).\n\n"
            "**C’est quoi JellyJob ?**\n"
            "App de recherche d’emploi façon **Tinder** : tu swipe les offres, "
            "JellyJob **postule pour toi** et adapte ton **CV** + ta **lettre de "
            "motivation**. Site : **jellyjob.co**\n\n"
            "**Qui tu es**\n"
            "Quelqu’un qui cherche un job, qui déteste postuler, ou qui est à bout "
            "du process. Parle de trucs relatable : 50+ candidatures sans réponse, "
            "des heures sur LinkedIn, les lettres de motivation, le CV à refaire, "
            "le ghosting, le premier job, vouloir quitter un job détesté.\n\n"
            "Le meilleur contenu est **perso, drôle, un peu dramatique**.\n"
            "> POV : je postule comme si je swipe sur Tinder.\n"
            "> La recherche d’emploi était si nulle que j’ai trouvé un moyen de "
            "faire postuler mon téléphone.\n\n"
            "Parle **d’abord de ta galère**, puis présente JellyJob comme une "
            "**découverte** — pas une pub. Le viewer doit se dire « wait, ça existe ? », "
            "pas « c’est clairement une sponso ».\n\n"
            "────────\n\n"
            "**Installer JellyJob**\n"
            "1. On t’envoie un **lien d’invitation** → inscription directe. Lien "
            "expiré ? Envoie-nous un message.\n"
            "2. Crée ton compte : l’invitation = **accès créateur gratuit**.\n"
            "3. **Mode démo** par défaut (facile à filmer). Tu peux le couper dans "
            "**Réglages IA**.\n"
            "4. Dans **Réglages IA**, choisis le **thème** du script (stage, jeunes "
            "diplômés…) **avant** de filmer.\n"
            "5. Explore l’app avant de filmer : swipe, auto-apply, CV / lettre "
            "adaptés. Tes démos doivent paraître naturelles.\n\n"
            "────────\n\n"
            "**Setup des comptes**\n"
            "1. Crée un **nouvel email** (Gmail, Yahoo, Outlook, iCloud) pour "
            "TikTok + Instagram. C’est perso — tu ne partages pas le mot de passe.\n"
            "2. Crée **TikTok ET Instagram**. Ça doit ressembler à une **vraie "
            "personne** qui documente sa recherche d’emploi — **pas** un compte promo.\n\n"
            "Exemples : careerwithlea, leasjobhunt, lifewithlea, leas9to5, "
            "corporategirllea, jobhuntingwithlea. Pas de « jellyjob » dans le "
            "pseudo. Pas de règle stricte, sois créatif.\n\n"
            "**Bio** — ne mets **pas** de bio avant tes vidéos de chauffe.\n"
            "Ex. : ma carrière un jour à la fois 💻 · en train de chercher mon "
            "prochain job 🤞 · just a girl qui survit au marché de l’emploi\n\n"
            "**Photo** — ne mets **pas** de PDP avant tes vidéos de chauffe. "
            "Photo **naturelle de ton visage**. Pas de logo, stock, IA, visuel pub.\n\n"
            "────────\n\n"
            "**CHAUFFE — OBLIGATOIRE**\n"
            "Les **24–72 premières heures** comptent. **Ne poste RIEN** juste après "
            "la création. Chauffe **~3–4 jours** pour entrer dans la niche "
            "**recherche d’emploi + carrière + bureau + vie pro**.\n\n"
            "**JOUR 1 — 45 à 60 min MINIMUM par compte** "
            "(ex. 15 min matin / 15 aprem / 15–30 soir)\n\n"
            "1. **Cherche + regarde** : recherche emploi, conseils CV, lettre de "
            "motivation, postuler, marché du travail, je trouve pas de job, "
            "entretien, jeune diplômé, premier job, vie de bureau, je déteste mon "
            "job, reconversion, humour corporate, 9h-18h…\n"
            "Regarde les vidéos **jusqu’au bout**. Pas de scroll spam.\n\n"
            "2. Sur le **FYP / Explore**, mélange **copier le lien → repost → "
            "enregistrer → commenter → like**. Pas la même séquence à chaque fois. "
            "**≥ 20 vidéos** pertinentes dans la journée. **Pas de spam.**\n"
            "Astuce : repost surtout les vidéos **virales (500k+ vues)** de la niche.\n\n"
            "**Follows jour 1 : 5 comptes max.** Pas de mass follow.\n"
            "Après 20–30 min, le FYP doit montrer de la carrière. Sinon : re-cherche, "
            "re-regarde, re-engage.\n\n"
            "**JOURS 2–3 — encore 45–60 min / jour / compte**\n"
            "Moins de recherche manuelle, plus d’engagement FYP. Toujours ≥ 20 vidéos. "
            "Follow **2–5 comptes / jour**. Avant de poster : **~15–20+ comptes** "
            "suivis dans la niche.\n\n"
            "**Avant le 1er post JellyJob** : 15–20+ comptes suivis · 20+ vidéos "
            "enregistrées · plusieurs reposts + vrais commentaires · FYP / Explore "
            "surtout carrière / emploi.\n\n"
            "**JOUR 4 — vérif avant de poster**\n"
            "Envoie dans le Discord, pour **TikTok et Instagram** :\n"
            "• capture du temps d’écran\n"
            "• **écran 30 secondes** de ton FYP (TikTok) / Explore-Reels (Instagram)\n"
            "On valide que **les deux** comptes sont bien chauffés.\n\n"
            "────────\n\n"
            "**Avant chaque post** : scroll un peu la niche. Mélange **partager → "
            "enregistrer → commenter → like**. Vidéos jusqu’au bout (2x OK). "
            "Espace les commentaires. Continue 2–3 follows / jour. Continue "
            "d’engager **après** avoir posté.\n\n"
            "**Posts de chauffe (après validation)**\n"
            "**1 vidéo / jour pendant 3 jours** (jours 4, 5 et 6). Pas de promo : "
            "clips **6–7 secondes** avec texte à l’écran, pour pousser le compte "
            "vers la bonne audience.\n\n"
            "**Mindset contenu**\n"
            "Problème → histoire relatable → découverte → démo → résultat.\n"
            "« J’ai postulé à 37 jobs cette semaine, je perds la tête. »\n"
            "« Puis j’ai trouvé une app qui transforme la recherche d’emploi en Tinder. »\n"
            "Montre le swipe. « Quand je swipe une offre, ça postule ET ça adapte "
            "mon CV + ma lettre. »\n"
            "Le viewer doit se dire **« j’en ai besoin »**, pas **« je regarde une pub »**."
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
