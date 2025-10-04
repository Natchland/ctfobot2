# cogs/feedback.py
# =============================================================
# /feedback → embed to staff channel + private per-case channel
# Category auto-created if missing.  ✅ Resolved deletes channel.
# =============================================================
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, cast

import discord
from discord import app_commands
from discord.ext import commands

# ─────────────── CONFIG ──────────────────────────────────────
FEEDBACK_CH_ID   = 1413188006499586158      # staff feedback channel
THREAD_CAT_NAME  = "Feedback Threads"       # created/used automatically
ANON_RATE        = timedelta(hours=24)      # 1 anonymous post / 24 h

CAT_COMPLAINT = "Staff / Member complaint"
CAT_DISCORD   = "Discord issue"
CAT_OTHER     = "Other"
# ─────────────────────────────────────────────────────────────


# ═══ helper – ensure category exists ═════════════════════════
async def ensure_case_category(
    guild: discord.Guild, staff_tpl: discord.TextChannel
) -> discord.CategoryChannel:
    for cat in guild.categories:
        if cat.name.lower() == THREAD_CAT_NAME.lower():
            return cat

    overwrites = {k: v for k, v in staff_tpl.overwrites.items()}
    overwrites.setdefault(
        guild.default_role, discord.PermissionOverwrite(view_channel=False)
    )

    return await guild.create_category(
        name=THREAD_CAT_NAME,
        overwrites=overwrites,
        reason="Initial feedback case category",
    )


# ═══ Modal (body only) ═══════════════════════════════════════
class BodyModal(discord.ui.Modal):
    def __init__(
        self,
        cog: "FeedbackCog",
        *,
        category_label: str,
        target: Optional[discord.Member],
        anonymous: bool,
    ):
        super().__init__(title="Describe your feedback")
        self.cog, self.cat, self.target, self.anon = (
            cog, category_label, target, anonymous
        )
        self.body = discord.ui.TextInput(
            label="Details (max 2000 chars)",
            style=discord.TextStyle.paragraph,
            max_length=2000,
        )
        self.add_item(self.body)

    async def on_submit(self, inter: discord.Interaction):
        await self.cog._finalise_feedback(
            inter,
            category_label=self.cat,
            target=self.target,
            anonymous=self.anon,
            text=self.body.value,
        )


# ═══ helper – create per-case channel ════════════════════════
async def create_case_channel(
    guild: discord.Guild,
    fid: int,
    *,
    staff_tpl: discord.TextChannel,
) -> discord.TextChannel:
    category = await ensure_case_category(guild, staff_tpl)

    overwrites = {k: v for k, v in staff_tpl.overwrites.items()}
    overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)

    return await guild.create_text_channel(
        name=f"feedback-{fid}",
        category=category,
        overwrites=overwrites,
        topic=f"Private discussion for feedback #{fid}",
        reason="New feedback received",
    )


# ═══ Staff triage / Contact view ═════════════════════════════
class TriageView(discord.ui.View):
    def __init__(self, db, fid: int, author_id: int, case_chan_id: int):
        super().__init__(timeout=None)
        self.db, self.fid, self.author_id, self.case_chan_id = (
            db, fid, author_id, case_chan_id
        )
        if author_id == 0:
            self.contact.disabled = True  # anonymous

    async def _set_status(self, inter, status, colour):
        if not (
            inter.user.guild_permissions.manage_messages
            or inter.user.guild_permissions.administrator
        ):
            return await inter.response.send_message("No permission.", ephemeral=True)

        emb = inter.message.embeds[0]
        emb.colour = colour
        emb.set_footer(
            text=f"Status: {status} • by {inter.user}",
            icon_url=inter.user.display_avatar.url,
        )
        await inter.message.edit(embed=emb, view=self)
        await self.db.update_feedback_status(self.fid, status)

        # Auto-delete when resolved
        if status == "Resolved":
            chan = inter.guild and inter.guild.get_channel(self.case_chan_id)
            if isinstance(chan, discord.TextChannel):
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await chan.delete(reason="Feedback resolved")

        await inter.response.send_message("Status updated.", ephemeral=True)

    @discord.ui.button(label="Ack", style=discord.ButtonStyle.gray, emoji="👀")
    async def ack(self, i, _): await self._set_status(i, "Ack", discord.Color.yellow())

    @discord.ui.button(label="WIP", style=discord.ButtonStyle.blurple, emoji="🔧")
    async def wip(self, i, _): await self._set_status(i, "WIP", discord.Color.blue())

    @discord.ui.button(label="Resolved", style=discord.ButtonStyle.green, emoji="✅")
    async def res(self, i, _): await self._set_status(i, "Resolved", discord.Color.green())

    # ---- Contact button -------------------------------------
    @discord.ui.button(label="Contact", style=discord.ButtonStyle.gray, emoji="✉️")
    async def contact(self, inter: discord.Interaction, _):
        if self.author_id == 0:
            return await inter.response.send_message("Author is anonymous.", ephemeral=True)

        guild = inter.guild
        chan  = guild and guild.get_channel(self.case_chan_id)
        if chan is None:
            return await inter.response.send_message("Case channel missing.", ephemeral=True)

        author_obj: discord.abc.Snowflake = (
            guild.get_member(self.author_id)
            or await inter.client.fetch_user(self.author_id)
        )

        try:
            await chan.set_permissions(
                author_obj,
                overwrite=discord.PermissionOverwrite(view_channel=True,
                                                      send_messages=True),
            )
        except discord.Forbidden:
            return await inter.response.send_message(
                "Cannot edit channel permissions.", ephemeral=True
            )

        await chan.send(
            f"{author_obj.mention} – staff member {inter.user.mention} would like "
            "to discuss your feedback. Please reply here."
        )
        await inter.response.send_message(f"Author invited! → {chan.mention}", ephemeral=True)


# ═══════════════════════ Cog ═════════════════════════════════
class FeedbackCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db):
        self.bot, self.db = bot, db
        if not hasattr(bot, "last_anonymous_time"):
            bot.last_anonymous_time = cast(Dict[int, datetime], {})

    # ---------- /feedback command ----------------------------
    @app_commands.command(name="feedback", description="Send feedback to the staff")
    @app_commands.describe(
        category="Select a category",
        target="User you're complaining about (if Complaint)",
        anonymous="Hide your name from staff?",
    )
    @app_commands.choices(
        category=[
            app_commands.Choice(name=CAT_COMPLAINT, value="complaint"),
            app_commands.Choice(name=CAT_DISCORD,   value="discord"),
            app_commands.Choice(name=CAT_OTHER,     value="other"),
        ]
    )
    async def feedback(
        self, inter: discord.Interaction,
        *, category: app_commands.Choice[str],
        anonymous: bool = False,
        target: Optional[discord.Member] = None,
    ):
        if category.value == "complaint" and target is None:
            return await inter.response.send_message(
                "Please choose a target user for complaints.", ephemeral=True
            )

        # anon cooldown
        if anonymous:
            now = datetime.now(timezone.utc)
            last = self.bot.last_anonymous_time.get(inter.user.id) \
                   or await self.db.get_last_anon_ts(inter.user.id)
            if last:
                self.bot.last_anonymous_time[inter.user.id] = last
            if last and now - last < ANON_RATE:
                rem = ANON_RATE - (now - last)
                h, r = divmod(rem.seconds, 3600); m, _ = divmod(r, 60)
                return await inter.response.send_message(
                    f"You can post anonymously again in {rem.days}d {h}h {m}m.",
                    ephemeral=True,
                )

        await inter.response.send_modal(
            BodyModal(self,
                      category_label=category.name,
                      target=target,
                      anonymous=anonymous)
        )

    # ---------- modal callback -------------------------------
    async def _finalise_feedback(
        self, inter: discord.Interaction,
        *, category_label: str, target: Optional[discord.Member],
        anonymous: bool, text: str,
    ):
        colour = discord.Color.light_gray() if anonymous else discord.Color.blue()
        embed = discord.Embed(title=category_label, description=text,
                              colour=colour, timestamp=datetime.now(timezone.utc))
        if target:
            embed.add_field(name="Target", value=target.mention, inline=False)

        if anonymous:
            embed.set_footer(text="Sent anonymously")
            author_id_db = 0
        else:
            embed.set_author(name=str(inter.user),
                             icon_url=inter.user.display_avatar.url)
            author_id_db = inter.user.id

        staff_chan = inter.client.get_channel(FEEDBACK_CH_ID)  # type: ignore
        if not isinstance(staff_chan, discord.TextChannel):
            return await inter.response.send_message(
                "Staff feedback channel missing.", ephemeral=True
            )

        msg = await staff_chan.send(embed=embed)

        fid = await self.db.record_feedback(
            msg_id=msg.id,
            author_id=author_id_db,
            category=category_label,
            target_id=target.id if target else None,
            text=text,
            rating=None,
            attachment_urls=None,
        )

        case_chan = await create_case_channel(
            guild=staff_chan.guild,
            fid=fid,
            staff_tpl=staff_chan,
        )

        await msg.edit(view=TriageView(self.db, fid, author_id_db, case_chan.id))

        if anonymous:
            now = datetime.now(timezone.utc)
            self.bot.last_anonymous_time[inter.user.id] = now
            await self.db.set_last_anon_ts(inter.user.id, now)

        await inter.response.send_message("✅  Thanks for the feedback!", ephemeral=True)

    # ---------- /myfeedback ----------------------------------
    @app_commands.command(name="myfeedback", description="DM your last 25 feedback submissions")
    async def myfeedback(self, inter: discord.Interaction):
        rows = await self.db.list_feedback_by_author(inter.user.id, 25)
        if not rows:
            return await inter.response.send_message(
                "You have no feedback entries.", ephemeral=True
            )

        summary = "\n".join(
            f"- {r['created_at']:%Y-%m-%d} • {r['category']} • {r['status']} (ID {r['id']})"
            for r in rows
        )
        await inter.user.send(summary)
        await inter.response.send_message("📨  Sent to your DMs.", ephemeral=True)


# ─────────────── setup entry point ───────────────────────────
async def setup(bot, db):
    await bot.add_cog(FeedbackCog(bot, db))