# cogs/stats.py
#
# Commands
#   /check player <steam-id-or-url>   – ban & profile check
#   /stats rust  <steam-id-or-url>    – Rust hours + detailed in-game stats
#
# Requires env vars
#   STEAM_API_KEY       (Steam Web-API key)         – required
#   BATTLEMETRICS_TOKEN (BM token, raises limits)   – optional
#
# ────────────────────────────────────────────────────────────────

from __future__ import annotations
import os, re, aiohttp, datetime as dt, discord
from discord.ext import commands
from discord import app_commands

STEAM_API_KEY  = os.getenv("STEAM_API_KEY")
BM_TOKEN       = os.getenv("BATTLEMETRICS_TOKEN", "")
BM_HEADERS     = {"Authorization": f"Bearer {BM_TOKEN}"} if BM_TOKEN else {}

APPID_RUST = 252490
PROFILE_RE = re.compile(r"https?://steamcommunity\.com/(?:profiles|id)/([^/]+)")


# ═══════════════════════════════════════════════════════════════
class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ─────────────── /check group ───────────────
    check = app_commands.Group(name="check", description="Look-ups & checks")

    @check.command(name="player", description="Steam ban & profile check")
    @app_commands.describe(steamid="SteamID64 or profile URL")
    async def check_player(self, inter: discord.Interaction, steamid: str):
        await inter.response.defer(ephemeral=True)
        sid = await self._resolve(steamid)
        if sid is None:
            return await inter.followup.send("Unable to resolve SteamID.", ephemeral=True)

        bans, profile                          = await self._steam_bans_and_profile(sid)
        bm_prof, bm_bans, eac, name_history    = await self._bm_info(sid)

        danger = (
            bans["VACBanned"] or bans["CommunityBanned"] or bans["NumberOfGameBans"]
            or bans["EconomyBan"] != "none" or eac or bm_bans
        )
        colour = discord.Color.red() if danger else discord.Color.green()

        e = (
            discord.Embed(
                title=f"Player check – {profile.get('personaname','Unknown')}",
                url=profile.get("profileurl") or discord.Embed.Empty,
                colour=colour,
            )
            .set_footer(text=f"SteamID64: {sid}")
        )
        if (av := profile.get("avatarfull")):
            e.set_thumbnail(url=av)

        # account info
        if (ts := profile.get("timecreated")):
            e.add_field(
                name="Account created",
                value=dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d"),
                inline=True,
            )
        if (cc := profile.get("loccountrycode")):
            flag = chr(0x1F1E6 + ord(cc[0]) - 65) + chr(0x1F1E6 + ord(cc[1]) - 65)
            e.add_field(name="Country", value=f"{flag} {cc}", inline=True)

        # bans
        e.add_field(
            name="VAC Ban",
            value=f"{'Yes' if bans['VACBanned'] else 'No'} ({bans['NumberOfVACBans']})",
            inline=True,
        )
        e.add_field(name="Game Bans", value=bans["NumberOfGameBans"], inline=True)
        e.add_field(
            name="Community Ban",
            value="Yes" if bans["CommunityBanned"] else "No",
            inline=True,
        )
        e.add_field(name="Trade Ban", value=bans["EconomyBan"].capitalize(), inline=True)
        e.add_field(name="Days Since Last Ban", value=bans["DaysSinceLastBan"], inline=True)

        # BM / EAC
        if eac is not None:
            e.add_field(name="EAC Ban", value="Yes" if eac else "No", inline=True)
        if bm_prof:
            url = f"https://www.battlemetrics.com/rcon/players/{bm_prof['id']}"
            e.add_field(name="BattleMetrics", value=f"[Profile]({url})", inline=True)

        if bm_bans:
            lines = []
            for b in bm_bans[:5]:
                org  = b["attributes"].get("organization", {}).get("name") or "Org"
                reas = b["attributes"].get("reason") or "No reason"
                exp  = b["attributes"].get("expires") or "Permanent"
                lines.append(f"• **{org}** – {reas} (exp: {exp[:10]})")
            if len(bm_bans) > 5:
                lines.append(f"…and {len(bm_bans) - 5} more.")
            e.add_field(name=f"BM bans ({len(bm_bans)})", value="\n".join(lines), inline=False)

        if name_history:
            e.add_field(name="Previous names", value="\n".join(name_history[:10]), inline=False)

        await inter.followup.send(embed=e, ephemeral=True)

    # ─────────────── /stats group ───────────────
    stats = app_commands.Group(name="stats", description="Game statistics")

    @stats.command(name="rust", description="Rust hours & detailed stats")
    @app_commands.describe(steamid="SteamID64 or profile URL")
    async def rust_stats(self, inter: discord.Interaction, steamid: str):
        await inter.response.defer(ephemeral=True)
        sid = await self._resolve(steamid)
        if sid is None:
            return await inter.followup.send("Unable to resolve SteamID.", ephemeral=True)

        # play-time + persona
        total_h, two_w_h, last_play, profile = await self._playtime_and_persona(sid)

        # achievements
        unlocked, total_ach, ach_pct = await self._achievements(sid)

        # in-game stats
        stats_ok, st = await self._rust_stats(sid)

        e = (
            discord.Embed(
                title=f"Rust stats – {profile.get('personaname','Unknown')}",
                url=profile.get("profileurl") or discord.Embed.Empty,
                colour=discord.Color.orange(),
            )
            .set_footer(text=f"SteamID64: {sid}")
        )
        if (av := profile.get("avatarfull")):
            e.set_thumbnail(url=av)

        e.add_field(name="Total hours",  value=f"{total_h:,}", inline=True)
        e.add_field(name="Last 2 weeks", value=f"{two_w_h:,}", inline=True)
        e.add_field(name="Last played",  value=last_play,      inline=True)
        e.add_field(
            name="Achievements",
            value=f"{unlocked}/{total_ach} ({ach_pct})",
            inline=True,
        )

        if not stats_ok:
            e.add_field(
                name="Detailed stats",
                value="Private / not available.",
                inline=False,
            )
            return await inter.followup.send(embed=e, ephemeral=True)

        # ───── PvP block (safe divide) ─────
        kills   = st.get("kill_player",   0)
        deaths  = st.get("death_player",  0)
        kd_val  = f"{kills / deaths:.2f}" if deaths else ("∞" if kills else "0")
        bullets_fired = st.get("shots_fired", 0)
        bullets_hit   = st.get("shots_hit",   0)
        headshots     = st.get("headshot_hits", 0)
        accuracy      = f"{(bullets_hit / bullets_fired * 100):.1f}%" if bullets_fired else "0%"
        hs_accuracy   = f"{(headshots  / bullets_hit * 100):.1f}%"     if bullets_hit   else "0%"

        e.add_field(
            name="PvP",
            value=(
                f"Kills **{kills:,}** / Deaths **{deaths:,}** "
                f"(K/D {kd_val})\n"
                f"Bullets **{bullets_hit:,}/{bullets_fired:,}** ({accuracy})\n"
                f"Head-shot accuracy {hs_accuracy}"
            ),
            inline=False,
        )

        # kill counts
        e.add_field(
            name="Kills",
            value=(
                f"Players **{kills:,}**\n"
                f"Scientists **{st.get('kill_scientist',0):,}**\n"
                f"Bears **{st.get('kill_bear',0):,}** | Wolves **{st.get('kill_wolf',0):,}**\n"
                f"Boars **{st.get('kill_boar',0):,}**  | Deer **{st.get('kill_deer',0):,}**\n"
                f"Horses **{st.get('kill_horse',0):,}**"
            ),
            inline=True,
        )

        # bow stats
        arrows_fired = st.get("arrow_fired", 0)
        arrows_hit   = st.get("arrow_hit",   0)
        arrow_acc    = f"{(arrows_hit / arrows_fired * 100):.1f}%" if arrows_fired else "0%"

        e.add_field(
            name="Bow",
            value=(
                f"Arrows **{arrows_hit:,}/{arrows_fired:,}**\n"
                f"Accuracy {arrow_acc}"
            ),
            inline=True,
        )

        # death reasons
        e.add_field(
            name="Deaths",
            value=(
                f"By players **{deaths:,}**\n"
                f"Suicides **{st.get('death_suicide',0):,}**\n"
                f"Falling **{st.get('death_fall',0):,}**"
            ),
            inline=True,
        )

        # resources
        e.add_field(
            name="Resources gathered",
            value=(
                f"Wood **{st.get('harvest_wood',0):,}**\n"
                f"Stone **{st.get('harvest_stones',0):,}**\n"
                f"Metal **{st.get('harvest_metal_ore',0):,}**\n"
                f"HQ Metal **{st.get('harvest_hq_metal_ore',0):,}**\n"
                f"Sulfur **{st.get('harvest_sulfur_ore',0):,}**"
            ),
            inline=False,
        )

        await inter.followup.send(embed=e, ephemeral=True)

    # ───────────────── helper methods ─────────────────
    async def _resolve(self, raw: str) -> str | None:
        if raw.isdigit() and len(raw) >= 16:
            return raw
        m = PROFILE_RE.search(raw)
        if not m:
            return None
        vanity = m.group(1)
        if vanity.isdigit():
            return vanity
        url = (
            "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
            f"?key={STEAM_API_KEY}&vanityurl={vanity}"
        )
        async with aiohttp.ClientSession() as s, s.get(url) as r:
            data = await r.json()
        return data["response"].get("steamid")

    async def _steam_bans_and_profile(self, sid: str):
        async with aiohttp.ClientSession() as ses:
            url_b = (
                "https://api.steampowered.com/ISteamUser/GetPlayerBans/v1/"
                f"?key={STEAM_API_KEY}&steamids={sid}"
            )
            url_p = (
                "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
                f"?key={STEAM_API_KEY}&steamids={sid}"
            )
            async with ses.get(url_b) as r1, ses.get(url_p) as r2:
                bans = (await r1.json())["players"][0]
                prof = (await r2.json())["response"]["players"][0]
        return bans, prof

    async def _bm_info(self, sid: str):
        if not sid.isdigit():
            return None, [], None, []
        async with aiohttp.ClientSession() as ses:
            url = f"https://api.battlemetrics.com/players?filter[search]={sid}"
            async with ses.get(url, headers=BM_HEADERS) as r:
                data = await r.json()
        if not data.get("data"):
            return None, [], None, []
        prof = data["data"][0]
        pid  = prof["id"]
        async with aiohttp.ClientSession() as ses:
            url = f"https://api.battlemetrics.com/bans?filter[player]={pid}&sort=-timestamp"
            async with ses.get(url, headers=BM_HEADERS) as r:
                bans = (await r.json()).get("data", [])
        flags = prof["attributes"].get("flags", [])
        eac   = any("eac" in (f or "").lower() for f in flags)
        names = [n.get("name","Unknown") for n in prof["attributes"].get("names", [])[::-1]]
        return prof, bans, eac, names

    async def _playtime_and_persona(self, sid: str):
        async with aiohttp.ClientSession() as ses:
            url = (
                "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
                f"?key={STEAM_API_KEY}&steamid={sid}&include_appinfo=1"
            )
            async with ses.get(url) as r:
                og = await r.json()
        g = next((x for x in og.get("response", {}).get("games", []) if x["appid"] == APPID_RUST), None)
        total_h = g["playtime_forever"] // 60 if g else 0
        two_w_h = g.get("playtime_2weeks", 0) // 60 if g else 0

        async with aiohttp.ClientSession() as ses:
            url = (
                "https://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v1/"
                f"?key={STEAM_API_KEY}&steamid={sid}"
            )
            async with ses.get(url) as r:
                rp = await r.json()
        recent = next((x for x in rp.get("response", {}).get("games", []) if x["appid"] == APPID_RUST), None)
        last_play = (
            dt.datetime.utcfromtimestamp(recent["playtime_at"]).strftime("%Y-%m-%d")
            if recent and "playtime_at" in recent
            else "Unknown"
        )

        async with aiohttp.ClientSession() as ses:
            url = (
                "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
                f"?key={STEAM_API_KEY}&steamids={sid}"
            )
            async with ses.get(url) as r:
                prof = (await r.json())["response"]["players"][0]

        return total_h, two_w_h, last_play, prof

    async def _achievements(self, sid: str):
        async with aiohttp.ClientSession() as ses:
            url = (
                "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v1/"
                f"?key={STEAM_API_KEY}&steamid={sid}&appid={APPID_RUST}"
            )
            async with ses.get(url) as r:
                ach = await r.json()
        if not ach.get("playerstats", {}).get("success"):
            return "Private", "N/A", "N/A"
        lst       = ach["playerstats"]["achievements"]
        unlocked  = sum(1 for a in lst if a["achieved"])
        total     = len(lst)
        pct       = f"{unlocked / total * 100:.1f}%"
        return unlocked, total, pct

    async def _rust_stats(self, sid: str):
        async with aiohttp.ClientSession() as ses:
            url = (
                "https://api.steampowered.com/ISteamUserStats/GetUserStatsForGame/v2/"
                f"?key={STEAM_API_KEY}&steamid={sid}&appid={APPID_RUST}"
            )
            async with ses.get(url) as r:
                data = await r.json()
        if not data.get("playerstats", {}).get("stats"):
            return False, {}
        raw = {s["name"]: s["value"] for s in data["playerstats"]["stats"]}
        aliases = {
            "kill_player":   ["kill_player", "player_kills"],
            "death_player":  ["death_player", "player_deaths"],
            "shots_fired":   ["shots_fired", "bullet_fired"],
            "shots_hit":     ["shots_hit",   "bullet_hit"],
            "headshot_hits":["headshot_hits", "headshot"],
            "arrow_fired":  ["arrow_fired"],
            "arrow_hit":    ["arrow_hit"],
            "kill_scientist":["kill_scientist"],
            "kill_bear":    ["kill_bear"],
            "kill_wolf":    ["kill_wolf"],
            "kill_boar":    ["kill_boar"],
            "kill_deer":    ["kill_deer"],
            "kill_horse":   ["kill_horse"],
            "death_suicide":["death_suicide"],
            "death_fall":   ["death_fall"],
            "harvest_wood": ["harvest_wood"],
            "harvest_stones":["harvest_stones"],
            "harvest_metal_ore":      ["harvest_metal_ore"],
            "harvest_hq_metal_ore":   ["harvest_hq_metal_ore"],
            "harvest_sulfur_ore":     ["harvest_sulfur_ore"],
        }
        fixed = {canon: next((raw[k] for k in keys if k in raw), 0)
                 for canon, keys in aliases.items()}
        return True, fixed


# ═════════════════════ setup entry point ═════════════════════
async def setup(bot: commands.Bot, db=None):
    await bot.add_cog(StatsCog(bot))