# cogs/welcome_member.py
from __future__ import annotations
import asyncio

import discord
from discord.ext import commands

WELCOME_CHANNEL_ID = 1421881846240645302   # 👈 change if needed
WELCOME_MARKER     = "**Welcome to CTFO!**"      # used for deduplication

class WelcomeMember(commands.Cog):
    """Send the big welcome message when a member form is accepted."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------------------------------------------------------
    # helper: delete older duplicates so only one welcome stays
    # ---------------------------------------------------------
    async def _dedupe(self, channel: discord.TextChannel, member: discord.Member):
        dupes = []
        async for msg in channel.history(limit=25):
            if (
                msg.author == channel.guild.me
                and WELCOME_MARKER in msg.content
                and member.mention in msg.content
            ):
                dupes.append(msg)

        if len(dupes) > 1:
            dupes.sort(key=lambda m: m.created_at, reverse=True)
            for m in dupes[1:]:
                try:
                    await m.delete()
                except Exception:
                    pass

    # ---------------------------------------------------------
    # core: send the message
    # ---------------------------------------------------------
    async def _send_welcome(self, member: discord.Member):
        channel: discord.TextChannel | None = member.guild.get_channel(WELCOME_CHANNEL_ID)
        if channel is None:
            return

        msg = (
            f"{member.mention}\n\n"
            "**Welcome to CTFO!**\n\n"
            "We're glad to have you here. Please take a moment to review the important "
            "information below to help you get started:\n\n"
            "**Basic Information:**\n"
            "- **Codes:** Codes may be shared in VC, but please avoid posting them in text channels.\n"
            "- **Base & Bag:** Join a VC to receive base location and a bag.\n"
            "- **Getting Started:** Once accepted, feel free to hop on at any time!\n"
            "- **Questions or Help:** If you have any questions or get stuck, please "
            "reach out to a staff member.\n"
            "- **Security Reminder:** For safety, do not discuss server details or "
            "clan matters in public channels.\n\n"
            "**Important Channels:**\n"
            "- <#1401604218711838893> **Current Server Information**\n"
            "- <#1403891526844551228> **Loot Thread - for any loot obtained you wish to share**\n"
            "- <#1414663176666222673> **In-Game Names - Make sure to post yours to avoid any confusion**\n"
            "- <#1413929735658016899> **Giveaways - Automatic entry for active members**\n"
            "- <#1416559771103924415> **Clan Guidelines & Support**"
        )

        await channel.send(
            msg,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False)
        )
        await self._dedupe(channel, member)

    # ---------------------------------------------------------
    # listener for the custom event we’ll dispatch in ActionView
    # ---------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_form_accepted(self, member: discord.Member):
        await self._send_welcome(member)


# ───────────────────────── setup() hook ─────────────────────────
async def setup(bot, db):              # db arg kept for symmetry, not used
    await bot.add_cog(WelcomeMember(bot))