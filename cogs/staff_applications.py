# cogs/staff_applications.py
# ───────────────────────────────────────────────────────────────
#   Staff application system (separate from member registration)
#   Drop-in for CTFO bot: /staffapply, review, and staff role granting.
# ───────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("cog.staff_applications")

# ═════════════════════ CONFIG (copy from your main) ═════════════════════
GUILD_ID                = 1377035207777194005
STAFF_APPLICATION_CH_ID = 1410649548837093436
ADMIN_ROLE_ID           = 1377103244089622719

STAFF_ROLE_IDS: dict[str, int] = {
    "Group Leader":      1377077466513932338,
    "Player Management": 1377084533706588201,
    "Recruitment":       1410659214959054988,
}

# tuple = (label, style, required)   LABEL **≤ 45 chars**
STAFF_QUESTION_SETS: dict[str, list[tuple[str, discord.TextStyle, bool]]] = {
    "Group Leader": [
        ("What group are you looking to lead?",          discord.TextStyle.short,     True),
        ("Why do you want to be a group leader?",        discord.TextStyle.paragraph, True),
        ("What makes you a good fit for this role?",     discord.TextStyle.paragraph, True),
        ("How many Rust hours do you have?",             discord.TextStyle.short,     True),
        ("How many hours a week are you available?",     discord.TextStyle.short,     True),
        ("When are you most active?",                    discord.TextStyle.short,     True),
        ("What time-zone are you in?",                   discord.TextStyle.short,     True),
        ("How would you rate your in-game skills?",      discord.TextStyle.short,     True),
        ("How old are you?",                             discord.TextStyle.short,     True),
    ],

    "Player Management": [
        ("Why do you want to join player management?",        discord.TextStyle.paragraph, True),
        ("What makes you good for this role?",                discord.TextStyle.paragraph, True),
        ("Describe your leadership skills.",                  discord.TextStyle.paragraph, True),
        ("How would you handle breaking of rules?",           discord.TextStyle.paragraph, True),
        ("How would you handle an unpopular decision?",       discord.TextStyle.paragraph, True),
        ("How would you handle an irritating player?",        discord.TextStyle.paragraph, True),
        ("What would you do if you felt annoyed?",            discord.TextStyle.paragraph, True),
        ("What time-zone are you in?",                        discord.TextStyle.short,     True),
        ("How many hours a week are you active?",             discord.TextStyle.short,     True),
        ("When are you most active?",                         discord.TextStyle.short,     True),
        ("How old are you?",                                  discord.TextStyle.short,     True),
    ],

    "Recruitment": [
        ("Why do you want this role?",                        discord.TextStyle.paragraph, True),
        ("What time-zone are you in?",                        discord.TextStyle.short,     True),
        ("When are you most active?",                         discord.TextStyle.short,     True),
        ("Are you banned from any Rust discords?",            discord.TextStyle.short,     True),
        ("How old are you?",                                  discord.TextStyle.short,     True),
        ("If a rejected user messages you, what do?",         discord.TextStyle.paragraph, True),   # 41 chars
    ],
}
# ════════════════════════════════════════════════════════════════════════


# ────────────────────────── Helper ──────────────────────────
async def safe_fetch(guild: discord.Guild, uid: int) -> Optional[discord.Member]:
    try:
        return await guild.fetch_member(uid)
    except (discord.NotFound, discord.HTTPException):
        return None
    except discord.Forbidden:
        log.debug("Could not fetch member %s – forbidden", uid)
        return None


# ══════════════════════ MAIN COG ══════════════════════
class StaffApplicationsCog(commands.Cog):
    """Handles staff application workflow (/staffapply and review)."""

    _ready_once: bool

    def __init__(self, bot: commands.Bot, db):
        self.bot, self.db = bot, db
        self._ready_once = False

    # ─────────────────── Bot ready ───────────────────
    @commands.Cog.listener()
    async def on_ready(self):
        if self._ready_once:
            return
        self._ready_once = True
        await self._restore_action_views()
        log.info("[staff_applications] persistent ActionViews re-attached")

    async def _restore_action_views(self):
        """Re-attach persistent ActionViews after a restart."""
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return

        rows = await self.db.get_pending_staff_apps()  # [{user_id, role, message_id}]
        for row in rows:
            # Avoid adding the same view twice if on_ready runs again
            if any(
                isinstance(v, StaffApplicationActionView)
                and getattr(v, "message_id", None) == row["message_id"]
                for v in self.bot.persistent_views
            ):
                continue

            self.bot.add_view(
                StaffApplicationActionView(guild, row["user_id"], row["role"], self.db),
                message_id=row["message_id"],
            )

    # ═════════ main slash command ════════════
    @app_commands.command(name="staffapply", description="Apply for a staff position")
    async def staffapply(self, i: discord.Interaction):
        # Prevent duplicate open applications
        # FIXED: Get all pending apps and filter manually
        pending_apps = await self.db.get_pending_staff_apps()
        user_has_pending = any(app["user_id"] == i.user.id for app in pending_apps)
        
        if user_has_pending:
            return await i.response.send_message(
                "You already have a pending staff application.", ephemeral=True
            )

        await i.response.send_message(
            "Select the staff role you'd like to apply for:",
            view=StaffRoleSelectView(self.db),
            ephemeral=True,
        )


# ══════════════════ STAFF APPLICATION UI ══════════════════
class StaffRoleSelectView(discord.ui.View):
    def __init__(self, db):
        super().__init__(timeout=300)
        self.db = db
        self.add_item(StaffRoleSelect(self.db))


class StaffRoleSelect(discord.ui.Select):
    def __init__(self, db):
        super().__init__(
            placeholder="Select the staff role you'd like to apply for…",
            options=[discord.SelectOption(label=r, value=r) for r in STAFF_QUESTION_SETS],
        )
        self.db = db

    async def callback(self, i: discord.Interaction):
        await i.response.send_modal(
            StaffApplicationModal(self.values[0], 0, [], self.db)
        )


class ContinueView(discord.ui.View):
    """Ephemeral view that shows a single **Continue** button."""

    def __init__(
        self,
        role: str,
        next_idx: int,
        collected: list[tuple[str, str]],
        db,
    ):
        super().__init__(timeout=300)
        self.role, self.next_idx, self.collected, self.db = role, next_idx, collected, db
        self.message: Optional[discord.Message] = None  # set after sending

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary, emoji="➡️")
    async def continue_btn(self, i: discord.Interaction, _):
        await i.response.send_modal(
            StaffApplicationModal(
                self.role, self.next_idx, list(self.collected), self.db
            )
        )
        self.disable_all_items()
        await i.message.edit(view=self)
        self.stop()  # prevent double interaction

    async def on_timeout(self):
        if self.is_finished():
            return
        self.disable_all_items()
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass
        self.stop()


class StaffApplicationModal(discord.ui.Modal):
    """One page (up to 5 questions) of the staff application."""

    def __init__(
        self, role: str, idx: int, collected: list[tuple[str, str]], db
    ):
        super().__init__(title=f"{role} Application")
        self.role, self.idx, self.collected, self.db = role, idx, collected, db

        qset = STAFF_QUESTION_SETS[role][idx : idx + 5]
        for q, style, req in qset:
            assert (
                len(q) <= 45
            ), f"Modal label >45 chars (Discord limit): {q!r}"
            self.add_item(
                discord.ui.TextInput(
                    label=q,
                    style=style,
                    required=req,
                    max_length=100 if style is discord.TextStyle.short else 1024,
                )
            )

    async def on_submit(self, i: discord.Interaction):
        # Cache answers from this page
        for comp in self.children:  # type: ignore
            label_txt = getattr(comp, "label", None) or comp._underlying.label
            self.collected.append((label_txt, comp.value))  # type: ignore

        next_idx = self.idx + 5
        if next_idx < len(STAFF_QUESTION_SETS[self.role]):
            view = ContinueView(self.role, next_idx, list(self.collected), self.db)
            await i.response.send_message(
                "Page saved — click **Continue** to answer the next set:",
                view=view,
                ephemeral=True,
            )
            # Store the message on the view for timeout edits
            view.message = await i.original_response()
            return

        # All questions answered: build embed & post to review channel
        review_ch = i.guild.get_channel(STAFF_APPLICATION_CH_ID)
        if not review_ch:
            return await i.response.send_message(
                "Review channel missing.", ephemeral=True
            )

        embed = (
            discord.Embed(
                title=f"{self.role} Application",
                colour=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc),
            )
            .set_author(name=str(i.user), icon_url=i.user.display_avatar.url)
            .set_footer(text=f"User ID: {i.user.id}")
        )

        for idx, (q, a) in enumerate(self.collected, 1):
            # Truncate to embed field limit (1 024)
            val = a or "N/A"
            if len(val) > 1024:
                val = f"{val[:1021]}…"
            embed.add_field(name=f"{idx}. {q}", value=val, inline=False)

        view = StaffApplicationActionView(i.guild, i.user.id, self.role, self.db)
        msg = await review_ch.send(f"<@&{ADMIN_ROLE_ID}>", embed=embed, view=view)

        await self.db.add_staff_app(i.user.id, self.role, msg.id)
        await i.response.send_message(
            "✅ Your staff application was submitted.", ephemeral=True
        )


# ══════════════ STAFF APPLICATION REVIEW (ActionView) ══════════════
class StaffApplicationActionView(discord.ui.View):
    """Persistent view with Accept / Deny buttons for admins."""

    def __init__(self, guild: discord.Guild, applicant_id: int, role: str, db):
        super().__init__(timeout=None)
        self.guild: discord.Guild = guild
        self.applicant_id: int = applicant_id
        self.role: str = role
        self.db = db
        # keep for duplicate-view guard
        self.message_id: Optional[int] = None

    # ────────── helpers ──────────
    async def _authorised(self, member: discord.Member) -> bool:
        return member.guild_permissions.administrator or any(
            r.id in STAFF_ROLE_IDS.values() for r in member.roles
        )

    async def _notify(self, txt: str):
        user = await safe_fetch(self.guild, self.applicant_id)
        if user:
            try:
                await user.send(txt)
            except discord.Forbidden:
                log.debug("Could not DM applicant %s", self.applicant_id)

    async def _finish(self, i: discord.Interaction, colour: discord.Colour):
        emb = i.message.embeds[0]
        emb.colour = colour
        self.disable_all_items()
        await i.message.edit(embed=emb, view=self)
        self.stop()

    # ────────── buttons ──────────
    @discord.ui.button(
        label="Accept",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="staff_app_accept",
    )
    async def accept(self, i: discord.Interaction, _):
        # Acknowledge interaction immediately
        await i.response.defer(ephemeral=True)
        
        if not await self._authorised(i.user):
            return await i.followup.send("Not authorised.", ephemeral=True)

        applicant = await safe_fetch(self.guild, self.applicant_id)
        if not applicant:
            return await i.followup.send("Applicant left.", ephemeral=True)

        role_obj = self.guild.get_role(STAFF_ROLE_IDS[self.role])
        if not role_obj:
            return await i.followup.send("Role missing.", ephemeral=True)

        try:
            await applicant.add_roles(role_obj, reason="Staff application accepted")
        except discord.Forbidden:
            await self.db.update_staff_app_status(i.message.id, "error")
            return await i.followup.send(
                "Cannot add role – my role is lower than the target role.",
                ephemeral=True,
            )

        await self.db.update_staff_app_status(i.message.id, "accepted")
        await i.followup.send(f"{applicant.mention} accepted ✅", ephemeral=True)
        await self._finish(i, discord.Color.green())
        await self._notify(f"🎉 You have been **accepted** as **{self.role}**!")

    @discord.ui.button(
        label="Deny",
        style=discord.ButtonStyle.danger,
        emoji="⛔",
        custom_id="staff_app_deny",
    )
    async def deny(self, i: discord.Interaction, _):
        # Acknowledge interaction immediately
        await i.response.defer(ephemeral=True)
        
        if not await self._authorised(i.user):
            return await i.followup.send("Not authorised.", ephemeral=True)

        await self.db.update_staff_app_status(i.message.id, "denied")
        await i.followup.send("Application denied ⛔", ephemeral=True)
        await self._finish(i, discord.Color.red())
        await self._notify(f"❌ Your application for **{self.role}** was **denied**.")


# ═════════════ setup entry-point ═════════════
async def setup(bot: commands.Bot, db):
    await bot.add_cog(StaffApplicationsCog(bot, db))