# cogs/quota.py
from __future__ import annotations

import asyncio, os, re, time, uuid
from collections import defaultdict
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ───────────────────────── CONFIG ──────────────────────────
FARMER_ROLE_ID     = 1379918816871448686        # members with this role have quotas
QUOTA_REVIEW_CH_ID = 1421920458437169254        # ← staff-only review channel
RESOURCES          = ["stone", "sulfur", "metal", "wood"]
LEADERBOARD_SIZE   = 10
QUOTA_IMAGE_DIR    = "./data/quota_images"      # local folder for saved screenshots

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

SET_DEFAULT_SQL  = """
INSERT INTO default_quotas (resource, weekly)
VALUES ($1,$2)
ON CONFLICT (resource) DO UPDATE SET weekly=$2
"""
SET_USER_SQL     = """
INSERT INTO quotas (user_id, resource, weekly)
VALUES ($1,$2,$3)
ON CONFLICT (user_id, resource) DO UPDATE SET weekly=$3
"""
ADD_SUB_SQL      = """
INSERT INTO quota_submissions (user_id, resource, amount, image_url)
VALUES ($1,$2,$3,$4)
"""
PENDING_SQL      = """
SELECT id, user_id, resource, amount, image_url
FROM quota_submissions
WHERE reviewed=FALSE
ORDER BY id
"""
MARK_REVIEWED_SQL = "UPDATE quota_submissions SET reviewed=TRUE WHERE id=$1"

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
    """Weekly multi-resource quota system restricted to the Farmer role."""

    def __init__(self, bot: commands.Bot, db):
        self.bot, self.db = bot, db
        self._table_ready = asyncio.Event()

        os.makedirs(QUOTA_IMAGE_DIR, exist_ok=True)
        asyncio.create_task(self._prepare_tables())
        self.weekly_cleanup.start()

    # ────────────────────────── DB bootstrap ──────────────────────────
    async def _prepare_tables(self):
        while self.db.pool is None:
            await asyncio.sleep(1)
        async with self.db.pool.acquire() as conn:
            await conn.execute(CREATE_SQL)
        self._table_ready.set()

    # ────────────────────────── helpers ───────────────────────────────
    @staticmethod
    def _has_farmer(member: discord.Member) -> bool:
        return any(r.id == FARMER_ROLE_ID for r in member.roles)

    async def _farmer_check(self, inter: discord.Interaction) -> bool:
        if inter.guild is None or not isinstance(inter.user, discord.Member):
            await inter.response.send_message("Use this command inside the guild.",
                                              ephemeral=True)
            return False
        if not self._has_farmer(inter.user):
            await inter.response.send_message(
                "You need the Farmer role to use quota commands.",
                ephemeral=True
            )
            return False
        return True

    async def _save_images(
        self,
        attachments: list[discord.Attachment],
        resource: str,
        user_id: int
    ) -> list[str]:
        """
        Save each attachment (max 10) to QUOTA_IMAGE_DIR.
        Returns list of local file paths.
        """
        saved: list[str] = []
        for att in attachments[:10]:
            if not (att.content_type and att.content_type.startswith("image/")):
                continue
            ext = os.path.splitext(att.filename)[1] or ".png"
            fname = (
                f"{int(time.time())}_{user_id}_{resource}_"
                f"{uuid.uuid4().hex[:8]}{ext}"
            )
            fpath = os.path.join(QUOTA_IMAGE_DIR, fname)
            try:
                await att.save(fpath)
                saved.append(fpath)
            except Exception as e:
                print(f"[quota] could not save {att.filename}: {e}")
        return saved

    # ═══════════════════ SLASH-COMMAND GROUP ═══════════════════
    quota = app_commands.Group(
        name="quota",
        description="Farmer-only weekly quotas",
        default_permissions=discord.Permissions(manage_guild=True)
    )

    # -------- /quota setdefault --------
    @quota.command(name="setdefault",
                   description="Set global weekly quota for a resource")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(
        resource=[app_commands.Choice(name=r, value=r) for r in RESOURCES]
    )
    async def set_default(
        self,
        inter: discord.Interaction,
        resource: app_commands.Choice[str],
        amount: app_commands.Range[int, 1, 1_000_000]
    ):
        await self._table_ready.wait()
        await self.db.pool.execute(SET_DEFAULT_SQL, resource.value, amount)
        await inter.response.send_message(
            f"Set **global** {resource.value} quota to **{amount}**.",
            ephemeral=True
        )

    # -------- /quota set --------
    @quota.command(name="set",
                   description="Set per-member weekly quota for a resource")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(
        resource=[app_commands.Choice(name=r, value=r) for r in RESOURCES]
    )
    async def set_user_quota(
        self,
        inter: discord.Interaction,
        member: discord.Member,
        resource: app_commands.Choice[str],
        amount: app_commands.Range[int, 1, 1_000_000]
    ):
        await self._table_ready.wait()
        if not self._has_farmer(member):
            return await inter.response.send_message(
                f"{member.mention} does not have the Farmer role.",
                ephemeral=True
            )

        await self.db.pool.execute(
            SET_USER_SQL, member.id, resource.value, amount
        )
        await inter.response.send_message(
            f"Set {member.mention}’s **{resource.value}** quota to **{amount}**.",
            ephemeral=True
        )

    # -------- /quota submit --------
    @quota.command(name="submit",
                   description="Submit screenshot (works in DMs too)")
    @app_commands.choices(
        resource=[app_commands.Choice(name=r, value=r) for r in RESOURCES]
    )
    async def submit(
        self,
        inter: discord.Interaction,
        resource: app_commands.Choice[str],
        amount: app_commands.Range[int, 1, 1_000_000]
    ):
        await self._table_ready.wait()
        if inter.guild and not await self._farmer_check(inter):
            return

        images = [
            a for a in inter.attachments
            if a.content_type and a.content_type.startswith("image/")
        ][:10]
        if not images:
            return await inter.response.send_message(
                "Attach 1–10 screenshots.",
                ephemeral=True
            )

        # save locally
        saved_paths = await self._save_images(images, resource.value, inter.user.id)

        # DB rows
        async with self.db.pool.acquire() as conn:
            for img in images:
                await conn.execute(
                    ADD_SUB_SQL, inter.user.id, resource.value, amount, img.url
                )

        await inter.response.send_message(
            f"Recorded **{amount} {resource.value}** "
            f"from {len(images)} image(s) – thank you!",
            ephemeral=True
        )

        # ping reviewers
        ch = self.bot.get_channel(QUOTA_REVIEW_CH_ID)
        if ch:
            extra = f" (+{len(images)-1} more)" if len(images) > 1 else ""
            await ch.send(
                f"New **{resource.value}** submission (`{amount}`) from "
                f"{inter.user.mention}{extra}",
                file=await images[0].to_file(),
                allowed_mentions=discord.AllowedMentions(users=True)
            )

    # -------- /quota leaderboard --------
    @quota.command(name="leaderboard",
                   description="Top reviewed submissions this week")
    @app_commands.choices(
        resource=[app_commands.Choice(name=r, value=r) for r in RESOURCES]
    )
    async def leaderboard(
        self, inter: discord.Interaction, resource: app_commands.Choice[str]
    ):
        await self._table_ready.wait()
        if inter.guild is None:
            return await inter.response.send_message(
                "Run inside a guild.", ephemeral=True
            )

        rows = await self.db.pool.fetch(
            LB_SQL, resource.value, LEADERBOARD_SIZE
        )
        rows = [
            r for r in rows
            if (m := inter.guild.get_member(r["user_id"])) and self._has_farmer(m)
        ][:LEADERBOARD_SIZE]

        if not rows:
            return await inter.response.send_message(
                "No reviewed submissions yet.", ephemeral=True
            )

        msg = "\n".join(
            f"**{i+1}.** <@{r['user_id']}> — `{r['total']}`"
            for i, r in enumerate(rows)
        )
        await inter.response.send_message(msg, ephemeral=True)

    # -------- /quota review --------
    @quota.command(name="review", description="Review pending submissions")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def review(self, inter: discord.Interaction):
        await self._table_ready.wait()

        rows = await self.db.pool.fetch(PENDING_SQL)
        if not rows:
            return await inter.response.send_message(
                "No pending submissions.", ephemeral=True
            )

        embed = discord.Embed(
            title="Pending quota submissions",
            colour=discord.Color.orange()
        )
        for r in rows:
            embed.add_field(
                name=f"ID {r['id']}",
                value=(
                    f"<@{r['user_id']}> — **{r['resource']}** "
                    f"`{r['amount']}`\n[link]({r['image_url']})"
                ),
                inline=False
            )

        await inter.response.send_message(
            embed=embed,
            view=self.ReviewView(rows, self),
            ephemeral=True
        )

    # -------- Review UI ----------
    class ReviewView(discord.ui.View):
        def __init__(self, rows, outer: "QuotaCog"):
            super().__init__(timeout=600)
            for r in rows:
                self.add_item(QuotaCog.ReviewBtn(r["id"], outer))

    class ReviewBtn(discord.ui.Button):
        def __init__(self, sub_id: int, outer: "QuotaCog"):
            super().__init__(label=f"{sub_id} ✅",
                             style=discord.ButtonStyle.success)
            self.sub_id, self.outer = sub_id, outer

        async def callback(self, inter: discord.Interaction):
            await self.outer.db.pool.execute(MARK_REVIEWED_SQL, self.sub_id)
            await inter.response.send_message(
                f"Submission **{self.sub_id}** marked reviewed.",
                ephemeral=True
            )
            self.disabled = True
            await inter.message.edit(view=self.view)

    # ════════════ DM listener – multi-resource & multi-image ═══════════
    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if msg.guild or msg.author.bot or not msg.attachments:
            return

        # 1. parse "resource amount" pairs
        cleaned = re.sub(r"[,;:]", " ", msg.content.lower())
        tokens  = cleaned.split()
        pairs: dict[str, int] = defaultdict(int)
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

        # 2. collect up to 10 images
        images = [
            a for a in msg.attachments
            if a.content_type and a.content_type.startswith("image/")
        ][:10]
        if not images:
            return await msg.channel.send("Attachment must be an image.")

        # 3. ensure sender has Farmer in any mutual guild
        guild = next((g for g in self.bot.guilds if g.get_member(msg.author.id)), None)
        if not guild:
            return
        member = guild.get_member(msg.author.id)
        if not member or not self._has_farmer(member):
            return await msg.channel.send(
                "You don’t have the Farmer role in the guild."
            )

        # 4. save images locally
        for img in images:
            await self._save_images([img], "multi", msg.author.id)  # 'multi' placeholder

        # 5. DB rows
        await self._table_ready.wait()
        async with self.db.pool.acquire() as conn:
            for img in images:
                for res, amt in pairs.items():
                    await conn.execute(
                        ADD_SUB_SQL, msg.author.id, res, amt, img.url
                    )

        nice = ", ".join(f"{res} `{amt}`" for res, amt in pairs.items())
        await msg.channel.send(
            f"Recorded {nice} from {len(images)} image(s) – thank you!"
        )

        # 6. notify staff
        ch = self.bot.get_channel(QUOTA_REVIEW_CH_ID)
        if ch:
            plist = "\n".join(f"• **{res}** `{amt}`" for res, amt in pairs.items())
            extra = f" (+{len(images)-1} more)" if len(images) > 1 else ""
            await ch.send(
                f"New multi-resource submission from {member.mention}{extra}:\n{plist}",
                file=await images[0].to_file(),
                allowed_mentions=discord.AllowedMentions(users=True)
            )

    # ═══════════════ weekly cleanup ═══════════════
    @tasks.loop(hours=1)
    async def weekly_cleanup(self):
        await self._table_ready.wait()
        now = datetime.now(timezone.utc)
        if now.weekday() == 6 and now.hour == 0:          # Sunday 00:00 UTC
            await self.db.pool.execute(PURGE_OLD_SQL)

    async def cog_unload(self):
        self.weekly_cleanup.cancel()

# ───────────────────────── setup hook ─────────────────────────
async def setup(bot, db):
    await bot.add_cog(QuotaCog(bot, db))