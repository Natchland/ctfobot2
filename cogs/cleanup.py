# cogs/cleanup.py
import discord
from discord.ext import commands

class CleanupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="cleanchannel")
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def clean_channel(self, ctx, limit: int = 100):
        """Delete all non-pinned messages in the current channel (Admin only)"""
        if limit <= 0 or limit > 10000:
            return await ctx.send("Please provide a limit between 1 and 10,000 messages.", ephemeral=True)
            
        await ctx.send(f"Starting cleanup of up to {limit} messages... This may take a while.", ephemeral=True)
        
        deleted_count = 0
        failed_count = 0
        processed = 0
        
        # Create progress embed
        progress_embed = discord.Embed(
            title="Channel Cleanup Progress",
            description=f"Processed: {processed} | Deleted: {deleted_count} | Failed: {failed_count}",
            color=discord.Color.orange()
        )
        progress_msg = await ctx.channel.send(embed=progress_embed)
        
        # Get messages in batches of 100 (Discord API limit)
        while deleted_count + failed_count < limit:
            try:
                messages = []
                async for message in ctx.channel.history(limit=100):
                    # Skip pinned messages
                    if not message.pinned:
                        messages.append(message)
                        
                if not messages:
                    break
                    
                # Limit to remaining needed deletions
                remaining = limit - (deleted_count + failed_count)
                messages = messages[:remaining]
                
                # Delete messages
                try:
                    deleted = await ctx.channel.delete_messages(messages)
                    deleted_count += len(deleted)
                except discord.HTTPException:
                    # If bulk delete fails, try deleting individually
                    for message in messages:
                        try:
                            await message.delete()
                            deleted_count += 1
                        except discord.HTTPException:
                            failed_count += 1
                            
                processed += len(messages)
                
                # Update progress
                progress_embed.description = f"Processed: {processed} | Deleted: {deleted_count} | Failed: {failed_count}"
                await progress_msg.edit(embed=progress_embed)
                
                # If we got less than 100 messages, we've reached the end
                if len(messages) < 100:
                    break
                    
            except discord.HTTPException as e:
                failed_count += 1
                
        # Final update
        final_embed = discord.Embed(
            title="Channel Cleanup Complete",
            description=f"Processed: {processed} | Deleted: {deleted_count} | Failed: {failed_count}",
            color=discord.Color.green() if failed_count == 0 else discord.Color.red()
        )
        await progress_msg.edit(embed=final_embed)
        
        # Send summary to command user
        await ctx.send(
            f"Channel cleanup complete!\n"
            f"Processed: {processed} messages\n"
            f"Deleted: {deleted_count} messages\n"
            f"Failed: {failed_count} messages",
            ephemeral=True
        )

async def setup(bot, db=None):
    await bot.add_cog(CleanupCog(bot))