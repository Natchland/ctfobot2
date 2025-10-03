# cogs/feedback.py

import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta, timezone

FEEDBACK_CH    = 1413188006499586158

class FeedbackCog(commands.Cog):
    def __init__(self, bot, db):
        self.bot = bot
        self.db = db
        # Use bot-level dict if available, else local dict
        if not hasattr(self.bot, "last_anonymous_time"):
            self.bot.last_anonymous_time = {}

    @app_commands.command(name="feedback")
    @app_commands.describe(message="Your feedback", anonymous="Send anonymously?")
    async def feedback(self, inter: discord.Interaction, message: str, anonymous: bool):
        # You may want to move FEEDBACK_CH to config or pass it in
        FEEDBACK_CH = 1413188006499586158
        ch = self.bot.get_channel(FEEDBACK_CH)
        if not ch:
            return await inter.response.send_message("Channel missing.", ephemeral=True)

        now = datetime.now(timezone.utc)
        last = self.bot.last_anonymous_time.get(inter.user.id)

        if anonymous and last and now - last < timedelta(days=1):
            rem = timedelta(days=1) - (now - last)
            h, r = divmod(rem.seconds, 3600)
            m, _ = divmod(r, 60)
            return await inter.response.send_message(
                f"One anonymous msg per 24 h. Retry in {rem.days} d {h} h {m} m.",
                ephemeral=True,
            )

        if anonymous:
            self.bot.last_anonymous_time[inter.user.id] = now
            embed = (
                discord.Embed(
                    title="Anonymous Feedback",
                    description=message,
                    colour=discord.Color.light_gray(),
                ).set_footer(text="Sent anonymously")
            )
        else:
            embed = (
                discord.Embed(
                    title="Feedback", description=message, colour=discord.Color.blue()
                ).set_author(name=str(inter.user), icon_url=inter.user.display_avatar.url)
            )

        await ch.send(embed=embed)
        await inter.response.send_message("Thanks!", ephemeral=True)

async def setup(bot, db):
    await bot.add_cog(FeedbackCog(bot, db))