# cogs/stats.py
#
# Commands
#   /check player <steam-id-or-url>
#   /stats rust  <steam-id-or-url>
#
# Env vars
#   STEAM_API_KEY
#   BATTLEMETRICS_TOKEN   (optional)
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

# ────────────────────────────────────────────────────────────────
class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ═══════════════════ /check group ══════════════════
    check = app_commands.Group(name="check", description="Look-ups & checks")

    @check.command(name="player", description="Steam ban & profile check")
    @app_commands.describe(steamid="SteamID64 or profile URL")
    async def check_player(self, inter: discord.Interaction, steamid: str):
        await inter.response.defer(ephemeral=True)
        sid = await self._resolve(steamid)
        if not sid:
            return await inter.followup.send("Unable to resolve SteamID.", ephemeral=True)

        bans, prof                        = await self._steam_bans_and_profile(sid)
        bm_prof, bm_bans, eac, name_hist  = await self._bm_info(sid)

        colour = discord.Color.red() if (
            bans["VACBanned"] or bans["CommunityBanned"] or
            bans["NumberOfGameBans"] or bans["EconomyBan"] != "none" or
            eac or bm_bans
        ) else discord.Color.green()

        e = discord.Embed(
            title=f"Player check – {prof.get('personaname','Unknown')}",
            url=prof.get("profileurl") or discord.Embed.Empty,
            colour=colour,
        ).set_footer(text=f"SteamID64: {sid}")

        if (av := prof.get("avatarfull")):
            e.set_thumbnail(url=av)

        v = lambda n: f"{n:,}" if n else "N/A"

        if (ts := prof.get("timecreated")):
            e.add_field(name="Account created",
                        value=dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d"),
                        inline=True)
        if (cc := prof.get("loccountrycode")):
            flag = chr(0x1F1E6 + ord(cc[0]) - 65) + chr(0x1F1E6 + ord(cc[1]) - 65)
            e.add_field(name="Country", value=f"{flag} {cc}", inline=True)

        e.add_field(name="VAC Ban",        value=f"{'Yes' if bans['VACBanned'] else 'No'} ({v(bans['NumberOfVACBans'])})", inline=True)
        e.add_field(name="Game Bans",      value=v(bans["NumberOfGameBans"]), inline=True)
        e.add_field(name="Community Ban",  value="Yes" if bans["CommunityBanned"] else "No", inline=True)
        e.add_field(name="Trade Ban",      value=bans["EconomyBan"].capitalize(), inline=True)
        e.add_field(name="Days Since Ban", value=v(bans["DaysSinceLastBan"]), inline=True)

        if eac is not None:
            e.add_field(name="EAC Ban", value="Yes" if eac else "No", inline=True)
        if bm_prof:
            url = f"https://www.battlemetrics.com/rcon/players/{bm_prof['id']}"
            e.add_field(name="BattleMetrics", value=f"[Profile]({url})", inline=True)

        if bm_bans:
            lines = []
            for b in bm_bans[:5]:
                org  = b["attributes"].get("organization", {}).get("name", "Org")
                reas = b["attributes"].get("reason", "No reason")
                exp  = b["attributes"].get("expires") or "Permanent"
                lines.append(f"• **{org}** – {reas} (exp {exp[:10]})")
            if len(bm_bans) > 5:
                lines.append(f"…and {len(bm_bans)-5} more.")
            e.add_field(name=f"BM bans ({len(bm_bans)})", value="\n".join(lines), inline=False)

        if name_hist:
            e.add_field(name="Previous names", value="\n".join(name_hist[:10]), inline=False)

        await inter.followup.send(embed=e, ephemeral=True)

    # ═══════════════════ /stats group ══════════════════
    stats = app_commands.Group(name="stats", description="Game statistics")

    @stats.command(name="rust", description="Rust hours & detailed stats")
    @app_commands.describe(steamid="SteamID64 or profile URL")
    async def rust_stats(self, inter: discord.Interaction, steamid: str):
        await inter.response.defer(ephemeral=True)
        sid = await self._resolve(steamid)
        if not sid:
            return await inter.followup.send("Unable to resolve SteamID.", ephemeral=True)

        total_h, two_w_h, last_play, prof  = await self._playtime_and_persona(sid)
        unlocked, total_ach, ach_pct        = await self._achievements(sid)
        stats_ok, st                        = await self._rust_stats(sid)

        bm_prof, *_ = await self._bm_info(sid)
        bm_online = False; bm_cur_srv = None; bm_tot = 0; bm_top=[]
        if bm_prof:
            *_ , bm_online, bm_cur_srv, bm_tot, bm_top = await self._bm_sessions(bm_prof["id"])

        steam_online = prof.get("gameid") == str(APPID_RUST)
        steam_server = prof.get("gameextrainfo") if steam_online else None

        v = lambda n: "N/A" if not n else f"{n:,}"

        e = discord.Embed(
            title=f"Rust stats – [{prof.get('personaname','Unknown')}]({prof.get('profileurl')})",
            colour=0xFF7A00
        ).set_thumbnail(url=prof.get("avatarfull", discord.Embed.Empty)
        ).set_footer(text=f"SteamID64: {sid}")

        # ───────── Overview ─────────
        e.add_field(name="Hours",
                    value=f"Total **{v(total_h)}**\n2-weeks **{v(two_w_h)}**",
                    inline=True)
        e.add_field(name="Last played", value=last_play, inline=True)
        e.add_field(name="Achievements",
                    value=f"{v(unlocked)}/{v(total_ach)} ({ach_pct})",
                    inline=True)

        # ───────── Presence ─────────
        pres = [
            f"Steam : **{'Yes' if steam_online else 'No'}**"
            + (f" – “{steam_server}”" if steam_server else ""),
        ]
        if bm_prof:
            pres.append(
                f"BM : **{'Yes' if bm_online else 'No'}**"
                + (f" – {bm_cur_srv}" if bm_online and bm_cur_srv else "")
            )
            pres.append(f"BM sessions : **{v(bm_tot)}**")
            if bm_top:
                pres.append("Top srv : " + ", ".join(bm_top))
        e.add_field(name="Presence", value="\n".join(pres), inline=False)

        if not stats_ok:
            e.add_field(name="Detailed stats", value="Private / not available.")
            return await inter.followup.send(embed=e, ephemeral=True)

        # ───────── Combat ─────────
        bullets_fired = st["shots_fired"]; bullets_hit = st["shots_hit"]
        kills = st["kill_player"]; deaths = st["death_player"]
        kd    = f"{kills/deaths:.2f}" if deaths else ("∞" if kills else "N/A")
        acc   = f"{(bullets_hit/bullets_fired*100):.1f}%" if bullets_fired else "N/A"
        hsacc = f"{(st['headshot_hits']/bullets_hit*100):.1f}%" if bullets_hit else "N/A"

        e.add_field(
            name="PvP",
            value=(
                f"Kills **{v(kills)}** / Deaths **{v(deaths)}** (K/D {kd})\n"
                f"Bullets **{v(bullets_hit)} / {v(bullets_fired)}** ({acc})\n"
                f"Head-shot acc. {hsacc}"
            ),
            inline=False)

        # ───────── Kills / Deaths ─────────
        e.add_field(
            name="Kills",
            value=(
                f"Scientists **{v(st['kill_scientist'])}**\n"
                f"Bears **{v(st['kill_bear'])}**, Wolves **{v(st['kill_wolf'])}**\n"
                f"Boars **{v(st['kill_boar'])}**, Deer **{v(st['kill_deer'])}**\n"
                f"Horses **{v(st['kill_horse'])}**"
            ),
            inline=True)
        arrows_fired = st["arrow_fired"]; arrows_hit = st["arrow_hit"]
        aacc = f"{(arrows_hit/arrows_fired*100):.1f}%" if arrows_fired else "N/A"
        e.add_field(
            name="Bow",
            value=f"Arrows **{v(arrows_hit)}/{v(arrows_fired)}**\nAccuracy {aacc}",
            inline=True)
        e.add_field(
            name="Deaths",
            value=f"Suicides **{v(st['death_suicide'])}**\nFalling **{v(st['death_fall'])}**",
            inline=True)

        # ───────── Resources ─────────
        res1 = (
            f"Wood **{v(st['harvest_wood'])}**\n"
            f"Stone **{v(st['harvest_stones'])}**\n"
            f"Metal ore **{v(st['harvest_metal_ore'])}**\n"
            f"HQ ore **{v(st['harvest_hq_metal_ore'])}**\n"
            f"Sulfur ore **{v(st['harvest_sulfur_ore'])}**"
        )
        res2 = (
            f"Low-grade **{v(st['acq_lowgrade'])}**\n"
            f"Scrap **{v(st['acq_scrap'])}**\n"
            f"Cloth **{v(st['acq_cloth'])}**\n"
            f"Leather **{v(st['acq_leather'])}**"
        )
        e.add_field(name="Resources (nodes)", value=res1, inline=True)
        e.add_field(name="Resources (pickup)", value=res2, inline=True)

        # ───────── Building / loot ─────────
        bld = (
            f"Blocks placed **{v(st['build_place'])}**\n"
            f"Blocks upgraded **{v(st['build_upgrade'])}**\n"
            f"Barrels broken **{v(st['barrels'])}**\n"
            f"BPs learned **{v(st['bps'])}**"
        )
        elec = (
            f"Wires conn. **{v(st['wires'])}**\n"
            f"Pipes conn. **{v(st['pipes'])}**\n"
            f"Friendly waves **{v(st['waves'])}**"
        )
        e.add_field(name="Building / Loot", value=bld, inline=True)
        e.add_field(name="Electric / Social", value=elec, inline=True)

        # ───────── Horse & consumption ─────────
        horse = (
            f"Miles ridden **{v(st['horse_miles'])}**\n"
            f"Horses ridden **{v(st['horses_ridden'])}**"
        )
        usage = (
            f"Calories **{v(st['calories'])}**\n"
            f"Water **{v(st['water'])}**\n"
            f"Map opens **{v(st['map_open'])}**\n"
            f"Inventory opens **{v(st['inv_open'])}**\n"
            f"Items crafted **{v(st['items_crafted'])}**"
        )
        e.add_field(name="Horses", value=horse, inline=True)
        e.add_field(name="Consumption / UI", value=usage, inline=True)

        await inter.followup.send(embed=e, ephemeral=True)

    # ═════════════════ helper methods ══════════════════
    async def _resolve(self, raw: str) -> str | None:
        if raw.isdigit() and len(raw) >= 16:
            return raw
        m = PROFILE_RE.search(raw)
        if not m:
            return None
        vanity = m.group(1)
        if vanity.isdigit():
            return vanity
        url = f"https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/?key={STEAM_API_KEY}&vanityurl={vanity}"
        async with aiohttp.ClientSession() as s, s.get(url) as r:
            data = await r.json()
        return data["response"].get("steamid")

    async def _steam_bans_and_profile(self, sid: str):
        async with aiohttp.ClientSession() as ses:
            url_b = f"https://api.steampowered.com/ISteamUser/GetPlayerBans/v1/?key={STEAM_API_KEY}&steamids={sid}"
            url_p = f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key={STEAM_API_KEY}&steamids={sid}"
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
        names = [n.get("name","Unknown") for n in prof["attributes"].get("names", [])[::-1]]
        return prof, bans, eac, names

    async def _bm_sessions(self, pid: str):
        url = (f"https://api.battlemetrics.com/sessions?"
               f"filter[player]={pid}&page[size]=100&include=server&sort=-start")
        async with aiohttp.ClientSession() as ses, ses.get(url, headers=BM_HEADERS) as r:
            data = await r.json()
        sessions = data.get("data", []); total=len(sessions)
        srv_name = {i["id"]: i["attributes"]["name"]
                    for i in data.get("included", []) if i["type"]=="server"}
        online=False; cur=None
        if sessions and sessions[0]["attributes"]["end"] is None:
            online=True
            sid = sessions[0]["relationships"]["server"]["data"]["id"]
            cur = srv_name.get(sid,"Unknown")
        freq = collections.Counter(
            srv_name.get(s["relationships"]["server"]["data"]["id"],"Unknown")
            for s in sessions
        )
        top=[f"{n} ({c})" for n,c in freq.most_common(3)]
        return sessions, online, cur, total, top

    async def _playtime_and_persona(self, sid: str):
        async with aiohttp.ClientSession() as ses:
            url=f"https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?key={STEAM_API_KEY}&steamid={sid}&include_appinfo=1"
            async with ses.get(url) as r: og = await r.json()
        g=next((x for x in og.get("response", {}).get("games", []) if x["appid"]==APPID_RUST),None)
        total_h=g["playtime_forever"]//60 if g else 0
        two_w_h=g.get("playtime_2weeks",0)//60 if g else 0

        async with aiohttp.ClientSession() as ses:
            url=f"https://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v1/?key={STEAM_API_KEY}&steamid={sid}"
            async with ses.get(url) as r: rp=await r.json()
        recent=next((x for x in rp.get("response", {}).get("games", []) if x["appid"]==APPID_RUST),None)
        last_play=dt.datetime.utcfromtimestamp(recent["playtime_at"]).strftime("%Y-%m-%d") if recent and "playtime_at" in recent else "Unknown"

        async with aiohttp.ClientSession() as ses:
            url=f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key={STEAM_API_KEY}&steamids={sid}"
            async with ses.get(url) as r: prof=(await r.json())["response"]["players"][0]
        return total_h, two_w_h, last_play, prof

    async def _achievements(self, sid: str):
        async with aiohttp.ClientSession() as ses:
            url=f"https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v1/?key={STEAM_API_KEY}&steamid={sid}&appid={APPID_RUST}"
            async with ses.get(url) as r: ach=await r.json()
        if not ach.get("playerstats",{}).get("success"): return "Private","N/A","N/A"
        lst=ach["playerstats"]["achievements"]; unlocked=sum(1 for a in lst if a["achieved"])
        total=len(lst); pct=f"{unlocked/total*100:.1f}%"
        return unlocked, total, pct

    # ═════════════════ Rust stats  ═════════════════
    async def _rust_stats(self, sid: str):
        async with aiohttp.ClientSession() as ses:
            url=f"https://api.steampowered.com/ISteamUserStats/GetUserStatsForGame/v2/?key={STEAM_API_KEY}&steamid={sid}&appid={APPID_RUST}"
            async with ses.get(url) as r: data=await r.json()
        raw_list=data.get("playerstats",{}).get("stats")
        if not raw_list: return False,{}

        raw={s["name"]:s["value"] for s in raw_list}
        sum_pref=lambda p: sum(v for k,v in raw.items() if k.startswith(p))

        # combat
        bullets_fired = raw.get("bullet_fired",0)+raw.get("shotgun_fired",0)
        bullets_hit   = sum_pref("bullet_hit_")+sum_pref("shotgun_hit_")
        arrows_fired  = raw.get("arrow_fired",0)
        arrows_hit    = sum_pref("arrow_hit_")
        headshots     = raw.get("headshots", raw.get("headshot",0))
        kills_player  = raw.get("kill_player",0)
        deaths_player = raw.get("death_player", raw.get("deaths",0))

        # misc kills / deaths
        stats={
            "kill_scientist": raw.get("kill_scientist",0),
            "kill_bear":      raw.get("kill_bear",0),
            "kill_wolf":      raw.get("kill_wolf",0),
            "kill_boar":      raw.get("kill_boar",0),
            "kill_deer":      raw.get("kill_stag",0),
            "kill_horse":     raw.get("kill_horse",0),
            "death_suicide":  raw.get("death_suicide",0),
            "death_fall":     raw.get("death_fall",0),
        }

        # resources – harvest / acquired
        stats.update({
            "harvest_wood":  raw.get("harvest.wood", raw.get("harvested_wood",0)),
            "harvest_stones":raw.get("harvest.stones",raw.get("harvested_stones",0)),
            "harvest_metal_ore": (
                raw.get("harvest.metal_ore",0)+raw.get("harvest_metal_ore",0)+raw.get("acquired_metal.ore",0)
            ),
            "harvest_hq_metal_ore":(
                raw.get("harvest.hq_metal_ore",0)+raw.get("harvest_hq_metal_ore",0)+
                raw.get("acquired_highqualitymetal.ore",0)+raw.get("acquired_hq_metal_ore",0)
            ),
            "harvest_sulfur_ore":(
                raw.get("harvest.sulfur_ore",0)+raw.get("harvest_sulfur_ore",0)+raw.get("acquired_sulfur.ore",0)
            ),
            # extra pickups
            "acq_lowgrade": raw.get("acquired_lowgradefuel",0),
            "acq_leather":  raw.get("acquired_leather",0),
            "acq_cloth":    raw.get("acquired_cloth",0),
            "acq_scrap":    raw.get("acquired_scrap",0),
        })

        # building / misc counts (keys may differ per server build)
        stats.update({
            "build_place":   raw.get("buildings_placed", raw.get("structure_built",0)),
            "build_upgrade": raw.get("buildings_upgraded", raw.get("structure_upgrade",0)),
            "barrels":       raw.get("destroyed_barrel",0)+raw.get("destroyed_barrel_town",0),
            "bps":           raw.get("blueprint_studied",0),
            "pipes":         raw.get("pipes_connected",0),
            "wires":         raw.get("wires_connected",0),
            "waves":         raw.get("gesture_wave",0),
        })

        # horse / consumption / ui
        stats.update({
            "horse_miles":       round(raw.get("horse_distance",0)/1609.344,1), # metres→miles
            "horses_ridden":     raw.get("horses_ridden",0),
            "calories":          raw.get("calories_consumed",0),
            "water":             raw.get("water_consumed",0),
            "map_open":          raw.get("map_opened", raw.get("opened_map",0)),
            "inv_open":          raw.get("inventory_opened",0),
            "items_crafted":     raw.get("items_crafted", raw.get("crafted_items",0)),
        })

        # core combat
        stats.update({
            "shots_fired": bullets_fired,
            "shots_hit":   bullets_hit,
            "arrow_fired": arrows_fired,
            "arrow_hit":   arrows_hit,
            "headshot_hits": headshots,
            "kill_player":   kills_player,
            "death_player":  deaths_player,
        })
        return True, stats

# ═══════════════════ setup loader ══════════════════
async def setup(bot: commands.Bot, db=None):
    await bot.add_cog(StatsCog(bot))