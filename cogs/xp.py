"""
cogs.xp – v2.2
==============

Full XP / levelling system for *discord.py*.

Changes in v2.2
---------------
• New level-up embed: nicer layout, gradient colour, percentage sits
  directly next to the progress bar.
"""

from __future__ import annotations

import contextlib
import math
import random
import textwrap
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

import colorsys
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ──────────────────────────── CONFIG ────────────────────────────────────
MSG_COOLDOWN_S = 45
MIN_CHARS = 5
MSG_XP_RANGE = (15, 25)

VOICE_XP_PER_MIN = 5
VOICE_TICK_SECONDS = 60

STREAK_BASE = 10
STREAK_PER_DAY = 5

LEADERBOARD_SIZE = 10
DECAY_AFTER_DAYS = 7
DECAY_FACTOR = 0.99
# ────────────────────────────────────────────────────────────────────────


# ══════════════════════════ EMBED HELPERS ══════════════════════════════
def _hsv_gradient(level: int) -> discord.Colour:
    """Return a pleasant hue that drifts as the level increases."""
    hue_deg = (level * 3) % 360  # 3° hue shift per level
    r, g, b = colorsys.hsv_to_rgb(hue_deg / 360, 0.65, 0.90)
    return discord.Colour.from_rgb(int(r * 255), int(g * 255), int(b * 255))


def build_levelup_embed(
    member: discord.Member, *, level: int, current_xp: int, next_level_xp: int
) -> discord.Embed:
    pct = current_xp / next_level_xp
    bar_len = 10
    bar = "▰" * int(pct * bar_len) + "▱" * (bar_len - int(pct * bar_len))
    percent_txt = f"{pct*100:4.1f}%"

    embed = discord.Embed(
        title=f"🎉  Level {level} unlocked!",
        description=f"{member.mention} reached a new level!",
        colour=_hsv_gradient(level),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    embed.add_field(name="Current XP", value=f"{current_xp:,}", inline=True)
    embed.add_field(name="Next level XP", value=f"{next_level_xp:,}", inline=True)
    embed.add_field(
        name="Progress", value=f"`{bar}` **{percent_txt}**", inline=False
    )
    embed.set_footer(
        text="Keep chatting and hanging out in voice to earn more XP!"
    )
    return embed


# ═════════════════════════════ COG ═════════════════════════════════════
class XPCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db):
        self.bot, self.db = bot, db
        self.voice_sessions: Dict[Tuple[int, int], datetime] = {}

        self._voice_tick.start()
        self._decay_loop.start()
        self._boost_watch.start()

    # ──────────────── maths & db helpers ───────────────────────────
    @staticmethod
    def level_from_xp(xp: int) -> int:
        return int(0.1 * math.sqrt(xp))

    @staticmethod
    def xp_for_next(level: int) -> int:
        return int(((level + 1) / 0.1) ** 2)

    async def _chan_mult(self, gid: int, cid: int) -> float:
        row = await self.db.fetch_one(
            "SELECT mult FROM xp_channel_mult WHERE guild_id=$1 AND channel_id=$2",
            gid,
            cid,
        )
        return float(row["mult"]) if row else 1.0

    async def _guild_boost(self, gid: int) -> float:
        row = await self.db.fetch_one(
            "SELECT multiplier, ends_at FROM xp_boosts WHERE guild_id=$1", gid
        )
        if not row or row["ends_at"] < datetime.now(timezone.utc):
            return 1.0
        return float(row["multiplier"])

    async def _lvl_channel_id(self, gid: int) -> Optional[int]:
        row = await self.db.fetch_one(
            "SELECT channel_id FROM xp_levelup_channel WHERE guild_id=$1", gid
        )
        return row["channel_id"] if row else None

    async def _voice_excluded(self, gid: int, cid: int) -> bool:
        return (
            await self.db.fetch_one(
                "SELECT 1 FROM xp_voice_excluded WHERE guild_id=$1 AND channel_id=$2",
                gid,
                cid,
            )
            is not None
        )

    # ─────────────────── TEXT XP LISTENER ────────────────────────
    @commands.Cog.listener("on_message")
    async def _text_xp(self, m: discord.Message):
        if m.author.bot or m.guild is None or len(m.content) < MIN_CHARS:
            return

        gid, uid, now = m.guild.id, m.author.id, datetime.now(timezone.utc)
        rec = await self.db.fetch_one(
            "SELECT xp, level, last_msg, streak FROM xp_members "
            "WHERE guild_id=$1 AND user_id=$2",
            gid,
            uid,
        )

        if rec and rec["last_msg"]:
            if (now - rec["last_msg"]).total_seconds() < MSG_COOLDOWN_S:
                return

        base = random.randint(*MSG_XP_RANGE)
        base = int(
            base
            * await self._chan_mult(gid, m.channel.id)
            * await self._guild_boost(gid)
        )

        streak, bonus = (rec["streak"] if rec else 0), 0
        if rec and rec["last_msg"]:
            gap = (now.date() - rec["last_msg"].date()).days
            if gap == 1:
                streak += 1
                bonus = STREAK_BASE + streak * STREAK_PER_DAY
            elif gap > 1:
                streak = 1
        else:
            streak = 1

        delta = base + bonus
        new_xp = delta + (rec["xp"] if rec else 0)
        new_lvl = self.level_from_xp(new_xp)
        old_lvl = rec["level"] if rec else 0

        await self.db.execute(
            """
            INSERT INTO xp_members (guild_id,user_id,xp,level,last_msg,streak)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (guild_id,user_id)
            DO UPDATE SET xp=$3, level=$4, last_msg=$5, streak=$6
            """,
            gid,
            uid,
            new_xp,
            new_lvl,
            now,
            streak,
        )
        await self.db.execute(
            "INSERT INTO xp_log (guild_id,user_id,delta,reason) "
            "VALUES ($1,$2,$3,'message')",
            gid,
            uid,
            delta,
        )

        if new_lvl > old_lvl:
            await self._announce_level_up(m.author, new_lvl, m.channel)

    # ─────────────────── VOICE XP TRACKING ────────────────────────
    @commands.Cog.listener("on_voice_state_update")
    async def _voice_state(self, m: discord.Member, before, after):
        key = (m.guild.id, m.id)

        # leaving / switching out of an included room
        if before.channel and key in self.voice_sessions:
            mins = int(
                (
                    datetime.now(timezone.utc) - self.voice_sessions.pop(key)
                ).total_seconds()
                / 60
            )
            if mins:
                await self._grant_voice_xp(m, mins)

        # joined / switched into an included room
        if after.channel and not await self._voice_excluded(
            m.guild.id, after.channel.id
        ):
            self.voice_sessions[key] = datetime.now(timezone.utc)

    async def _grant_voice_xp(self, m: discord.Member, mins: int):
        if mins <= 0:
            return

        delta = int(
            mins * VOICE_XP_PER_MIN * await self._guild_boost(m.guild.id)
        )
        rec = await self.db.fetch_one(
            "SELECT xp, level FROM xp_members WHERE guild_id=$1 AND user_id=$2",
            m.guild.id,
            m.id,
        )
        new_xp = delta + (rec["xp"] if rec else 0)
        new_lvl = self.level_from_xp(new_xp)
        old_lvl = rec["level"] if rec else 0

        await self.db.execute(
            """
            INSERT INTO xp_members (guild_id,user_id,xp,level,last_msg)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT (guild_id,user_id) DO UPDATE SET xp=$3, level=$4
            """,
            m.guild.id,
            m.id,
            new_xp,
            new_lvl,
            datetime.now(timezone.utc),
        )
        await self.db.execute(
            "INSERT INTO xp_log (guild_id,user_id,delta,reason) "
            "VALUES ($1,$2,$3,'voice')",
            m.guild.id,
            m.id,
            delta,
        )

        if new_lvl > old_lvl:
            chan = next(
                (
                    c
                    for c in m.guild.text_channels
                    if c.permissions_for(m.guild.me).send_messages
                ),
                None,
            )
            if chan:
                await self._announce_level_up(m, new_lvl, chan)

    @tasks.loop(seconds=VOICE_TICK_SECONDS)
    async def _voice_tick(self):
        now = datetime.now(timezone.utc)
        for (gid, uid), last in list(self.voice_sessions.items()):
            mins = int((now - last).total_seconds() / 60)
            if mins <= 0:
                continue

            guild = self.bot.get_guild(gid)
            member = guild and guild.get_member(uid)
            if not member or not member.voice:
                self.voice_sessions.pop((gid, uid), None)
                continue

            if await self._voice_excluded(gid, member.voice.channel.id):
                self.voice_sessions.pop((gid, uid), None)
                continue

            await self._grant_voice_xp(member, mins)
            self.voice_sessions[(gid, uid)] = now

    @_voice_tick.before_loop
    async def _voice_ready(self):
        await self.bot.wait_until_ready()

    # ──────────────────────── LEVEL UP ────────────────────────────
    async def _announce_level_up(
        self,
        member: discord.Member,
        level: int,
        origin_channel: discord.TextChannel,
    ):
        # get current XP for the embed
        rec = await self.db.fetch_one(
            "SELECT xp FROM xp_members WHERE guild_id=$1 AND user_id=$2",
            member.guild.id,
            member.id,
        )
        current_xp = rec["xp"] if rec else 0
        next_xp = self.xp_for_next(level)

        embed = build_levelup_embed(
            member,
            level=level,
            current_xp=current_xp,
            next_level_xp=next_xp,
        )

        # role rewards
        rows = await self.db.fetch_all(
            "SELECT role_id FROM xp_roles WHERE guild_id=$1 AND min_level <= $2",
            member.guild.id,
            level,
        )
        for r in rows:
            role = member.guild.get_role(r["role_id"])
            if role and role not in member.roles:
                with contextlib.suppress(discord.Forbidden):
                    await member.add_roles(role, reason="XP reward")

        pub_id = await self._lvl_channel_id(member.guild.id)
        if pub_id:
            pub = member.guild.get_channel(pub_id)
            if isinstance(pub, discord.TextChannel):
                with contextlib.suppress(discord.Forbidden):
                    await pub.send(embed=embed)

        try:
            await member.send(embed=embed)
        except discord.Forbidden:
            with contextlib.suppress(discord.Forbidden):
                await origin_channel.send(member.mention, embed=embed)

    # ───────────────────── USER COMMANDS ─────────────────────────
    @app_commands.command(name="rank", description="Show your level / XP")
    async def rank(
        self, inter: discord.Interaction, member: Optional[discord.Member] = None
    ):
        member = member or inter.user
        rec = await self.db.fetch_one(
            "SELECT xp, level, streak FROM xp_members "
            "WHERE guild_id=$1 AND user_id=$2",
            inter.guild.id,
            member.id,
        )
        if not rec:
            return await inter.response.send_message(
                f"{member.mention} has no XP yet.", ephemeral=True
            )

        next_xp = self.xp_for_next(rec["level"])
        pct = rec["xp"] / next_xp
        bar = "▰" * int(pct * 10) + "▱" * (10 - int(pct * 10))

        embed = discord.Embed(
            title=f"Rank for {member.display_name}",
            colour=discord.Colour.dark_embed(),
            description=textwrap.dedent(
                f"""
                Level **{rec['level']}**
                XP **{rec['xp']} / {next_xp}**
                {bar} **`{pct*100:4.1f}%`**
                Daily streak **{rec['streak']}**
                """
            ),
        ).set_thumbnail(url=member.display_avatar.url)
        await inter.response.send_message(embed=embed)

    @app_commands.command(name="leaderboard", description="Top XP users")
    async def leaderboard(
        self,
        inter: discord.Interaction,
        length: app_commands.Range[int, 1, 25] = LEADERBOARD_SIZE,
    ):
        rows = await self.db.fetch_all(
            "SELECT user_id, xp, level FROM xp_members "
            "WHERE guild_id=$1 ORDER BY xp DESC LIMIT $2",
            inter.guild.id,
            length,
        )
        if not rows:
            return await inter.response.send_message(
                "Nobody has XP yet.", ephemeral=True
            )

        lines = []
        for rank, r in enumerate(rows, 1):
            user = inter.guild.get_member(r["user_id"]) or f"<@{r['user_id']}>"
            lines.append(
                f"`#{rank:02}` **{user}** — L{r['level']} ({r['xp']} XP)"
            )

        embed = discord.Embed(
            title=f"Top {len(rows)} — {inter.guild.name}",
            description="\n".join(lines),
            colour=discord.Colour.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        await inter.response.send_message(embed=embed)

    # ───────────────────── ADMIN SUB-GROUP ───────────────────────
    xp_admin = app_commands.Group(
        name="xpadmin", description="XP admin tools", guild_only=True
    )

    # multiplier
    @xp_admin.command(
        name="multiplier", description="Set per-channel XP multiplier (0-5)"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def adm_multiplier(
        self,
        inter: discord.Interaction,
        channel: discord.TextChannel,
        value: app_commands.Range[float, 0, 5],
    ):
        await self.db.execute(
            """
            INSERT INTO xp_channel_mult (guild_id,channel_id,mult)
            VALUES ($1,$2,$3)
            ON CONFLICT (guild_id,channel_id) DO UPDATE SET mult=$3
            """,
            inter.guild.id,
            channel.id,
            value,
        )
        await inter.response.send_message(
            f"Multiplier for {channel.mention} set to ×{value}."
        )

    # grantrole
    @xp_admin.command(
        name="grantrole",
        description="Auto-grant a role once a member reaches a level",
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def adm_grantrole(
        self,
        inter: discord.Interaction,
        role: discord.Role,
        min_level: app_commands.Range[int, 1, 200],
    ):
        await self.db.execute(
            """
            INSERT INTO xp_roles (guild_id,role_id,min_level)
            VALUES ($1,$2,$3)
            ON CONFLICT (guild_id,role_id) DO UPDATE SET min_level=$3
            """,
            inter.guild.id,
            role.id,
            min_level,
        )
        await inter.response.send_message(
            f"{role.mention} will now be granted at level {min_level}."
        )

    # level-up channel
    @xp_admin.command(
        name="setlevelupchannel",
        description="Set the channel where level-up cards are posted",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def adm_set_lvl_channel(
        self, inter: discord.Interaction, channel: discord.TextChannel
    ):
        await self.db.execute(
            """
            INSERT INTO xp_levelup_channel (guild_id,channel_id)
            VALUES ($1,$2)
            ON CONFLICT (guild_id) DO UPDATE SET channel_id=$2
            """,
            inter.guild.id,
            channel.id,
        )
        await inter.response.send_message(
            f"Level-up cards will be posted in {channel.mention}."
        )

    # boost
    @xp_admin.command(
        name="boost", description="Activate a temporary global XP boost"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        multiplier="Boost factor (1-10)",
        minutes="Duration (1-1440)",
        announce_channel="Where to announce (defaults to current)",
        message="Optional extra text",
    )
    async def adm_boost(
        self,
        inter: discord.Interaction,
        multiplier: app_commands.Range[float, 1.0, 10.0],
        minutes: app_commands.Range[int, 1, 1440],
        announce_channel: Optional[discord.TextChannel] = None,
        message: Optional[str] = None,
    ):
        ends = datetime.now(timezone.utc) + timedelta(minutes=minutes)

        await self.db.execute(
            """
            INSERT INTO xp_boosts (guild_id,multiplier,ends_at,message,
                                   announce_channel_id,announce_msg_id)
            VALUES ($1,$2,$3,$4,NULL,NULL)
            ON CONFLICT (guild_id) DO UPDATE
            SET multiplier=$2, ends_at=$3, message=$4,
                announce_channel_id=NULL, announce_msg_id=NULL
            """,
            inter.guild.id,
            multiplier,
            ends,
            message,
        )

        ch = announce_channel or inter.channel
        emb = discord.Embed(
            title="🚀 XP BOOST ACTIVE",
            description=f"All XP gains are multiplied by **×{multiplier}** "
            f"for the next **{minutes} minutes**!",
            colour=discord.Colour.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        if message:
            emb.add_field(name="Info", value=message, inline=False)

        msg = await ch.send(embed=emb)

        await self.db.execute(
            """
            UPDATE xp_boosts
            SET announce_channel_id=$1, announce_msg_id=$2
            WHERE guild_id=$3
            """,
            ch.id,
            msg.id,
            inter.guild.id,
        )
        await inter.response.send_message("Boost activated!", ephemeral=True)

    # exclude
    @xp_admin.command(
        name="exclude",
        description="Enable / disable XP in a text or voice channel",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Channel to toggle",
        enabled="True = enable XP, False = disable (default)",
    )
    async def adm_exclude(
        self,
        inter: discord.Interaction,
        channel: discord.abc.GuildChannel,
        enabled: bool = False,
    ):
        if isinstance(channel, discord.TextChannel):
            if enabled:
                await self.db.execute(
                    "DELETE FROM xp_channel_mult WHERE guild_id=$1 AND channel_id=$2",
                    inter.guild.id,
                    channel.id,
                )
            else:
                await self.db.execute(
                    """
                    INSERT INTO xp_channel_mult (guild_id,channel_id,mult)
                    VALUES ($1,$2,0.0)
                    ON CONFLICT (guild_id,channel_id) DO UPDATE SET mult=0.0
                    """,
                    inter.guild.id,
                    channel.id,
                )
        elif isinstance(channel, discord.VoiceChannel):
            if enabled:
                await self.db.execute(
                    "DELETE FROM xp_voice_excluded WHERE guild_id=$1 AND channel_id=$2",
                    inter.guild.id,
                    channel.id,
                )
            else:
                await self.db.execute(
                    """
                    INSERT INTO xp_voice_excluded (guild_id,channel_id)
                    VALUES ($1,$2)
                    ON CONFLICT DO NOTHING
                    """,
                    inter.guild.id,
                    channel.id,
                )
        else:
            return await inter.response.send_message(
                "Unsupported channel type.", ephemeral=True
            )

        state = "enabled" if enabled else "disabled"
        await inter.response.send_message(f"XP {state} in {channel.mention}.")

    # ────────────────── BACKGROUND MAINTENANCE ───────────────────
    @tasks.loop(hours=24)
    async def _decay_loop(self):
        cutoff = datetime.now(timezone.utc) - timedelta(days=DECAY_AFTER_DAYS)
        await self.db.execute(
            "UPDATE xp_members SET xp=floor(xp*$1) WHERE last_msg<$2",
            DECAY_FACTOR,
            cutoff,
        )

    @_decay_loop.before_loop
    async def _decay_ready(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=1)
    async def _boost_watch(self):
        rows = await self.db.fetch_all(
            "SELECT guild_id,announce_channel_id,announce_msg_id "
            "FROM xp_boosts WHERE ends_at < $1",
            datetime.now(timezone.utc),
        )
        for r in rows:
            g = self.bot.get_guild(r["guild_id"])
            ch = g and g.get_channel(r["announce_channel_id"])
            if isinstance(ch, discord.TextChannel) and r["announce_msg_id"]:
                with contextlib.suppress(Exception):
                    msg = await ch.fetch_message(r["announce_msg_id"])
                    await msg.edit(content="🟢 Boost ended.", embed=None)
        if rows:
            await self.db.execute(
                "DELETE FROM xp_boosts WHERE ends_at < $1",
                datetime.now(timezone.utc),
            )

    @_boost_watch.before_loop
    async def _boost_ready(self):
        await self.bot.wait_until_ready()

    # ────────────────── graceful unload ────────────────────────
    def cog_unload(self):
        self._voice_tick.cancel()
        self._decay_loop.cancel()
        self._boost_watch.cancel()


# ═════════════════════ EXTENSION ENTRY ═════════════════════════
async def setup(bot: commands.Bot, db):
    cog = XPCog(bot, db)
    await bot.add_cog(cog)
    if bot.tree.get_command("xpadmin") is None:
        bot.tree.add_command(cog.xp_admin)