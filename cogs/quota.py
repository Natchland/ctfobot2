# cogs/quota.py
from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ───────────────────────── CONFIG ──────────────────────────
FARMER_ROLE_ID: int = 1379918816871448686          # role allowed to submit quotas
QUOTA_REVIEW_CH_ID: int = 1421920458437169254      # staff review channel ID
RESOURCES: List[str] = ["stone", "sulfur", "metal", "wood"]
LEADERBOARD_SIZE: int = 10
QUOTA_IMAGE_DIR: str = "/data/quota_images"       # Railway volume mount path

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

MARK_REVIEWED_SQL = "UPDATE quota_submissions SET reviewed=TRUE WHERE id=$1"
MARK_MANY_SQL     = "UPDATE quota_submissions SET reviewed=TRUE WHERE id = ANY($1::INT[])"

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

# ═══════════════════════════  COG  ═══════════════════════════
class QuotaCog(commands.Cog):
    """Weekly multi-resource quotas (Farmer role only)."""

    # ─────────────────────── nested UI classes ───────────────────────
    # They are declared first so Pylance sees them before use.

    class AcceptUserBtn(discord.ui.Button):
        def __init__(self, ids: List[int], uid: int, outer: "QuotaCog"):
            super().__init__(label=f"{uid} ✅", style=discord.ButtonStyle.success)
            self.ids, self.uid, self.outer = ids, uid, outer

        async def callback(self, inter: discord.Interaction):
            await self.outer.db.pool.execute(MARK_MANY_SQL, self.ids)
            await inter.response.send_message(
                f"Marked all submissions from <@{self.uid}> reviewed.",
                ephemeral=True
            )
            self.disabled, self.label = True, "Reviewed"
            await inter.message.edit(view=self.view)

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

    class SingleAcceptView(discord.ui.View):
        def __init__(self, outer: "QuotaCog", ids: List[int]):
            super().__init__(timeout=None)  # persistent
            self.add_item(QuotaCog.SingleAcceptBtn(ids, outer))

    # ───────────────────────── COG INIT ─────────────────────────
    def __init__(self, bot: commands.Bot, db):
        self.bot, self.db = bot, db
        self._table_ready = asyncio.Event()

        os.makedirs(QUOTA_IMAGE_DIR, exist_ok=True)
        asyncio.create_task(self._prepare_tables())
        self.weekly_cleanup.start()

    # ───────────────── DB bootstrap & migration ─────────────────
    async def _prepare_tables(self):
        while self.db.pool is None:
            await asyncio.sleep(1)

        async with self.db.pool.acquire() as conn:
            await conn.execute(CREATE_SQL)
            await conn.execute(
                """
                ALTER TABLE quota_submissions
                  ADD COLUMN IF NOT EXISTS resource  TEXT,
                  ADD COLUMN IF NOT EXISTS amount    INTEGER,
                  ADD COLUMN IF NOT EXISTS image_url TEXT,
                  ADD COLUMN IF NOT EXISTS reviewed  BOOLEAN DEFAULT FALSE,
                  ADD COLUMN IF NOT EXISTS week_ts   DATE    DEFAULT CURRENT_DATE;
                """
            )
        self._table_ready.set()

    # ───────────────────────── helpers ─────────────────────────
    @staticmethod
    def _has_farmer(member: discord.Member) -> bool:
        return any(r.id == FARMER_ROLE_ID for r in member.roles)

    async def _farmer_check(self, inter: discord.Interaction) -> bool:
        if inter.guild is None or not isinstance(inter.user, discord.Member):
            await inter.response.send_message("Run this inside the guild.", ephemeral=True)
            return False
        if not self._has_farmer(inter.user):
            await inter.response.send_message(
                "You need the Farmer role to use quota commands.", ephemeral=True
            )
            return False
        return True

    async def _save_images(self, atts: List[discord.Attachment],
                           resource: str, uid: int) -> List[str]:
        saved: List[str] = []
        for att in atts[:10]:
            if not (att.content_type and att.content_type.startswith("image/")):
                continue
            ext = os.path.splitext(att.filename)[1] or ".png"
            name = f"{int(time.time())}_{uid}_{resource}_{uuid.uuid4().hex[:8]}{ext}"
            path = os.path.join(QUOTA_IMAGE_DIR, name)
            try:
                await att.save(path)
                saved.append(path)
            except Exception as exc:
                print(f"[quota] save error {att.filename}: {exc}")
        return saved

    # ───────────────────── notify staff helper ─────────────────────
    async def _notify_staff(
        self,
        member: discord.Member | discord.User,
        pairs: Dict[str, int],
        images: List[discord.Attachment],
        ids: List[int],
    ):
        ch = self.bot.get_channel(QUOTA_REVIEW_CH_ID)
        if not ch:
            return
        plist = "\n".join(f"• **{res}** `{amt}`" for res, amt in pairs.items())
        extra = f" (+{len(images)-1} more)" if len(images) > 1 else ""
        embed = discord.Embed(
            title=f"Submission from {member}",
            description=plist,
            colour=discord.Color.green(),
        )
        file0 = await images[0].to_file()
        view = QuotaCog.SingleAcceptView(self, ids)
        await ch.send(
            embed=embed,
            file=file0,
            view=view,
            content=extra,
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    # ═════════════ SLASH-COMMAND GROUP ═════════════
    quota = app_commands.Group(
        name="quota",
        description="Farmer-only weekly quotas",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    # ---------- /quota setdefault ----------
    @quota.command(name="setdefault", description="Set global weekly quota")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(resource=[app_commands.Choice(name=r, value=r) for r in RESOURCES])
    async def set_default(
        self,
        inter: discord.Interaction,
        resource: app_commands.Choice[str],
        amount: app_commands.Range[int, 1, 1_000_000],
    ):
        await self._table_ready.wait()
        await self.db.pool.execute(SET_DEFAULT_SQL, resource.value, amount)
        await inter.response.send_message(
            f"Set **global** {resource.value} quota to **{amount}**.", ephemeral=True
        )

    # ---------- /quota set ----------
    @quota.command(name="set", description="Set per-member weekly quota")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(resource=[app_commands.Choice(name=r, value=r) for r in RESOURCES])
    async def set_user_quota(
        self,
        inter: discord.Interaction,
        member: discord.Member,
        resource: app_commands.Choice[str],
        amount: app_commands.Range[int, 1, 1_000_000],
    ):
        await self._table_ready.wait()
        if not self._has_farmer(member):
            return await inter.response.send_message(
                f"{member.mention} does not have the Farmer role.", ephemeral=True
            )
        await self.db.pool.execute(SET_USER_SQL, member.id, resource.value, amount)
        await inter.response.send_message(
            f"Set {member.mention}’s **{resource.value}** quota to **{amount}**.",
            ephemeral=True,
        )

    # ---------- /quota submit ----------
    @quota.command(name="submit", description="Submit screenshot(s)")
    @app_commands.choices(resource=[app_commands.Choice(name=r, value=r) for r in RESOURCES])
    async def submit(
        self,
        inter: discord.Interaction,
        resource: app_commands.Choice[str],
        amount: app_commands.Range[int, 1, 1_000_000],
    ):
        await self._table_ready.wait()
        if inter.guild and not await self._farmer_check(inter):
            return

        images = [
            a
            for a in inter.attachments
            if a.content_type and a.content_type.startswith("image/")
        ][:10]
        if not images:
            return await inter.response.send_message(
                "Attach 1–10 screenshots.", ephemeral=True
            )

        await self._save_images(images, resource.value, inter.user.id)

        sub_ids: List[int] = []
        async with self.db.pool.acquire() as conn:
            for img in images:
                rec = await conn.fetchrow(
                    "INSERT INTO quota_submissions (user_id,resource,amount,image_url)"
                    " VALUES ($1,$2,$3,$4) RETURNING id",
                    inter.user.id,
                    resource.value,
                    amount,
                    img.url,
                )
                sub_ids.append(rec["id"])

        await inter.response.send_message(
            f"Recorded **{amount} {resource.value}** "
            f"from {len(images)} image(s) – thank you!",
            ephemeral=True,
        )

        await self._notify_staff(inter.user, {resource.value: amount}, images, sub_ids)

    # ---------- /quota leaderboard ----------
    @quota.command(name="leaderboard", description="Weekly top contributors")
    @app_commands.choices(resource=[app_commands.Choice(name=r, value=r) for r in RESOURCES])
    async def leaderboard(
        self,
        inter: discord.Interaction,
        resource: app_commands.Choice[str],
    ):
        await self._table_ready.wait()
        if inter.guild is None:
            return await inter.response.send_message("Run inside the guild.", ephemeral=True)

        rows = await self.db.pool.fetch(LB_SQL, resource.value, LEADERBOARD_SIZE)
        rows = [
            r
            for r in rows
            if (m := inter.guild.get_member(r["user_id"])) and self._has_farmer(m)
        ][:LEADERBOARD_SIZE]
        if not rows:
            return await inter.response.send_message("No reviewed submissions yet.", ephemeral=True)

        body = "\n".join(f"**{i+1}.** <@{r['user_id']}> — `{r['total']}`" for i, r in enumerate(rows))
        await inter.response.send_message(body, ephemeral=True)

    # ---------- /quota review ----------
    @quota.command(name="review", description="Review pending submissions")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def review(self, inter: discord.Interaction):
        await self._table_ready.wait()
        rows = await self.db.pool.fetch(PENDING_GROUP_SQL)
        if not rows:
            return await inter.response.send_message("No pending submissions.", ephemeral=True)

        embed = discord.Embed(title="Pending quota submissions", colour=discord.Color.orange())
        for r in rows:
            member = inter.guild.get_member(r["user_id"]) or f"<@{r['user_id']}>"
            body = "\n".join(f"• **{res}** `{amt}`" for res, amt in r["res_totals"].items())
            embed.add_field(name=str(member), value=body, inline=False)

        await inter.response.send_message(embed=embed, view=QuotaCog.ReviewUsersView(rows, self), ephemeral=True)

    # ═════════════ DM listener – multi-resource & multi-image ═════════════
    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if msg.guild or msg.author.bot or not msg.attachments:
            return

        # parse pairs
        cleaned = re.sub(r"[,;:]", " ", msg.content.lower())
        tokens = cleaned.split()
        pairs: Dict[str, int] = defaultdict(int)
        cur: str | None = None
        for tok in tokens:
            if tok in RESOURCES:
                cur = tok
            elif tok.replace(",", "").isdigit() and cur:
                pairs[cur] += int(tok.replace(",", ""))
                cur = None
        if not pairs:
            return await msg.channel.send(
                "Couldn’t find any `resource amount` pairs. "
                "Example: `stone 6000 sulfur 3500`."
            )

        images = [a for a in msg.attachments if a.content_type and a.content_type.startswith("image/")][:10]
        if not images:
            return await msg.channel.send("Attachment must be an image.")

        guild = next((g for g in self.bot.guilds if g.get_member(msg.author.id)), None)
        if not guild:
            return
        member = guild.get_member(msg.author.id)
        if not member or not self._has_farmer(member):
            return await msg.channel.send("You don’t have the Farmer role in the guild.")

        await self._save_images(images, "multi", msg.author.id)

        sub_ids: List[int] = []
        await self._table_ready.wait()
        async with self.db.pool.acquire() as conn:
            for img in images:
                for res, amt in pairs.items():
                    rec = await conn.fetchrow(
                        "INSERT INTO quota_submissions (user_id,resource,amount,image_url)"
                        " VALUES ($1,$2,$3,$4) RETURNING id",
                        msg.author.id,
                        res,
                        amt,
                        img.url,
                    )
                    sub_ids.append(rec["id"])

        nice = ", ".join(f"{res} `{amt}`" for res, amt in pairs.items())
        await msg.channel.send(f"Recorded {nice} from {len(images)} image(s) – thank you!")

        await self._notify_staff(member, pairs, images, sub_ids)

    # ═════════════ weekly cleanup ═════════════
    @tasks.loop(hours=1)
    async def weekly_cleanup(self):
        await self._table_ready.wait()
        now = datetime.now(timezone.utc)
        if now.weekday() == 6 and now.hour == 0:  # Sunday 00:00 UTC
            await self.db.pool.execute(PURGE_OLD_SQL)

    async def cog_unload(self):
        self.weekly_cleanup.cancel()

# ─────────────────── setup hook ───────────────────
async def setup(bot, db):
    await bot.add_cog(QuotaCog(bot, db))