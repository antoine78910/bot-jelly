import discord

# Limites Discord
MESSAGE_CHAR_LIMIT = 2000
EMBED_DESCRIPTION_LIMIT = 4096
EMBED_FIELD_VALUE_LIMIT = 1024
EMBED_FIELD_NAME_LIMIT = 256
MAX_EMBEDS_PER_MESSAGE = 10


def split_text(text: str, limit: int) -> list[str]:
    """Découpe un texte sans dépasser limit (privilégie les sauts de ligne)."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit

        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")

    return chunks


def chunk_message(text: str) -> list[str]:
    return split_text(text, MESSAGE_CHAR_LIMIT)


def build_embeds_from_template(template: dict) -> list[discord.Embed]:
    """Construit 1 à N embeds (descriptions et champs découpés si besoin)."""
    color = template.get("color", 0x9B59B6)
    title = template.get("title")
    footer = template.get("footer")
    fields = template.get("fields", [])
    description = template.get("description", "")

    embeds: list[discord.Embed] = []

    if fields:
        current = discord.Embed(title=title, color=color)

        for field in fields:
            name = str(field.get("name", ""))[:EMBED_FIELD_NAME_LIMIT]
            value = str(field.get("value", ""))
            inline = bool(field.get("inline", False))

            for value_chunk in split_text(value, EMBED_FIELD_VALUE_LIMIT):
                if len(current.fields) >= 25:
                    embeds.append(current)
                    current = discord.Embed(color=color)
                current.add_field(name=name, value=value_chunk, inline=inline)

        if current.fields or current.title:
            embeds.append(current)

    if description:
        desc_chunks = split_text(description, EMBED_DESCRIPTION_LIMIT)
        for i, chunk in enumerate(desc_chunks):
            chunk_title = title if i == 0 and not embeds else None
            embed = discord.Embed(title=chunk_title, description=chunk, color=color)
            embeds.append(embed)

    if not embeds:
        embeds.append(discord.Embed(title=title, description="(vide)", color=color))

    if footer:
        embeds[-1].set_footer(text=str(footer)[:2048])

    return embeds[:MAX_EMBEDS_PER_MESSAGE]
