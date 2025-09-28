# cogs/quota.py
from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ───────────────────────── CONFIG ─────────────────────────
GROUP_LEADER_ROLE_ID = 1377077466513932338
PLAYER_MGMT_ROLE_ID  = 1377084533706588201
ADMIN_ROLE_ID        = 1377103244089622719

QUOTA_REVIEW_CH_ID   = 1421920458437169254
PUBLIC_QUOTA_CH_ID   = 1421945592522739824

RESOURCES            = ["wood", "stone", "metal", "sulfur"]
PROGRESS_BAR_LEN     = 25
TASK_TOP_LIMIT       = 5
GLOBAL_LB_SIZE       = 15
LB_REFRESH_SECONDS   = 15          # seconds between embed edits
QUOTA_IMAGE_DIR      = "/data/quota_images"

# ─────  Weekly reset time  ─────
# Reset every THURSDAY 15:00 BST  ≙  14:00 UTC (fixed)
RESET_WEEKDAY        = 3          # 0=Mon … 3=Thu
RESET_HOUR_UTC       = 14         # 14:00 UTC  (15:00 BST/GMT+1)

# ───────────────────────── SQL ─────────────────────────
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
    week_ts   DATE DEFAULT CURRENT_DATE
);
CREATE TABLE IF NOT EXISTS quota_tasks (
    id         SERIAL PRIMARY KEY,
    name       TEXT,
    start_sub  INTEGER,
    completed  BOOLEAN DEFAULT FALSE,
    message_id BIGINT
);
CREATE TABLE IF NOT EXISTS quota_task_needs (
    task_id  INTEGER REFERENCES quota_tasks(id) ON DELETE CASCADE,
    resource TEXT,
    required INTEGER,
    PRIMARY KEY (task_id, resource)
);
CREATE TABLE IF NOT EXISTS quota_weekly_lb (
    id SERIAL PRIMARY KEY,
    message_id BIGINT
);
"""

SET_DEFAULT_SQL = """
INSERT INTO default_quotas (resource,weekly)
VALUES ($1,$2)
ON CONFLICT(resource) DO UPDATE SET weekly=$2
"""
SET_USER_SQL = """
INSERT INTO quotas (user_id,resource,weekly)
VALUES ($1,$2,$3)
ON CONFLICT(user_id,resource) DO UPDATE SET weekly=$3
"""

PENDING_GROUP_SQL = """
SELECT user_id,
       jsonb_object_agg(resource,total) AS res_totals,
       array_agg(id)                    AS ids
FROM (
    SELECT user_id,resource,SUM(amount) total,array_agg(id) id
    FROM quota_submissions
    WHERE reviewed=FALSE
    GROUP BY user_id,resource
) t
GROUP BY user_id
ORDER BY user_id
"""

MARK_MANY_SQL = "UPDATE quota_submissions SET reviewed=TRUE WHERE id = ANY($1::INT[])"

GLOBAL_LB_SQL = """
SELECT user_id,SUM(amount) AS total
FROM quota_submissions
WHERE reviewed=TRUE AND week_ts>=CURRENT_DATE - INTERVAL '6 days'
GROUP BY user_id
ORDER BY total DESC
LIMIT $1
"""

RESOURCE_TOT_SQL = """
SELECT resource,SUM(amount) total
FROM quota_submissions
WHERE reviewed=TRUE AND week_ts>=CURRENT_DATE - INTERVAL '6 days'
GROUP BY resource
"""

PURGE_OLD_SQL = "DELETE FROM quota_submissions WHERE week_ts < CURRENT_DATE - INTERVAL '15 days'"

# ──────────────────── ROLE HELPERS ────────────────────
def _has(member: discord.Member, *ids: int) -> bool:
    return any(r.id in ids for r in member.roles)

def role_check(*ids: int) -> Callable:
    async def predicate(inter: discord.Interaction) -> bool:
        if isinstance(inter.user, discord.Member) and _has(inter.user, *ids):
            return True
        raise app_commands.CheckFailure("You don't have permission for that command.")
    return app_commands.check(predicate)

# ════════════════════════  COG  ════════════════════════
class QuotaCog(commands.Cog):
    # ──────────── Buttons / Views ────────────
    class AcceptUserBtn(discord.ui.Button):
        def __init__(self, ids: List[int], outer: "QuotaCog"):
            super().__init__(label="Accept ✅", style=discord.ButtonStyle.success)
            self.ids, self.outer = ids, outer
        async def callback(self, inter: discord.Interaction):
            await self.outer.db.pool.execute(MARK_MANY_SQL, self.ids)
            await inter.response.send_message("Reviewed.", ephemeral=True)
            self.disabled, self.label = True, "Reviewed"
            await inter.message.edit(view=self.view)
            await self.outer.refresh_everything()

    class ReviewUsersView(discord.ui.View):
        def __init__(self, rows, outer: "QuotaCog"):
            super().__init__(timeout=600)
            for r in rows:
                self.add_item(QuotaCog.AcceptUserBtn(r["ids"], outer))

    class ResetConfirmBtn(discord.ui.Button):
        def __init__(self, outer: "QuotaCog"):
            super().__init__(label="Wipe All Data", style=discord.ButtonStyle.danger)
            self.outer = outer
        async def callback(self, inter: discord.Interaction):
            await self.outer._wipe_all_data()
            await inter.response.edit_message(content="✅ Data wiped.", embed=None, view=None)

    class ResetConfirmView(discord.ui.View):
        def __init__(self, outer: "QuotaCog"):
            super().__init__(timeout=60)
            self.add_item(QuotaCog.ResetConfirmBtn(outer))

    # ──────────── INIT ────────────
    def __init__(self, bot: commands.Bot, db):
        self.bot, self.db = bot, db
        self._table_ready = asyncio.Event()
        os.makedirs(QUOTA_IMAGE_DIR, exist_ok=True)
        asyncio.create_task(self._prepare_db())
        self.weekly_cleanup.start()
        self.lb_refresher.start()

    async def _prepare_db(self):
        while self.db.pool is None:
            await asyncio.sleep(1)
        async with self.db.pool.acquire() as conn:
            await conn.execute(CREATE_SQL)
        self._table_ready.set()
        await self.refresh_everything()

    # ──────────── HELPERS ────────────
    @staticmethod
    def _bar(have: int, need: int, length: int = PROGRESS_BAR_LEN) -> str:
        if need <= 0:
            return "—"
        pct = min(1.0, have / need)
        filled = round(pct * length)
        return f"{'▰'*filled}{'▱'*(length-filled)}  {pct*100:5.1f}%"

    @staticmethod
    def _box(n: int, width: int = 9) -> str:
        return f"{n:,}".ljust(width)

    @staticmethod
    def _next_reset(now: datetime) -> datetime:
        """Return next Thursday 14:00 UTC."""
        days = (RESET_WEEKDAY - now.weekday()) % 7
        reset = datetime(
            year=now.year,
            month=now.month,
            day=now.day,
            tzinfo=timezone.utc
        ) + timedelta(days=days)
        reset = reset.replace(hour=RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
        if reset <= now:
            reset += timedelta(days=7)
        return reset

    def _countdown(self) -> str:
        now = datetime.now(timezone.utc)
        delta = self._next_reset(now) - now
        days, rem = divmod(int(delta.total_seconds()), 86400)
        hours, rem = divmod(rem, 3600)
        mins = rem // 60
        return f"{days}d {hours}h {mins}m"

    async def _save_images(self, atts: List[discord.Attachment], res: str, uid: int):
        for att in atts[:10]:
            if att.content_type and att.content_type.startswith("image/"):
                ext = os.path.splitext(att.filename)[1] or ".png"
                name = f"{int(time.time())}_{uid}_{res}_{uuid.uuid4().hex[:8]}{ext}"
                await att.save(os.path.join(QUOTA_IMAGE_DIR, name))

    async def _notify_staff(self, member, pairs, images, ids):
        ch = self.bot.get_channel(QUOTA_REVIEW_CH_ID)
        if not ch:
            return
        desc = "\n".join(f"• **{r}** `{a}`" for r, a in pairs.items())
        embed = discord.Embed(title=f"Submission from {member}", description=desc, colour=0x2ecc71)
        await ch.send(embed=embed, file=await images[0].to_file(),
                      view=QuotaCog.SingleAcceptView(self, ids),
                      allowed_mentions=discord.AllowedMentions(users=True))

    async def refresh_everything(self):
        await self.refresh_all_tasks()
        await self.refresh_weekly_leaderboard()

    # ──────────── TASK EMBEDS (unchanged) ────────────
    async def refresh_all_tasks(self):
        await self._table_ready.wait()
        async with self.db.pool.acquire() as conn:
            tasks_rows = await conn.fetch("SELECT * FROM quota_tasks WHERE completed=FALSE")
            for t in tasks_rows:
                await self._refresh_task_embed(conn, t)

    async def _refresh_task_embed(self, conn, task):
        task_id, start = task["id"], task["start_sub"]
        needs = await conn.fetch("SELECT resource,required FROM quota_task_needs WHERE task_id=$1", task_id)
        prog = {n["resource"]: 0 for n in needs}
        for n in needs:
            prog[n["resource"]] = await conn.fetchval(
                "SELECT COALESCE(SUM(amount),0) FROM quota_submissions "
                "WHERE reviewed=TRUE AND resource=$1 AND id>$2",
                n["resource"], start) or 0

        completed = all(prog[n["resource"]] >= n["required"] for n in needs)

        lb = await conn.fetch(
            "SELECT user_id,SUM(amount) total FROM quota_submissions "
            "WHERE reviewed=TRUE AND id>$1 GROUP BY user_id ORDER BY total DESC LIMIT $2",
            start, TASK_TOP_LIMIT)

        embed = discord.Embed(
            title="✅ COMPLETED" if completed else f"Task: {task['name']}",
            colour=discord.Color.green() if completed else discord.Color.blue())

        need_sum = sum(n["required"] for n in needs)
        have_sum = sum(min(prog[n["resource"]], n["required"]) for n in needs)
        embed.description = f"**Overall Progress**\n`{have_sum:,}` / `{need_sum:,}`\n{self._bar(have_sum, need_sum)}"
        embed.clear_fields()
        for n in needs:
            res, need = n["resource"], n["required"]
            have = min(prog[res], need)
            embed.add_field(name=res.title(),
                            value=f"`{have:,}` / `{need:,}`\n{self._bar(have, need)}",
                            inline=False)

        if lb:
            embed.add_field(name="Top 5",
                            value="\n".join(f"**{i+1}.** <@{r['user_id']}> `{r['total']:,}`"
                                            for i, r in enumerate(lb, 1)),
                            inline=False)

        ch = self.bot.get_channel(PUBLIC_QUOTA_CH_ID)
        if ch:
            try:
                m = await ch.fetch_message(task["message_id"])
                await m.edit(embed=embed)
            except discord.NotFound:
                pass

        if completed and not task["completed"]:
            await conn.execute("UPDATE quota_tasks SET completed=TRUE WHERE id=$1", task_id)

    # ──────────── WEEKLY LEADERBOARD ────────────
    async def refresh_weekly_leaderboard(self):
        await self._table_ready.wait()
        ch = self.bot.get_channel(PUBLIC_QUOTA_CH_ID)
        if not ch:
            return

        async with self.db.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT message_id FROM quota_weekly_lb LIMIT 1")
            msg: Optional[discord.Message] = None
            if row:
                try:
                    msg = await ch.fetch_message(row["message_id"])
                except discord.NotFound:
                    msg = None

            quotas = {r["resource"]: r["weekly"] for r in await conn.fetch("SELECT * FROM default_quotas")}
            totals = {r["resource"]: r["total"] for r in await conn.fetch(RESOURCE_TOT_SQL)}

            embed = discord.Embed(
                title="Weekly Leaderboard",
                description=(f"**Weekly reset:** Thursday 15:00 BST / 14:00 UTC\n"
                             f"Resets in **{self._countdown()}**"),
                colour=discord.Color.purple())

            # overall bar
            overall_have = sum(totals.get(r, 0) for r in RESOURCES)
            overall_need = sum(quotas.get(r, 0) for r in RESOURCES) or 1
            embed.add_field(name="Overall",
                            value=f"`{overall_have:,}` / `{overall_need:,}`\n{self._bar(overall_have, overall_need)}",
                            inline=False)

            # per-resource
            for r in RESOURCES:
                have, need = totals.get(r, 0), quotas.get(r, max(totals.get(r, 0), 1))
                embed.add_field(name=r.title(),
                                value=f"`{have:,}` / `{need:,}`\n{self._bar(have, need)}",
                                inline=False)

            # contributors
            users = await conn.fetch(GLOBAL_LB_SQL, GLOBAL_LB_SIZE)
            if users:
                header = "#  User (Top 15)             Total"
                guild = ch.guild
                rows = []
                for i, u in enumerate(users, 1):
                    disp = (guild.get_member(u["user_id"]).display_name
                            if guild and guild.get_member(u["user_id"]) else f"ID:{u['user_id']}")
                    rows.append(f"{str(i).ljust(2)} {disp[:25].ljust(25)} {self._box(u['total'])}")
                embed.add_field(name="Top Contributors",
                                value="```text\n" + "\n".join([header, *rows]) + "\n```",
                                inline=False)
            else:
                embed.add_field(name="Top Contributors",
                                value="No reviewed submissions yet.", inline=False)

            if msg:
                await msg.edit(embed=embed)
            else:
                new = await ch.send(embed=embed)
                await conn.execute("INSERT INTO quota_weekly_lb (message_id) VALUES ($1)", new.id)

    # ──────────── BACKGROUND TASKS ────────────
    @tasks.loop(seconds=LB_REFRESH_SECONDS)
    async def lb_refresher(self):
        await self.refresh_weekly_leaderboard()

    @tasks.loop(hours=1)
    async def weekly_cleanup(self):
        await self._table_ready.wait()
        now = datetime.now(timezone.utc)
        # Thursday 14:00 UTC cleanup
        if now.weekday() == RESET_WEEKDAY and now.hour == RESET_HOUR_UTC:
            async with self.db.pool.acquire() as conn:
                row = await conn.fetchrow("SELECT message_id FROM quota_weekly_lb LIMIT 1")
                ch = self.bot.get_channel(PUBLIC_QUOTA_CH_ID)
                if row and ch:
                    try:
                        m = await ch.fetch_message(row["message_id"]); await m.delete()
                    except discord.NotFound:
                        pass
                await conn.execute("TRUNCATE quota_weekly_lb RESTART IDENTITY")
        await self.db.pool.execute(PURGE_OLD_SQL)

    async def cog_unload(self):
        self.lb_refresher.cancel()
        self.weekly_cleanup.cancel()

    # ──────────── SLASH COMMANDS ────────────
    quota = app_commands.Group(name="quota", description="Quota commands")

    # ----- SUBMIT -----
    @quota.command(name="submit", description="Submit up to 10 screenshots")
    @app_commands.choices(resource=[app_commands.Choice(name=r, value=r) for r in RESOURCES])
    @app_commands.describe(
        image1="Screenshot 1", image2="Screenshot 2", image3="Screenshot 3",
        image4="Screenshot 4", image5="Screenshot 5", image6="Screenshot 6",
        image7="Screenshot 7", image8="Screenshot 8", image9="Screenshot 9", image10="Screenshot 10"
    )
    async def submit(
        self, inter: discord.Interaction,
        resource: app_commands.Choice[str],
        amount: app_commands.Range[int, 1, 1_000_000],
        image1: Optional[discord.Attachment] = None, image2: Optional[discord.Attachment] = None,
        image3: Optional[discord.Attachment] = None, image4: Optional[discord.Attachment] = None,
        image5: Optional[discord.Attachment] = None, image6: Optional[discord.Attachment] = None,
        image7: Optional[discord.Attachment] = None, image8: Optional[discord.Attachment] = None,
        image9: Optional[discord.Attachment] = None, image10: Optional[discord.Attachment] = None
    ):
        await self._table_ready.wait()
        imgs = [i for i in (image1, image2, image3, image4, image5,
                            image6, image7, image8, image9, image10) if i][:10]
        if not imgs:
            return await inter.response.send_message("Attach at least one image.", ephemeral=True)
        await self._save_images(imgs, resource.value, inter.user.id)
        ids: List[int] = []
        async with self.db.pool.acquire() as conn:
            for img in imgs:
                rec_id = await conn.fetchval(
                    "INSERT INTO quota_submissions "
                    "(user_id,resource,amount,image_url) VALUES ($1,$2,$3,$4) RETURNING id",
                    inter.user.id, resource.value, amount, img.url)
                ids.append(rec_id)
        await inter.response.send_message("Submission received.", ephemeral=True)
        await self._notify_staff(inter.user, {resource.value: amount}, imgs, ids)

    # ----- INDIVIDUAL RESOURCE LEADERBOARD -----
    @quota.command(name="leaderboard", description="Per-resource weekly leaderboard")
    @app_commands.choices(resource=[app_commands.Choice(name=r, value=r) for r in RESOURCES])
    async def leaderboard(self, inter: discord.Interaction, resource: app_commands.Choice[str]):
        await self._table_ready.wait()
        rows = await self.db.pool.fetch(
            "SELECT user_id,SUM(amount) total FROM quota_submissions "
            "WHERE reviewed=TRUE AND resource=$1 AND week_ts>=CURRENT_DATE - INTERVAL '6 days' "
            "GROUP BY user_id ORDER BY total DESC LIMIT 15",
            resource.value)
        if not rows:
            return await inter.response.send_message("No data yet.", ephemeral=True)
        body = "\n".join(f"**{i+1}.** <@{r['user_id']}> `{r['total']:,}`" for i, r in enumerate(rows, 1))
        await inter.response.send_message(body, ephemeral=True)

    # privilege decorators
    leader_perm = role_check(GROUP_LEADER_ROLE_ID, PLAYER_MGMT_ROLE_ID, ADMIN_ROLE_ID)
    admin_perm  = role_check(ADMIN_ROLE_ID)

    # ----- TASK -----
    @quota.command(name="task", description="Create a multi-resource task")
    @leader_perm
    @app_commands.describe(name="Task name", details="Pairs e.g. stone 20000 sulfur 5k")
    async def task_create(self, inter: discord.Interaction, name: str, details: str):
        await self._table_ready.wait()
        toks = re.sub(r"[,;:]", " ", details.lower()).split()
        pairs: Dict[str, int] = {}
        cur: Optional[str] = None
        for tok in toks:
            if tok in RESOURCES:
                cur = tok
            elif tok.replace(",", "").replace("k", "").isdigit() and cur:
                amt = int(float(tok.replace(",", "").replace("k", "")) * (1000 if "k" in tok else 1))
                pairs[cur] = pairs.get(cur, 0) + amt
                cur = None
        if not pairs:
            return await inter.response.send_message("No valid pairs.", ephemeral=True)

        embed = discord.Embed(title=f"Task: {name}", colour=discord.Color.blue())
        for res, need in pairs.items():
            embed.add_field(name=res.title(),
                            value=f"`0` / `{need:,}`\n{self._bar(0, need)}",
                            inline=False)

        ch = self.bot.get_channel(PUBLIC_QUOTA_CH_ID)
        if not ch:
            return await inter.response.send_message("Public channel missing.", ephemeral=True)
        msg = await ch.send(embed=embed)

        async with self.db.pool.acquire() as conn:
            start_id = await conn.fetchval("SELECT COALESCE(MAX(id),0) FROM quota_submissions")
            task_id  = await conn.fetchval(
                "INSERT INTO quota_tasks (name,start_sub,message_id) VALUES ($1,$2,$3) RETURNING id",
                name, start_id, msg.id)
            for res, need in pairs.items():
                await conn.execute(
                    "INSERT INTO quota_task_needs (task_id,resource,required) VALUES ($1,$2,$3)",
                    task_id, res, need)
        await inter.response.send_message("Task created.", ephemeral=True)
        await self.refresh_everything()

    # ----- REVIEW -----
    @quota.command(name="review", description="Review pending submissions")
    @leader_perm
    async def review(self, inter: discord.Interaction):
        await self._table_ready.wait()
        rows = await self.db.pool.fetch(PENDING_GROUP_SQL)
        if not rows:
            return await inter.response.send_message("Nothing pending.", ephemeral=True)
        embed = discord.Embed(title="Pending submissions", colour=discord.Color.orange())
        for r in rows:
            body = "\n".join(f"• **{k}** `{v}`" for k, v in r["res_totals"].items())
            embed.add_field(name=f"<@{r['user_id']}>", value=body, inline=False)
        await inter.response.send_message(embed=embed, view=QuotaCog.ReviewUsersView(rows, self), ephemeral=True)

    # ----- SETDEFAULT -----
    @quota.command(name="setdefault", description="Set global weekly quota")
    @leader_perm
    @app_commands.choices(resource=[app_commands.Choice(name=r, value=r) for r in RESOURCES])
    async def set_default(self, inter: discord.Interaction,
                          resource: app_commands.Choice[str],
                          amount: app_commands.Range[int, 1, 1_000_000]):
        await self._table_ready.wait()
        await self.db.pool.execute(SET_DEFAULT_SQL, resource.value, amount)
        await inter.response.send_message("Global quota set.", ephemeral=True)
        await self.refresh_weekly_leaderboard()

    # ----- SET -----
    @quota.command(name="set", description="Set member weekly quota")
    @leader_perm
    @app_commands.choices(resource=[app_commands.Choice(name=r, value=r) for r in RESOURCES])
    async def set_user_quota(self, inter: discord.Interaction,
                             member: discord.Member,
                             resource: app_commands.Choice[str],
                             amount: app_commands.Range[int, 1, 1_000_000]):
        await self._table_ready.wait()
        await self.db.pool.execute(SET_USER_SQL, member.id, resource.value, amount)
        await inter.response.send_message("Member quota set.", ephemeral=True)

    # ----- RESET -----
    @quota.command(name="reset", description="Wipe ALL quota data (button confirm)")
    @admin_perm
    async def reset(self, inter: discord.Interaction):
        embed = discord.Embed(
            title="⚠️ Wipe all quota data?",
            description="Deletes every submission, task and leaderboard message.",
            colour=discord.Color.red())
        await inter.response.send_message(embed=embed,
                                          view=QuotaCog.ResetConfirmView(self),
                                          ephemeral=True)

    async def _wipe_all_data(self):
        async with self.db.pool.acquire() as conn:
            ch = self.bot.get_channel(PUBLIC_QUOTA_CH_ID)
            for r in await conn.fetch("SELECT message_id FROM quota_tasks"):
                if ch:
                    try:
                        m = await ch.fetch_message(r["message_id"]); await m.delete()
                    except discord.NotFound:
                        pass
            lb_row = await conn.fetchrow("SELECT message_id FROM quota_weekly_lb LIMIT 1")
            if lb_row and ch:
                try:
                    m = await ch.fetch_message(lb_row["message_id"]); await m.delete()
                except discord.NotFound:
                    pass
            await conn.execute("TRUNCATE quota_submissions, quota_tasks, quota_task_needs RESTART IDENTITY")
            await conn.execute("TRUNCATE quota_weekly_lb RESTART IDENTITY")
        await self.refresh_everything()

    # ----- DM LISTENER -----
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
            return await msg.channel.send("Provide resource amount pairs like `stone 6000`.")
        imgs = [a for a in msg.attachments if a.content_type and a.content_type.startswith("image/")][:10]
        if not imgs:
            return await msg.channel.send("Attach images.")
        guild = next((g for g in self.bot.guilds if g.get_member(msg.author.id)), None)
        if not guild:
            return
        member = guild.get_member(msg.author.id)
        await self._save_images(imgs, "multi", member.id)
        ids: List[int] = []
        await self._table_ready.wait()
        async with self.db.pool.acquire() as conn:
            for img in imgs:
                for res, amt in pairs.items():
                    rec_id = await conn.fetchval(
                        "INSERT INTO quota_submissions (user_id,resource,amount,image_url) "
                        "VALUES ($1,$2,$3,$4) RETURNING id",
                        member.id, res, amt, img.url)
                    ids.append(rec_id)
        await msg.channel.send("Submission received – thank you!")
        await self._notify_staff(member, pairs, imgs, ids)

# ───────────────────── setup ─────────────────────
async def setup(bot, db):
    await bot.add_cog(QuotaCog(bot, db))