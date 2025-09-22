# /check player <steam-profile>
# ──────────────────────────────────────────────────────────────
# • VAC / Game / Community / Trade-ban status
# • Days since last ban
# • EAC-ban flag (via BattleMetrics)
# • BattleMetrics bans list
# • Account-creation date
# • Country code / flag
# • Previous persona names (from BattleMetrics)

from __future__ import annotations
import os, re, aiohttp, datetime as dt, discord
from discord.ext import commands
from discord import app_commands

STEAM_API_KEY        = os.getenv("STEAM_API_KEY")
BATTLEMETRICS_TOKEN  = os.getenv("BATTLEMETRICS_TOKEN", "")

BM_HEADERS = {"Authorization": f"Bearer {BATTLEMETRICS_TOKEN}"} if BATTLEMETRICS_TOKEN else {}

STEAM_PROFILE_RE = re.compile(r"https?://steamcommunity\.com/(?:profiles|id)/([^/]+)")

# ──────────────────────────────────────────────────────────────
class GameStatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # /check group
    check = app_commands.Group(name="check", description="Look-ups & checks")

    # ───────────────  /check player  ───────────────
    @check.command(name="player", description="Steam ban & profile check")
    @app_commands.describe(steamid="SteamID64 or profile URL")
    async def check_player(self, inter: discord.Interaction, steamid: str):
        await inter.response.defer(ephemeral=True)

        sid = await self._resolve_steamid(steamid)
        if sid is None:
            return await inter.followup.send("Unable to resolve SteamID.", ephemeral=True)

        # 1) Steam-ban + profile summary
        bans, summary = await self._steam_info(sid)

        # 2) BattleMetrics bans, EAC flag, name history
        bm_profile, bm_bans, eac_flag, name_history = await self._bm_info(sid)

        # ───── embed ─────
        colour = discord.Color.green()
        if bans and (bans["VACBanned"] or bans["CommunityBanned"]
                     or bans["NumberOfGameBans"] or bans["EconomyBan"] != "none"
                     or eac_flag or bm_bans):
            colour = discord.Color.red()

        embed = discord.Embed(
            title=f"Player check – {summary.get('personaname','Unknown')}",
            url=summary.get("profileurl") or discord.Embed.Empty,
            colour=colour,
        ).set_footer(text=f"SteamID64: {sid}")

        if avatar := summary.get("avatarfull"):
            embed.set_thumbnail(url=avatar)

        # --- account info ---
        if (ts := summary.get("timecreated")):
            created = dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
            embed.add_field(name="Account created", value=created, inline=True)
        if (cc := summary.get("loccountrycode")):
            flag = chr(0x1F1E6 + (ord(cc[0]) - 65)) + chr(0x1F1E6 + (ord(cc[1]) - 65))
            embed.add_field(name="Country", value=f"{flag} {cc}", inline=True)

        # --- Steam bans ---
        if bans:
            embed.add_field(
                name="VAC Ban",
                value=f"{'Yes' if bans['VACBanned'] else 'No'} "
                      f"({bans['NumberOfVACBans']})",
                inline=True)
            embed.add_field(
                name="Game Bans",
                value=str(bans['NumberOfGameBans']),
                inline=True)
            embed.add_field(
                name="Community Ban",
                value="Yes" if bans['CommunityBanned'] else "No",
                inline=True)
            embed.add_field(
                name="Trade Ban",
                value=bans['EconomyBan'].capitalize(),
                inline=True)
            embed.add_field(
                name="Days Since Last Ban",
                value=str(bans['DaysSinceLastBan']),
                inline=True)

        # --- BattleMetrics / EAC ---
        if eac_flag is not None:
            embed.add_field(name="EAC Ban", value="Yes" if eac_flag else "No", inline=True)

        if bm_profile:
            bm_url = f"https://www.battlemetrics.com/rcon/players/{bm_profile['id']}"
            embed.add_field(name="BattleMetrics", value=f"[Profile]({bm_url})", inline=True)

        if bm_bans:
            lines = []
            for ban in bm_bans[:5]:            # show up to 5
                org  = ban.get("attributes", {}).get("organization", {}).get("name") or "Org"
                reas = ban["attributes"].get("reason") or "No reason"
                exp  = ban["attributes"].get("expires") or "Perm"
                lines.append(f"• **{org}** – {reas} (expires: {exp[:10]})")
            more = len(bm_bans) - 5
            if more > 0:
                lines.append(f"…and {more} more.")
            embed.add_field(
                name=f"BattleMetrics bans ({len(bm_bans)})",
                value="\n".join(lines),
                inline=False)

        # --- name history ---
        if name_history:
            txt = "\n".join(name_history[:10])
            embed.add_field(name="Previous names (BM)", value=txt, inline=False)

        await inter.followup.send(embed=embed, ephemeral=True)

    # ──────────────────────────────────────────────
    # helpers
    # ──────────────────────────────────────────────
    async def _resolve_steamid(self, inp: str) -> str | None:
        if inp.isdigit() and len(inp) >= 16:
            return inp

        m = STEAM_PROFILE_RE.search(inp)
        if m:
            vanity = m.group(1)
            if vanity.isdigit():
                return vanity
            # resolve vanity
            url = ("https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
                   f"?key={STEAM_API_KEY}&vanityurl={vanity}")
            async with aiohttp.ClientSession() as ses, ses.get(url) as r:
                data = await r.json()
            if data["response"]["success"] == 1:
                return data["response"]["steamid"]
        return None

    async def _steam_info(self, sid: str):
        async with aiohttp.ClientSession() as ses:
            # bans
            url_b = ("https://api.steampowered.com/ISteamUser/GetPlayerBans/v1/"
                     f"?key={STEAM_API_KEY}&steamids={sid}")
            url_s = ("https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
                     f"?key={STEAM_API_KEY}&steamids={sid}")
            async with ses.get(url_b) as r1, ses.get(url_s) as r2:
                bans   = (await r1.json())["players"][0]
                summ   = (await r2.json())["response"]["players"][0]
        return bans, summ

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

        # bans list
        async with aiohttp.ClientSession() as ses:
            url_b = f"https://api.battlemetrics.com/bans?filter[player]={pid}&sort=-timestamp"
            async with ses.get(url_b, headers=BM_HEADERS) as r:
                bans = (await r.json()).get("data", [])

        # EAC flag & names
        attrs = prof.get("attributes", {})
        flags = attrs.get("flags", [])
        names_attr = attrs.get("names", [])
        eac = any("eac" in (f or "").lower() for f in flags)

        names = [n.get("name", "Unknown") for n in names_attr[::-1]]  # newest last

        return prof, bans, eac, names


# ─── setup entry point ────────────────────────────────────────────────
async def setup(bot: commands.Bot, db=None):
    await bot.add_cog(GameStatsCog(bot))