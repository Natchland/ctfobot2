# cogs/steam_sync.py
# ───────────────────────────────────────────────────────────────
#   • Periodically sync Discord nicknames with Steam names
#   • /link steam <url>   → users link / update their profile
#   • /steamsync now      → mods trigger instant sync
#   • Missing links: silent DM reminder, max once / 24 h
# ───────────────────────────────────────────────────────────────
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

# Re-use helpers from member_forms
from cogs.member_forms import (                         # type: ignore
    extract_steam_id,
    get_steam_username,
    is_steam_profile_valid,
    ROLE_PREFIXES,
)

log = logging.getLogger("cog.steam_sync")

# ═══════════════════ CONFIG ═══════════════════════════════════
GUILD_ID          = int(os.getenv("GUILD_ID", 0))
SYNC_INTERVAL_MIN = int(os.getenv("STEAM_SYNC_MINUTES", 60))          # periodic loop
PING_COOLDOWN_H   = int(os.getenv("STEAM_PING_COOLDOWN_H", 24))       # DM rate-limit

OWNER_ROLE_ID     = 1383201150140022784  # exempt from auto-nick

# staff role-id → suffix
STAFF_SUFFIXES: dict[int, str] = {
    1377077466513932338: " | G L",
    1377084533706588201: " | P M",
    1377103244089622719: " | Admin",
    1410659214959054988: " | Rec",
}
STAR = "*"  # put in prefix to bump in voice

FOCUS_ROLE_IDS = {
    "Farming":      1379918816871448686,
    "Base Sorting": 1400849292524130405,
    "Building":     1380233086544908428,
    "Electricity":  1380233234675400875,
    "PvP":          1408687710159245362,
}
# ══════════════════════════════════════════════════════════════


class SteamSyncCog(commands.Cog):
    """Automatically keeps nicknames in sync with Steam."""

    def __init__(self, bot: commands.Bot, db):
        self.bot, self.db = bot, db
        self._last_ping: dict[int, float] = {}  # discord_id → last-DM ts
        self.sync_task.start()

    # ───────────────────────── /link steam ─────────────────────
    link_group = app_commands.Group(
        name="link", description="Link or update external accounts"
    )

    @link_group.command(name="steam")
    @app_commands.describe(url="Your steamcommunity.com profile URL")
    async def link_steam(self, i: discord.Interaction, url: str):
        """Store or update a member's Steam profile."""
        await i.response.defer(ephemeral=True)

        steam_id = await extract_steam_id(url)
        if not steam_id:
            return await i.followup.send(
                "❌ I couldn’t read that link. Use the full "
                "`steamcommunity.com/profiles/...` or `/id/...` URL.",
                ephemeral=True,
            )

        if not await is_steam_profile_valid(steam_id):
            return await i.followup.send(
                "❌ That Steam profile is not public or doesn’t meet the "
                "requirements (≥ 1 game, ≥ 1 friend, ≥ 1 h play-time).",
                ephemeral=True,
            )

        await self.db.set_steam_id(i.user.id, steam_id)
        await i.followup.send("✅ Steam account linked!", ephemeral=True)

    # ───────────────────────── /steamsync now ──────────────────
    steamsync_group = app_commands.Group(
        name="steamsync", description="Manually control Steam nickname sync"
    )

    @steamsync_group.command(
        name="now", description="Run the Steam nickname sync immediately"
    )
    async def steamsync_now(self, i: discord.Interaction):
        if not i.user.guild_permissions.manage_guild:
            return await i.response.send_message("No permission.", ephemeral=True)

        await i.response.defer(thinking=True, ephemeral=True)
        await self._sync_once()
        await i.followup.send("✅ Steam sync finished.", ephemeral=True)

    # ───────────────────────── periodic task ───────────────────
    @tasks.loop(minutes=SYNC_INTERVAL_MIN)
    async def sync_task(self):
        await self._sync_once()

    @sync_task.before_loop
    async def _wait_for_ready(self):
        await self.bot.wait_until_ready()

    # ========== core sync logic (used by task & /now) ==========
    async def _sync_once(self):
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return

        for member in guild.members:
            if member.bot or member.get_role(OWNER_ROLE_ID):
                continue

            steam_id: Optional[str] = await self.db.get_steam_id(member.id)
            if not steam_id:
                await self._remind_link(member)
                continue

            steam_name = await get_steam_username(steam_id)
            if not steam_name:
                await self._remind_link(member)
                continue

            target_nick = self._build_nickname(member, steam_name)
            if member.nick != target_nick:
                try:
                    await member.edit(nick=target_nick, reason="SteamSync")
                except (discord.Forbidden, discord.HTTPException):
                    pass  # no perms or hierarchy issue

    # ───────────────────────── helper: DM reminder ─────────────
    async def _remind_link(self, member: discord.Member):
        """DM the member at most once every PING_COOLDOWN_H hours (persistent)."""
        last_dt = await self.db.get_last_steam_ping(member.id)
        if last_dt:
            elapsed_h = (discord.utils.utcnow() - last_dt).total_seconds() / 3600
            if elapsed_h < PING_COOLDOWN_H:
                return  # still on cooldown

        try:
            await member.send(
                "Hi! I can't find a valid Steam profile linked to your account "
                "on the server. Please use the </link steam:…> command there "
                "to add or update it. Thanks!"
            )
            await self.db.set_last_steam_ping(member.id)  # record successful DM
        except discord.Forbidden:
            # DMs disabled → we still record the attempt so we don't spam publicly
            await self.db.set_last_steam_ping(member.id)

    # ───────────────────────── helper: nick builder ────────────
    def _build_nickname(self, member: discord.Member, steam_name: str) -> str:
        # focus prefix
        prefix = ""
        for focus, role_id in FOCUS_ROLE_IDS.items():
            if member.get_role(role_id):
                prefix = ROLE_PREFIXES.get(focus, "")
                break

        # star for staff
        if any(member.get_role(rid) for rid in STAFF_SUFFIXES):
            if prefix.startswith("[") and not prefix.startswith("[*"):
                prefix = prefix.replace("[", "[*", 1)

        # staff suffix
        suffix = ""
        for rid, txt in STAFF_SUFFIXES.items():
            if member.get_role(rid):
                suffix = txt
                break

        nick = f"{prefix} {steam_name}{suffix}".strip()
        return nick[:32]  # Discord limit

# ═══════════════════ setup entry-point ════════════════════════
async def setup(bot: commands.Bot, db):
    await bot.add_cog(SteamSyncCog(bot, db))