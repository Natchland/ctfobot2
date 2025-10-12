# cogs/warnings.py
import discord
from discord.ext import commands, tasks
from typing import Optional
import logging
from datetime import datetime, timedelta
import json
import os
import io
import csv

# Create data directory if it doesn't exist
DATA_DIR = "data"
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

WARNINGS_FILE = os.path.join(DATA_DIR, "warnings.json")
CONFIG_FILE = os.path.join(DATA_DIR, "warning_config.json")

class WarningSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.warnings = self._load_warnings()
        self.config = self._load_config()
        
    def _load_warnings(self):
        """Load warnings from JSON file"""
        try:
            if os.path.exists(WARNINGS_FILE):
                with open(WARNINGS_FILE, 'r') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logging.error(f"Error loading warnings: {e}")
            return {}
    
    def _save_warnings(self):
        """Save warnings to JSON file"""
        try:
            with open(WARNINGS_FILE, 'w') as f:
                json.dump(self.warnings, f, indent=2, default=str)
        except Exception as e:
            logging.error(f"Error saving warnings: {e}")
    
    def _load_config(self):
        """Load config from JSON file"""
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logging.error(f"Error loading config: {e}")
            return {}
    
    def _save_config(self):
        """Save config to JSON file"""
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            logging.error(f"Error saving config: {e}")
    
    async def cog_load(self):
        """Initialize the cog"""
        # Start expiration checker
        self.check_expired_warnings.start()
    
    async def cog_unload(self):
        """Clean up when cog is unloaded"""
        if hasattr(self, 'check_expired_warnings') and self.check_expired_warnings.is_running():
            self.check_expired_warnings.cancel()
        self._save_warnings()
        self._save_config()
    
    @tasks.loop(hours=1)
    async def check_expired_warnings(self):
        """Check for expired warnings and mark them as expired"""
        try:
            now = datetime.utcnow()
            changed = False
            
            for guild_id, guild_warnings in self.warnings.items():
                for warning in guild_warnings:
                    if warning.get('expiry_date'):
                        expiry = datetime.fromisoformat(warning['expiry_date'])
                        if expiry <= now and not warning.get('expired', False):
                            warning['expired'] = True
                            changed = True
                            
                            # Log expiration if log channel exists
                            guild = self.bot.get_guild(int(guild_id))
                            if guild:
                                config = self.config.get(guild_id, {})
                                if config.get('log_channel_id'):
                                    channel = guild.get_channel(config['log_channel_id'])
                                    if channel:
                                        user = guild.get_member(warning['user_id'])
                                        if user:
                                            try:
                                                await channel.send(
                                                    f"Warning #{warning['case_id']} for {user.mention} "
                                                    f"has expired automatically."
                                                )
                                            except Exception:
                                                pass  # Ignore channel send errors
            
            if changed:
                self._save_warnings()
                
        except Exception as e:
            logging.error(f"Error checking expired warnings: {e}")
    
    def _get_next_case_id(self, guild_id: str):
        """Get next case ID for a guild"""
        try:
            guild_warnings = self.warnings.get(guild_id, [])
            if not guild_warnings:
                return 1
            return max(w['case_id'] for w in guild_warnings) + 1
        except Exception as e:
            logging.error(f"Failed to get next case ID: {e}")
            return 1
    
    def _get_guild_config(self, guild_id: str):
        """Get guild configuration"""
        return self.config.get(guild_id, {})
    
    def _log_warning(self, user_id: int, moderator_id: int, guild_id: int, 
                    reason: str, case_id: int, timestamp: datetime, warning_type: str = "general"):
        """Log warning to storage"""
        try:
            guild_id_str = str(guild_id)
            if guild_id_str not in self.warnings:
                self.warnings[guild_id_str] = []
            
            warning = {
                'id': len(self.warnings[guild_id_str]) + 1,
                'user_id': user_id,
                'moderator_id': moderator_id,
                'guild_id': guild_id,
                'reason': reason,
                'case_id': case_id,
                'timestamp': timestamp.isoformat(),
                'warning_type': warning_type,
                'appeal_text': None,
                'appeal_status': 'none',
                'expiry_date': None,
                'expired': False
            }
            
            self.warnings[guild_id_str].append(warning)
            self._save_warnings()
            return True
        except Exception as e:
            logging.error(f"Failed to log warning: {e}")
            return False
    
    def _get_warnings(self, user_id: int, guild_id: int, active_only: bool = True):
        """Get warnings for a user"""
        try:
            guild_id_str = str(guild_id)
            if guild_id_str not in self.warnings:
                return []
            
            user_warnings = [w for w in self.warnings[guild_id_str] if w['user_id'] == user_id]
            
            if active_only:
                user_warnings = [w for w in user_warnings if not w.get('expired', False)]
            
            # Sort by timestamp descending
            user_warnings.sort(key=lambda x: x['timestamp'], reverse=True)
            return user_warnings
        except Exception as e:
            logging.error(f"Failed to fetch warnings: {e}")
            return []
    
    def _get_warning_by_case(self, case_id: int, guild_id: int):
        """Get a specific warning by case ID"""
        try:
            guild_id_str = str(guild_id)
            if guild_id_str not in self.warnings:
                return None
            
            for warning in self.warnings[guild_id_str]:
                if warning['case_id'] == case_id:
                    return warning
            return None
        except Exception as e:
            logging.error(f"Failed to fetch warning by case ID: {e}")
            return None
    
    async def _log_mod_action(self, guild_id: int, action: str, moderator_id: int, 
                             target_id: int, reason: str, case_id: Optional[int] = None):
        """Log moderation action to channel"""
        config = self._get_guild_config(str(guild_id))
        if not config.get('log_channel_id'):
            return
            
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
            
        channel = guild.get_channel(config['log_channel_id'])
        if not channel:
            return
            
        moderator = guild.get_member(moderator_id)
        target = guild.get_member(target_id)
        
        if not moderator or not target:
            return
            
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
        guild_id_str = str(ctx.guild.id)
        case_id = self._get_next_case_id(guild_id_str)
        
        # Log warning
        success = self._log_warning(
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
        config = self._get_guild_config(guild_id_str)
        if config.get('dm_users', True):
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
    
    @commands.hybrid_command(name="warnings")
    @commands.has_permissions(manage_messages=True)
    async def list_warnings(self, ctx, member: Optional[discord.Member] = None, 
                           include_expired: bool = False):
        """List warnings for a user"""
        target = member or ctx.author
        warnings = self._get_warnings(target.id, ctx.guild.id, active_only=not include_expired)
        
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
            
            timestamp = datetime.fromisoformat(warning['timestamp'])
            
            embed.add_field(
                name=f"Case #{warning['case_id']}{expired_text}{appeal_status}",
                value=f"**Type:** {warning['warning_type'].title()}\n"
                      f"**Reason:** {warning['reason']}\n"
                      f"**Moderator:** {moderator}\n"
                      f"**Date:** {timestamp.strftime('%Y-%m-%d %H:%M:%S')}",
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
        guild_id_str = str(ctx.guild.id)
        warning = self._get_warning_by_case(case_id, ctx.guild.id)
        if not warning:
            await ctx.send("No warning found with that case ID.", ephemeral=True)
            return
    
        try:
            # Remove warning from list
            if guild_id_str in self.warnings:
                self.warnings[guild_id_str] = [
                    w for w in self.warnings[guild_id_str] 
                    if w['case_id'] != case_id
                ]
                self._save_warnings()
                
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
            guild_id_str = str(ctx.guild.id)
            if guild_id_str in self.warnings:
                initial_count = len(self.warnings[guild_id_str])
                self.warnings[guild_id_str] = [
                    w for w in self.warnings[guild_id_str] 
                    if w['user_id'] != member.id
                ]
                self._save_warnings()
                
                deleted_count = initial_count - len(self.warnings[guild_id_str])
            else:
                deleted_count = 0
                
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
        warnings = self._get_warnings(target.id, ctx.guild.id)
        
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
        guild_id_str = str(ctx.guild.id)
        warning = self._get_warning_by_case(case_id, ctx.guild.id)
        if not warning:
            await ctx.send("No warning found with that case ID.", ephemeral=True)
            return
    
        try:
            # Update warning reason
            if guild_id_str in self.warnings:
                for w in self.warnings[guild_id_str]:
                    if w['case_id'] == case_id:
                        w['reason'] = new_reason
                        break
                self._save_warnings()
            
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
        guild_id_str = str(ctx.guild.id)
        warning = self._get_warning_by_case(case_id, ctx.guild.id)
        if not warning:
            await ctx.send("No warning found with that case ID.", ephemeral=True)
            return
            
        expiry_date = datetime.utcnow() + timedelta(days=days)
        
        try:
            # Update warning expiry date
            if guild_id_str in self.warnings:
                for w in self.warnings[guild_id_str]:
                    if w['case_id'] == case_id:
                        w['expiry_date'] = expiry_date.isoformat()
                        break
                self._save_warnings()
            
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
        guild_id_str = str(ctx.guild.id)
        warning = self._get_warning_by_case(case_id, ctx.guild.id)
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
            # Update warning appeal status
            if guild_id_str in self.warnings:
                for w in self.warnings[guild_id_str]:
                    if w['case_id'] == case_id:
                        w['appeal_text'] = appeal_text
                        w['appeal_status'] = 'pending'
                        break
                self._save_warnings()
            
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
            
        guild_id_str = str(ctx.guild.id)
        warning = self._get_warning_by_case(case_id, ctx.guild.id)
        if not warning:
            await ctx.send("No warning found with that case ID.", ephemeral=True)
            return
            
        if warning['appeal_status'] != 'pending':
            await ctx.send("This warning does not have a pending appeal.", ephemeral=True)
            return
            
        try:
            # Update warning appeal status
            if guild_id_str in self.warnings:
                for w in self.warnings[guild_id_str]:
                    if w['case_id'] == case_id:
                        w['appeal_status'] = decision
                        break
                self._save_warnings()
            
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
        warnings = self._get_warnings(target.id, ctx.guild.id, active_only=False)
        
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
                warning['timestamp'],
                warning['appeal_status'],
                warning['expiry_date'] or '',
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
        valid_settings = ['dm_users', 'auto_moderation', 'log_channel_id', 'escalate_3_warns', 'escalate_5_warns']
        if setting not in valid_settings:
            await ctx.send(f"Valid settings: {', '.join(valid_settings)}", ephemeral=True)
            return
            
        try:
            guild_id_str = str(ctx.guild.id)
            if guild_id_str not in self.config:
                self.config[guild_id_str] = {}
            
            # Update setting
            if setting == 'log_channel_id':
                try:
                    channel_id = int(value)
                    channel = ctx.guild.get_channel(channel_id)
                    if not channel:
                        await ctx.send("Invalid channel ID.", ephemeral=True)
                        return
                except ValueError:
                    await ctx.send("Channel ID must be a number.", ephemeral=True)
                    return
                    
                self.config[guild_id_str]['log_channel_id'] = channel_id
            elif setting in ['dm_users', 'auto_moderation', 'escalate_3_warns', 'escalate_5_warns']:
                bool_value = value.lower() in ['true', '1', 'yes', 'on']
                self.config[guild_id_str][setting] = bool_value
                
            self._save_config()
            await ctx.send(f"Setting `{setting}` updated to `{value}`.")
        except Exception as e:
            logging.error(f"Failed to update config: {e}")
            await ctx.send("Failed to update configuration.", ephemeral=True)
    
    @commands.hybrid_command(name="warnstats")
    @commands.has_permissions(manage_messages=True)
    async def warning_stats(self, ctx):
        """Show server warning statistics"""
        try:
            guild_id_str = str(ctx.guild.id)
            guild_warnings = self.warnings.get(guild_id_str, [])
            
            # Total warnings
            total = len(guild_warnings)
            
            # Active warnings
            active = len([w for w in guild_warnings if not w.get('expired', False)])
            
            # Top moderators
            mod_counts = {}
            for warning in guild_warnings:
                mod_id = warning['moderator_id']
                mod_counts[mod_id] = mod_counts.get(mod_id, 0) + 1
            
            top_mods = sorted(mod_counts.items(), key=lambda x: x[1], reverse=True)[:5]
            
            # Warning types
            type_counts = {}
            for warning in guild_warnings:
                w_type = warning['warning_type']
                type_counts[w_type] = type_counts.get(w_type, 0) + 1
            
            top_types = sorted(type_counts.items(), key=lambda x: x[1], reverse=True)[:5]
            
            embed = discord.Embed(
                title="Warning Statistics",
                color=discord.Color.blue()
            )
            embed.add_field(name="Total Warnings", value=str(total), inline=True)
            embed.add_field(name="Active Warnings", value=str(active), inline=True)
            embed.add_field(name="Expired Warnings", value=str(max(0, total-active)), inline=True)
            
            if top_mods:
                mod_list = []
                for mod_id, count in top_mods:
                    moderator = ctx.guild.get_member(mod_id)
                    mod_list.append(f"{moderator or 'Unknown'}: {count}")
                embed.add_field(name="Top Moderators", value="\n".join(mod_list[:5]), inline=False)
                
            if top_types:
                type_list = [f"{w_type.title()}: {count}" for w_type, count in top_types[:5]]
                embed.add_field(name="Warning Types", value="\n".join(type_list), inline=False)
                
            await ctx.send(embed=embed)
        except Exception as e:
            logging.error(f"Failed to get warning stats: {e}")
            await ctx.send("Failed to retrieve statistics.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(WarningSystem(bot))