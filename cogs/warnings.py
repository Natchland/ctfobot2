# cogs/warnings.py
import discord
from discord.ext import commands, tasks
from typing import Optional
import logging
from datetime import datetime, timedelta
import io
import csv

class WarningSystem(commands.Cog):
    def __init__(self, bot, db):
        self.bot = bot
        self.db = db

    async def cog_load(self):
        # Create warnings table
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS warnings (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                moderator_id BIGINT NOT NULL,
                guild_id BIGINT NOT NULL,
                reason TEXT NOT NULL,
                case_id INTEGER UNIQUE NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                warning_type VARCHAR(50) DEFAULT 'general',
                appeal_text TEXT,
                appeal_status VARCHAR(20) DEFAULT 'none',  -- none, pending, approved, denied
                expiry_date TIMESTAMP,
                expired BOOLEAN DEFAULT FALSE
            )
            """
        )
        
        # Create warning config table
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS warning_config (
                guild_id BIGINT PRIMARY KEY,
                dm_users BOOLEAN DEFAULT TRUE,
                auto_moderation BOOLEAN DEFAULT FALSE,
                log_channel_id BIGINT,
                escalate_3_warns BOOLEAN DEFAULT TRUE,
                escalate_5_warns BOOLEAN DEFAULT TRUE
            )
            """
        )
        
        # Start expiration checker
        self.check_expired_warnings.start()

    async def cog_unload(self):
        self.check_expired_warnings.cancel()

    @tasks.loop(hours=1)
    async def check_expired_warnings(self):
        """Check for expired warnings and mark them as expired"""
        try:
            result = await self.db.fetch(
                """
                SELECT id, user_id, guild_id, case_id
                FROM warnings
                WHERE expiry_date <= $1 AND expired = FALSE
                """,
                datetime.utcnow()
            )
            
            for warning in result:
                await self.db.execute(
                    "UPDATE warnings SET expired = TRUE WHERE id = $1",
                    warning['id']
                )
                
                # Log expiration if log channel exists
                config = await self._get_guild_config(warning['guild_id'])
                if config and config['log_channel_id']:
                    guild = self.bot.get_guild(warning['guild_id'])
                    if guild:
                        channel = guild.get_channel(config['log_channel_id'])
                        if channel:
                            user = guild.get_member(warning['user_id'])
                            await channel.send(
                                f"Warning #{warning['case_id']} for {user.mention} "
                                f"has expired automatically."
                            )
        except Exception as e:
            logging.error(f"Error checking expired warnings: {e}")

    async def _get_next_case_id(self):
        """Get next case ID"""
        try:
            result = await self.db.fetchval("SELECT MAX(case_id) FROM warnings")
            return (result or 0) + 1
        except Exception as e:
            logging.error(f"Failed to get next case ID: {e}")
            return int(datetime.utcnow().timestamp())

    async def _get_guild_config(self, guild_id: int):
        """Get guild configuration"""
        try:
            return await self.db.fetchrow(
                "SELECT * FROM warning_config WHERE guild_id = $1",
                guild_id
            )
        except Exception as e:
            logging.error(f"Failed to get guild config: {e}")
            return None

    async def _log_warning(self, user_id: int, moderator_id: int, guild_id: int, 
                          reason: str, case_id: int, timestamp: datetime, warning_type: str = "general"):
        """Log warning to database"""
        try:
            await self.db.execute(
                """
                INSERT INTO warnings 
                (user_id, moderator_id, guild_id, reason, case_id, timestamp, warning_type)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                user_id, moderator_id, guild_id, reason, case_id, timestamp, warning_type
            )
            return True
        except Exception as e:
            logging.error(f"Failed to log warning: {e}")
            return False

    async def _get_warnings(self, user_id: int, guild_id: int, active_only: bool = True):
        """Get warnings for a user"""
        try:
            if active_only:
                return await self.db.fetch(
                    """
                    SELECT case_id, moderator_id, reason, timestamp, warning_type, 
                           appeal_status, expiry_date, expired
                    FROM warnings
                    WHERE user_id = $1 AND guild_id = $2 AND expired = FALSE
                    ORDER BY timestamp DESC
                    """,
                    user_id, guild_id
                )
            else:
                return await self.db.fetch(
                    """
                    SELECT case_id, moderator_id, reason, timestamp, warning_type, 
                           appeal_status, expiry_date, expired
                    FROM warnings
                    WHERE user_id = $1 AND guild_id = $2
                    ORDER BY timestamp DESC
                    """,
                    user_id, guild_id
                )
        except Exception as e:
            logging.error(f"Failed to fetch warnings: {e}")
            return []

    async def _get_warning_by_case(self, case_id: int, guild_id: int):
        """Get a specific warning by case ID"""
        try:
            return await self.db.fetchrow(
                """
                SELECT user_id, moderator_id, reason, timestamp, warning_type, 
                       appeal_text, appeal_status, expiry_date, expired
                FROM warnings
                WHERE case_id = $1 AND guild_id = $2
                """,
                case_id, guild_id
            )
        except Exception as e:
            logging.error(f"Failed to fetch warning by case ID: {e}")
            return None

    async def _log_mod_action(self, guild_id: int, action: str, moderator_id: int, 
                             target_id: int, reason: str, case_id: Optional[int] = None):
        """Log moderation action to channel"""
        config = await self._get_guild_config(guild_id)
        if not config or not config['log_channel_id']:
            return
            
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
            
        channel = guild.get_channel(config['log_channel_id'])
        if not channel:
            return
            
        moderator = guild.get_member(moderator_id)
        target = guild.get_member(target_id)
        
        embed = discord.Embed(
            title=f"Moderation Action: {action}",
            color=discord.Color.orange(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="Moderator", value=f"{moderator} ({moderator_id})", inline=False)
        embed.add_field(name="User", value=f"{target} ({target_id})", inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        if case_id:
            embed.add_field(name="Case ID", value=f"#{case_id}", inline=False)
            
        try:
            await channel.send(embed=embed)
        except Exception as e:
            logging.error(f"Failed to log mod action: {e}")

    async def _check_auto_moderation(self, user_id: int, guild_id: int):
        """Check if user should receive automatic moderation"""
        config = await self._get_guild_config(guild_id)
        if not config or not config['auto_moderation']:
            return
            
        warnings = await self._get_warnings(user_id, guild_id)
        warn_count = len(warnings)
        
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
            
        member = guild.get_member(user_id)
        if not member:
            return
            
        # Escalation rules
        if warn_count == 3 and config.get('escalate_3_warns', True):
            # Add muted role or similar action
            pass
        elif warn_count == 5 and config.get('escalate_5_warns', True):
            # Temp ban or similar action
            pass

    @commands.hybrid_command(name="warn")
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def warn_user(self, ctx, member: discord.Member, 
                       warning_type: str = "general", *, reason: str = "No reason provided"):
        """Warn a user with a reason"""
        if member == ctx.author:
            await ctx.send("You cannot warn yourself!", ephemeral=True)
            return

        if member.bot:
            await ctx.send("You cannot warn a bot!", ephemeral=True)
            return

        # Create case ID
        case_id = await self._get_next_case_id()
        
        # Log warning
        success = await self._log_warning(
            user_id=member.id,
            moderator_id=ctx.author.id,
            guild_id=ctx.guild.id,
            reason=reason,
            case_id=case_id,
            timestamp=ctx.message.created_at,
            warning_type=warning_type
        )

        if not success:
            await ctx.send("Failed to log warning. Please try again.", ephemeral=True)
            return

        # Send DM to warned user
        dm_success = True
        config = await self._get_guild_config(ctx.guild.id)
        if config and config.get('dm_users', True):
            try:
                embed = discord.Embed(
                    title="⚠️ You have been warned",
                    description=f"You received a warning in **{ctx.guild.name}**",
                    color=discord.Color.red(),
                    timestamp=ctx.message.created_at
                )
                embed.add_field(name="Type", value=warning_type.title(), inline=False)
                embed.add_field(name="Reason", value=reason, inline=False)
                embed.add_field(name="Case ID", value=f"`#{case_id}`", inline=False)
                embed.set_footer(text="Please follow server rules to avoid further action")
                
                await member.send(embed=embed)
            except discord.Forbidden:
                dm_success = False

        # Confirmation message
        embed = discord.Embed(
            title="User Warned",
            description=f"{member.mention} has been warned",
            color=discord.Color.orange(),
            timestamp=ctx.message.created_at
        )
        embed.add_field(name="Type", value=warning_type.title(), inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Case ID", value=f"`#{case_id}`", inline=False)
        embed.add_field(name="User", value=f"{member} ({member.id})", inline=False)
        embed.add_field(name="Moderator", value=f"{ctx.author} ({ctx.author.id})", inline=False)
        if not dm_success:
            embed.set_footer(text="⚠️ Could not DM user")
        
        await ctx.send(embed=embed)
        
        # Log action
        await self._log_mod_action(
            ctx.guild.id, "Warning", ctx.author.id, member.id, 
            f"{warning_type}: {reason}", case_id
        )
        
        # Check for auto-moderation
        await self._check_auto_moderation(member.id, ctx.guild.id)

    @commands.hybrid_command(name="warnings")
    @commands.has_permissions(manage_messages=True)
    async def list_warnings(self, ctx, member: Optional[discord.Member] = None, 
                           include_expired: bool = False):
        """List warnings for a user"""
        target = member or ctx.author
        warnings = await self._get_warnings(target.id, ctx.guild.id, active_only=not include_expired)
        
        if not warnings:
            status = " (including expired)" if include_expired else ""
            await ctx.send(f"{target.mention} has no warnings{status}.")
            return

        embed = discord.Embed(
            title=f"Warnings for {target}",
            color=discord.Color.orange()
        )
        
        warning_count = len(warnings)
        for i, warning in enumerate(warnings[:10]):  # Show only last 10 warnings
            moderator = ctx.guild.get_member(warning['moderator_id']) or "Unknown"
            expired_text = " (Expired)" if warning.get('expired') else ""
            appeal_status = f" ({warning['appeal_status'].title()})" if warning['appeal_status'] != 'none' else ""
            
            embed.add_field(
                name=f"Case #{warning['case_id']}{expired_text}{appeal_status}",
                value=f"**Type:** {warning['warning_type'].title()}\n"
                      f"**Reason:** {warning['reason']}\n"
                      f"**Moderator:** {moderator}\n"
                      f"**Date:** {warning['timestamp'].strftime('%Y-%m-%d %H:%M:%S')}",
                inline=False
            )
        
        if warning_count > 10:
            embed.set_footer(text=f"Showing 10 of {warning_count} warnings")
        else:
            embed.set_footer(text=f"Total: {warning_count} warning(s)")
            
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="delwarn")
    @commands.has_permissions(administrator=True)
    async def delete_warning(self, ctx, case_id: int):
        """Permanently delete a specific warning by case ID"""
        warning = await self._get_warning_by_case(case_id, ctx.guild.id)
        if not warning:
            await ctx.send("No warning found with that case ID.", ephemeral=True)
            return

        try:
            await self.db.execute(
                "DELETE FROM warnings WHERE case_id = $1 AND guild_id = $2",
                case_id, ctx.guild.id
            )
                
            await ctx.send(f"Warning case #{case_id} has been permanently deleted.")
            
            # Log action
            await self._log_mod_action(
                ctx.guild.id, "Warning Deletion", ctx.author.id, 
                warning['user_id'], f"Deleted case #{case_id}", case_id
            )
        except Exception as e:
            logging.error(f"Failed to delete warning: {e}")
            await ctx.send("An error occurred while deleting the warning.", ephemeral=True)

    @commands.hybrid_command(name="clearwarns")
    @commands.has_permissions(administrator=True)
    async def clear_warnings(self, ctx, member: discord.Member):
        """Clear all warnings for a user"""
        try:
            result = await self.db.execute(
                "DELETE FROM warnings WHERE user_id = $1 AND guild_id = $2",
                member.id, ctx.guild.id
            )
            
            deleted_count = int(result.split()[1]) if result.startswith("DELETE") else 0
            await ctx.send(f"Cleared {deleted_count} warning(s) for {member.mention}.")
            
            # Log action
            await self._log_mod_action(
                ctx.guild.id, "Warnings Cleared", ctx.author.id, 
                member.id, f"Cleared {deleted_count} warnings"
            )
        except Exception as e:
            logging.error(f"Failed to clear warnings: {e}")
            await ctx.send("An error occurred while clearing warnings.", ephemeral=True)

    @commands.hybrid_command(name="warncount")
    @commands.has_permissions(manage_messages=True)
    async def warn_count(self, ctx, member: Optional[discord.Member] = None):
        """Show warning count for a user"""
        target = member or ctx.author
        warnings = await self._get_warnings(target.id, ctx.guild.id)
        
        embed = discord.Embed(
            title=f"Warning Summary for {target}",
            color=discord.Color.orange()
        )
        embed.add_field(name="Active Warnings", value=str(len(warnings)), inline=True)
        embed.add_field(name="User ID", value=str(target.id), inline=True)
        embed.set_thumbnail(url=target.display_avatar.url)
        
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="editwarn")
    @commands.has_permissions(manage_messages=True)
    async def edit_warning(self, ctx, case_id: int, *, new_reason: str):
        """Edit the reason for a warning"""
        warning = await self._get_warning_by_case(case_id, ctx.guild.id)
        if not warning:
            await ctx.send("No warning found with that case ID.", ephemeral=True)
            return

        try:
            await self.db.execute(
                "UPDATE warnings SET reason = $1 WHERE case_id = $2 AND guild_id = $3",
                new_reason, case_id, ctx.guild.id
            )
            
            await ctx.send(f"Warning case #{case_id} reason updated.")
            
            # Log action
            await self._log_mod_action(
                ctx.guild.id, "Warning Edit", ctx.author.id, 
                warning['user_id'], f"Changed reason to: {new_reason}", case_id
            )
        except Exception as e:
            logging.error(f"Failed to edit warning: {e}")
            await ctx.send("An error occurred while editing the warning.", ephemeral=True)

    @commands.hybrid_command(name="expirewarn")
    @commands.has_permissions(manage_messages=True)
    async def expire_warning(self, ctx, case_id: int, days: int = 30):
        """Set a warning to expire after specified days"""
        warning = await self._get_warning_by_case(case_id, ctx.guild.id)
        if not warning:
            await ctx.send("No warning found with that case ID.", ephemeral=True)
            return
            
        expiry_date = datetime.utcnow() + timedelta(days=days)
        
        try:
            await self.db.execute(
                "UPDATE warnings SET expiry_date = $1 WHERE case_id = $2 AND guild_id = $3",
                expiry_date, case_id, ctx.guild.id
            )
            
            await ctx.send(f"Warning #{case_id} will expire in {days} days.")
            
            # Log action
            await self._log_mod_action(
                ctx.guild.id, "Warning Expiry Set", ctx.author.id, 
                warning['user_id'], f"Set to expire in {days} days", case_id
            )
        except Exception as e:
            logging.error(f"Failed to set expiry: {e}")
            await ctx.send("Failed to set expiry date.", ephemeral=True)

    @commands.hybrid_command(name="appeal")
    async def appeal_warning(self, ctx, case_id: int, *, appeal_text: str):
        """Appeal a warning"""
        warning = await self._get_warning_by_case(case_id, ctx.guild.id)
        if not warning:
            await ctx.send("No warning found with that case ID.", ephemeral=True)
            return
            
        if warning['user_id'] != ctx.author.id:
            await ctx.send("You can only appeal your own warnings.", ephemeral=True)
            return
            
        if warning['appeal_status'] != 'none':
            await ctx.send("This warning already has an appeal status.", ephemeral=True)
            return
            
        try:
            await self.db.execute(
                """
                UPDATE warnings 
                SET appeal_text = $1, appeal_status = 'pending'
                WHERE case_id = $2 AND guild_id = $3
                """,
                appeal_text, case_id, ctx.guild.id
            )
            
            await ctx.send("Your appeal has been submitted.")
            
            # Log action
            await self._log_mod_action(
                ctx.guild.id, "Warning Appeal", ctx.author.id, 
                ctx.author.id, f"Appealed case #{case_id}", case_id
            )
        except Exception as e:
            logging.error(f"Failed to submit appeal: {e}")
            await ctx.send("Failed to submit appeal.", ephemeral=True)

    @commands.hybrid_command(name="resolveappeal")
    @commands.has_permissions(manage_messages=True)
    async def resolve_appeal(self, ctx, case_id: int, decision: str, *, reason: str = "No reason provided"):
        """Resolve a warning appeal (approve/deny)"""
        if decision not in ['approve', 'deny']:
            await ctx.send("Decision must be 'approve' or 'deny'.", ephemeral=True)
            return
            
        warning = await self._get_warning_by_case(case_id, ctx.guild.id)
        if not warning:
            await ctx.send("No warning found with that case ID.", ephemeral=True)
            return
            
        if warning['appeal_status'] != 'pending':
            await ctx.send("This warning does not have a pending appeal.", ephemeral=True)
            return
            
        try:
            await self.db.execute(
                """
                UPDATE warnings 
                SET appeal_status = $1
                WHERE case_id = $2 AND guild_id = $3
                """,
                decision, case_id, ctx.guild.id
            )
            
            status_text = "approved" if decision == "approve" else "denied"
            await ctx.send(f"Appeal for warning #{case_id} has been {status_text}.")
            
            # Notify user
            user = ctx.guild.get_member(warning['user_id'])
            if user:
                try:
                    await user.send(
                        f"Your appeal for warning #{case_id} has been {status_text}.\n"
                        f"Reason: {reason}"
                    )
                except discord.Forbidden:
                    pass  # Can't DM user
                    
            # Log action
            await self._log_mod_action(
                ctx.guild.id, f"Appeal {status_text.title()}", ctx.author.id, 
                warning['user_id'], reason, case_id
            )
        except Exception as e:
            logging.error(f"Failed to resolve appeal: {e}")
            await ctx.send("Failed to resolve appeal.", ephemeral=True)

    @commands.hybrid_command(name="exportwarns")
    @commands.has_permissions(administrator=True)
    async def export_warnings(self, ctx, member: Optional[discord.Member] = None):
        """Export warnings to CSV"""
        target = member or ctx.author
        warnings = await self._get_warnings(target.id, ctx.guild.id, active_only=False)
        
        if not warnings:
            await ctx.send(f"{target.mention} has no warnings to export.")
            return
            
        # Create CSV
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Case ID', 'User ID', 'Moderator ID', 'Reason', 'Type', 
                        'Timestamp', 'Appeal Status', 'Expiry Date', 'Expired'])
        
        for warning in warnings:
            writer.writerow([
                warning['case_id'],
                target.id,
                warning['moderator_id'],
                warning['reason'],
                warning['warning_type'],
                warning['timestamp'].isoformat(),
                warning['appeal_status'],
                warning['expiry_date'].isoformat() if warning['expiry_date'] else '',
                warning['expired']
            ])
            
        output.seek(0)
        buffer = io.BytesIO(output.getvalue().encode())
        buffer.name = f"warnings_{target.id}.csv"
        
        await ctx.send(
            f"Exported {len(warnings)} warnings for {target}",
            file=discord.File(buffer, filename=buffer.name)
        )

    @commands.hybrid_command(name="warnconfig")
    @commands.has_permissions(administrator=True)
    async def config_warnings(self, ctx, setting: str, value: str):
        """Configure warning system settings"""
        valid_settings = ['dm_users', 'auto_moderation', 'log_channel', 'escalate_3_warns', 'escalate_5_warns']
        if setting not in valid_settings:
            await ctx.send(f"Valid settings: {', '.join(valid_settings)}", ephemeral=True)
            return
            
        try:
            # Get current config
            current = await self.db.fetchrow(
                "SELECT * FROM warning_config WHERE guild_id = $1",
                ctx.guild.id
            )
            
            # Create if not exists
            if not current:
                await self.db.execute(
                    """
                    INSERT INTO warning_config (guild_id) VALUES ($1)
                    """,
                    ctx.guild.id
                )
            
            # Update setting
            if setting == 'log_channel':
                try:
                    channel_id = int(value)
                    channel = ctx.guild.get_channel(channel_id)
                    if not channel:
                        await ctx.send("Invalid channel ID.", ephemeral=True)
                        return
                except ValueError:
                    await ctx.send("Channel ID must be a number.", ephemeral=True)
                    return
                    
                await self.db.execute(
                    f"UPDATE warning_config SET {setting} = $1 WHERE guild_id = $2",
                    channel_id, ctx.guild.id
                )
            elif setting in ['dm_users', 'auto_moderation', 'escalate_3_warns', 'escalate_5_warns']:
                bool_value = value.lower() in ['true', '1', 'yes', 'on']
                await self.db.execute(
                    f"UPDATE warning_config SET {setting} = $1 WHERE guild_id = $2",
                    bool_value, ctx.guild.id
                )
                
            await ctx.send(f"Setting `{setting}` updated to `{value}`.")
        except Exception as e:
            logging.error(f"Failed to update config: {e}")
            await ctx.send("Failed to update configuration.", ephemeral=True)

    @commands.hybrid_command(name="warnstats")
    @commands.has_permissions(manage_messages=True)
    async def warning_stats(self, ctx):
        """Show server warning statistics"""
        try:
            # Total warnings
            total = await self.db.fetchval(
                "SELECT COUNT(*) FROM warnings WHERE guild_id = $1",
                ctx.guild.id
            )
            
            # Active warnings
            active = await self.db.fetchval(
                "SELECT COUNT(*) FROM warnings WHERE guild_id = $1 AND expired = FALSE",
                ctx.guild.id
            )
            
            # Top moderators
            top_mods = await self.db.fetch(
                """
                SELECT moderator_id, COUNT(*) as count
                FROM warnings
                WHERE guild_id = $1
                GROUP BY moderator_id
                ORDER BY count DESC
                LIMIT 5
                """,
                ctx.guild.id
            )
            
            # Warning types
            type_counts = await self.db.fetch(
                """
                SELECT warning_type, COUNT(*) as count
                FROM warnings
                WHERE guild_id = $1
                GROUP BY warning_type
                ORDER BY count DESC
                """,
                ctx.guild.id
            )
            
            embed = discord.Embed(
                title="Warning Statistics",
                color=discord.Color.blue()
            )
            embed.add_field(name="Total Warnings", value=str(total), inline=True)
            embed.add_field(name="Active Warnings", value=str(active), inline=True)
            embed.add_field(name="Expired Warnings", value=str(total-active), inline=True)
            
            if top_mods:
                mod_list = []
                for mod in top_mods:
                    moderator = ctx.guild.get_member(mod['moderator_id'])
                    mod_list.append(f"{moderator or 'Unknown'}: {mod['count']}")
                embed.add_field(name="Top Moderators", value="\n".join(mod_list), inline=False)
                
            if type_counts:
                type_list = [f"{t['warning_type'].title()}: {t['count']}" for t in type_counts]
                embed.add_field(name="Warning Types", value="\n".join(type_list), inline=False)
                
            await ctx.send(embed=embed)
        except Exception as e:
            logging.error(f"Failed to get warning stats: {e}")
            await ctx.send("Failed to retrieve statistics.", ephemeral=True)

async def setup(bot, db):
    await bot.add_cog(WarningSystem(bot, db))