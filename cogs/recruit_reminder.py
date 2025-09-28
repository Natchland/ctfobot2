# cogs/recruit_reminder.py
import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

RECRUIT_CHANNEL_ID   = 1421856820460388383      # 👈  replace if needed
SHIFT_SECONDS        = 6 * 60 * 60              # 6 h
UPDATE_EVERY_SECONDS = 15

# the role that is allowed to press “Accept”
RECRUITMENT_ROLE_ID  = 1410659214959054988


# ══════════════════════════════════════════════════════════════
#                       DATABASE HELPERS
# ══════════════════════════════════════════════════════════════
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS recruit_reminder (
    id          BOOLEAN PRIMARY KEY DEFAULT TRUE,      -- one-row table
    message_id  BIGINT,
    claimed_by  BIGINT,
    end_ts      BIGINT
);
INSERT INTO recruit_reminder (id) VALUES (TRUE)
ON CONFLICT (id) DO NOTHING;
"""

GET_SQL    = "SELECT message_id, claimed_by, end_ts FROM recruit_reminder LIMIT 1"
SET_SQL    = """
UPDATE recruit_reminder
SET message_id=$1, claimed_by=$2, end_ts=$3
WHERE id = TRUE
"""


class RecruitReminder(commands.Cog):
    def __init__(self, bot: commands.Bot, db):
        self.bot, self.db = bot, db
        self.update_message.start()                # background task

    async def cog_load(self):
        async with self.db.pool.acquire() as conn:
            await conn.execute(CREATE_SQL)

    async def cog_unload(self):
        self.update_message.cancel()

    # ------------------------------------------------------------
    #                       UI  (persistent)
    # ------------------------------------------------------------
    class AcceptView(discord.ui.View):
        def __init__(self, outer: "RecruitReminder"):
            super().__init__(timeout=None)
            self.outer = outer

        @discord.ui.button(
            label="Accept",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id="recruit_accept"          # needed for persistence
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

            # store in DB
            await self.outer._set_state(
                message_id=inter.message.id,
                claimed_by=inter.user.id,
                end_ts=end_ts
            )

            # disable the button
            for c in self.children:
                c.disabled = True

            # update the message
            await inter.message.edit(
                content=(
                    f"✅ {inter.user.mention} is handling recruitment for the next "
                    f"<t:{end_ts}:R>."
                ),
                view=self
            )
            await inter.response.send_message("Thanks for taking the shift!", ephemeral=True)

    # ------------------------------------------------------------
    #                 15-second background updater
    # ------------------------------------------------------------
    @tasks.loop(seconds=UPDATE_EVERY_SECONDS)
    async def update_message(self):
        await self.bot.wait_until_ready()
        state = await self._get_state()
        channel: discord.TextChannel | None = self.bot.get_channel(RECRUIT_CHANNEL_ID)
        if channel is None:
            return

        now_ts = int(datetime.now(timezone.utc).timestamp())

        # 1) ─── no message tracked → create a fresh one ───
        if state["message_id"] is None:
            msg = await channel.send(
                f"<@&{RECRUITMENT_ROLE_ID}> Any staff available to recruit?\n"
                "Click **Accept** below to claim the next six-hour shift.",
                view=self.AcceptView(self)
            )
            await self._set_state(message_id=msg.id, claimed_by=None, end_ts=None)
            return

        # 2) ─── fetch the message ───
        try:
            msg = await channel.fetch_message(state["message_id"])
        except discord.NotFound:
            # message was deleted – clear state so we recreate it next tick
            await self._set_state(message_id=None, claimed_by=None, end_ts=None)
            return

        # 3) ─── if shift active, show remaining time ───
        if state["end_ts"]:
            if now_ts >= state["end_ts"]:                 # shift expired → reset
                await msg.edit(
                    content=(
                        f"<@&{RECRUITMENT_ROLE_ID}> Any staff available to recruit?\n"
                        "Click **Accept** below to claim the next six-hour shift."
                    ),
                    view=self.AcceptView(self)            # new, enabled view
                )
                await self._set_state(
                    message_id=msg.id, claimed_by=None, end_ts=None
                )
            else:
                # update remaining time string (~15 s accuracy)
                claimer_mention = f"<@{state['claimed_by']}>"
                await msg.edit(
                    content=(
                        f"✅ {claimer_mention} is handling recruitment for the next "
                        f"<t:{state['end_ts']}:R>."
                    )
                )
        else:
            # ensure button view is attached (e.g. after bot restart)
            if not msg.components:                         # no buttons visible
                await msg.edit(view=self.AcceptView(self))

    # ------------------------------------------------------------
    #                 Lightweight DB wrappers
    # ------------------------------------------------------------
    async def _get_state(self) -> dict:
        async with self.db.pool.acquire() as conn:
            row = await conn.fetchrow(GET_SQL)
            return dict(row) if row else {"message_id": None, "claimed_by": None, "end_ts": None}

    async def _set_state(self, *, message_id, claimed_by, end_ts):
        async with self.db.pool.acquire() as conn:
            await conn.execute(SET_SQL, message_id, claimed_by, end_ts)

    # ------------------------------------------------------------
    #      Restart-persistence: re-add AcceptView on cog load
    # ------------------------------------------------------------
    @commands.Cog.listener()
    async def on_ready(self):
        state = await self._get_state()
        if state["message_id"]:
            self.bot.add_view(self.AcceptView(self), message_id=state["message_id"])


# ──────────────────────────  setup  entry-point ──────────────────────────
async def setup(bot, db):
    await bot.add_cog(RecruitReminder(bot, db))