# cogs/member_forms.py
# ───────────────────────────────────────────────────────────────
#   Member registration workflow + reviewer commands
# ───────────────────────────────────────────────────────────────
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from typing import Dict, Optional

import httpx
import discord
from discord import app_commands
from discord.ext import commands

# ───────────────────────── log setup ──────────────────────────
log = logging.getLogger("cog.member_forms")

# ═══════════════════ ENV / CONFIG ═════════════════════════════
try:
    # Optional – load .env if python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass


def req(name: str) -> str:
    """Get a required env-var or raise & log clearly."""
    value = os.getenv(name)
    if not value:
        log.error("[member_forms] REQUIRED env variable %s missing - cog disabled", name)
        raise RuntimeError(f"Missing env {name}")
    return value


STEAM_API_KEY = req("STEAM_API_KEY")
GUILD_ID = int(req("GUILD_ID"))

# The rest are optional – fall back to literal IDs if not found
MEMBER_FORM_CH = int(os.getenv("MEMBER_FORM_CH", "1378118620873494548"))
UNCOMPLETED_APP_ROLE_ID = int(os.getenv("UNCOMPLETED_APP_ROLE_ID", "1390143545066917931"))
COMPLETED_APP_ROLE_ID = int(os.getenv("COMPLETED_APP_ROLE_ID", "1398708167525011568"))
ACCEPT_ROLE_ID = int(os.getenv("ACCEPT_ROLE_ID", "1377075930144571452"))

ROLE_PREFIXES = {
    "PvP": "[P]",
    "Farming": "[F]",
    "Electricity": "[E]",
    "Building": "[B]",
    "Base Sorting": "[BB]",
}

REGION_ROLE_IDS = {
    "North America": 1411364406096433212,
    "Europe": 1411364744484491287,
    "Asia": 1411364982117105684,
    "Other": 1411365034440921260,
}
FOCUS_ROLE_IDS = {
    "Farming": 1379918816871448686,
    "Base Sorting": 1400849292524130405,
    "Building": 1380233086544908428,
    "Electricity": 1380233234675400875,
    "PvP": 1408687710159245362,
}
TEMP_BAN_SECONDS = 7 * 24 * 60 * 60
# ══════════════════════════════════════════════════════════════

# ──────────────────────────── STEAM API ────────────────────────────
BASE_URL = "https://api.steampowered.com"


async def _steam_get(client: httpx.AsyncClient, endpoint: str, params: dict) -> dict:
    """Call Steam Web API endpoint with given params. Never raise; return {} on error.
    The API key is passed as a param and never logged.
    """
    try:
        r = await client.get(f"{BASE_URL}/{endpoint}", params=params, timeout=6.0)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        # Avoid logging the key; show endpoint only
        if status == 403:
            log.warning("Steam request 403 Forbidden on %s", endpoint)
        else:
            log.warning("Steam request failed (%s) on %s: %s", status, endpoint, exc)
        return {}
    except Exception as exc:
        log.warning("Steam request error on %s: %s", endpoint, exc)
        return {}


async def extract_steam_id(url: str) -> Optional[str]:
    """Extract a 64-bit SteamID from a profile URL. Resolve vanity if needed."""
    url = url.strip().lower()

    # /profiles/<64-bit>
    if (m := re.search(r"steamcommunity\.com/profiles/(\d{17})", url)):
        return m.group(1)

    # /id/<vanity>  → ResolveVanityURL
    if (m := re.search(r"steamcommunity\.com/id/([\w\-]+)", url)):
        vanity = m.group(1)
        async with httpx.AsyncClient() as client:
            data = await _steam_get(
                client,
                "ISteamUser/ResolveVanityURL/v1/",
                {"key": STEAM_API_KEY, "vanityurl": vanity},
            )
        return data.get("response", {}).get("steamid")

    return None


async def is_steam_profile_valid(steam_id: str) -> bool:
    """
    Requirements for a *valid* profile:
      • profile visibility  == public (3)
      • at least 1 owned game  AND  at least 1 h (≥60 min) on ANY game
      • friends list not empty
    """
    async with httpx.AsyncClient() as client:
        summary, games, friends = await asyncio.gather(
            _steam_get(
                client,
                "ISteamUser/GetPlayerSummaries/v2/",
                {"key": STEAM_API_KEY, "steamids": steam_id},
            ),
            _steam_get(
                client,
                "IPlayerService/GetOwnedGames/v1/",
                {"key": STEAM_API_KEY, "steamid": steam_id, "include_appinfo": 0},
            ),
            _steam_get(
                client,
                "ISteamUser/GetFriendList/v1/",
                {"key": STEAM_API_KEY, "steamid": steam_id},
            ),
        )

    # 1) profile visibility
    player = (summary.get("response", {}).get("players") or [{}])[0]
    if player.get("communityvisibilitystate") != 3:
        return False

    # 2) games + ≥1 h play-time
    g_resp = games.get("response", {})
    if g_resp.get("game_count", 0) == 0:
        return False

    games_list = g_resp.get("games", [])
    has_1h = any((g.get("playtime_forever") or 0) >= 60 for g in games_list)
    if not has_1h:
        return False

    # 3) friends not empty
    if not friends.get("friendslist", {}).get("friends"):
        return False

    return True


async def get_steam_username(steam_id: str) -> Optional[str]:
    async with httpx.AsyncClient() as client:
        data = await _steam_get(
            client,
            "ISteamUser/GetPlayerSummaries/v2/",
            {"key": STEAM_API_KEY, "steamids": steam_id},
        )
    player = (data.get("response", {}).get("players") or [{}])[0]
    return player.get("personaname")


def _opts(*lbl: str) -> list[discord.SelectOption]:
    return [discord.SelectOption(label=l, value=l) for l in lbl]


async def safe_fetch(guild: discord.Guild, uid: int) -> Optional[discord.Member]:
    try:
        return await guild.fetch_member(uid)
    except (discord.NotFound, discord.HTTPException):
        return None


# ═══════════════════ MAIN COG ═══════════════════
class MemberFormCog(commands.Cog):
    """Member registration workflow + reviewer helper commands."""

    def __init__(self, bot: commands.Bot, db):
        self.bot, self.db = bot, db
        self._ready_once = False

    # ───────────────────────── on_ready ─────────────────────────
    @commands.Cog.listener()
    async def on_ready(self):
        if self._ready_once:
            return
        self._ready_once = True
        await self._restore_action_views()
        log.info("[member_forms] persistent ActionViews reattached")

    async def _restore_action_views(self):
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return

        chan = guild.get_channel(MEMBER_FORM_CH)
        if not isinstance(chan, discord.TextChannel):
            return

        for row in await self.db.get_pending_member_forms():
            try:
                await chan.fetch_message(row["message_id"])
            except discord.NotFound:
                continue  # message gone

            region = row.get("region")
            focus = row.get("focus")

            if not region or not focus:
                raw = row.get("data") or {}
                if isinstance(raw, str):
                    with contextlib.suppress(json.JSONDecodeError):
                        raw = json.loads(raw)
                region = region or raw.get("region")
                focus = focus or raw.get("focus")

            if not region or not focus:
                continue

            self.bot.add_view(
                ActionView(guild, row["user_id"], region, focus, self.db),
                message_id=row["message_id"],
            )

    # ───────────────────────── reviewer cmds ────────────────────
    @app_commands.command(name="addreviewer", description="Add a reviewer")
    async def add_reviewer(self, i: discord.Interaction, member: discord.Member):
        if not i.user.guild_permissions.administrator:
            return await i.response.send_message("No permission.", ephemeral=True)
        await self.db.add_reviewer(member.id)
        await i.response.send_message("Added.", ephemeral=True)

    @app_commands.command(name="removereviewer", description="Remove a reviewer")
    async def remove_reviewer(self, i: discord.Interaction, member: discord.Member):
        if not i.user.guild_permissions.administrator:
            return await i.response.send_message("No permission.", ephemeral=True)
        await self.db.remove_reviewer(member.id)
        await i.response.send_message("Removed.", ephemeral=True)

    @app_commands.command(name="reviewers", description="List reviewers")
    async def list_reviewers(self, i: discord.Interaction):
        reviewers = await self.db.get_reviewers()
        txt = ", ".join(f"<@{u}>" for u in reviewers) or "None."
        await i.response.send_message(txt, ephemeral=True)

    # /memberform entry-point
    @app_commands.command(name="memberform", description="Start member registration")
    async def memberform(self, i: discord.Interaction):
        await i.response.send_message(
            "Click below to begin registration:",
            view=MemberRegistrationView(self.db),
            ephemeral=True,
        )


# ═══════════════════  REGISTRATION UI  ═══════════════════
class MemberRegistrationView(discord.ui.View):
    """Five dropdowns → then Submit button."""

    def __init__(self, db):
        super().__init__(timeout=300)
        self.db = db
        self.data: Dict[str, str] = {}
        self.user: Optional[discord.User] = None
        self.start_msg: Optional[discord.Message] = None
        self.submit_msg: Optional[discord.Message] = None
        self.submit_sent = False

    # first button
    @discord.ui.button(label="Start Registration", style=discord.ButtonStyle.primary)
    async def start(self, i: discord.Interaction, _):
        self.user = i.user
        self.clear_items()
        self.add_item(SelectAge(self))
        self.add_item(SelectRegion(self))
        self.add_item(SelectBans(self))
        self.add_item(SelectFocus(self))
        self.add_item(SelectSkill(self))
        await i.response.edit_message(
            content="Fill each dropdown – **Submit** appears when all done.",
            view=self,
        )
        self.start_msg = await i.original_response()


# ---------- generic dropdown base ----------
class _BaseSelect(discord.ui.Select):
    def __init__(self, v: MemberRegistrationView, key: str, **kw):
        self.v, self.key = v, key
        super().__init__(**kw)

    async def callback(self, i: discord.Interaction):
        self.v.data[self.key] = self.values[0]
        self.placeholder = self.values[0]
        await i.response.edit_message(view=self.v)
        if (
            not self.v.submit_sent
            and all(k in self.v.data for k in ("age", "region", "bans", "focus", "skill"))
        ):
            self.v.submit_sent = True
            self.v.submit_msg = await i.followup.send(
                "All set – click **Submit**:",
                view=SubmitView(self.v),
                ephemeral=True,
                wait=True,
            )


# ---------- concrete dropdowns ----------
class SelectAge(_BaseSelect):
    def __init__(self, v):
        super().__init__(v, "age", placeholder="Age", options=_opts("12-14", "15-17", "18-21", "21+"))


class SelectRegion(_BaseSelect):
    def __init__(self, v):
        super().__init__(v, "region", placeholder="Region", options=_opts("North America", "Europe", "Asia", "Other"))


class SelectBans(_BaseSelect):
    def __init__(self, v):
        super().__init__(v, "bans", placeholder="Any bans?", options=_opts("Yes", "No"))


class SelectFocus(_BaseSelect):
    def __init__(self, v):
        super().__init__(
            v,
            "focus",
            placeholder="Main focus",
            options=_opts("PvP", "Farming", "Base Sorting", "Building", "Electricity"),
        )


class SelectSkill(_BaseSelect):
    def __init__(self, v):
        super().__init__(v, "skill", placeholder="Skill level", options=_opts("Beginner", "Intermediate", "Advanced", "Expert"))


# ---------- submit helper view ----------
class SubmitView(discord.ui.View):
    def __init__(self, v: MemberRegistrationView):
        super().__init__(timeout=300)
        self.v = v

    @discord.ui.button(label="Submit", style=discord.ButtonStyle.success)
    async def submit(self, i: discord.Interaction, _):
        await i.response.send_modal(FinalRegistrationModal(self.v))


# ═══════════════════  FINAL MODAL  ═══════════════════
class FinalRegistrationModal(discord.ui.Modal):
    def __init__(self, v: MemberRegistrationView):
        self.v = v
        needs_ban = v.data.get("bans") == "Yes"
        super().__init__(title="More Details" if needs_ban else "Additional Info")

        self.steam = discord.ui.TextInput(label="Steam Profile Link", placeholder="https://steamcommunity.com/…")
        self.hours = discord.ui.TextInput(label="Hours in Rust")
        self.heard = discord.ui.TextInput(label="Where did you hear about us?")

        self.ban_expl = self.gender = self.referral = None
        if needs_ban:
            self.ban_expl = discord.ui.TextInput(label="Ban Explanation", style=discord.TextStyle.paragraph)
            self.referral = discord.ui.TextInput(label="Referral (optional)", required=False)
            comps = (self.steam, self.hours, self.heard, self.ban_expl, self.referral)
        else:
            self.referral = discord.ui.TextInput(label="Referral (optional)", required=False)
            self.gender = discord.ui.TextInput(label="Gender (optional)", required=False)
            comps = (self.steam, self.hours, self.heard, self.referral, self.gender)
        for c in comps:
            self.add_item(c)

    # ------------------------------------------------------------
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        d = self.v.data
        user = self.v.user or interaction.user
        link = self.steam.value.strip()

        steam_id = await extract_steam_id(link)
        if not steam_id:
            return await interaction.followup.send(
                "⚠️ I couldn't read that Steam link – make sure it's a full profile URL "
                "(`/profiles/…` or `/id/…`).", ephemeral=True
            )

        if not await is_steam_profile_valid(steam_id):
            return await interaction.followup.send(
                "⚠️ Your Steam profile must be **fully public** and show at least "
                "one game, at least one friend, and **at least one hour of play-time**.\n"
                "Please adjust your privacy settings and try again.",
                ephemeral=True,
    )

        # ───── Build reviewer embed ─────
        e = (
            discord.Embed(
                title="📋 NEW MEMBER REGISTRATION",
                colour=discord.Color.gold(),
                timestamp=interaction.created_at,
            )
            .set_author(name=str(user), icon_url=user.display_avatar.url)
        )
        e.add_field(name="👤 User",   value=user.mention, inline=False)
        e.add_field(name="🔗 Steam",  value=link,         inline=False)
        e.add_field(name="🗓️ Age",   value=d["age"],     inline=True)
        e.add_field(name="🌍 Region", value=d["region"],  inline=True)
        e.add_field(name="🚫 Bans",   value=d["bans"],    inline=True)
        if d["bans"] == "Yes" and self.ban_expl:
            e.add_field(name="📝 Ban Explanation", value=self.ban_expl.value, inline=False)
        e.add_field(name="🎯 Focus",  value=d["focus"],   inline=True)
        e.add_field(name="⭐ Skill",  value=d["skill"],   inline=True)
        e.add_field(name="⏱️ Hours", value=self.hours.value, inline=True)
        e.add_field(name="📢 Heard about us", value=self.heard.value, inline=False)
        e.add_field(name="🤝 Referral", value=self.referral.value if self.referral else "N/A", inline=True)
        if self.gender:
            e.add_field(name="⚧️ Gender", value=self.gender.value or "N/A", inline=True)

        review_ch: discord.TextChannel = interaction.client.get_channel(MEMBER_FORM_CH)  # type: ignore
        msg = await review_ch.send(
            embed=e,
            view=ActionView(interaction.guild, user.id, d["region"], d["focus"], self.v.db),
        )

        # ───── DB save ─────
        await self.v.db.add_member_form(
            user.id,
            {
                **d,
                "steam": link,
                "hours": self.hours.value,
                "heard": self.heard.value,
                "referral": self.referral.value if self.referral else None,
                "gender":   self.gender.value   if self.gender   else None,
                "ban_explanation": self.ban_expl.value if self.ban_expl else None,
            },
            message_id=msg.id,
        )

        # swap roles
        try:
            member = await interaction.guild.fetch_member(user.id)
            unc    = interaction.guild.get_role(UNCOMPLETED_APP_ROLE_ID)
            comp   = interaction.guild.get_role(COMPLETED_APP_ROLE_ID)
            if comp and comp not in member.roles:
                await member.add_roles(comp, reason="Application submitted")
            if unc and unc in member.roles:
                await member.remove_roles(unc, reason="Application submitted")
        except discord.Forbidden:
            pass

        await interaction.followup.send("✅ Registration submitted – thank you!", ephemeral=True)

        # tidy helper messages
        async def tidy():
            await asyncio.sleep(2)
            for m in (self.v.start_msg, self.v.submit_msg):
                if m:
                    with contextlib.suppress(discord.HTTPException):
                        await m.delete()
        asyncio.create_task(tidy())


# ═══════════════════  REVIEWER ActionView  ═══════════════════
class ActionView(discord.ui.View):
    # unchanged __init__
    def __init__(self, guild: discord.Guild, uid: int, region: str, focus: str, db):
        super().__init__(timeout=None)
        self.guild, self.uid, self.region, self.focus, self.db = guild, uid, region, focus, db

    async def _reviewers(self) -> set[int]:
        return await self.db.get_reviewers()

    async def _finish(
        self,
        interaction: discord.Interaction,
        txt: str,
        colour: discord.Colour,
    ):
        """Disable buttons, recolour embed, then send confirmation."""
        emb = interaction.message.embeds[0]
        emb.colour = colour

        for c in self.children:
            c.disabled = True
        await interaction.message.edit(embed=emb, view=self)

        # choose response vs follow-up
        if interaction.response.is_done():
            await interaction.followup.send(txt, ephemeral=True)
        else:
            await interaction.response.send_message(txt, ephemeral=True)

    # ───────────── Accept ─────────────
    @discord.ui.button(
        label="Accept",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="memberform_accept",
    )
    async def accept(self, interaction: discord.Interaction, _):
        # Acknowledge as early as possible
        await interaction.response.defer(ephemeral=True)

        if (
            interaction.user.id not in await self._reviewers()
            and not interaction.user.guild_permissions.manage_roles
        ):
            return await interaction.followup.send("Not authorised.", ephemeral=True)

        mem = await safe_fetch(self.guild, self.uid)
        if not mem:
            return await interaction.followup.send("Member left.", ephemeral=True)

        # ── nickname ───────────────────────────────────────────
        steam_link = next(
            (f.value for f in interaction.message.embeds[0].fields
             if f.name.startswith("🔗")),
            None,
        )
        steam_username = None
        if steam_link:
            if (steam_id := await extract_steam_id(steam_link)):
                steam_username = await get_steam_username(steam_id)

        prefix = ROLE_PREFIXES.get(self.focus, "")
        nick   = f"{prefix} {steam_username or mem.display_name}".strip()[:32]
        with contextlib.suppress(discord.Forbidden):
            await mem.edit(nick=nick)

        # ── roles ──────────────────────────────────────────────
        roles = [
            r for r in (
                self.guild.get_role(ACCEPT_ROLE_ID),
                self.guild.get_role(REGION_ROLE_IDS.get(self.region, 0)),
                self.guild.get_role(FOCUS_ROLE_IDS.get(self.focus, 0)),
            ) if r
        ]
        with contextlib.suppress(discord.Forbidden):
            if roles:
                await mem.add_roles(*roles, reason="Application accepted")

        await self.db.update_member_form_status(interaction.message.id, "accepted")
        await self._finish(interaction, f"{mem.mention} accepted ✅", discord.Color.green())

    # ───────────── Deny ─────────────
    @discord.ui.button(
        label="Deny",
        style=discord.ButtonStyle.danger,
        emoji="⛔",
        custom_id="memberform_deny",
    )
    async def deny(self, interaction: discord.Interaction, _):
        # Defer immediately
        await interaction.response.defer(ephemeral=True)

        # permission check
        if (
            interaction.user.id not in await self._reviewers()
            and not interaction.user.guild_permissions.ban_members
        ):
            return await interaction.followup.send("Not authorised.", ephemeral=True)

        # fetch applicant
        mem = await safe_fetch(self.guild, self.uid)
        if mem:
            with contextlib.suppress(discord.Forbidden):
                await self.guild.ban(
                    mem,
                    reason="Application denied – temp ban",
                    delete_message_seconds=0,
                )

            # schedule un-ban
            async def unban_later():
                await asyncio.sleep(TEMP_BAN_SECONDS)
                with contextlib.suppress(Exception):
                    await self.guild.unban(discord.Object(self.uid))

            asyncio.create_task(unban_later())

        # DB + UI
        await self.db.update_member_form_status(interaction.message.id, "denied")
        await self._finish(
            interaction,
            "Application denied ⛔",
            discord.Color.red(),
        )


# ═══════════════════ setup entry-point ═══════════════════
async def setup(bot: commands.Bot, db):
    await bot.add_cog(MemberFormCog(bot, db))