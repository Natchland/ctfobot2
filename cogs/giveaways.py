# cogs/giveaways.py  –  production-ready giveaway cog  (2024-09-22)
from __future__ import annotations

import asyncio
import datetime as dt
import random
from typing import Dict, Optional, TYPE_CHECKING

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks

if TYPE_CHECKING:              # for type checkers only
    from ctfobot2_0 import Database


# ═════════════════════════════════════════════════════════════════════
#                              CONSTANTS
# ═════════════════════════════════════════════════════════════════════
GUILD_ID            = 1377035207777194005
GIVEAWAY_CHANNEL_ID = 1413929735658016899        # #giveaways
ACTIVE_ROLE_ID      = 1403337937722019931        # “Active Member”

ADMIN_ROLE_IDS = {1377103244089622719}

STAFF_BONUS_ROLE_IDS = {
    1377077466513932338,                         # Group Leader
    1377084533706588201,                         # Player Management
    1410659214959054988,                         # Recruitment
}

STREAK_BONUS_PER_SET = 3                         # +3 per full 3-day streak
STAFF_BONUS_PER_WEEK = 1                         # +1 /week with staff role
BOOST_BONUS_PER_WEEK = 2                         # +2 /week of Nitro boost


# ═════════════════════════════════════════════════════════════════════
#                              HELPERS
# ═════════════════════════════════════════════════════════════════════
def _fmt_left(td: dt.timedelta) -> str:
    sec = max(int(td.total_seconds()), 0)
    d, sec = divmod(sec, 86_400)
    h, sec = divmod(sec, 3_600)
    m, s   = divmod(sec, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _is_admin(mem: discord.Member) -> bool:
    return (
        mem.guild_permissions.administrator
        or mem.id == mem.guild.owner_id
        or any(r.id in ADMIN_ROLE_IDS for r in mem.roles)
    )


async def _activity_map(pool: asyncpg.Pool) -> dict[int, dict]:
    rows = await pool.fetch("SELECT * FROM activity")
    return {r["user_id"]: dict(r) for r in rows}


# ═════════════════════════════════════════════════════════════════════
#                          Giveaway object
# ═════════════════════════════════════════════════════════════════════
class Giveaway:
    """Keeps state for one running giveaway message."""
    instances: Dict[int, "Giveaway"] = {}        # msg_id → instance

    def __init__(self, prize: str, ends_at: dt.datetime):
        self.prize      = prize
        self.ends_at    = ends_at
        self.started_at = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)

        self.message:  Optional[discord.Message] = None
        self.entrants: Dict[int, int]            = {}

    # ──────────────────────────────────────────────────────────────
    async def _tickets_for(self, m: discord.Member,
                           activity: dict[int, dict]) -> int:
        if ACTIVE_ROLE_ID not in [r.id for r in m.roles]:
            return 0

        now = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
        tickets = 1                                               # base ticket

        # activity streak bonus (capped to giveaway lifetime)
        if rec := activity.get(m.id):
            days_since_start = (now.date() - self.started_at.date()).days + 1
            effective_streak = min(rec["streak"], days_since_start)
            tickets += (effective_streak // 3) * STREAK_BONUS_PER_SET

        # booster bonus
        if m.premium_since:
            effective = max(m.premium_since, self.started_at)
            tickets += ((now - effective).days // 7) * BOOST_BONUS_PER_WEEK

        # staff-role bonus
        if any(r.id in STAFF_BONUS_ROLE_IDS for r in m.roles):
            tickets += ((now - self.started_at).days // 7) * STAFF_BONUS_PER_WEEK

        return tickets

    async def recompute(self, guild: discord.Guild, pool: asyncpg.Pool):
        activity = await _activity_map(pool)
        self.entrants.clear()
        for m in guild.members:
            if m.bot:
                continue
            n = await self._tickets_for(m, activity)
            if n:
                self.entrants[m.id] = n

    # ─────────────────────────── embeds ───────────────────────────
    def _embed(self, guild: discord.Guild,
               *, ended=False,
               winner: Optional[discord.Member] = None) -> discord.Embed:
        e = discord.Embed(title="🎉 GIVEAWAY 🎉",
                          colour=discord.Color.blurple())
        e.add_field(name="Prize", value=f"**{self.prize}**", inline=False)

        if ended:
            e.add_field(name="Time left", value="**Ended**", inline=False)
        else:
            left = self.ends_at - dt.datetime.utcnow().replace(
                tzinfo=dt.timezone.utc
            )
            e.add_field(name="Time left", value=f"**{_fmt_left(left)}**",
                        inline=False)

        e.add_field(name="Eligibility",
                    value=f"Only <@&{ACTIVE_ROLE_ID}> can win.",
                    inline=False)

        if ended:
            e.add_field(name="Winner",
                        value=winner.mention if winner else "Cancelled",
                        inline=False)
        else:
            listing = (
                "\n".join(
                    f"• {guild.get_member(uid).mention} – **{t}**"
                    for uid, t in sorted(self.entrants.items(),
                                         key=lambda kv: (-kv[1], kv[0]))
                )
                or "*None yet*"
            )
            e.add_field(name="Eligible Entrants", value=listing, inline=False)
        return e

    # ───────────────────── database helpers ──────────────────────
    async def _insert_row(self, pool: asyncpg.Pool, ch_id: int):
        await pool.execute(
            "INSERT INTO giveaways (channel_id, message_id, prize, "
            "start_ts, end_ts, active) VALUES ($1,$2,$3,$4,$5,TRUE)",
            ch_id, self.message.id, self.prize,
            int(self.started_at.timestamp()), int(self.ends_at.timestamp())
        )

    async def _close_row(self, pool: asyncpg.Pool,
                         winner_id: Optional[int]):
        await pool.execute(
            "UPDATE giveaways SET active=FALSE, note=$1 WHERE message_id=$2",
            str(winner_id) if winner_id else "cancelled",
            self.message.id
        )

    # ───────────────────── lifecycle helpers ─────────────────────
    async def start(self, ch: discord.TextChannel, pool: asyncpg.Pool):
        await self.recompute(ch.guild, pool)
        view = _GwButtons(self, pool)
        self.message = await ch.send(embed=self._embed(ch.guild), view=view)
        Giveaway.instances[self.message.id] = self
        await self._insert_row(pool, ch.id)

    async def cancel(self, guild: discord.Guild, pool: asyncpg.Pool):
        await self._close_row(pool, None)
        await self.message.edit(embed=self._embed(guild, ended=True), view=None)

    async def draw(self, guild: discord.Guild,
                   pool: asyncpg.Pool) -> Optional[discord.Member]:
        await self.recompute(guild, pool)
        if not self.entrants:
            return None
        ids, weights = zip(*self.entrants.items())
        winner_id = random.choices(ids, weights)[0]
        return guild.get_member(winner_id)


# ═════════════════════════════════════════════════════════════════════
#                         Buttons (admin only)
# ═════════════════════════════════════════════════════════════════════
class _GwButtons(discord.ui.View):
    def __init__(self, gw: Giveaway, pool: asyncpg.Pool):
        super().__init__(timeout=None)
        self.gw, self.pool = gw, pool

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if not _is_admin(inter.user):
            await inter.response.send_message("Not authorised.",
                                              ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🎁 End & Draw",
                       style=discord.ButtonStyle.green)
    async def _end(self, inter: discord.Interaction, _: discord.ui.Button):
        winner = await self.gw.draw(inter.guild, self.pool)
        await self.gw._close_row(self.pool, winner.id if winner else None)
        await self.gw.message.edit(
            embed=self.gw._embed(inter.guild, True, winner), view=None
        )
        if winner:
            await inter.channel.send(
                f"🎉 Congratulations {winner.mention}! "
                f"You won **{self.gw.prize}**!"
            )
        await inter.response.send_message("Giveaway ended.", ephemeral=True)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def _cancel(self, inter: discord.Interaction, _: discord.ui.Button):
        await self.gw.cancel(inter.guild, self.pool)
        await inter.response.send_message("Cancelled.", ephemeral=True)


# ═════════════════════════════════════════════════════════════════════
#                               COG
# ═════════════════════════════════════════════════════════════════════
class GiveawayCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database):
        self.bot = bot
        self.db  = db                     # keep full DB object
        self._resume_started = False      # ensures resume runs once

    # ───────────────────  slash command  ───────────────────
    @app_commands.command(name="giveaway", description="Create a giveaway")
    @app_commands.describe(prize="Prize",
                           duration="Duration in hours (1-720)")
    async def giveaway_cmd(
        self,
        inter: discord.Interaction,
        prize: str,
        duration: app_commands.Range[int, 1, 720],
    ):
        if not _is_admin(inter.user):
            return await inter.response.send_message("Admins only.",
                                                     ephemeral=True)

        await inter.response.defer(ephemeral=True)

        ch = inter.guild.get_channel(GIVEAWAY_CHANNEL_ID)
        if not ch:
            return await inter.followup.send("Giveaway channel missing.",
                                             ephemeral=True)

        ends_at = (
            dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
            + dt.timedelta(hours=duration)
        )
        gw = Giveaway(prize, ends_at)
        await gw.start(ch, self.db.pool)
        self._background(gw).start()

        await inter.followup.send("Giveaway created!", ephemeral=True)

    # ───────────── 5-second updater for one giveaway ─────────────
    def _background(self, gw: Giveaway):
        @tasks.loop(seconds=15)
        async def _loop():
            now = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
            if now >= gw.ends_at:
                winner = await gw.draw(gw.message.guild, self.db.pool)
                await gw._close_row(self.db.pool,
                                    winner.id if winner else None)
                await gw.message.edit(
                    embed=gw._embed(gw.message.guild, True, winner),
                    view=None
                )
                if winner:
                    await gw.message.channel.send(
                        f"🎉 {winner.mention} wins **{gw.prize}**!"
                    )
                _loop.cancel()
                return

            await gw.recompute(gw.message.guild, self.db.pool)
            await gw.message.edit(embed=gw._embed(gw.message.guild))

        return _loop

    # ───────────────────────── resume ─────────────────────────
    async def _resume(self):
        """Re-attach views to active giveaways stored in the DB."""
        await self.bot.wait_until_ready()

        # wait until the main on_ready finished opening the pool
        while self.db.pool is None:
            await asyncio.sleep(1)

        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return

        rows = await self.db.pool.fetch(
            "SELECT * FROM giveaways WHERE active=TRUE"
        )
        for r in rows:
            chan = guild.get_channel(r["channel_id"])
            if not chan:
                continue
            try:
                msg = await chan.fetch_message(r["message_id"])
            except discord.NotFound:
                await self.db.pool.execute(
                    "UPDATE giveaways SET active=FALSE WHERE message_id=$1",
                    r["message_id"],
                )
                continue

            gw = Giveaway(
                r["prize"],
                dt.datetime.fromtimestamp(r["end_ts"],
                                          tz=dt.timezone.utc),
            )
            gw.message = msg
            Giveaway.instances[msg.id] = gw
            await msg.edit(view=_GwButtons(gw, self.db.pool))
            self._background(gw).start()
        print("[giveaways] resume finished")

    # run _resume once, right after the first ready event
    @commands.Cog.listener()
    async def on_ready(self):
        if not self._resume_started:
            self._resume_started = True
            asyncio.create_task(self._resume())


# ═════════════════════════════════════════════════════════════════════
#                           public entry point
# ═════════════════════════════════════════════════════════════════════
async def setup(bot: commands.Bot, db: Database):
    """
    Called from ctfobot2_0.py:

        await (import_module("cogs.giveaways").setup)(bot, db)
    """
    await bot.add_cog(GiveawayCog(bot, db))