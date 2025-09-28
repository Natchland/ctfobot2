# cogs/recruit_reminder.py
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ─────────────────────────── CONFIG ───────────────────────────
RECRUIT_CHANNEL_ID   = 1421856820460388383      # channel that hosts the reminder
RECRUITMENT_ROLE_ID  = 1410659214959054988      # staff role allowed to click “Accept”

SHIFT_SECONDS   = 6 * 60 * 60                  # 6-hour shift
UPDATE_INTERVAL = 15                           # how often we edit the message (s)

# ─────────────────────────── SQL ──────────────────────────────
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS recruit_reminder (
    id          BOOLEAN PRIMARY KEY DEFAULT TRUE,
    message_id  BIGINT,
    claimed_by  BIGINT,
    end_ts      BIGINT
);
INSERT INTO recruit_reminder (id) VALUES (TRUE)
ON CONFLICT (id) DO NOTHING;
"""
GET_SQL = "SELECT message_id, claimed_by, end_ts FROM recruit_reminder LIMIT 1"
SET_SQL = """
UPDATE recruit_reminder
SET message_id = $1, claimed_by = $2, end_ts = $3
WHERE id = TRUE;
"""


# ════════════════════════════ COG ════════════════════════════
class RecruitReminder(commands.Cog):
    """Recruitment-shift workflow with 6-hour window and auto-reset."""

    # ---------------------------------------------------------
    def __init__(self, bot: commands.Bot, db):
        self.bot, self.db = bot, db
        self._table_ready = asyncio.Event()          # set once helper table exists

        # start the updater (it will wait on _table_ready)
        self.update_message.start()

        # background task waits until db.pool exists, then creates the table
        asyncio.create_task(self._prepare_table())

    # ---------------------------------------------------------
    #           create helper table once pool exists
    # ---------------------------------------------------------
    async def _prepare_table(self):
        while self.db.pool is None:                 # wait for db.connect()
            await asyncio.sleep(1)
        async with self.db.pool.acquire() as conn:
            await conn.execute(CREATE_SQL)
        self._table_ready.set()                     # unleash updater

    # ---------------------------------------------------------
    #                  DB convenience helpers
    # ---------------------------------------------------------
    async def _get_state(self) -> dict[str, Optional[int]]:
        if self.db.pool is None:                    # pool not ready yet
            return {"message_id": None, "claimed_by": None, "end_ts": None}

        async with self.db.pool.acquire() as conn:
            try:
                row = await conn.fetchrow(GET_SQL)
            except asyncpg.UndefinedTableError:
                await conn.execute(CREATE_SQL)
                row = None
        return dict(row) if row else {"message_id": None,
                                      "claimed_by": None,
                                      "end_ts": None}

    async def _set_state(self, *, message_id, claimed_by, end_ts):
        if self.db.pool is None:                    # too early, silently ignore
            return
        async with self.db.pool.acquire() as conn:
            try:
                await conn.execute(SET_SQL, message_id, claimed_by, end_ts)
            except asyncpg.UndefinedTableError:
                await conn.execute(CREATE_SQL)
                await conn.execute(SET_SQL, message_id, claimed_by, end_ts)

    # ---------------------------------------------------------
    #                 Accept-button view
    # ---------------------------------------------------------
    class AcceptView(discord.ui.View):
        def __init__(self, outer: "RecruitReminder"):
            super().__init__(timeout=None)
            self.outer = outer

        @discord.ui.button(
            label="Accept",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id="recruit_accept"
        )
        async def accept(self, inter: discord.Interaction, _):
            guild = inter.guild
            role  = guild.get_role(RECRUITMENT_ROLE_ID)

            if not (
                inter.user.guild_permissions.administrator
                or (role and role in inter.user.roles)
            ):
                return await inter.response.send_message(
                    "You’re not recruitment staff.", ephemeral=True
                )

            end_ts = int(datetime.now(timezone.utc).timestamp()) + SHIFT_SECONDS
            await self.outer._set_state(
                message_id=inter.message.id,
                claimed_by=inter.user.id,
                end_ts=end_ts
            )

            # disable button
            for c in self.children:
                c.disabled = True

            await inter.message.edit(
                content=(
                    f"✅ {inter.user.mention} has sent out recruitment — "
                    f"next recruitment posts can be sent in **6 hours**."
                ),
                view=self
            )
            await inter.response.send_message("Shift accepted — thank you!", ephemeral=True)

    # ---------------------------------------------------------
    #                  15-second updater loop
    # ---------------------------------------------------------
    @tasks.loop(seconds=UPDATE_INTERVAL)
    async def update_message(self):
        await self._table_ready.wait()              # ensure table exists

        state   = await self._get_state()
        channel = self.bot.get_channel(RECRUIT_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return

        now_ts = int(datetime.now(timezone.utc).timestamp())

        # 1️⃣  no message stored → create prompt
        if state["message_id"] is None:
            msg = await channel.send(
                f"<@&{RECRUITMENT_ROLE_ID}> "
                "Click **Accept** below if you’re available to send out recruitment posts!",
                view=self.AcceptView(self)
            )
            await self._set_state(message_id=msg.id, claimed_by=None, end_ts=None)
            return

        # 2️⃣  fetch existing message, recreate if deleted
        try:
            msg = await channel.fetch_message(state["message_id"])
        except discord.NotFound:
            await self._set_state(message_id=None, claimed_by=None, end_ts=None)
            return

        # 3️⃣  shift active?
        if state["end_ts"]:
            if now_ts >= state["end_ts"]:           # expired → reset
                await msg.edit(
                    content=(
                        f"<@&{RECRUITMENT_ROLE_ID}> "
                        "Click **Accept** below if you’re available to send out recruitment posts!"
                    ),
                    view=self.AcceptView(self)
                )
                await self._set_state(message_id=msg.id, claimed_by=None, end_ts=None)
            else:
                remaining = state["end_ts"] - now_ts
                hrs, rem  = divmod(remaining, 3600)
                mins      = rem // 60
                await msg.edit(
                    content=(
                        f"✅ <@{state['claimed_by']}> has sent out recruitment — "
                        f"next recruitment posts can be sent in **{hrs} h {mins:02} m**."
                    )
                )
        else:                                       # idle but message exists
            if not msg.components:                  # make sure button visible
                await msg.edit(view=self.AcceptView(self))

    # ---------------------------------------------------------
    #              re-attach view on reconnect
    # ---------------------------------------------------------
    @commands.Cog.listener()
    async def on_ready(self):
        state = await self._get_state()
        if state["message_id"]:
            self.bot.add_view(self.AcceptView(self), message_id=state["message_id"])

    # ---------------------------------------------------------
    #               /recruitreset admin command
    # ---------------------------------------------------------
    @app_commands.command(
        name="recruitreset",
        description="Force-reset the recruitment reminder (admin only)"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def recruit_reset(self, inter: discord.Interaction):
        await self._set_state(message_id=None, claimed_by=None, end_ts=None)
        await inter.response.send_message(
            "Recruitment reminder will refresh within 15 seconds.",
            ephemeral=True
        )


# ─────────────────────────── setup hook ────────────────────────────
async def setup(bot, db):
    await bot.add_cog(RecruitReminder(bot, db))