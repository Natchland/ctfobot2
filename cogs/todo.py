# cogs/todo.py – Server-wide To-Do list
# Works with discord.py ≥ 2.3  (persistent buttons)
# -----------------------------------------------------------------------

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("cog.todo")
log.setLevel(logging.INFO)

# ───────────────────────── CONFIG ─────────────────────────
GUILD_ID        = 1377035207777194005
TODO_CH_ID      = 1422698527342989322
MAX_CLAIMS_CAP  = 3         # slots per task
DAILY_USER_CAP  = 3         # max open claims / user
# ──────────────────────────────────────────────────────────


# ═════════════════════════ COG ═══════════════════════════
class TodoCog(commands.Cog):
    """/todo commands, task embeds & claim / complete buttons."""

    todo_group = app_commands.Group(name="todo", description="Server To-Do list")

    # ─────────────── lifecycle ───────────────
    def __init__(self, bot: commands.Bot, db):
        self.bot, self.db = bot, db
        self._started = False          # ensure on_ready runs once

    @commands.Cog.listener()
    async def on_ready(self):
        if self._started:
            return
        self._started = True

        # wait for DB pool created by the core bot
        while self.db.pool is None:
            await asyncio.sleep(0.5)

        # add the `title` column once (ignored if exists)
        await self.db.pool.execute(
            "ALTER TABLE todo_tasks ADD COLUMN IF NOT EXISTS title TEXT"
        )

        await self._initial_sync()
        log.info("TodoCog initialised")

    # ────────────── helpers ──────────────
    async def _embed(self, row: Dict[str, Any]) -> discord.Embed:
        title = row.get("title") or f"📝 Task #{row['id']}"
        e = discord.Embed(
            title=title,
            description=row["description"],
            colour=discord.Color.orange(),
            timestamp=row.get("created_at", datetime.now(timezone.utc)),
        )
        if row["max_claims"]:
            claimers = ", ".join(f"<@{u}>" for u in row["claimed"]) or "*None*"
            e.add_field(
                name=f"Claims ({len(row['claimed'])}/{row['max_claims']})",
                value=claimers,
                inline=False,
            )
            e.set_footer(text="Claimable task")
        else:
            e.set_footer(text="Global task – everyone can help")
        return e

    async def _refresh_msg(self, guild: discord.Guild, task_id: int):
        """
        Create / update / delete the message for one task and attach a
        persistent TaskView.
        """
        row = await self.db.pool.fetchrow(
            "SELECT * FROM todo_tasks WHERE id=$1", task_id
        )
        if not row:
            return
        row = dict(row)

        ch = guild.get_channel(TODO_CH_ID)
        if not isinstance(ch, discord.TextChannel):
            return

        try:
            msg = await ch.fetch_message(row["message_id"])
        except (discord.NotFound, discord.HTTPException):
            msg = None

        if row["completed"]:
            if msg:
                with contextlib.suppress(discord.Forbidden):
                    await msg.delete()
            return

        view = TaskView(self, task_id, row["max_claims"] > 0)

        if msg is None:
            msg = await ch.send(embed=await self._embed(row), view=view)
            await self.db.pool.execute(
                "UPDATE todo_tasks SET message_id=$1 WHERE id=$2",
                msg.id, task_id
            )
        else:
            await msg.edit(embed=await self._embed(row), view=view)

        # Register the view as persistent so it works after restarts
        self.bot.add_view(view, message_id=msg.id)

    async def _initial_sync(self):
        """Refresh every still-open task when the bot starts."""
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return

        rows: List[Dict[str, Any]] = await self.db.list_open_todos(guild.id)
        for r in rows:
            await self._refresh_msg(guild, r["id"])

        log.info("[todo] initial sync finished – %s open tasks", len(rows))

    # ═══════════ SLASH COMMANDS ════════════
    # /todo add ------------------------------------------------------
    @todo_group.command(name="add", description="Create a new task (staff only)")
    @app_commands.describe(
        title="Short title",
        description="Task details / instructions",
        claimable_slots=f"0 = global • 1-{MAX_CLAIMS_CAP} = claimable slots",
    )
    async def todo_add(
        self,
        inter: discord.Interaction,
        title: str,
        description: str,
        claimable_slots: app_commands.Range[int, 0, MAX_CLAIMS_CAP] = 0,
    ):
        if not inter.user.guild_permissions.manage_guild:
            return await inter.response.send_message("Staff only.", ephemeral=True)

        ch = inter.guild.get_channel(TODO_CH_ID)
        if not isinstance(ch, discord.TextChannel):
            return await inter.response.send_message("Todo channel missing.", ephemeral=True)

        await inter.response.defer(ephemeral=True)

        placeholder = await ch.send(embed=discord.Embed(
            title="Creating task …", description=description, colour=discord.Color.orange()
        ))

        await self.db.pool.execute(
            """
            INSERT INTO todo_tasks
                  (guild_id, creator_id, title, description,
                   max_claims, message_id)
            VALUES ($1,$2,$3,$4,$5,$6)
            """,
            inter.guild.id, inter.user.id, title, description,
            claimable_slots, placeholder.id,
        )
        task_id = await self.db.pool.fetchval(
            "SELECT id FROM todo_tasks WHERE message_id=$1", placeholder.id
        )

        await self._refresh_msg(inter.guild, task_id)
        await inter.followup.send(f"Task **#{task_id}** added.", ephemeral=True)

    # /todo list -----------------------------------------------------
    @todo_group.command(name="list", description="Show all open tasks")
    async def todo_list(self, inter: discord.Interaction):
        rows = await self.db.list_open_todos(inter.guild.id)
        if not rows:
            return await inter.response.send_message("No open tasks – 🎉", ephemeral=True)

        txt = "\n".join(
            f"• **#{r['id']}** – {r.get('title') or r['description']}"
            for r in rows
        )
        await inter.response.send_message(txt[:1990], ephemeral=True)


# ═════════════════ VIEW (persistent buttons) ═════════════════
class TaskView(discord.ui.View):
    def __init__(self, cog: TodoCog, task_id: int, claimable: bool):
        super().__init__(timeout=None)
        self.cog, self.task_id, self.claimable = cog, task_id, claimable

        if not claimable:
            for child in self.children:
                if child.custom_id in {"todo_claim", "todo_unclaim"}:
                    child.disabled = True

    # helper
    async def _ack(self, inter: discord.Interaction, msg: str):
        await self.cog._refresh_msg(inter.guild, self.task_id)
        await inter.followup.send(msg, ephemeral=True)

    # ---- Claim -----------------------------------------------------
    @discord.ui.button(
        label="Claim", style=discord.ButtonStyle.primary,
        emoji="🙋", custom_id="todo_claim"
    )
    async def claim(self, inter: discord.Interaction, _):
        await inter.response.defer(ephemeral=True)
        if not self.claimable:
            return await self._ack(inter, "Global task – can't claim.")

        ok = await self.cog.db.pool.fetchval(
            """
            UPDATE todo_tasks
               SET claimed = array_append(claimed, $2::bigint)
             WHERE id=$1
               AND NOT claimed @> ARRAY[$2::bigint]
               AND coalesce(array_length(claimed,1),0) < max_claims
             RETURNING TRUE
            """,
            self.task_id, inter.user.id,
        )
        if not ok:
            return await self._ack(inter, "Already claimed or slots full.")

        # user cap check
        if await self.cog.db.count_open_claims(inter.guild.id, inter.user.id) > DAILY_USER_CAP:
            await self.cog.db.pool.execute(
                "UPDATE todo_tasks "
                "SET claimed = array_remove(claimed,$2::bigint) "
                "WHERE id=$1",
                self.task_id, inter.user.id,
            )
            return await self._ack(inter, f"Claim limit ({DAILY_USER_CAP}) reached – reverted.")

        await self._ack(inter, "Claimed!")

    # ---- Unclaim ---------------------------------------------------
    @discord.ui.button(
        label="Unclaim", style=discord.ButtonStyle.secondary,
        emoji="↩️", custom_id="todo_unclaim"
    )
    async def unclaim(self, inter: discord.Interaction, _):
        await inter.response.defer(ephemeral=True)
        if not self.claimable:
            return await self._ack(inter, "Global task – can't claim.")

        await self.cog.db.pool.execute(
            "UPDATE todo_tasks "
            "SET claimed = array_remove(claimed,$2::bigint) "
            "WHERE id=$1",
            self.task_id, inter.user.id,
        )
        await self._ack(inter, "Unclaimed.")

    # ---- Complete --------------------------------------------------
    @discord.ui.button(
        label="Complete", style=discord.ButtonStyle.success,
        emoji="✅", custom_id="todo_complete"
    )
    async def complete(self, inter: discord.Interaction, _):
        await inter.response.defer(ephemeral=True)
        if not inter.user.guild_permissions.manage_guild:
            return await self._ack(inter, "Staff only.")

        await self.cog.db.complete_todo(self.task_id)
        await self._ack(inter, "Task completed!")


# ═════════════ setup entry-point ═════════════
async def setup(bot, db):
    await bot.add_cog(TodoCog(bot, db))