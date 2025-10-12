# cogs/activity.py  –  daily inactivity management
# Production-ready • discord.py ≥2.3 • 2024-10-03

from __future__ import annotations

import asyncio
import logging
import os
import traceback
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List

import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("cog.activity")
log.setLevel(logging.DEBUG)

GUILD_ID              = int(os.getenv("GUILD_ID", "1377035207777194005"))
ACTIVE_MEMBER_ROLE_ID = 1403337937722019931
INACTIVE_ROLE_ID      = 1416864151829221446

STAFF_ROLE_IDS: set[int] = {
    1377103244089622719,
    1377077466513932338,
    1377084533706588201,
    1410659214959054988,
}

PROMOTE_STREAK      = 3
INACTIVE_AFTER_DAYS = 5
KICK_AFTER_DAYS     = 14
WARN_BEFORE_DAYS    = INACTIVE_AFTER_DAYS - 1

KICK_WARN_D1 = 7
KICK_WARN_D2 = 13

GENERAL_CH_ID  = 1398657081338237028   # #clan-general
INACTIVE_CH_ID = 1416865404860502026  # #inactive-players
LOG_CH_ID      = 1422627154826493983  # #bot-logs

PERIOD_CHOICES = [
    ("1 week", 7), ("2 weeks", 14), ("1 month", 30), ("2 months", 60)
]


class ActivityCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db):
        self.bot, self.db = bot, db
        self._maintenance_task = None
        log.info("ActivityCog initialised")

    async def cog_load(self):
        """Called when the cog is loaded"""
        # Schedule maintenance task to start after bot is ready
        self._maintenance_task = asyncio.create_task(self._start_maintenance_task())
        log.info("ActivityCog load scheduled")

    async def _start_maintenance_task(self):
        """Start the maintenance task after bot is ready"""
        try:
            # Wait for bot to be ready
            await self.bot.wait_until_ready()
            
            # Wait a bit more for everything to stabilize
            await asyncio.sleep(5)
            
            # Start the maintenance task
            self.maintenance.start()
            log.info("Maintenance task started successfully")
        except Exception as e:
            log.error(f"Failed to start maintenance task: {e}")

    # ───────────────────── generic helpers ─────────────────────
    async def _set_activity(self, uid: int, *, streak: int,
                            last_date: date, warned: bool):
        await self.db.set_activity(
            uid, streak, last_date, warned, datetime.now(timezone.utc)
        )

    async def _safe_dm(
        self, m: discord.Member, msg: str, *, fallback_channel=None
    ) -> bool:
        """Try to DM the member; on failure mention them in a fallback channel.

        Returns True if the DM succeeded, False otherwise.  Any exception is
        swallowed so the caller never crashes.
        """
        try:
            await m.send(msg)
            return True
        except Exception as e:
            ch_id = fallback_channel or INACTIVE_CH_ID
            ch = m.guild.get_channel(ch_id)
            if isinstance(ch, discord.TextChannel):
                try:
                    await ch.send(f"{m.mention} {msg}")
                except Exception as ch_err:
                    log.warning(f"Fallback message failed for {m}: {ch_err}")
            log.info(f"DM failed for {m}: {e} (used fallback channel)")
            return False

    async def _audit(self, user_id, event_type, details):
        try:
            await self.db.log_activity_event(user_id, event_type, details)
        except Exception as e:
            log.warning(f"Audit DB log failed: {e}")

    def _is_staff(self, m: discord.Member) -> bool:
        return (
            m.guild_permissions.administrator
            or any(r.id in STAFF_ROLE_IDS for r in m.roles)
        )

    async def _is_exempt(self, uid: int) -> bool:
        return uid in await self.db.get_exempt_users()

    async def _log(self, msg: str):
        ch = self.bot.get_channel(LOG_CH_ID)
        if isinstance(ch, discord.TextChannel):
            try:
                await ch.send(msg)
            except Exception as e:
                log.warning(f"Failed to log to channel: {e}")
        log.info(msg)

    # ───────────────────── listeners ─────────────────────
    async def mark_active(self, m: discord.Member):
        if m.bot:
            return
        today = date.today()
        rec = await self.db.get_activity(m.id)
        if not rec:
            streak, warned = 1, False
        else:
            if rec["date"] != today:
                streak = (
                    rec["streak"] + 1
                    if rec["date"] + timedelta(days=1) == today
                    else 1
                )
                warned = False
            else:
                streak, warned = rec["streak"], rec["warned"]
        await self._set_activity(
            m.id, streak=streak, last_date=today, warned=warned
        )

        # promotion
        if streak >= PROMOTE_STREAK:
            role = m.guild.get_role(ACTIVE_MEMBER_ROLE_ID)
            if role and role not in m.roles:
                try:
                    await m.add_roles(role, reason="Reached activity streak")
                    await self._log(f"⭐ Promoted {m} (streak {streak})")
                    await self._audit(m.id, "promote", f"Streak {streak}")
                except Exception as e:
                    await self._log(f"❌ Could not promote {m}: {e}")

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if msg.guild and not msg.author.bot:
            await self.mark_active(msg.author)

    @commands.Cog.listener()
    async def on_voice_state_update(self, m, before, after):
        if m.guild.id == GUILD_ID and not m.bot and before.channel is None and after.channel:
            await self.mark_active(m)

    # ───────────────────── maintenance ─────────────────────
    @tasks.loop(hours=24, reconnect=True)
    async def maintenance(self):
        """Main maintenance task - runs once per day"""
        try:
            await self._maintenance_cycle()
        except Exception as e:
            log.error(f"Error in maintenance cycle: {e}")
            await self._log(f"❌ Maintenance cycle failed: {e}")

    async def _maintenance_cycle(self):
        """Execute the daily maintenance cycle"""
        try:
            # --- skip if today's cycle already executed ---
            result = await self.db.fetch_one(
                """
                SELECT 1 FROM activity_audit
                 WHERE event_type = 'maintenance'
                   AND DATE(timestamp) = CURRENT_DATE
                 LIMIT 1
                """
            )
            if result:
                log.info("[activity] Daily maintenance already ran today – skipping.")
                return

            guild = self.bot.get_guild(GUILD_ID)
            if guild is None:
                log.warning("Guild not found")
                return

            role_active   = guild.get_role(ACTIVE_MEMBER_ROLE_ID)
            role_inactive = guild.get_role(INACTIVE_ROLE_ID)
            today         = date.today()

            stats = dict(promoted=0, demote_warn=0, kick_warn=0,
                         demoted=0, kicked=0, unmarked=0)

            recs: Dict[int, Dict] = await self.db.get_all_activity()
            exempt_ids = set(await self.db.get_exempt_users())

            for uid, rec in recs.items():
                m = guild.get_member(uid)
                if not m or m.bot or uid in exempt_ids:
                    continue

                days_idle = (today - rec["date"]).days
                exempt    = self._is_staff(m)

                # ---------- promotion catch-up ----------
                if (
                    rec["streak"] >= PROMOTE_STREAK
                    and role_active
                    and role_active not in m.roles
                ):
                    try:
                        await m.add_roles(role_active,
                                          reason="Reached activity streak (catch-up)")
                        stats["promoted"] += 1
                        await self._log(f"⭐ Promoted {m} (catch-up)")
                        await self._audit(m.id, "promote", "catch-up")
                    except Exception as e:
                        await self._log(f"❌ Could not promote {m}: {e}")

                # ---------- demotion warning ----------
                if (
                    days_idle == WARN_BEFORE_DAYS
                    and role_active in m.roles
                    and not rec["warned"]
                ):
                    left = INACTIVE_AFTER_DAYS - days_idle
                    msg = (
                        f"⚠️  You have been inactive in **{guild.name}** for "
                        f"**{days_idle} days**.\n"
                        f"You will lose your **Active Member** role in **{left} day**."
                    )
                    ok = await self._safe_dm(m, msg, fallback_channel=INACTIVE_CH_ID)
                    stats["demote_warn"] += 1
                    await self._log(f"⚠️ Demotion warning to {m} "
                                    f"(DM {'ok' if ok else 'fallback'})")
                    await self._audit(m.id, "demote_warn", f"idle={days_idle}")
                    await self._set_activity(uid, streak=rec["streak"],
                                             last_date=rec["date"], warned=True)

                # ---------- demote ----------
                if days_idle >= INACTIVE_AFTER_DAYS and role_active in m.roles:
                    try:
                        await m.remove_roles(role_active, reason="Inactive > 5 days")
                        stats["demoted"] += 1
                        await self._log(f"🔻 Demoted {m} (idle {days_idle}d)")
                        await self._audit(m.id, "demote", f"idle={days_idle}")
                    except Exception as e:
                        await self._log(f"❌ Could not demote {m}: {e}")
                    await self._set_activity(uid, streak=0, last_date=rec["date"],
                                             warned=False)

                # ---------- kick warnings ----------
                if (
                    not exempt
                    and days_idle in (KICK_WARN_D1, KICK_WARN_D2)
                ):
                    left = KICK_AFTER_DAYS - days_idle
                    msg = (
                        f"⚠️  You have been inactive in **{guild.name}** "
                        f"for **{days_idle} days**.\n"
                        f"You will be removed from the server in **{left} days**."
                    )
                    ok = await self._safe_dm(m, msg, fallback_channel=INACTIVE_CH_ID)
                    stats["kick_warn"] += 1
                    await self._log(f"⚠️ Kick warning to {m} "
                                    f"(DM {'ok' if ok else 'fallback'})")
                    await self._audit(m.id, "kick_warn", f"idle={days_idle}")

                # ---------- kick ----------
                if not exempt and days_idle >= KICK_AFTER_DAYS:
                    try:
                        kick_msg = (
                            f"👢 You have been removed from **{guild.name}** "
                            f"due to 14 days of inactivity.\n"
                            "You are welcome to re-join at any time!"
                        )
                        await self._safe_dm(m, kick_msg,
                                            fallback_channel=INACTIVE_CH_ID)
                        await guild.kick(m, reason="Inactive ≥14 days")
                        stats["kicked"] += 1
                        await self._log(f"👢 Kicked {m} (idle {days_idle}d)")
                        await self._audit(m.id, "kick", f"idle={days_idle}")
                    except Exception as e:
                        await self._log(f"❌ Could not kick {m}: {e}")

            # ---------- remove expired inactive roles ----------
            now_ts = int(datetime.now(timezone.utc).timestamp())
            expired = await self.db.get_expired_inactive(now_ts)
            for row in expired:
                mem = guild.get_member(row["user_id"])
                if not mem:
                    await self.db.remove_inactive(row["user_id"])
                    continue
                if role_inactive and role_inactive in mem.roles:
                    try:
                        await mem.remove_roles(role_inactive,
                                               reason="Inactive period elapsed")
                        stats["unmarked"] += 1
                        await self._audit(mem.id, "unmark_inactive", "period over")
                    except Exception as e:
                        await self._log(f"❌ Could not unmark {mem}: {e}")
                await self._set_activity(mem.id, streak=0,
                                         last_date=today, warned=False)
                await self.db.remove_inactive(mem.id)

            await self._log(f"[Daily] {stats}")
            # mark that today's maintenance ran
            await self._audit(0, "maintenance", "daily cycle")
            
        except Exception as e:
            log.error(f"Maintenance cycle error: {e}")
            raise

    # ───────────────────── Slash-commands (existing) ─────────────────────
    activity_group = app_commands.Group(name="activity", description="Activity tools")

    @activity_group.command(name="info", description="Show your activity status")
    async def activity_info(self, inter: discord.Interaction):
        rec = await self.db.get_activity(inter.user.id)
        if not rec:
            await inter.response.send_message(
                "No activity recorded for you yet.", ephemeral=True
            )
            return
        today = date.today()
        days_idle = (today - rec["date"]).days
        embed = discord.Embed(
            title="Your Activity Stats",
            colour=discord.Color.orange(),
            description=(
                f"**Current streak:** {rec['streak']} day(s)\n"
                f"**Last activity:** {rec['date']}\n"
                f"**Days idle:** {days_idle}\n"
                f"**Days until demotion:** {max(INACTIVE_AFTER_DAYS - days_idle, 0)}\n"
                f"**Days until kick:** {max(KICK_AFTER_DAYS - days_idle, 0)}"
            ),
        )
        await inter.response.send_message(embed=embed, ephemeral=True)

    @activity_group.command(
        name="inactive-list",
        description="List members idle ≥4 days (staff)"
    )
    async def inactive_list(self, inter: discord.Interaction):
        if not self._is_staff(inter.user):
            await inter.response.send_message("Permission denied.", ephemeral=True)
            return
        recs = await self.db.get_all_activity()
        today = date.today()
        rows: List[str] = []
        for uid, rec in sorted(
            recs.items(),
            key=lambda it: (today - it[1]["date"]).days,
            reverse=True,
        ):
            days_idle = (today - rec["date"]).days
            if days_idle < WARN_BEFORE_DAYS:
                continue
            mem = inter.guild.get_member(uid)
            if mem:
                rows.append(f"{mem.mention} – **{days_idle}** d idle")
        text = "\n".join(rows) if rows else "No members currently idle ≥4 days."
        await inter.response.send_message(text[:1990], ephemeral=True)

    @activity_group.command(name="exempt", description="Manage exempt users (ADMIN ONLY)")
    @app_commands.describe(action="add/remove/list", user="User to exempt/unexempt")
    async def exempt(
        self, inter: discord.Interaction,
        action: str, user: discord.Member = None
    ):
        if not inter.user.guild_permissions.administrator:
            await inter.response.send_message("Admins only.", ephemeral=True)
            return

        if action == "add" and user:
            await self.db.add_exempt_user(user.id)
            await inter.response.send_message(
                f"{user.mention} exempted from inactivity actions.",
                ephemeral=True)
        elif action == "remove" and user:
            await self.db.remove_exempt_user(user.id)
            await inter.response.send_message(
                f"{user.mention} is no longer exempt.", ephemeral=True)
        elif action == "list":
            ids = await self.db.get_exempt_users()
            guild = inter.guild
            mentions = [
                guild.get_member(uid).mention
                for uid in ids if guild.get_member(uid)
            ]
            msg = (
                "Exempt users:\n" + "\n".join(mentions)
                if mentions else "No one is currently exempt."
            )
            await inter.response.send_message(msg, ephemeral=True)
        else:
            await inter.response.send_message(
                "Usage: `/activity exempt add|remove @user` "
                "or `/activity exempt list`",
                ephemeral=True)

    # ---- inactive set ----
    inactive_group = app_commands.Group(name="inactive", description="Temporarily mark yourself inactive")

    @inactive_group.command(name="set", description="Set your inactive status")
    @app_commands.choices(period=[app_commands.Choice(name=n, value=d)
                                  for n, d in PERIOD_CHOICES])
    @app_commands.describe(reason="Reason for inactivity")
    async def set_inactive(
        self, inter: discord.Interaction,
        period: app_commands.Choice[int], reason: str
    ):
        await inter.response.defer(ephemeral=True)
        guild = inter.guild
        mem   = inter.user if isinstance(inter.user, discord.Member) else None
        role  = guild.get_role(INACTIVE_ROLE_ID) if guild else None
        if not all((guild, mem, role)):
            await inter.followup.send("Inactive system not configured.",
                                      ephemeral=True)
            return

        until_ts = int(datetime.now(timezone.utc).timestamp()) + period.value * 86400
        try:
            await mem.add_roles(role, reason=f"Inactive – {reason}")
        except Exception as e:
            tb = traceback.format_exc()
            await inter.followup.send(f"Missing permission to add the role.\n{tb}",
                                      ephemeral=True)
            return

        await self.db.add_inactive(mem.id, until_ts)
        await mem.send(
            f"ellular You are marked inactive for **{period.name}**.\n"
            f"Reason: {reason}\nUntil <t:{until_ts}:R>."
        )
        await self._audit(mem.id, "set_inactive", f"{period.name}: {reason}")
        await self._log(f"ellular {mem} set inactive for {period.name} ({reason})")
        await inter.followup.send("You are now marked inactive – take care!",
                                  ephemeral=True)

    # -----------------------------------------------------------------

    def cog_unload(self):
        if self._maintenance_task and not self._maintenance_task.done():
            self._maintenance_task.cancel()
        if hasattr(self, 'maintenance') and self.maintenance.is_running():
            self.maintenance.cancel()
        log.info("ActivityCog unloaded")


async def setup(bot, db):
    await bot.add_cog(ActivityCog(bot, db))
    log.debug("ActivityCog added")