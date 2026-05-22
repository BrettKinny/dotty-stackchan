#!/usr/bin/env python3
"""
provision.py — declaratively build out "The Dotty Project" Discord server.

Run this once (or any number of times — it is idempotent) after you have:
  1. created an empty Discord server,
  2. enabled the Community feature (Server Settings -> Enable Community),
  3. invited this bot with Administrator permission.

It creates the role set, the category/channel layout, and the permission
overwrites described in community/discord/README.md. Re-running only fills in
what is missing — it never deletes channels and never creates duplicates.

Environment (see .env.example):
  DISCORD_BOT_TOKEN   bot token from the Discord Developer Portal (required)
  DISCORD_GUILD_ID    target server ID (optional — if unset and the bot is in
                      exactly one server, that server is used)
"""

from __future__ import annotations

import os
import sys

import discord

# Optional: load a local .env file if python-dotenv is installed.
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

# ----------------------------------------------------------------------------
# Layout spec — edit THIS, not the logic below.
# ----------------------------------------------------------------------------

# Roles, ordered top-to-bottom. `color` is a hex int; `hoist` shows the role
# as its own group in the member list.
ROLES = [
    {"name": "Maintainer",  "color": 0xE91E63, "hoist": True,  "mentionable": True},
    {"name": "Contributor", "color": 0x3498DB, "hoist": True,  "mentionable": True},
    {"name": "Helper",      "color": 0x2ECC71, "hoist": True,  "mentionable": True},
    {"name": "Builder",     "color": 0xF1C40F, "hoist": False, "mentionable": True},
]

# Channel kinds.
TEXT, VOICE, FORUM, NEWS = "text", "voice", "forum", "news"
RULES, UPDATES = "rules", "updates"  # adopt the Community-feature channels

# Each category: (name, [(channel_name, kind, topic), ...]).
LAYOUT = [
    ("INFORMATION", [
        ("welcome-and-rules", RULES,   "Start here — the house rules for The Dotty Project."),
        ("announcements",     NEWS,    "Project news, releases, and updates."),
        ("mod-updates",       UPDATES, "Discord's Community updates (mods only)."),
    ]),
    ("COMMUNITY", [
        ("general",         TEXT,  "General chat about Dotty and StackChan."),
        ("introductions",   TEXT,  "New here? Say hello."),
        ("show-your-dotty", FORUM, "Show off your build — photos, demos, mods."),
        ("off-topic",       TEXT,  "Everything else."),
    ]),
    ("BUILD & SUPPORT", [
        ("setup-help",             FORUM, "Stuck on setup? Open a thread."),
        ("hardware-and-firmware",  TEXT,  "ESP32-S3, M5Stack, wiring, flashing."),
        ("voice-and-self-hosting", TEXT,  "xiaozhi-server, ASR/TTS, Docker, the bridge."),
    ]),
    ("DEVELOPMENT", [
        ("github-feed",   TEXT, "Automated commit / PR / issue feed from the repo."),
        ("contributing",  TEXT, "Dev discussion and contribution coordination."),
        ("feature-ideas", TEXT, "Propose and discuss new features."),
    ]),
    ("VOICE", [
        ("General",       VOICE, None),
        ("Build Hangout", VOICE, None),
    ]),
]

# Channels the @everyone role may read but not post in.
READ_ONLY = {"welcome-and-rules", "announcements", "github-feed"}

# ----------------------------------------------------------------------------
# Logic
# ----------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"  {msg}")


def find_channel(guild: discord.Guild, name: str):
    """Find a channel by name across the whole guild (case-insensitive)."""
    lname = name.lower()
    return discord.utils.find(lambda c: c.name.lower() == lname, guild.channels)


def overwrites_for(name: str, guild: discord.Guild, roles: dict):
    """Build the permission-overwrite map for a channel."""
    everyone = guild.default_role

    if name == "mod-updates":
        ov = {everyone: discord.PermissionOverwrite(view_channel=False)}
        for r in ("Maintainer", "Helper"):
            if r in roles:
                ov[roles[r]] = discord.PermissionOverwrite(view_channel=True)
        return ov

    if name in READ_ONLY:
        ov = {
            everyone: discord.PermissionOverwrite(
                view_channel=True,
                read_message_history=True,
                add_reactions=True,
                send_messages=False,
                create_public_threads=False,
                create_private_threads=False,
                send_messages_in_threads=False,
            )
        }
        if "Maintainer" in roles:
            ov[roles["Maintainer"]] = discord.PermissionOverwrite(send_messages=True)
        return ov

    return {}


async def sync_roles(guild: discord.Guild) -> dict:
    """Create any missing roles. Returns {name: Role}."""
    print("Roles:")
    result = {}
    for spec in ROLES:
        existing = discord.utils.get(guild.roles, name=spec["name"])
        if existing:
            log(f"= {spec['name']} (exists)")
            result[spec["name"]] = existing
            continue
        role = await guild.create_role(
            name=spec["name"],
            colour=discord.Colour(spec["color"]),
            hoist=spec["hoist"],
            mentionable=spec["mentionable"],
            reason="Dotty Project provisioning",
        )
        log(f"+ {spec['name']} (created)")
        result[spec["name"]] = role
    return result


async def ensure_special(guild, kind, name, category, topic, roles):
    """Adopt the Community Rules / Community-Updates channels if they exist."""
    target = guild.rules_channel if kind == RULES else guild.public_updates_channel
    if target is None:
        return None  # Community not enabled — caller falls back to a text channel
    edits = {}
    if target.category_id != category.id:
        edits["category"] = category
    if target.name.lower() != name:
        edits["name"] = name
    if topic and getattr(target, "topic", None) != topic:
        edits["topic"] = topic
    ov = overwrites_for(name, guild, roles)
    if ov:
        edits["overwrites"] = ov
    if edits:
        await target.edit(reason="Dotty Project provisioning", **edits)
        log(f"~ #{name} (adopted Community {kind} channel)")
    else:
        log(f"= #{name} (Community {kind} channel)")
    return target


async def sync_layout(guild: discord.Guild, roles: dict) -> None:
    """Create any missing categories and channels."""
    for cat_name, channels in LAYOUT:
        print(f"\nCategory: {cat_name}")
        category = discord.utils.get(guild.categories, name=cat_name)
        if category is None:
            category = await guild.create_category(
                cat_name, reason="Dotty Project provisioning"
            )
            log(f"+ {cat_name} (created)")
        else:
            log(f"= {cat_name} (exists)")

        for ch_name, kind, topic in channels:
            # Community-feature channels: adopt rather than create.
            if kind in (RULES, UPDATES):
                adopted = await ensure_special(
                    guild, kind, ch_name, category, topic, roles
                )
                if adopted is not None:
                    continue
                kind = TEXT  # fall back: Community not enabled yet

            existing = find_channel(guild, ch_name)
            if existing is not None:
                if existing.category_id != category.id:
                    await existing.edit(category=category)
                log(f"= #{ch_name} (exists)")
                continue

            ov = overwrites_for(ch_name, guild, roles)
            if kind == VOICE:
                await guild.create_voice_channel(
                    ch_name, category=category, reason="Dotty Project provisioning"
                )
            elif kind == FORUM:
                await guild.create_forum(
                    ch_name, category=category, topic=topic or "",
                    reason="Dotty Project provisioning",
                )
            else:  # TEXT or NEWS
                ch = await guild.create_text_channel(
                    ch_name, category=category, topic=topic or "",
                    overwrites=ov, reason="Dotty Project provisioning",
                )
                if kind == NEWS:
                    try:
                        await ch.edit(type=discord.ChannelType.news)
                    except (discord.HTTPException, discord.InvalidData):
                        log(f"  ! could not flag #{ch_name} as an Announcement "
                            f"channel — convert it in the UI (Edit Channel).")
            log(f"+ #{ch_name} ({kind}, created)")


class Provisioner(discord.Client):
    async def on_ready(self):
        try:
            guild = self.pick_guild()
            print(f"\nProvisioning: {guild.name}  (id={guild.id})\n")
            roles = await sync_roles(guild)
            await sync_layout(guild, roles)
            print("\nDone. Next steps:")
            print("  - If #announcements is still a normal text channel, open it ->")
            print("    Edit Channel and toggle it to an Announcement channel.")
            print("  - Wire the GitHub feed: #github-feed -> Integrations -> Webhooks,")
            print("    then add that URL (with /github appended) to the repo's webhooks.")
            print("  - Tighten or remove the bot's permissions now that setup is done.")
        except Exception as exc:  # noqa: BLE001 - surface any failure cleanly
            print(f"\nERROR: {exc}", file=sys.stderr)
        finally:
            await self.close()

    def pick_guild(self) -> discord.Guild:
        gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
        if gid:
            guild = self.get_guild(int(gid))
            if guild is None:
                raise RuntimeError(
                    f"Bot is not a member of guild {gid}. Invite it first."
                )
            return guild
        if len(self.guilds) == 1:
            return self.guilds[0]
        if not self.guilds:
            raise RuntimeError("Bot is not in any server — invite it first.")
        listing = ", ".join(f"{g.name} ({g.id})" for g in self.guilds)
        raise RuntimeError(
            f"Bot is in multiple servers; set DISCORD_GUILD_ID. Options: {listing}"
        )


def main() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        sys.exit("DISCORD_BOT_TOKEN is not set (see .env.example).")
    # Provisioning needs no privileged intents — default intents are enough.
    Provisioner(intents=discord.Intents.default()).run(token)


if __name__ == "__main__":
    main()
