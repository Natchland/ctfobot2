# cogs/stats.py
#
# Slash-commands
#   /check player <steam-id|url>   – VAC / BM / profile check
#   /stats rust  <steam-id|url>    – Rust hours + detailed stats
#
# Env vars
#   STEAM_API_KEY        (required)   – Steam Web-API key
#   BATTLEMETRICS_TOKEN  (optional)   – raises BM rate-limit
# ────────────────────────────────────────────────────────────────

from __future__ import annotations

import os, re, collections, aiohttp, datetime as dt, discord
from discord.ext import commands
from discord import app_commands

STEAM_API_KEY = os.getenv("STEAM_API_KEY")

BM_TOKEN   = os.getenv("BATTLEMETRICS_TOKEN", "")
BM_HEADERS = {"Authorization": f"Bearer {BM_TOKEN}"} if BM_TOKEN else {}

APPID_RUST = 252490
PROFILE_RE = re.compile(r"https?://steamcommunity\.com/(?:profiles|id)/([^/]+)")

# ╔══════════════════════════════════════════════════════════╗
# ║                         COG                              ║
# ╚══════════════════════════════════════════════════════════╝
class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ───────────────────── /check ─────────────────────
    check = app_commands.Group(name="check", description="Look-ups & checks")

    @check.command(name="player", description="Steam ban & profile check")
    @app_commands.describe(steamid="SteamID64 or profile URL")
    async def check_player(self, inter: discord.Interaction, steamid: str):
        await inter.response.defer(ephemeral=True)

        sid = await self._resolve(steamid)
        if not sid:
            return await inter.followup.send("Unable to resolve SteamID.", ephemeral=True)

        bans, profile                     = await self._steam_bans_and_profile(sid)
        bm_prof, bm_bans, eac, name_hist  = await self._bm_info(sid)

        danger = (
            bans["VACBanned"]
            or bans["CommunityBanned"]
            or bans["NumberOfGameBans"]
            or bans["EconomyBan"] != "none"
            or eac
            or bm_bans
        )
        colour = discord.Color.red() if danger else discord.Color.green()

        e = discord.Embed(
            title=f"Player check – {profile.get('personaname','Unknown')}",
            url=profile.get("profileurl") or None,
            colour=colour,
        ).set_footer(text=f"SteamID64: {sid}")

        if (av := profile.get("avatarfull")):
            e.set_thumbnail(url=av)

        fmt = lambda n: "N/A" if not n else f"{n:,}"

        if (ts := profile.get("timecreated")):
            e.add_field(
                name="Account created",
                value=dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d"),
                inline=True,
            )
        if (cc := profile.get("loccountrycode")):
            flag = chr(0x1F1E6 + ord(cc[0]) - 65) + chr(0x1F1E6 + ord(cc[1]) - 65)
            e.add_field(name="Country", value=f"{flag} {cc}", inline=True)

        e.add_field(
            name="VAC Ban",
            value=f"{'Yes' if bans['VACBanned'] else 'No'} ({fmt(bans['NumberOfVACBans'])})",
            inline=True,
        )
        e.add_field(name="Game Bans", value=fmt(bans["NumberOfGameBans"]), inline=True)
        e.add_field(
            name="Community Ban",
            value="Yes" if bans["CommunityBanned"] else "No",
            inline=True,
        )
        e.add_field(name="Trade Ban", value=bans["EconomyBan"].capitalize(), inline=True)
        e.add_field(
            name="Days Since Ban", value=fmt(bans["DaysSinceLastBan"]), inline=True
        )

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
                lines.append(f"• **{org}** – {reas} (exp {exp[:10]})")
            if len(bm_bans) > 5:
                lines.append(f"…and {len(bm_bans) - 5} more.")
            e.add_field(
                name=f"BM bans ({len(bm_bans)})", value="\n".join(lines), inline=False
            )

        if name_hist:
            e.add_field(
                name="Previous names", value="\n".join(name_hist[:10]), inline=False
            )

        await inter.followup.send(embed=e, ephemeral=True)

    # ───────────────────── /stats ─────────────────────
    stats = app_commands.Group(name="stats", description="Game statistics")

    @stats.command(name="rust", description="Rust hours & detailed stats")
    @app_commands.describe(steamid="SteamID64 or profile URL")
    async def rust_stats(self, inter: discord.Interaction, steamid: str):
        await inter.response.defer(ephemeral=True)

        sid = await self._resolve(steamid)
        if not sid:
            return await inter.followup.send("Unable to resolve SteamID.", ephemeral=True)

        total_h, two_w_h, last_play, profile = await self._playtime_and_persona(sid)
        unlocked, total_ach, ach_pct         = await self._achievements(sid)
        stats_ok, st                         = await self._rust_stats(sid)

        bm_prof, *_ = await self._bm_info(sid)
        bm_online = bm_cur_srv = None
        bm_tot = 0
        bm_top: list[str] = []
        if bm_prof:
            *_x, bm_online, bm_cur_srv, bm_tot, bm_top = await self._bm_sessions(bm_prof["id"])

        steam_online = profile.get("gameid") == str(APPID_RUST)
        steam_server = profile.get("gameextrainfo") if steam_online else None

        fmt = lambda n: "N/A" if not n else f"{n:,}"

        e = discord.Embed(
            title=f"Rust stats – {profile.get('personaname','Unknown')}",
            url=profile.get("profileurl") or None,
            colour=0xFF7A00,
        ).set_footer(text=f"SteamID64: {sid}")

        if (av := profile.get("avatarfull")):
            e.set_thumbnail(url=av)

        # ───────── overview ─────────
        e.add_field(
            name="Hours",
            value=f"Total **{fmt(total_h)}**\n2-weeks **{fmt(two_w_h)}**",
            inline=True,
        )
        e.add_field(name="Last played", value=last_play, inline=True)
        e.add_field(
            name="Achievements",
            value=f"{fmt(unlocked)}/{fmt(total_ach)} ({ach_pct})",
            inline=True,
        )

        # ───────── live presence ─────────
        pres = [
            f"Steam : **{'Yes' if steam_online else 'No'}**"
            + (f" – “{steam_server}”" if steam_server else "")
        ]
        if bm_prof:
            pres.append(
                f"BM : **{'Yes' if bm_online else 'No'}**"
                + (f" – {bm_cur_srv}" if bm_online and bm_cur_srv else "")
            )
            pres.append(f"BM sessions : **{fmt(bm_tot)}**")
            if bm_top:
                pres.append("Top servers : " + ", ".join(bm_top))
        e.add_field(name="Presence", value="\n".join(pres), inline=False)

        # private stats?
        if not stats_ok:
            e.add_field(name="Detailed stats", value="Private / not available.")
            return await inter.followup.send(embed=e, ephemeral=True)

        # ───────── PvP ─────────
        kills, deaths = st["kill_player"], st["death_player"]
        kd = f"{kills/deaths:.2f}" if deaths else ("∞" if kills else "N/A")
        bullets_fired, bullets_hit = st["shots_fired"], st["shots_hit"]
        headshots = st["headshot_hits"]
        acc  = f"{(bullets_hit/bullets_fired*100):.1f}%" if bullets_fired else "N/A"
        hsac = f"{(headshots/bullets_hit*100):.1f}%"    if bullets_hit   else "N/A"
        e.add_field(
            name="PvP",
            value=(
                f"Kills **{fmt(kills)}** / Deaths **{fmt(deaths)}** (K/D {kd})\n"
                f"Bullets **{fmt(bullets_hit)} / {fmt(bullets_fired)}** ({acc})\n"
                f"Head-shot acc. {hsac}"
            ),
            inline=False,
        )

        # ───────── kills / deaths / bow ─────────
        e.add_field(
            name="Kills",
            value=(
                f"Scientists **{fmt(st['kill_scientist'])}**\n"
                f"Bears **{fmt(st['kill_bear'])}**, Wolves **{fmt(st['kill_wolf'])}**\n"
                f"Boars **{fmt(st['kill_boar'])}**, Deer **{fmt(st['kill_deer'])}**\n"
                f"Horses **{fmt(st['kill_horse'])}**"
            ),
            inline=True,
        )
        arrows_fired, arrows_hit = st["arrow_fired"], st["arrow_hit"]
        aacc = f"{(arrows_hit/arrows_fired*100):.1f}%" if arrows_fired else "N/A"
        e.add_field(
            name="Bow",
            value=f"Arrows **{fmt(arrows_hit)}/{fmt(arrows_fired)}**\nAccuracy {aacc}",
            inline=True,
        )
        e.add_field(
            name="Deaths",
            value=f"Suicides **{fmt(st['death_suicide'])}**\nFalling **{fmt(st['death_fall'])}**",
            inline=True,
        )

        # ───────── resources ─────────
        nodes = (
            f"Wood **{fmt(st['harvest_wood'])}**\n"
            f"Stone **{fmt(st['harvest_stones'])}**\n"
            f"Metal ore **{fmt(st['harvest_metal_ore'])}**\n"
            f"HQ ore **{fmt(st['harvest_hq_metal_ore'])}**\n"
            f"Sulfur ore **{fmt(st['harvest_sulfur_ore'])}**"
        )
        pickups = (
            f"Low-grade **{fmt(st['acq_lowgrade'])}**\n"
            f"Scrap **{fmt(st['acq_scrap'])}**\n"
            f"Cloth **{fmt(st['acq_cloth'])}**\n"
            f"Leather **{fmt(st['acq_leather'])}**"
        )
        e.add_field(name="Resources (nodes)",   value=nodes,   inline=True)
        e.add_field(name="Resources (pick-up)", value=pickups, inline=True)

        # ───────── building / loot / electric ─────────
        bld = (
            f"Blocks placed **{fmt(st['build_place'])}**\n"
            f"Blocks upgraded **{fmt(st['build_upgrade'])}**\n"
            f"Barrels broken **{fmt(st['barrels'])}**\n"
            f"BPs learned **{fmt(st['bps'])}**"
        )
        elec = (
            f"Wires conn. **{fmt(st['wires'])}**\n"
            f"Pipes conn. **{fmt(st['pipes'])}**\n"
            f"Friendly waves **{fmt(st['waves'])}**"
        )
        e.add_field(name="Building / Loot", value=bld,  inline=True)
        e.add_field(name="Electric / Social", value=elec, inline=True)

        # ───────── horse / consumption / ui ─────────
        horse = (
            f"Miles ridden **{fmt(st['horse_miles'])}**\n"
            f"Horses ridden **{fmt(st['horses_ridden'])}**"
        )
        usage = (
            f"Calories **{fmt(st['calories'])}**\n"
            f"Water **{fmt(st['water'])}**\n"
            f"Map opens **{fmt(st['map_open'])}**\n"
            f"Inventory opens **{fmt(st['inv_open'])}**\n"
            f"Items crafted **{fmt(st['items_crafted'])}**"
        )
        e.add_field(name="Horses", value=horse, inline=True)
        e.add_field(name="Consumption / UI", value=usage, inline=True)

        await inter.followup.send(embed=e, ephemeral=True)

    # ═════════════════ helper methods ═════════════════
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
        prof = data["data"][0]; pid = prof["id"]
        async with aiohttp.ClientSession() as ses:
            url = f"https://api.battlemetrics.com/bans?filter[player]={pid}&sort=-timestamp"
            async with ses.get(url, headers=BM_HEADERS) as r:
                bans = (await r.json()).get("data", [])
        flags = prof["attributes"].get("flags", [])
        eac   = any("eac" in (f or "").lower() for f in flags)
        names = [
            n.get("name", "Unknown")
            for n in prof["attributes"].get("names", [])[::-1]
        ]
        return prof, bans, eac, names

    async def _bm_sessions(self, pid: str):
        url = (
            "https://api.battlemetrics.com/sessions?"
            f"filter[player]={pid}&page[size]=100&include=server&sort=-start"
        )
        async with aiohttp.ClientSession() as ses, ses.get(url, headers=BM_HEADERS) as r:
            data = await r.json()
        sessions = data.get("data", []); total = len(sessions)
        srv_name = {i["id"]: i["attributes"]["name"]
                    for i in data.get("included", []) if i["type"] == "server"}
        online = False; current = None
        if sessions and sessions[0]["attributes"]["end"] is None:
            online = True
            sid = sessions[0]["relationships"]["server"]["data"]["id"]
            current = srv_name.get(sid, "Unknown")
        freq = collections.Counter(
            srv_name.get(s["relationships"]["server"]["data"]["id"], "Unknown")
            for s in sessions
        )
        top = [f"{n} ({c})" for n, c in freq.most_common(3)]
        return sessions, online, current, total, top

    async def _playtime_and_persona(self, sid: str):
        async with aiohttp.ClientSession() as ses:
            url = (
                "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
                f"?key={STEAM_API_KEY}&steamid={sid}&include_appinfo=1"
            )
            async with ses.get(url) as r:
                og = await r.json()
        g = next(
            (x for x in og.get("response", {}).get("games", []) if x["appid"] == APPID_RUST),
            None,
        )
        total_h = g["playtime_forever"] // 60 if g else 0
        two_w_h = g.get("playtime_2weeks", 0) // 60 if g else 0

        async with aiohttp.ClientSession() as ses:
            url = (
                "https://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v1/"
                f"?key={STEAM_API_KEY}&steamid={sid}"
            )
            async with ses.get(url) as r:
                rp = await r.json()
        recent = next(
            (x for x in rp.get("response", {}).get("games", []) if x["appid"] == APPID_RUST),
            None,
        )
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
        lst = ach["playerstats"]["achievements"]
        unlocked = sum(1 for a in lst if a["achieved"])
        total = len(lst)
        pct = f"{unlocked / total * 100:.1f}%"
        return unlocked, total, pct

    # ═════════════ Rust user-stats helper ═════════════
    async def _rust_stats(self, sid: str):
        """
        Return (ok: bool, stats: dict[str,int])

        • understands every key in the list you provided
        • wildcard   '*'  = prefix-sum  (e.g. bullet_hit_*)
        • _sum=True       = sum ALL listed variants
        • _scale=x        = divide by x   (used for metres→miles, km→miles)
        """

        async with aiohttp.ClientSession() as ses:
            url = (
                "https://api.steampowered.com/ISteamUserStats/GetUserStatsForGame/v2/"
                f"?key={STEAM_API_KEY}&steamid={sid}&appid={APPID_RUST}"
            )
            async with ses.get(url) as r:
                data = await r.json()

        raw_list = data.get("playerstats", {}).get("stats")
        if not raw_list:
            return False, {}

        raw = {s["name"]: s["value"] for s in raw_list}

        # ------------- helpers -------------
        def _sum_prefix(prefix: str) -> int:
            return sum(v for k, v in raw.items() if k.startswith(prefix))

        def get(*variants: str, _sum=False, _scale=1):
            """
            Return   – first non-zero variant  or  sum of all variants.
            Variant ending in '*' = prefix wildcard.
            """
            if _sum:
                total = 0
                for var in variants:
                    total += (
                        _sum_prefix(var[:-1])
                        if var.endswith("*") else
                        raw.get(var, 0)
                    )
                return int(total / _scale)
            for var in variants:
                val = (
                    _sum_prefix(var[:-1])
                    if var.endswith("*") else
                    raw.get(var, 0)
                )
                if val:
                    return int(val / _scale)
            return 0

        # ------------- combat -------------
        bullets_fired = get("bullet_fired") + get("shotgun_fired")
        bullets_hit   = get("bullet_hit_*", "shotgun_hit_*", _sum=True)
        arrows_fired  = get("arrow_fired", "arrows_shot")
        arrows_hit    = get("arrow_hit_*", _sum=True)
        headshots     = get("headshot", "headshots")
        kills_player  = get("kill_player")
        deaths_player = get("death_player", "deaths")

        # ------------- animals / misc -------------
        stats = {
            "kill_scientist": get("kill_scientist"),
            "kill_bear":      get("kill_bear"),
            "kill_wolf":      get("kill_wolf"),
            "kill_boar":      get("kill_boar"),
            "kill_deer":      get("kill_stag"),
            "kill_horse":     get("horse_mounted_count"),      # mounts ~ rides
            "death_suicide":  get("death_suicide","death_selfinflicted"),
            "death_fall":     get("death_fall"),
        }

        # ------------- resources – nodes -------------
        stats.update({
            "harvest_wood":  get("harvested_wood",  "harvest.wood"),
            "harvest_stones":get("harvested_stones","harvest.stones"),
            "harvest_metal_ore": get(
                "acquired_metal.ore", "harvest.metal_ore", _sum=True
            ),
            "harvest_hq_metal_ore": 0,   # HQM not in your list – keep 0 / N-A
            "harvest_sulfur_ore":    0,   # Sulfur not in your list – keep 0 / N-A
        })

        # ------------- resources – pick-ups -------------
        stats.update({
            "acq_lowgrade": get("acquired_lowgradefuel"),
            "acq_scrap":    get("acquired_scrap"),
            "acq_cloth":    get("harvested_cloth",  "acquired_cloth",  "acquired_cloth.item"),
            "acq_leather":  get("harvested_leather","acquired_leather","acquired_leather.item"),
        })

        # ------------- building / loot / social -------------
        stats.update({
            "build_place":   get("placed_blocks",  "building_blocks_placed",
                                 "buildings_placed", "structure_built"),
            "build_upgrade": get("upgraded_blocks","building_blocks_upgraded",
                                 "buildings_upgraded","structure_upgrade"),
            "barrels":       get("destroyed_barrels","destroyed_barrel*", _sum=True),
            "bps":           get("blueprint_studied"),
            "pipes":         get("pipes_connected"),
            "wires":         get("wires_connected","tincanalarms_wired"),
            "waves":         get("gesture_wave_count","waved_at_players","gesture_wave"),
        })

        # ------------- horse / consumption / UI -------------
        # horse distance can be metres or km – check both then convert to miles
        metres = get("horse_distance_ridden", _sum=True)  # metres
        km     = get("horse_distance_ridden_km")          # km
        miles  = (metres / 1609.344) if metres else (km * 0.621371)  # km→mi
        stats.update({
            "horse_miles":       int(miles),
            "horses_ridden":     get("horse_mounted_count"),
            "calories":          get("calories_consumed"),
            "water":             get("water_consumed"),
            "map_open":          get("MAP_OPENED",  "map_opened",  "map_open"),
            "inv_open":          get("INVENTORY_OPENED","inventory_opened"),
            "items_crafted":     get("CRAFTING_OPENED", "items_crafted", "crafted_items"),
        })

        # ------------- core combat numbers -------------
        stats.update({
            "shots_fired":   bullets_fired,
            "shots_hit":     bullets_hit,
            "arrow_fired":   arrows_fired,
            "arrow_hit":     arrows_hit,
            "headshot_hits": headshots,
            "kill_player":   kills_player,
            "death_player":  deaths_player,
        })

        return True, stats

# ╔══════════════════════════════════════════════════════════╗
# ║                     EXTENSION LOADER                     ║
# ╚══════════════════════════════════════════════════════════╝
async def setup(bot: commands.Bot, db=None):
    await bot.add_cog(StatsCog(bot))
    print("[cogs.stats] loaded")       # quick visual confirmation