# cogs/welcome_general.py
#
# Handles:
#   • on_member_join   – add “Uncompleted application” role + public welcome
#   • on_member_remove – announce leave / kick
#   • on_member_ban    – announce ban
#
# NOTE: The existing cog `cogs/welcome_member.py` (accepted-member welcome)
#       stays unchanged – its listeners will run in parallel.

from __future__ import annotations

import contextlib
import logging
from typing import Optional

import discord
from discord.ext import commands

log = logging.getLogger("cog.welcome_general")

# ─────────────────────── constants (copy from main) ───────────────────────
GUILD_ID                = 1377035207777194005
WELCOME_CHANNEL_ID      = 1398659438960971876
APPLICATION_CH_ID       = 1378081331686412468
UNCOMPLETED_APP_ROLE_ID = 1390143545066917931
LEAVE_BAN_CH_ID         = 1404955868054814761
# ───────────────────────────────────────────────────────────────────────────


class WelcomeGeneralCog(commands.Cog):
    """Public join / leave / ban announcements (not accepted-member welcome)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ─────────────────── helper: deduplicate welcomes ───────────────────
    @staticmethod
    async def _clean_old_welcomes(
        channel: discord.TextChannel,
        member: discord.Member,
        marker: str = "👋 **Welcome",
    ):
        seen: list[discord.Message] = []
        async for msg in channel.history(limit=20):
            if (
                msg.author == channel.guild.me
                and marker in msg.content
                and member.mention in msg.content
            ):
                seen.append(msg)

        if len(seen) > 1:
            seen.sort(key=lambda m: m.created_at, reverse=True)
            for old in seen[1:]:
                with contextlib.suppress(Exception):
                    await old.delete()

    # ───────────────────── on_member_join ─────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        # ignore other guilds & bots
        if member.bot or member.guild.id != GUILD_ID:
            return

        guild = member.guild
        welcome_ch: Optional[discord.TextChannel] = guild.get_channel(WELCOME_CHANNEL_ID)  # type: ignore
        apply_ch  : Optional[discord.TextChannel] = guild.get_channel(APPLICATION_CH_ID)   # type: ignore

        # 1) add “Uncompleted application” role
        role = guild.get_role(UNCOMPLETED_APP_ROLE_ID)
        if role and role not in member.roles:
            try:
                await member.add_roles(role, reason="Joined – application not started")
            except discord.Forbidden:
                log.warning("[welcome] Can't add role to %s", member)
            except Exception as exc:
                log.exception("[welcome] Error adding role: %s", exc)

        # 2) send public welcome
        if welcome_ch and apply_ch:
            txt = (
                f"👋 **Welcome {member.mention}!**\n"
                f"To join CTFO, please run **`/memberform`** "
                f"in {apply_ch.mention} and fill out the quick application.\n"
                "If you have any questions, just ask a mod.  Enjoy your stay!"
            )
            try:
                await welcome_ch.send(txt)
            except Exception as exc:
                log.warning("[welcome] Failed to send message: %s", exc)

            # 3) delete duplicate welcomes
            try:
                await self._clean_old_welcomes(welcome_ch, member)
            except Exception as exc:
                log.debug("[welcome] Dedup error: %s", exc)
        else:
            log.info("[welcome] Welcome or application channel missing.")

    # ────────────────── on_member_remove ──────────────────
    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.guild.id != GUILD_ID:
            return
        ch: Optional[discord.TextChannel] = member.guild.get_channel(LEAVE_BAN_CH_ID)  # type: ignore
        if ch:
            await ch.send(f"👋 **{member}** has left the server.")

    # ─────────────────── on_member_ban ────────────────────
    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        if guild.id != GUILD_ID:
            return
        ch: Optional[discord.TextChannel] = guild.get_channel(LEAVE_BAN_CH_ID)  # type: ignore
        if ch:
            await ch.send(f"⛔ **{user}** has been banned from the server.")


# ═════════════ setup entry point ═════════════
async def setup(bot: commands.Bot, _db=None):
    await bot.add_cog(WelcomeGeneralCog(bot))