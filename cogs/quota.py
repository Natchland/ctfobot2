# cogs/quota.py
from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ───────────────────────── CONFIG ──────────────────────────
FARMER_ROLE_ID      = 1379918816871448686
QUOTA_REVIEW_CH_ID  = 0
PUBLIC_QUOTA_CH_ID  = 1421945592522739824
RESOURCES           = ["stone", "sulfur", "metal", "wood"]
LEADERBOARD_SIZE    = 10
QUOTA_IMAGE_DIR     = "/data/quota_images"
PROGRESS_BAR_LEN    = 20

# ───────────────────────── SQL ─────────────────────────────
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS default_quotas (
    resource TEXT PRIMARY KEY,
    weekly   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS quotas (
    user_id  BIGINT,
    resource TEXT,
    weekly   INTEGER NOT NULL,
    PRIMARY KEY (user_id, resource)
);
CREATE TABLE IF NOT EXISTS quota_submissions (
    id        SERIAL PRIMARY KEY,
    user_id   BIGINT,
    resource  TEXT,
    amount    INTEGER,
    image_url TEXT,
    reviewed  BOOLEAN DEFAULT FALSE,
    week_ts   DATE    DEFAULT CURRENT_DATE
);
CREATE TABLE IF NOT EXISTS quota_tasks (
    id         SERIAL PRIMARY KEY,
    name       TEXT,
    start_sub  INTEGER,
    completed  BOOLEAN DEFAULT FALSE,
    message_id BIGINT
);
CREATE TABLE IF NOT EXISTS quota_task_needs (
    task_id   INTEGER REFERENCES quota_tasks(id) ON DELETE CASCADE,
    resource  TEXT,
    required  INTEGER,
    PRIMARY KEY (task_id, resource)
);
"""

SET_DEFAULT_SQL = """
INSERT INTO default_quotas (resource, weekly)
VALUES ($1,$2)
ON CONFLICT (resource) DO UPDATE SET weekly=$2
"""

SET_USER_SQL = """
INSERT INTO quotas (user_id, resource, weekly)
VALUES ($1,$2,$3)
ON CONFLICT (user_id, resource) DO UPDATE SET weekly=$3
"""

PENDING_GROUP_SQL = """
SELECT user_id,
       jsonb_object_agg(resource, total) AS res_totals,
       array_agg(id)                     AS ids
FROM (
    SELECT user_id,
           resource,
           SUM(amount)  AS total,
           array_agg(id) AS id
    FROM quota_submissions
    WHERE reviewed=FALSE
    GROUP BY user_id, resource
) t
GROUP BY user_id
ORDER BY user_id
"""

MARK_MANY_SQL = "UPDATE quota_submissions SET reviewed=TRUE WHERE id = ANY($1::INT[])"

LB_SQL = """
SELECT user_id, SUM(amount) AS total
FROM quota_submissions
WHERE reviewed=TRUE
  AND resource=$1
  AND week_ts = CURRENT_DATE
GROUP BY user_id
ORDER BY total DESC
LIMIT $2
"""

PURGE_OLD_SQL = """
DELETE FROM quota_submissions
WHERE week_ts < CURRENT_DATE - INTERVAL '8 days'
"""

# ═══════════════════════════ COG ═══════════════════════════
class QuotaCog(commands.Cog):
    """Weekly quotas, screenshot review, and one-off task quotas."""

    # ---------------- UI classes (declared first) ----------------
    class AcceptUserBtn(discord.ui.Button):
        def __init__(self, ids: List[int], uid: int, outer: "QuotaCog"):
            super().__init__(label=f"{uid} ✅", style=discord.ButtonStyle.success)
            self.ids, self.uid, self.outer = ids, uid, outer

        async def callback(self, inter: discord.Interaction):
            await self.outer.db.pool.execute(MARK_MANY_SQL, self.ids)
            await inter.response.send_message(
                f"Reviewed submissions from <@{self.uid}>.", ephemeral=True
            )
            self.disabled, self.label = True, "Reviewed"
            await inter.message.edit(view=self.view)
            await self.outer.refresh_all_tasks()

    class ReviewUsersView(discord.ui.View):
        def __init__(self, rows, outer: "QuotaCog"):
            super().__init__(timeout=600)
            for r in rows:
                self.add_item(QuotaCog.AcceptUserBtn(r["ids"], r["user_id"], outer))

    class SingleAcceptBtn(discord.ui.Button):
        def __init__(self, ids: List[int], outer: "QuotaCog"):
            super().__init__(label="Accept ✅", style=discord.ButtonStyle.success)
            self.ids, self.outer = ids, outer

        async def callback(self, inter: discord.Interaction):
            await self.outer.db.pool.execute(MARK_MANY_SQL, self.ids)
            await inter.response.send_message("Marked reviewed.", ephemeral=True)
            self.disabled, self.label = True, "Reviewed"
            await inter.message.edit(view=self.view, content="**(reviewed)**")
            await self.outer.refresh_all_tasks()

    class SingleAcceptView(discord.ui.View):
        def __init__(self, outer: "QuotaCog", ids: List[int]):
            super().__init__(timeout=None)
            self.add_item(QuotaCog.SingleAcceptBtn(ids, outer))

    # ---------------- init ----------------
    def __init__(self, bot: commands.Bot, db):
        self.bot, self.db = bot, db
        self._table_ready = asyncio.Event()
        os.makedirs(QUOTA_IMAGE_DIR, exist_ok=True)
        asyncio.create_task(self._prepare_tables())
        self.weekly_cleanup.start()

    async def _prepare_tables(self):
        while self.db.pool is None:
            await asyncio.sleep(1)
        async with self.db.pool.acquire() as conn:
            await conn.execute(CREATE_SQL)
        self._table_ready.set()
        asyncio.create_task(self.refresh_all_tasks())

    # ---------------- helpers ----------------
    @staticmethod
    def _has_farmer(member: discord.Member) -> bool:
        return any(r.id == FARMER_ROLE_ID for r in member.roles)

    async def _farmer_check(self, inter: discord.Interaction) -> bool:
        if inter.guild is None or not isinstance(inter.user, discord.Member):
            await inter.response.send_message("Run inside the guild.", ephemeral=True)
            return False
        if not self._has_farmer(inter.user):
            await inter.response.send_message(
                "You need the Farmer role.", ephemeral=True
            )
            return False
        return True

    async def _save_images(self, atts: List[discord.Attachment], res: str, uid: int):
        for att in atts[:10]:
            if att.content_type and att.content_type.startswith("image/"):
                ext = os.path.splitext(att.filename)[1] or ".png"
                name = f"{int(time.time())}_{uid}_{res}_{uuid.uuid4().hex[:8]}{ext}"
                await att.save(os.path.join(QUOTA_IMAGE_DIR, name))

    def _bar(self, have: int, need: int) -> str:
        pct = min(1, have / need) if need else 1
        filled = int(pct * PROGRESS_BAR_LEN)
        return "█" * filled + "─" * (PROGRESS_BAR_LEN - filled)

    # ---------------- staff ping helper ----------------
    async def _notify_staff(self, member, pairs, images, ids):
        ch = self.bot.get_channel(QUOTA_REVIEW_CH_ID)
        if not ch:
            return
        desc = "\n".join(f"• **{r}** `{a}`" for r, a in pairs.items())
        embed = discord.Embed(title=f"Submission from {member}", description=desc, colour=0x2ecc71)
        await ch.send(
            embed=embed,
            file=await images[0].to_file(),
            view=QuotaCog.SingleAcceptView(self, ids),
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    # ---------------- task refresh ----------------
    async def refresh_all_tasks(self):
        await self._table_ready.wait()
        async with self.db.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM quota_tasks WHERE completed=FALSE")
            for task in rows:
                await self._refresh_task_embed(conn, task)

    async def _refresh_task_embed(self, conn, task_row):
        task_id = task_row["id"]
        start   = task_row["start_sub"]
        needs   = await conn.fetch(
            "SELECT resource, required FROM quota_task_needs WHERE task_id=$1", task_id
        )
        prog: Dict[str, int] = {}
        for n in needs:
            val = await conn.fetchval(
                """
                SELECT COALESCE(SUM(amount),0)
                FROM quota_submissions
                WHERE reviewed=TRUE AND resource=$1 AND id>$2
                """,
                n["resource"], start
            )
            prog[n["resource"]] = val or 0

        completed = all(prog[n["resource"]] >= n["required"] for n in needs)

        lb = await conn.fetch(
            """
            SELECT user_id, SUM(amount) AS total
            FROM quota_submissions
            WHERE reviewed=TRUE AND id>$1
            GROUP BY user_id
            ORDER BY total DESC
            LIMIT 10
            """,
            start,
        )

        embed = discord.Embed(
            title=("✅ COMPLETED" if completed else f"Task: {task_row['name']}"),
            colour=discord.Color.green() if completed else discord.Color.blue(),
        )

        tot_need = sum(n["required"] for n in needs)
        tot_have = sum(min(prog[n["resource"]], n["required"]) for n in needs)
        embed.description = f"Progress **{tot_have}/{tot_need}**"

        for n in needs:
            have = min(prog[n["resource"]], n["required"])
            embed.add_field(
                name=f"{n['resource'].title()}  {have}/{n['required']}",
                value=self._bar(have, n["required"]),
                inline=False,
            )

        if lb:
            body = "\n".join(
                f"**{i+1}.** <@{r['user_id']}> `{r['total']}`"
                for i, r in enumerate(lb)
            )
            embed.add_field(name="Top Contributors", value=body, inline=False)

        ch = self.bot.get_channel(PUBLIC_QUOTA_CH_ID)
        if ch:
            try:
                msg = await ch.fetch_message(task_row["message_id"])
                await msg.edit(embed=embed)
            except discord.NotFound:
                pass

        if completed and not task_row["completed"]:
            await conn.execute("UPDATE quota_tasks SET completed=TRUE WHERE id=$1", task_id)

    # ═════════════ SLASH COMMANDS ═════════════
    quota = app_commands.Group(
        name="quota",
        description="Quota commands",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @quota.command(name="task", description="Create a multi-resource task")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(name="Task name",
                           details="Pairs: stone 20000 sulfur 5000 ...")
    async def task_create(self, inter: discord.Interaction, name: str, details: str):
        await self._table_ready.wait()
        tokens = re.sub(r"[,;:]", " ", details.lower()).split()
        pairs: Dict[str, int] = {}
        cur: Optional[str] = None
        for tok in tokens:
            if tok in RESOURCES:
                cur = tok
            elif tok.replace(",", "").replace("k", "").isdigit() and cur:
                amt = tok.replace(",", "")
                amt = int(float(amt.replace("k", "")) * (1000 if "k" in tok else 1))
                pairs[cur] = pairs.get(cur, 0) + amt
                cur = None
        if not pairs:
            return await inter.response.send_message("No valid pairs found.", ephemeral=True)

        embed = discord.Embed(title=f"Task: {name}", colour=discord.Color.blue())
        for r, need in pairs.items():
            embed.add_field(name=f"{r.title()}  0/{need}", value=self._bar(0, need), inline=False)

        ch = self.bot.get_channel(PUBLIC_QUOTA_CH_ID)
        if not ch:
            return await inter.response.send_message("Public channel missing.", ephemeral=True)

        msg = await ch.send(embed=embed)
        async with self.db.pool.acquire() as conn:
            start_id = await conn.fetchval("SELECT COALESCE(MAX(id),0) FROM quota_submissions")
            task_row = await conn.fetchrow(
                "INSERT INTO quota_tasks (name,start_sub,message_id) VALUES ($1,$2,$3) RETURNING id",
                name, start_id, msg.id
            )
            for r, need in pairs.items():
                await conn.execute(
                    "INSERT INTO quota_task_needs (task_id,resource,required) VALUES ($1,$2,$3)",
                    task_row["id"], r, need
                )
        await inter.response.send_message("Task created.", ephemeral=True)

    @quota.command(name="setdefault", description="Set global weekly quota")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(resource=[app_commands.Choice(name=r, value=r) for r in RESOURCES])
    async def set_default(self, inter: discord.Interaction,
                          resource: app_commands.Choice[str],
                          amount: app_commands.Range[int, 1, 1_000_000]):
        await self._table_ready.wait()
        await self.db.pool.execute(SET_DEFAULT_SQL, resource.value, amount)
        await inter.response.send_message(
            f"Set global {resource.value} quota to {amount}.", ephemeral=True
        )

    @quota.command(name="set", description="Set member weekly quota")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(resource=[app_commands.Choice(name=r, value=r) for r in RESOURCES])
    async def set_user_quota(self, inter: discord.Interaction,
                             member: discord.Member,
                             resource: app_commands.Choice[str],
                             amount: app_commands.Range[int, 1, 1_000_000]):
        await self._table_ready.wait()
        if not self._has_farmer(member):
            return await inter.response.send_message(
                f"{member.mention} lacks the Farmer role.", ephemeral=True
            )
        await self.db.pool.execute(SET_USER_SQL, member.id, resource.value, amount)
        await inter.response.send_message(
            f"Set {member.mention} {resource.value} quota to {amount}.", ephemeral=True
        )

    @quota.command(name="submit", description="Submit screenshots")
    @app_commands.choices(resource=[app_commands.Choice(name=r, value=r) for r in RESOURCES])
    async def submit(self, inter: discord.Interaction,
                     resource: app_commands.Choice[str],
                     amount: app_commands.Range[int, 1, 1_000_000]):
        await self._table_ready.wait()
        if inter.guild and not await self._farmer_check(inter):
            return

        imgs = [a for a in inter.attachments if a.content_type and a.content_type.startswith("image/")][:10]
        if not imgs:
            return await inter.response.send_message("Attach screenshots.", ephemeral=True)

        await self._save_images(imgs, resource.value, inter.user.id)

        ids: List[int] = []
        async with self.db.pool.acquire() as conn:
            for img in imgs:
                rec = await conn.fetchrow(
                    "INSERT INTO quota_submissions (user_id,resource,amount,image_url)"
                    " VALUES ($1,$2,$3,$4) RETURNING id",
                    inter.user.id, resource.value, amount, img.url
                )
                ids.append(rec["id"])

        await inter.response.send_message("Submission received.", ephemeral=True)
        await self._notify_staff(inter.user, {resource.value: amount}, imgs, ids)

    @quota.command(name="leaderboard", description="Weekly leaderboard")
    @app_commands.choices(resource=[app_commands.Choice(name=r, value=r) for r in RESOURCES])
    async def leaderboard(self, inter: discord.Interaction,
                          resource: app_commands.Choice[str]):
        await self._table_ready.wait()
        if inter.guild is None:
            return await inter.response.send_message("Run in guild.", ephemeral=True)

        rows = await self.db.pool.fetch(LB_SQL, resource.value, LEADERBOARD_SIZE)
        rows = [r for r in rows
                if (m := inter.guild.get_member(r["user_id"])) and self._has_farmer(m)]
        if not rows:
            return await inter.response.send_message("No data yet.", ephemeral=True)

        body = "\n".join(f"**{i+1}.** <@{r['user_id']}> `{r['total']}`"
                         for i, r in enumerate(rows))
        await inter.response.send_message(body, ephemeral=True)

    @quota.command(name="review", description="Review pending")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def review(self, inter: discord.Interaction):
        await self._table_ready.wait()
        rows = await self.db.pool.fetch(PENDING_GROUP_SQL)
        if not rows:
            return await inter.response.send_message("Nothing pending.", ephemeral=True)

        embed = discord.Embed(title="Pending submissions", colour=discord.Color.orange())
        for r in rows:
            member = inter.guild.get_member(r["user_id"]) or f"<@{r['user_id']}>"
            body = "\n".join(f"• **{res}** `{amt}`"
                             for res, amt in r["res_totals"].items())
            embed.add_field(name=str(member), value=body, inline=False)

        await inter.response.send_message(
            embed=embed, view=QuotaCog.ReviewUsersView(rows, self), ephemeral=True
        )

    # ═════════════ LISTENER ═════════════
    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if msg.guild or msg.author.bot or not msg.attachments:
            return

        toks = re.sub(r"[,;:]", " ", msg.content.lower()).split()
        pairs: Dict[str, int] = defaultdict(int)
        cur: Optional[str] = None
        for tok in toks:
            if tok in RESOURCES:
                cur = tok
            elif tok.replace(",", "").isdigit() and cur:
                pairs[cur] += int(tok.replace(",", ""))
                cur = None
        if not pairs:
            return await msg.channel.send("Provide resource amount pairs.")

        imgs = [a for a in msg.attachments if a.content_type and a.content_type.startswith("image/")][:10]
        if not imgs:
            return await msg.channel.send("Attach images.")

        guild = next((g for g in self.bot.guilds if g.get_member(msg.author.id)), None)
        if not guild:
            return
        member = guild.get_member(msg.author.id)
        if not member or not self._has_farmer(member):
            return await msg.channel.send("Farmer role required.")

        await self._save_images(imgs, "multi", member.id)

        ids: List[int] = []
        await self._table_ready.wait()
        async with self.db.pool.acquire() as conn:
            for img in imgs:
                for res, amt in pairs.items():
                    rec = await conn.fetchrow(
                        "INSERT INTO quota_submissions (user_id,resource,amount,image_url)"
                        " VALUES ($1,$2,$3,$4) RETURNING id",
                        member.id, res, amt, img.url
                    )
                    ids.append(rec["id"])

        await msg.channel.send("Submission received, thank you!")
        await self._notify_staff(member, pairs, imgs, ids)

    # ═════════════ TASKS ═════════════
    @tasks.loop(hours=1)
    async def weekly_cleanup(self):
        await self._table_ready.wait()
        now = datetime.now(timezone.utc)
        if now.weekday() == 6 and now.hour == 0:
            await self.db.pool.execute(PURGE_OLD_SQL)

    async def cog_unload(self):
        self.weekly_cleanup.cancel()

# ─────────────────── setup ───────────────────
async def setup(bot, db):
    await bot.add_cog(QuotaCog(bot, db))