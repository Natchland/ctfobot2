# cogs/quota.py
import asyncio
from datetime import datetime
import discord
from discord import app_commands
from discord.ext import commands

CREATE_QUOTAS_SQL = """
CREATE TABLE IF NOT EXISTS quotas (
    id SERIAL PRIMARY KEY,
    resource TEXT NOT NULL,
    amount INTEGER NOT NULL,
    deadline TIMESTAMP NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE
);
"""

CREATE_SUBMISSIONS_SQL = """
CREATE TABLE IF NOT EXISTS quota_submissions (
    id SERIAL PRIMARY KEY,
    quota_id INTEGER REFERENCES quotas(id),
    user_id BIGINT NOT NULL,
    amount INTEGER NOT NULL,
    proof TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    submitted_at TIMESTAMP DEFAULT now()
);
"""

class QuotaCog(commands.Cog):
    def __init__(self, bot, db):
        self.bot, self.db = bot, db
        asyncio.create_task(self._init_tables())

    async def _init_tables(self):
        await self.bot.wait_until_ready()
        async with self.db.pool.acquire() as conn:
            await conn.execute(CREATE_QUOTAS_SQL)
            await conn.execute(CREATE_SUBMISSIONS_SQL)

    # Admin-only: Set a new quota
    @app_commands.command(name="quota_set", description="Set a resource quota (admin only)")
    @app_commands.describe(resource="Resource (e.g. sulfur)", amount="Required amount", deadline="Deadline (YYYY-MM-DD)")
    @app_commands.checks.has_permissions(administrator=True)
    async def quota_set(self, inter: discord.Interaction, resource: str, amount: int, deadline: str):
        try:
            deadline_dt = datetime.strptime(deadline, "%Y-%m-%d")
        except ValueError:
            return await inter.response.send_message("Invalid date format! Use YYYY-MM-DD.", ephemeral=True)
        async with self.db.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO quotas (resource, amount, deadline, active) VALUES ($1, $2, $3, TRUE)",
                resource.lower(), amount, deadline_dt
            )
        await inter.response.send_message(f"Quota set: **{resource}** — **{amount}** by **{deadline}**.", ephemeral=True)

    # List all active quotas
    @app_commands.command(name="quota_progress", description="View current quotas and your contributions")
    async def quota_progress(self, inter: discord.Interaction):
        async with self.db.pool.acquire() as conn:
            quotas = await conn.fetch(
                "SELECT * FROM quotas WHERE active=TRUE AND deadline >= now() ORDER BY deadline"
            )
            if not quotas:
                return await inter.response.send_message("No active quotas set.", ephemeral=True)
            msg = "**Current Quotas:**\n"
            for q in quotas:
                # Sum user's contributions
                contrib = await conn.fetchval(
                    "SELECT COALESCE(SUM(amount),0) FROM quota_submissions WHERE quota_id=$1 AND user_id=$2 AND status='approved'",
                    q["id"], inter.user.id
                )
                msg += (
                    f"- **{q['resource'].capitalize()}**: {contrib}/{q['amount']} "
                    f"(by {q['deadline'].date()})\n"
                )
            await inter.response.send_message(msg, ephemeral=True)

    # Submit a contribution
    @app_commands.command(name="quota_submit", description="Submit your resource contribution")
    @app_commands.describe(resource="Resource name", amount="Amount contributed", proof="Optional proof (imgur/discord link)")
    async def quota_submit(self, inter: discord.Interaction, resource: str, amount: int, proof: str = None):
        async with self.db.pool.acquire() as conn:
            quota = await conn.fetchrow(
                "SELECT * FROM quotas WHERE resource=$1 AND active=TRUE AND deadline >= now()",
                resource.lower()
            )
            if not quota:
                return await inter.response.send_message("No active quota for that resource.", ephemeral=True)
            await conn.execute(
                "INSERT INTO quota_submissions (quota_id, user_id, amount, proof) VALUES ($1,$2,$3,$4)",
                quota["id"], inter.user.id, amount, proof
            )
        await inter.response.send_message("Submission received! A staff member will review it soon.", ephemeral=True)

    # Leaderboard for a resource
    @app_commands.command(name="quota_leaderboard", description="Show the leaderboard for a quota resource")
    @app_commands.describe(resource="Resource name")
    async def quota_leaderboard(self, inter: discord.Interaction, resource: str):
        async with self.db.pool.acquire() as conn:
            quota = await conn.fetchrow(
                "SELECT * FROM quotas WHERE resource=$1 AND active=TRUE AND deadline >= now()",
                resource.lower()
            )
            if not quota:
                return await inter.response.send_message("No active quota for that resource.", ephemeral=True)
            rows = await conn.fetch(
                "SELECT user_id, SUM(amount) as total FROM quota_submissions WHERE quota_id=$1 AND status='approved' GROUP BY user_id ORDER BY total DESC",
                quota["id"]
            )
            if not rows:
                return await inter.response.send_message("No contributions yet.", ephemeral=True)
            msg = f"**Leaderboard for {resource.capitalize()}:**\n"
            for i, row in enumerate(rows, 1):
                user = inter.guild.get_member(row["user_id"])
                user_str = user.mention if user else f"<@{row['user_id']}>"
                msg += f"{i}. {user_str}: {row['total']}\n"
            await inter.response.send_message(msg, ephemeral=True)

    # Staff: review submissions (approve/deny)
    @app_commands.command(name="quota_review", description="Review quota submissions (admin/reviewer only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def quota_review(self, inter: discord.Interaction):
        async with self.db.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM quota_submissions WHERE status='pending' ORDER BY submitted_at LIMIT 1"
            )
            if not row:
                return await inter.response.send_message("No pending submissions.", ephemeral=True)
            user = inter.guild.get_member(row["user_id"])
            user_str = user.mention if user else f"<@{row['user_id']}>"
            embed = discord.Embed(
                title="Quota Submission",
                description=f"**User:** {user_str}\n**Resource:** {row['quota_id']}\n**Amount:** {row['amount']}\n**Proof:** {row['proof'] or 'None'}",
                color=discord.Color.orange()
            )
            view = QuotaReviewView(self.db, row['id'])
            await inter.response.send_message(embed=embed, view=view, ephemeral=True)

class QuotaReviewView(discord.ui.View):
    def __init__(self, db, submission_id):
        super().__init__(timeout=60)
        self.db = db
        self.submission_id = submission_id

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, inter: discord.Interaction, _):
        async with self.db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE quota_submissions SET status='approved' WHERE id=$1",
                self.submission_id
            )
        await inter.response.edit_message(content="Submission approved.", view=None)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, inter: discord.Interaction, _):
        async with self.db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE quota_submissions SET status='denied' WHERE id=$1",
                self.submission_id
            )
        await inter.response.edit_message(content="Submission denied.", view=None)

# --- setup() hook for cog loading ---
async def setup(bot, db):
    await bot.add_cog(QuotaCog(bot, db))