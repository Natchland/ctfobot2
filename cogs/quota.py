# cogs/quota.py
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ───────────────────────── CONFIG ──────────────────────────
FARMER_ROLE_ID  = 1379918816871448686      # only these members have quotas
LEADERBOARD_SIZE = 10                      # /quota leaderboard top-N

# ───────────────────────── SQL ─────────────────────────────
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS quotas (
    user_id BIGINT PRIMARY KEY,
    weekly  INTEGER NOT NULL DEFAULT 0          -- target amount
);

CREATE TABLE IF NOT EXISTS quota_submissions (
    id        SERIAL PRIMARY KEY,
    user_id   BIGINT REFERENCES quotas(user_id) ON DELETE CASCADE,
    amount    INTEGER NOT NULL,
    reviewed  BOOLEAN DEFAULT FALSE,
    week_ts   DATE DEFAULT CURRENT_DATE
);
"""

SET_QUOTA_SQL = """
INSERT INTO quotas (user_id, weekly)
VALUES ($1,$2)
ON CONFLICT (user_id) DO UPDATE SET weekly=$2
"""

ADD_SUB_SQL = """
INSERT INTO quota_submissions (user_id, amount, reviewed)
VALUES ($1,$2,FALSE)
"""

PENDING_REVIEW_SQL = """
SELECT id, user_id, amount
FROM quota_submissions
WHERE reviewed=FALSE
ORDER BY id
"""

MARK_REVIEWED_SQL  = "UPDATE quota_submissions SET reviewed=TRUE WHERE id=$1"

CLEAR_OLD_SQL      = """
DELETE FROM quota_submissions
WHERE week_ts < CURRENT_DATE - INTERVAL '8 days'
"""

# ═══════════════════════════ COG ═══════════════════════════
class QuotaCog(commands.Cog):
    """Weekly quota system (Farmer role only)."""

    def __init__(self, bot: commands.Bot, db):
        self.bot, self.db = bot, db
        self._table_ready = asyncio.Event()

        asyncio.create_task(self._init_tables())
        self.weekly_cleanup.start()

    # -------------------------------------------------------- #
    async def _init_tables(self):
        while self.db.pool is None:               # wait for db.connect()
            await asyncio.sleep(1)
        async with self.db.pool.acquire() as conn:
            await conn.execute(CREATE_SQL)
        self._table_ready.set()

    # -------------------------------------------------------- #
    #                     Helper checks
    # -------------------------------------------------------- #
    @staticmethod
    def _has_farmer_role(member: discord.Member) -> bool:
        return any(r.id == FARMER_ROLE_ID for r in member.roles)

    async def _farmer_check_response(self, inter: discord.Interaction) -> bool:
        """Return True if caller has Farmer role, else respond with error."""
        if inter.guild is None or not isinstance(inter.user, discord.Member):
            await inter.response.send_message(
                "Guild context required.", ephemeral=True
            )
            return False
        if not self._has_farmer_role(inter.user):
            await inter.response.send_message(
                "You need the Farmer role to use quota commands.", ephemeral=True
            )
            return False
        return True

    # -------------------------------------------------------- #
    #                 Slash-command group
    # -------------------------------------------------------- #
    quota = app_commands.Group(
        name="quota",
        description="Farmer-only weekly quotas",
        default_permissions=discord.Permissions(manage_guild=True)
    )

    # /quota set
    @quota.command(name="set", description="Set weekly quota for a Farmer")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def quota_set(
        self,
        inter: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, 1_000_000]
    ):
        await self._table_ready.wait()

        if not self._has_farmer_role(member):
            return await inter.response.send_message(
                f"{member.mention} does not have the Farmer role.",
                ephemeral=True
            )

        await self.db.pool.execute(SET_QUOTA_SQL, member.id, amount)
        await inter.response.send_message(
            f"Set {member.mention}’s weekly quota to **{amount}**.",
            ephemeral=True
        )

    # /quota progress
    @quota.command(name="progress", description="Submit amount towards your quota")
    async def quota_progress(
        self,
        inter: discord.Interaction,
        amount: app_commands.Range[int, 1, 1_000_000]
    ):
        await self._table_ready.wait()
        if not await self._farmer_check_response(inter):
            return

        await self.db.pool.execute(ADD_SUB_SQL, inter.user.id, amount)
        await inter.response.send_message(
            f"Recorded **{amount}** towards your quota.",
            ephemeral=True
        )

    # /quota leaderboard
    @quota.command(name="leaderboard", description="Top Farmers this week")
    async def quota_leaderboard(self, inter: discord.Interaction):
        await self._table_ready.wait()

        guild = inter.guild
        if guild is None:
            return await inter.response.send_message(
                "Command must be run inside a guild.",
                ephemeral=True
            )

        # aggregate in SQL first, then filter to farmers
        rows = await self.db.pool.fetch("""
            SELECT user_id, SUM(amount) AS total
            FROM quota_submissions
            WHERE reviewed=TRUE AND week_ts = CURRENT_DATE
            GROUP BY user_id
            ORDER BY total DESC
        """)

        # keep only members who still have the Farmer role
        rows = [
            r for r in rows
            if (m := guild.get_member(r["user_id"])) and self._has_farmer_role(m)
        ][:LEADERBOARD_SIZE]

        if not rows:
            return await inter.response.send_message(
                "No reviewed submissions from Farmers yet this week.",
                ephemeral=True
            )

        lines = []
        for rank, r in enumerate(rows, 1):
            member = guild.get_member(r["user_id"])
            name   = member.mention if member else f"<@{r['user_id']}>"
            lines.append(f"**{rank}.** {name} — `{r['total']}`")

        await inter.response.send_message("\n".join(lines), ephemeral=True)

    # /quota review
    @quota.command(name="review", description="Review pending Farmer submissions")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def quota_review(self, inter: discord.Interaction):
        await self._table_ready.wait()

        rows = await self.db.pool.fetch(PENDING_REVIEW_SQL)
        if not rows:
            return await inter.response.send_message(
                "No pending submissions.", ephemeral=True
            )

        embed = discord.Embed(
            title="Pending quota submissions",
            colour=discord.Color.blue()
        )

        for r in rows:
            member = inter.guild.get_member(r["user_id"])
            name   = member.mention if member else f"<@{r['user_id']}>"
            embed.add_field(
                name=f"ID {r['id']}",
                value=f"{name} — `{r['amount']}`",
                inline=False
            )

        await inter.response.send_message(
            embed=embed,
            view=self.ReviewView(rows, self, inter.guild),
            ephemeral=True
        )

    # ---------------- Review UI ----------------
    class ReviewView(discord.ui.View):
        def __init__(self, rows, outer: "QuotaCog", guild: discord.Guild):
            super().__init__(timeout=600)
            for r in rows:
                member = guild.get_member(r["user_id"])
                disabled = not (member and outer._has_farmer_role(member))
                self.add_item(
                    QuotaCog.ReviewBtn(r["id"], r["user_id"], r["amount"],
                                       outer, disabled)
                )

    class ReviewBtn(discord.ui.Button):
        def __init__(self, sub_id, uid, amt, outer: "QuotaCog", disabled: bool):
            super().__init__(
                label=f"{sub_id} ✅",
                style=discord.ButtonStyle.success,
                disabled=disabled
            )
            self.sub_id, self.uid, self.amt, self.outer = sub_id, uid, amt, outer

        async def callback(self, inter: discord.Interaction):
            await self.outer.db.pool.execute(MARK_REVIEWED_SQL, self.sub_id)
            await inter.response.send_message(
                f"Submission **{self.sub_id}** marked reviewed.",
                ephemeral=True
            )
            self.disabled = True
            await inter.message.edit(view=self.view)

    # -------------------------------------------------------- #
    #               Weekly cleanup (Sunday 00:00 UTC)
    # -------------------------------------------------------- #
    @tasks.loop(hours=1)
    async def weekly_cleanup(self):
        await self._table_ready.wait()
        now = datetime.now(timezone.utc)
        if now.weekday() == 6 and now.hour == 0:          # Sunday midnight UTC
            await self.db.pool.execute(CLEAR_OLD_SQL)

    async def cog_unload(self):
        self.weekly_cleanup.cancel()

# ───────────────────────── setup hook ─────────────────────────
async def setup(bot, db):
    await bot.add_cog(QuotaCog(bot, db))