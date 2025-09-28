# cogs/recruit_reminder.py
"""
Recruitment-reminder system
───────────────────────────
• Keeps exactly ONE message in the recruitment channel.
• Mentions the recruitment staff role whenever the message (re)appears.
• “Accept” button lets a staff member claim a 6-hour shift.
• During a shift the message shows who claimed it and a relative countdown.
• Countdown is refreshed every 15 s → well under rate-limits.
• After 6 h the message resets, re-pings the role and re-enables the button.
• State is stored in Postgres so the system survives bot restarts.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

# ─────────────────────────────────────────────────────────────
#                CONFIG  – replace IDs if needed
# ─────────────────────────────────────────────────────────────
RECRUIT_CHANNEL_ID   = 1421856820460388383      # channel where the message lives
RECRUITMENT_ROLE_ID  = 1410659214959054988      # role that may press Accept

SHIFT_SECONDS        = 6 * 60 * 60              # 6-hour shift
UPDATE_INTERVAL      = 15                       # seconds between edits

# ─────────────────────────────────────────────────────────────
#                SQL – single-row helper table
# ─────────────────────────────────────────────────────────────
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS recruit_reminder (
    id          BOOLEAN  PRIMARY KEY DEFAULT TRUE,   -- always one row
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
    """Cog implementing the recruitment-reminder workflow."""

    # ---------------------------------------------------------
    #                     Start-up
    # ---------------------------------------------------------
    def __init__(self, bot: commands.Bot, db):
        self.bot, self.db = bot, db

        # background runner (15-s loop)
        self.update_message.start()

        # ensure the helper table exists once DB-pool is ready
        bot.loop.create_task(self._ensure_table())

    async def _ensure_table(self):
        """Create the table after db.connect() finished."""
        await self.bot.wait_until_ready()
        # db.pool may still be None if on_ready failed for some reason
        if self.db.pool is None:
            return
        async with self.db.pool.acquire() as conn:
            await conn.execute(CREATE_SQL)

    async def cog_unload(self):
        self.update_message.cancel()

    # ---------------------------------------------------------
    #                    Accept-button view
    # ---------------------------------------------------------
    class AcceptView(discord.ui.View):
        def __init__(self, outer: "RecruitReminder"):
            super().__init__(timeout=None)          # persistent
            self.outer = outer

        @discord.ui.button(
            label="Accept",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id="recruit_accept"              # required for persistence
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

            # store state
            await self.outer._set_state(
                message_id=inter.message.id,
                claimed_by=inter.user.id,
                end_ts=end_ts
            )

            # disable the button
            for c in self.children:
                c.disabled = True

            await inter.message.edit(
                content=(
                    f"✅ {inter.user.mention} is handling recruitment for the next "
                    f"<t:{end_ts}:R>."
                ),
                view=self
            )
            await inter.response.send_message("Thanks for taking the shift!", ephemeral=True)

    # ---------------------------------------------------------
    #              15-second background updater
    # ---------------------------------------------------------
    @tasks.loop(seconds=UPDATE_INTERVAL)
    async def update_message(self):
        # wait until bot is logged in, guild cache ready, table created
        await self.bot.wait_until_ready()
        if self.db.pool is None:
            return                                  # db not connected yet

        state = await self._get_state()

        # fetch channel
        channel = self.bot.get_channel(RECRUIT_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return

        now_ts = int(datetime.now(timezone.utc).timestamp())

        # ── 1. No message tracked → create one ──────────────────────────
        if state["message_id"] is None:
            msg = await channel.send(
                f"<@&{RECRUITMENT_ROLE_ID}> Any staff available to recruit?\n"
                "Click **Accept** below to claim the next six-hour shift.",
                view=self.AcceptView(self)
            )
            await self._set_state(message_id=msg.id, claimed_by=None, end_ts=None)
            return

        # ── 2. Fetch message; recreate if deleted ───────────────────────
        try:
            msg = await channel.fetch_message(state["message_id"])
        except discord.NotFound:
            await self._set_state(message_id=None, claimed_by=None, end_ts=None)
            return

        # ── 3. If shift active, show countdown / reset when expired ────
        if state["end_ts"]:
            if now_ts >= state["end_ts"]:          # shift over → reset
                await msg.edit(
                    content=(
                        f"<@&{RECRUITMENT_ROLE_ID}> Any staff available to recruit?\n"
                        "Click **Accept** below to claim the next six-hour shift."
                    ),
                    view=self.AcceptView(self)      # fresh enabled view
                )
                await self._set_state(
                    message_id=msg.id, claimed_by=None, end_ts=None
                )
            else:
                # update remaining time (~15 s granularity)
                await msg.edit(
                    content=(
                        f"✅ <@{state['claimed_by']}> is handling recruitment for the next "
                        f"<t:{state['end_ts']}:R>."
                    )
                )
        else:
            # ensure the Accept button is visible (after bot restart, etc.)
            if not msg.components:
                await msg.edit(view=self.AcceptView(self))

    # ---------------------------------------------------------
    #             Lightweight DB read/write helpers
    # ---------------------------------------------------------
    async def _get_state(self) -> dict[str, int | None]:
        async with self.db.pool.acquire() as conn:
            row = await conn.fetchrow(GET_SQL)
            if row:
                return dict(row)
            return {"message_id": None, "claimed_by": None, "end_ts": None}

    async def _set_state(self, *, message_id, claimed_by, end_ts):
        async with self.db.pool.acquire() as conn:
            await conn.execute(SET_SQL, message_id, claimed_by, end_ts)

    # ---------------------------------------------------------
    #    Re-attach AcceptView on bot restart (persistence)
    # ---------------------------------------------------------
    @commands.Cog.listener()
    async def on_ready(self):
        state = await self._get_state()
        if state["message_id"]:
            self.bot.add_view(self.AcceptView(self), message_id=state["message_id"])


# ═════════════════════════════════════════════════════════════
#                    standard setup() entry-point
# ═════════════════════════════════════════════════════════════
async def setup(bot, db):
    """Called by the main script to load the cog."""
    await bot.add_cog(RecruitReminder(bot, db))