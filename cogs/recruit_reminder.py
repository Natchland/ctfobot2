# cogs/recruit_reminder.py
# Production-ready • discord.py ≥ 2.3 • 2024-10-05
#
#  • A message in the #recruit channel lets recruitment staff press ✅ Accept
#    to “take the shift” and locks further posts for 6 h.
#  • The button is PERSISTENT – survives bot restarts.
#  • Table `recruit_reminder` is created automatically.
#  • Fix: view is re-attached only after the DB table is ready, so the button
#    always works after a reboot.

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

# ───────────── Server-specific constants ─────────────
RECRUIT_CHANNEL_ID  = 1421856820460388383          # channel where the reminder lives
RECRUITMENT_ROLE_ID = 1410659214959054988          # role allowed to accept

SHIFT_SECONDS   = 6 * 60 * 60                      # 6-hour cooldown
UPDATE_INTERVAL = 15                               # seconds between checks

# ───────────── SQL ─────────────
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS recruit_reminder (
    id         BOOLEAN PRIMARY KEY DEFAULT TRUE,
    message_id BIGINT,
    claimed_by BIGINT,
    end_ts     BIGINT
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


class RecruitReminder(commands.Cog):
    # ═════════════════ INITIALISATION ═════════════════
    def __init__(self, bot: commands.Bot, db):
        self.bot, self.db = bot, db

        # will be set after the table exists & pool ready
        self._table_ready = asyncio.Event()

        self.update_message.start()
        asyncio.create_task(self._prepare_table())

    # ═════════════════ DB helpers ═════════════════
    async def _prepare_table(self):
        """Wait for `db.pool`, then create the table once."""
        while self.db.pool is None:
            await asyncio.sleep(1)

        async with self.db.pool.acquire() as conn:
            await conn.execute(CREATE_SQL)
        self._table_ready.set()
        log.debug("[recruit] table ready")

    async def _get_state(self) -> dict[str, Optional[int]]:
        """Return dict with keys: message_id, claimed_by, end_ts (can be None)."""
        if self.db.pool is None:
            return {"message_id": None, "claimed_by": None, "end_ts": None}

        async with self.db.pool.acquire() as conn:
            try:
                row = await conn.fetchrow(GET_SQL)
            except asyncpg.UndefinedTableError:
                await conn.execute(CREATE_SQL)
                row = None
        return dict(row) if row else {"message_id": None, "claimed_by": None, "end_ts": None}

    async def _set_state(self, *, message_id, claimed_by, end_ts):
        if self.db.pool is None:
            return
        async with self.db.pool.acquire() as conn:
            try:
                await conn.execute(SET_SQL, message_id, claimed_by, end_ts)
            except asyncpg.UndefinedTableError:
                await conn.execute(CREATE_SQL)
                await conn.execute(SET_SQL, message_id, claimed_by, end_ts)

    # ═════════════════ Persistent VIEW ═════════════════
    class AcceptView(discord.ui.View):
        def __init__(self, outer: "RecruitReminder"):
            super().__init__(timeout=None)  # keep alive forever
            self.outer = outer

        @discord.ui.button(
            label="Accept",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id="recruit_accept"
        )
        async def accept(self, inter: discord.Interaction, _button: discord.ui.Button):
            guild = inter.guild
            role  = guild.get_role(RECRUITMENT_ROLE_ID) if guild else None
            if not (
                inter.user.guild_permissions.administrator
                or (role and role in inter.user.roles)
            ):
                return await inter.response.send_message(
                    "You’re not recruitment staff.", ephemeral=True
                )

            await inter.response.defer(ephemeral=True)

            try:
                end_ts = int(datetime.now(timezone.utc).timestamp()) + SHIFT_SECONDS
                await self.outer._set_state(
                    message_id=inter.message.id,
                    claimed_by=inter.user.id,
                    end_ts=end_ts,
                )

                for child in self.children:
                    child.disabled = True

                await inter.message.edit(
                    content=(
                        f"✅ {inter.user.mention} has sent out recruitment — "
                        f"next recruitment posts can be sent in **6 hours**."
                    ),
                    view=self,
                )
                await inter.followup.send("Shift accepted — thank you!", ephemeral=True)
            except Exception as exc:                         # noqa: BLE001
                log.exception("Recruit Accept callback failed: %s", exc)
                try:
                    await inter.followup.send(
                        "Something went wrong – try again later.", ephemeral=True
                    )
                except discord.HTTPException:
                    pass

    # ═════════════════ Periodic updater ═════════════════
    @tasks.loop(seconds=UPDATE_INTERVAL)
    async def update_message(self):
        await self._table_ready.wait()

        state   = await self._get_state()
        channel = self.bot.get_channel(RECRUIT_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return

        now_ts = int(datetime.now(timezone.utc).timestamp())

        # ---------- no message stored – create ----------
        if state["message_id"] is None:
            msg = await channel.send(
                f"<@&{RECRUITMENT_ROLE_ID}> "
                "Click **Accept** below if you’re available to send out recruitment posts!",
                view=self.AcceptView(self),
            )
            await self._set_state(message_id=msg.id, claimed_by=None, end_ts=None)
            return

        # ---------- fetch stored message ----------
        try:
            msg = await channel.fetch_message(state["message_id"])
        except discord.NotFound:                               # message deleted
            await self._set_state(message_id=None, claimed_by=None, end_ts=None)
            return

        # ---------- update content ----------
        if state["end_ts"]:                                    # shift active
            if now_ts >= state["end_ts"]:                      # shift expired
                await msg.edit(
                    content=(
                        f"<@&{RECRUITMENT_ROLE_ID}> "
                        "Click **Accept** below if you’re available to send out recruitment posts!"
                    ),
                    view=self.AcceptView(self),
                )
                await self._set_state(message_id=msg.id, claimed_by=None, end_ts=None)
            else:                                              # still locked
                remaining = state["end_ts"] - now_ts
                hrs, rem  = divmod(remaining, 3600)
                mins      = rem // 60
                await msg.edit(
                    content=(
                        f"✅ <@{state['claimed_by']}> has sent out recruitment — "
                        f"next recruitment posts can be sent in **{hrs} h {mins:02} m**."
                    )
                )
        else:                                                  # idle
            if not msg.components:
                await msg.edit(view=self.AcceptView(self))

    # ═════════════════ Persist view on reboot ═════════════════
    @commands.Cog.listener()
    async def on_ready(self):
        await self._table_ready.wait()
        state = await self._get_state()
        if state["message_id"]:
            try:
                # re-attach the persistent view so the button works
                self.bot.add_view(
                    self.AcceptView(self), message_id=state["message_id"]
                )
                log.info("[recruit] View reattached to message %s", state["message_id"])
            except Exception as e:                             # noqa: BLE001
                log.warning("Failed to add persistent view: %s", e)

    # ═════════════════ Admin command ═════════════════
    @app_commands.command(name="recruitreset",
                          description="Force-reset the recruitment reminder (admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def recruit_reset(self, inter: discord.Interaction):
        await self._set_state(message_id=None, claimed_by=None, end_ts=None)
        await inter.response.send_message(
            "Recruitment reminder will refresh within 15 seconds.", ephemeral=True
        )

    # ═════════════════ teardown ═════════════════
    def cog_unload(self):
        self.update_message.cancel()
        log.info("RecruitReminder unloaded")


async def setup(bot, db):
    await bot.add_cog(RecruitReminder(bot, db))