# cogs/recruit_reminder.py
"""
Recruitment-reminder system
──────────────────────────
• Keeps exactly ONE message in the recruitment channel.
• Mentions the recruitment staff role whenever the message (re)appears.
• “Accept” button lets a staff member claim a 6-hour shift.
• Shows live countdown (edited every 15 s).
• Resets automatically after 6 h.
• State is kept in Postgres and survives restarts.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import asyncpg
import discord
from discord.ext import commands, tasks

# ─────────────────────────────────────────────────────────────
#                CONFIG – adjust IDs if required
# ─────────────────────────────────────────────────────────────
RECRUIT_CHANNEL_ID   = 1421856820460388383     # channel for the reminder
RECRUITMENT_ROLE_ID  = 1410659214959054988     # staff role allowed to accept

SHIFT_SECONDS        = 6 * 60 * 60             # 6-hour shift
UPDATE_INTERVAL      = 15                      # seconds between edits

# ─────────────────────────────────────────────────────────────
#                SQL – single-row helper table
# ─────────────────────────────────────────────────────────────
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS recruit_reminder (
    id          BOOLEAN PRIMARY KEY DEFAULT TRUE,
    message_id  BIGINT,
    claimed_by  BIGINT,
    end_ts      BIGINT
);
INSERT INTO recruit_reminder (id)
VALUES (TRUE)
ON CONFLICT (id) DO NOTHING;
"""

GET_SQL = "SELECT message_id, claimed_by, end_ts FROM recruit_reminder LIMIT 1"

SET_SQL = """
UPDATE recruit_reminder
SET message_id = $1,
    claimed_by = $2,
    end_ts     = $3
WHERE id = TRUE;
"""


# ═════════════════════════════════════════════════════════════
class RecruitReminder(commands.Cog):
    """Implements the recruitment-shift workflow."""

    # ---------------------------------------------------------
    #           Cog initialisation & table creation
    # ---------------------------------------------------------
    def __init__(self, bot: commands.Bot, db):
        self.bot, self.db = bot, db
        self._table_ready = asyncio.Event()

        # kick off table-creation task
        asyncio.create_task(self._init_table())

    async def _init_table(self):
        """Wait until DB pool exists, then create the helper table."""
        await self.bot.wait_until_ready()

        # wait (max ~30 s) for db.connect() to finish
        for _ in range(30):
            if self.db.pool:
                break
            await asyncio.sleep(1)
        else:                                      # pool still None → give up
            return

        async with self.db.pool.acquire() as conn:
            await conn.execute(CREATE_SQL)

        self._table_ready.set()                    # signal readiness
        self.update_message.start()                # now start the loop

    async def cog_unload(self):
        if self.update_message.is_running():
            self.update_message.cancel()

    # ---------------------------------------------------------
    #                    Accept-button view
    # ---------------------------------------------------------
    class AcceptView(discord.ui.View):
        def __init__(self, outer: "RecruitReminder"):
            super().__init__(timeout=None)
            self.outer = outer

        @discord.ui.button(
            label="Accept",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id="recruit_accept"             # needed for persistence
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

            # disable the button
            for child in self.children:
                child.disabled = True

            await inter.message.edit(
                content=(
                    f"✅ {inter.user.mention} is handling recruitment for the next "
                    f"<t:{end_ts}:R>."
                ),
                view=self
            )
            await inter.response.send_message("Thanks for taking the shift!", ephemeral=True)

    # ---------------------------------------------------------
    #              15-second background updater loop
    # ---------------------------------------------------------
    @tasks.loop(seconds=UPDATE_INTERVAL)
    async def update_message(self):
        await self._table_ready.wait()             # ensure table exists

        state   = await self._get_state()
        channel = self.bot.get_channel(RECRUIT_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return

        now_ts = int(datetime.now(timezone.utc).timestamp())

        # 1️⃣  No message tracked → create one
        if state["message_id"] is None:
            msg = await channel.send(
                f"<@&{RECRUITMENT_ROLE_ID}> Any staff available to recruit?\n"
                "Click **Accept** below to claim the next six-hour shift.",
                view=self.AcceptView(self)
            )
            await self._set_state(message_id=msg.id, claimed_by=None, end_ts=None)
            return

        # 2️⃣  Fetch stored message; recreate if deleted
        try:
            msg = await channel.fetch_message(state["message_id"])
        except discord.NotFound:
            await self._set_state(message_id=None, claimed_by=None, end_ts=None)
            return

        # 3️⃣  Handle active / expired shift
        if state["end_ts"]:
            if now_ts >= state["end_ts"]:          # shift expired → reset
                await msg.edit(
                    content=(
                        f"<@&{RECRUITMENT_ROLE_ID}> Any staff available to recruit?\n"
                        "Click **Accept** below to claim the next six-hour shift."
                    ),
                    view=self.AcceptView(self)
                )
                await self._set_state(message_id=msg.id, claimed_by=None, end_ts=None)
            else:
                await msg.edit(
                    content=(
                        f"✅ <@{state['claimed_by']}> is handling recruitment for the next "
                        f"<t:{state['end_ts']}:R>."
                    )
                )
        else:
            # ensure button visible after bot restart
            if not msg.components:
                await msg.edit(view=self.AcceptView(self))

    # ---------------------------------------------------------
    #                 Lightweight DB wrappers
    # ---------------------------------------------------------
    async def _get_state(self) -> dict[str, int | None]:
        async with self.db.pool.acquire() as conn:
            try:
                row = await conn.fetchrow(GET_SQL)
            except asyncpg.UndefinedTableError:
                await conn.execute(CREATE_SQL)
                return {"message_id": None, "claimed_by": None, "end_ts": None}
        return dict(row) if row else {"message_id": None,
                                      "claimed_by": None,
                                      "end_ts": None}

    async def _set_state(self, *, message_id, claimed_by, end_ts):
        async with self.db.pool.acquire() as conn:
            try:
                await conn.execute(SET_SQL, message_id, claimed_by, end_ts)
            except asyncpg.UndefinedTableError:
                await conn.execute(CREATE_SQL)
                await conn.execute(SET_SQL, message_id, claimed_by, end_ts)

    # ---------------------------------------------------------
    #        Re-attach the AcceptView after (re)connect
    # ---------------------------------------------------------
    @commands.Cog.listener()
    async def on_ready(self):
        # Wait until table is ready before trying to fetch state
        await self._table_ready.wait()
        state = await self._get_state()
        if state["message_id"]:
            self.bot.add_view(self.AcceptView(self), message_id=state["message_id"])


# ─────────────────────────────────────────────────────────────
#                  standard setup() entry-point
# ─────────────────────────────────────────────────────────────
async def setup(bot, db):
    await bot.add_cog(RecruitReminder(bot, db))