"""Discord-facing wrapper around the copied carousel generator."""

from __future__ import annotations

import random
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

CAROUSEL_ROOT = Path(__file__).parent / "carousel"


class CarouselAssemblyError(Exception):
    pass


@dataclass
class CarouselRecipe:
    pair_style: str
    augment: str
    color: str
    avatar_name: str
    photo_names: list[str]
    hook_preview: str
    job_dir: Path
    slides: list[Path]


def recipe_summary(recipe: CarouselRecipe) -> str:
    photos = ", ".join(f"`{name}`" for name in recipe.photo_names) or "—"
    return (
        f"Accroche : {recipe.hook_preview}\n"
        f"Style `{recipe.pair_style}` · effet `{recipe.augment}` · `{recipe.color}`\n"
        f"Avatar `{recipe.avatar_name}` · photos {photos}"
    )


def assets_status() -> dict:
    from carousel.generate_carousel import (
        AVATARS_DIR,
        CAPTIONS_FILE,
        PROJECT_ROOT,
        _collect_images,
        _collect_photos,
        _list_avatar_packs,
    )

    packs = _list_avatar_packs()
    # Count every pack so new folders under avatars/ are detected.
    avatars = _collect_images(AVATARS_DIR) if AVATARS_DIR.is_dir() else []
    photos = _collect_photos(None)
    return {
        "root": str(PROJECT_ROOT),
        "avatars": len(avatars),
        "avatar_packs": packs,
        "photos": len(photos),
        "captions": CAPTIONS_FILE.is_file(),
    }


def assemble_carousel(*, seed: int, color: str = "pink") -> CarouselRecipe:
    from carousel.generate_carousel import (
        DEFAULT_AVATAR_PACK,
        DEFAULT_COLOR,
        TIKTOK_STYLE,
        _pick_avatar_pack,
        generate_type1_carousel,
    )

    status = assets_status()
    if status["avatars"] < 1 or status["photos"] < 1:
        raise CarouselAssemblyError(
            "Assets carrousel manquants. Ajoute des fichiers dans `carousel/assets/avatars` et "
            f"`carousel/assets/photos` sur la machine du bot (vérifié : `{status['root']}` — "
            f"avatars={status['avatars']}, photos={status['photos']})."
        )
    if not status["captions"]:
        raise CarouselAssemblyError(
            f"Fichier de légendes manquant : `{status['root']}/carousel_captions.txt`."
        )

    theme = color if color in TIKTOK_STYLE else DEFAULT_COLOR
    job_dir = CAROUSEL_ROOT / "output" / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=True)

    random.seed(seed)
    avatar_pack = _pick_avatar_pack() if status.get("avatar_packs") else DEFAULT_AVATAR_PACK

    try:
        build = generate_type1_carousel(
            color=theme,
            set_index=0,
            output_dir=job_dir,
            quiet=True,
            avatar_pack=avatar_pack,
        )
    except FileNotFoundError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise CarouselAssemblyError(str(exc)) from exc
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise CarouselAssemblyError(f"Rendu du carrousel échoué : {exc}") from exc

    slides = list(build.slides)
    if len(slides) < 4:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise CarouselAssemblyError("Le rendu a produit moins de 4 slides.")

    return CarouselRecipe(
        pair_style=build.pair_style,
        augment=build.augment,
        color=build.color,
        avatar_name=build.avatar_name,
        photo_names=list(build.photo_names),
        hook_preview=build.hook_preview,
        job_dir=job_dir,
        slides=slides,
    )


def cleanup_carousel_artifacts(job_dir: Path) -> None:
    if not job_dir.is_dir():
        return
    if job_dir.parent.resolve() != (CAROUSEL_ROOT / "output").resolve():
        return
    shutil.rmtree(job_dir, ignore_errors=True)
