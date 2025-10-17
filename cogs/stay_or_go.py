# cogs/stay_or_go.py

from __future__ import annotations

import discord
from discord.ext import commands

from db import Database

# ————————————————————— Configurable ———————————————————————
CHANNEL_ID = 1378080948259786782  # Set your desired channel ID here
ROLE_NAME = "Staying Role"
EMBED_TITLE = "⚠️ Do you wish to stick around after the cleanup? ⚠️"
EMBED_DESCRIPTION = (
    "We're cleaning up inactive members soon.\n"
    "If you wish to remain active and not be kicked, please click the ✅ below.\n"
    "This will give you the **Staying Role** and protect your account!"
)
REACTION_EMOJI = "✅"
# ——————————————————————————————————————————————————————————


class StayOrGo(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database):
        self.bot = bot
        self.db = db
        self.target_channel = None
        self.stay_role = None
        self.message = None
        self.active = False

    async def cog_load(self) -> None:
        self.target_channel = self.bot.get_channel(CHANNEL_ID)
        if not self.target_channel:
            print("Could not find target channel. Make sure CHANNEL_ID is correct.")
            return

        guild = self.target_channel.guild
        self.stay_role = discord.utils.get(guild.roles, name=ROLE_NAME)
        if not self.stay_role:
            self.stay_role = await guild.create_role(
                name=ROLE_NAME,
                reason="Auto-created for stay-or-go system"
            )

        # Register the slash command
        @discord.app_commands.command(name="startstayorgo", description="Start the stay or go system")
        @discord.app_commands.default_permissions(administrator=True)
        async def start_stay_or_go_command(interaction: discord.Interaction):
            if self.active:
                await interaction.response.send_message("Stay-or-go system is already active!", ephemeral=True)
                return

            # Try to find existing message or create new one
            await self.find_or_create_message()
            self.active = True
            await interaction.response.send_message("Stay-or-go system activated! Message is now live.", ephemeral=True)

        # Add command to the bot's tree for the specific guild
        self.bot.tree.add_command(start_stay_or_go_command, guild=guild, override=True)

    async def find_or_create_message(self):
        """Find existing message or create a new one"""
        # First, try to find existing message by looking through channel history
        async for message in self.target_channel.history(limit=50):
            if (message.author == self.bot.user and 
                message.embeds and 
                message.embeds[0].title == EMBED_TITLE):
                self.message = message
                print("Found existing stay-or-go message")
                return

        # If not found, create new message
        embed = discord.Embed(
            title=EMBED_TITLE,
            description=EMBED_DESCRIPTION,
            color=discord.Color.red()
        )
        self.message = await self.target_channel.send(embed=embed)
        await self.message.add_reaction(REACTION_EMOJI)
        print("Created new stay-or-go message")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        # Ignore if system not active
        if not self.active:
            return
            
        # Ignore bots
        if payload.member and payload.member.bot:
            return

        # Check if the reaction is on our message and emoji
        if str(payload.emoji) != REACTION_EMOJI:
            return

        # Check if it's the correct message
        if not self.message or payload.message_id != self.message.id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        member = guild.get_member(payload.user_id)
        if not member:
            return

        # Give role
        if self.stay_role and self.stay_role not in member.roles:
            try:
                await member.add_roles(self.stay_role, reason="User confirmed staying")
                print(f"Added {ROLE_NAME} to {member.display_name}")
            except Exception as e:
                print(f"Failed to assign role: {e}")

async def setup(bot: commands.Bot, db: Database) -> None:
    await bot.add_cog(StayOrGo(bot, db))