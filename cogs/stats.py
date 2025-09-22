from __future__ import annotations
import os, re, collections, datetime as dt, aiohttp, cachetools, discord
from discord.ext import commands
from discord import app_commands

STEAM_API_KEY = os.getenv("STEAM_API_KEY")
BM_TOKEN = os.getenv("BATTLEMETRICS_TOKEN", "")
BM_HEADERS = {"Authorization": f"Bearer {BM_TOKEN}"} if BM_TOKEN else {}
APPID_RUST = 252490
PROFILE_RE = re.compile(r"https?://steamcommunity\.com/(?:profiles|id)/([^/]+)")
PLAYER_CACHE = cachetools.TTLCache(maxsize=1_000, ttl=300)

RISK_FLAG_EXPLANATIONS = {
    "🔒 Private profile":            "Profile not public",
    "👤 Default avatar":             "Using default Steam avatar",
    "🆕 New account":                "Account < 30 days old",
    "⬇️ Low Steam level":            "Level < 10",
    "🎮 Few games":                  "Owns < 3 games",
    "👥 Few friends":                "Has < 3 friends",
    "⚠️ Recent ban":                 "Ban in last 90 days",
    "⚠️ Very recent ban":            "Ban in last 14 days",
    "⚠️ Multiple bans":              "More than one VAC / game ban",
    "🔴 BattleMetrics ban":          "Banned on BattleMetrics",
    "🔴 EAC ban":                    "EAC ban recorded",
    "🔴 RustBans ban":               "Banned on RustBans",
    "⚠️ SteamRep flagged":           "Negative SteamRep",
    "✏️ Frequent name changes":      "≥ 3 previous names",
    "🕵️‍♂️ Suspicious name":          "Alt / smurf style name",
    "🕹️ Rust-only account":          "Only owns Rust (plus F2P)",
    "⏳ High Rust hours (fast)":      "High hours but new account",
}

class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    check = app_commands.Group(name="check", description="Look-ups & checks")
    stats = app_commands.Group(name="stats", description="Game statistics")

    @check.command(name="help", description="Explain risk flags")
    async def check_help(self, inter: discord.Interaction):
        txt = "\n".join(f"{k} — {v}" for k, v in RISK_FLAG_EXPLANATIONS.items())
        await inter.response.send_message(txt, ephemeral=True)

    @check.command(name="player", description="Steam profile & ban check")
    @app_commands.describe(steamid="SteamID64 or profile URL")
    async def check_player(self, inter: discord.Interaction, steamid: str):
        await inter.response.defer(ephemeral=True)
        sid = await self._resolve(steamid)
        if not sid:
            return await inter.followup.send("Unable to resolve SteamID.", ephemeral=True)

        if sid in PLAYER_CACHE:
            cached = PLAYER_CACHE[sid]
        else:
            bans, prof = await self._steam_bans_and_profile(sid)
            lvl, game_cnt, friend_cnt, top_games = await self._level_games_friends(sid)
            bm_prof, bm_bans, eac, names = await self._bm_info(sid)
            rb_status, rb_reason, rb_date = await self._rustbans_info(sid)
            sr_status = await self._steamrep_info(sid)
            rust_h, two_w_h = await self._rust_hours(sid)
            comments = await self._profile_comments(sid)
            patterns = [n for n in names if re.search(r"(alt|smurf|rust|\d{5,})", n, re.I)]
            cached = (bans, prof, lvl, game_cnt, friend_cnt, top_games, bm_prof, bm_bans,
                      eac, names, rb_status, rb_reason, rb_date, sr_status,
                      rust_h, two_w_h, comments, patterns)
            PLAYER_CACHE[sid] = cached

        (bans, prof, lvl, game_cnt, friend_cnt, top_games, bm_prof, bm_bans, eac,
         names, rb_status, rb_reason, rb_date, sr_status, rust_h, two_w_h,
         comments, patterns) = cached

        now = dt.datetime.utcnow()
        created = dt.datetime.utcfromtimestamp(prof.get("timecreated", 0)) if prof.get("timecreated") else None
        age = (now - created).days if created else None
        recent_ban = bans["DaysSinceLastBan"] is not None and bans["DaysSinceLastBan"] <= 90
        very_recent = bans["DaysSinceLastBan"] is not None and bans["DaysSinceLastBan"] <= 14
        low_lvl = lvl is not None and lvl < 10
        low_games = game_cnt is not None and game_cnt < 3
        rust_only = game_cnt is not None and game_cnt <= 2 and top_games and top_games[0]['name'].lower() == "rust"
        low_friends = friend_cnt is not None and friend_cnt < 3
        private = prof.get("communityvisibilitystate", 3) != 3
        default_av = prof.get("avatarfull", "").endswith("/avatar.jpg")
        many_names = len(names) >= 3
        multi_bans = (bans["NumberOfVACBans"] or 0) + (bans["NumberOfGameBans"] or 0) > 1
        suspicious_name = bool(patterns)
        fast_rust = rust_h is not None and age is not None and rust_h > 100 and age < 30

        flags = []
        if private: flags.append("🔒 Private profile")
        if default_av: flags.append("👤 Default avatar")
        if age is not None and age < 30: flags.append(f"🆕 New account")
        if low_lvl: flags.append("⬇️ Low Steam level")
        if low_games: flags.append("🎮 Few games")
        if low_friends: flags.append("👥 Few friends")
        if very_recent: flags.append("⚠️ Very recent ban")
        elif recent_ban: flags.append("⚠️ Recent ban")
        if multi_bans: flags.append("⚠️ Multiple bans")
        if bm_bans: flags.append("🔴 BattleMetrics ban")
        if eac: flags.append("🔴 EAC ban")
        if rb_status: flags.append("🔴 RustBans ban")
        if sr_status: flags.append("⚠️ SteamRep flagged")
        if many_names: flags.append("✏️ Frequent name changes")
        if suspicious_name: flags.append("🕵️‍♂️ Suspicious name")
        if rust_only: flags.append("🕹️ Rust-only account")
        if fast_rust: flags.append("⏳ High Rust hours (fast)")

        score = 0
        if private: score += 2
        if default_av: score += 1
        if age is not None and age < 7: score += 5
        elif age is not None and age < 30: score += 3
        if low_lvl: score += 2
        if lvl and lvl > 50: score -= 2
        if low_games: score += 3
        if game_cnt and game_cnt > 100: score -= 2
        if low_friends: score += 2
        if friend_cnt and friend_cnt > 100: score -= 1
        if very_recent: score += 8
        elif recent_ban: score += 5
        if multi_bans: score += 3
        if bm_bans: score += 5
        if eac: score += 5
        if rb_status: score += 5
        if sr_status: score += 5
        if many_names: score += 1
        if suspicious_name: score += 2
        if rust_only: score += 2
        if fast_rust: score += 2
        score = max(score, 0)

        if score >= 12:
            risk, colour = "🔴 HIGH RISK", discord.Color.red()
        elif score >= 5:
            risk, colour = "🟠 MODERATE RISK", discord.Color.orange()
        else:
            risk, colour = "🟢 LOW RISK", discord.Color.green()

        e = discord.Embed(
            title=prof.get("personaname", "Unknown"),
            url=prof.get("profileurl"),
            colour=colour,
            description=f"{risk}\n\n{' '.join(flags) or 'No immediate risk factors.'}"
        ).set_footer(text=f"SteamID64: {sid} | Score: {score}")

        if prof.get("avatarfull"):
            e.set_thumbnail(url=prof["avatarfull"])

        fmt = lambda n: f"{n:,}" if n is not None else "N/A"
        e.add_field(name="Created", value=created.strftime("%Y-%m-%d") if created else "N/A", inline=True)
        e.add_field(name="Age", value=f"{age} d" if age else "N/A", inline=True)
        e.add_field(name="Level", value=fmt(lvl), inline=True)
        e.add_field(name="Games", value=fmt(game_cnt), inline=True)
        e.add_field(name="Friends", value=fmt(friend_cnt), inline=True)
        e.add_field(name="Status", value="Private" if private else "Public", inline=True)
        e.add_field(name="Rust hrs", value=fmt(rust_h), inline=True)
        e.add_field(name="2-wks hrs", value=fmt(two_w_h), inline=True)

        if top_games:
            e.add_field(name="Top games", value="\n".join(f"{g['name']} ({g['playtime']} h)" for g in top_games[:5]), inline=False)

        badge = lambda v: "🔴" if v else "🟢"
        e.add_field(name="VAC Ban", value=f"{badge(bans['VACBanned'])} {bans['NumberOfVACBans']} ({bans['DaysSinceLastBan']} d ago)" if bans['VACBanned'] else "🟢 None", inline=True)
        e.add_field(name="Game Bans", value=f"{badge(bans['NumberOfGameBans'])} {bans['NumberOfGameBans']}" if bans['NumberOfGameBans'] else "🟢 None", inline=True)
        e.add_field(name="Comm. Ban", value=f"{badge(bans['CommunityBanned'])} {'Yes' if bans['CommunityBanned'] else 'No'}", inline=True)
        e.add_field(name="Trade Ban", value=bans["EconomyBan"].capitalize(), inline=True)
        if eac is not None:
            e.add_field(name="EAC Ban", value=f"{badge(eac)} {'Yes' if eac else 'No'}", inline=True)

        if bm_prof:
            bm_url = f"https://www.battlemetrics.com/rcon/players/{bm_prof['id']}"
            txt = f"[Profile]({bm_url})"
            if bm_bans:
                for b in bm_bans[:3]:
                    org = b["attributes"].get("organization", {}).get("name") or "Org"
                    reason = b["attributes"].get("reason") or "No reason"
                    date = (b["attributes"].get("timestamp") or "")[:10]
                    txt += f"\n🔴 {org}: {reason} ({date})"
                if len(bm_bans) > 3:
                    txt += f"\n…and {len(bm_bans)-3} more"
            else:
                txt += "\n🟢 No BM bans"
            e.add_field(name="BattleMetrics", value=txt, inline=False)

        if rb_status:
            e.add_field(name="RustBans", value=f"🔴 {rb_status}: {rb_reason or 'No reason'} ({rb_date})", inline=True)
        else:
            e.add_field(name="RustBans", value="🟢 None", inline=True)

        if sr_status:
            e.add_field(name="SteamRep", value=f"⚠️ {sr_status}", inline=True)
        else:
            e.add_field(name="SteamRep", value="🟢 Clean", inline=True)

        if names:
            e.add_field(name="Prev. names", value="\n".join(names[:10]), inline=False)
        if patterns:
            e.add_field(name="Suspicious patterns", value="\n".join(patterns[:5]), inline=False)
        if comments:
            e.add_field(name="Profile comments", value="\n".join(comments[:5]), inline=False)

        links = [
            f"[Steam]({prof.get('profileurl')})",
            f"[BattleMetrics](https://www.battlemetrics.com/rcon/players/{bm_prof['id']})" if bm_prof else None,
            f"[RustBans](https://rustbans.com/lookup/{sid})",
            f"[SteamDB](https://steamdb.info/calculator/{sid}/)",
            f"[SteamRep](https://steamrep.com/profiles/{sid})",
        ]
        e.add_field(name="Links", value=" | ".join(l for l in links if l), inline=False)

        if flags:
            glossary = "\n".join(f"{f} — {RISK_FLAG_EXPLANATIONS[f]}" for f in flags if f in RISK_FLAG_EXPLANATIONS)
            e.add_field(name="Flag glossary", value=glossary, inline=False)

        await inter.followup.send(embed=e, ephemeral=True)

    @stats.command(name="rust", description="Rust stats")
    @app_commands.describe(steamid="SteamID64 or profile URL")
    async def rust_stats(self, inter: discord.Interaction, steamid: str):
        await inter.response.defer(ephemeral=True)
        sid = await self._resolve(steamid)
        if not sid:
            return await inter.followup.send("Unable to resolve SteamID.", ephemeral=True)
        total_h, two_w_h, last_play, profile = await self._playtime_and_persona(sid)
        unlocked, total_ach, ach_pct = await self._achievements(sid)
        stats_ok, st = await self._rust_stats(sid)
        bm_prof, *_ = await self._bm_info(sid)
        bm_online = bm_srv = None
        if bm_prof:
            _, bm_online, bm_srv, *_ = await self._bm_sessions(bm_prof["id"])
        color = 0xFF7A00
        e = discord.Embed(
            title=f"Rust stats – {profile.get('personaname')}",
            url=profile.get("profileurl"), colour=color
        ).set_footer(text=f"SteamID64: {sid}")
        if profile.get("avatarfull"):
            e.set_thumbnail(url=profile["avatarfull"])
        fmt = lambda n: f"{n:,}" if n else "N/A"
        e.add_field(name="Hours", value=f"Total {fmt(total_h)}\n2 wks {fmt(two_w_h)}", inline=True)
        e.add_field(name="Last played", value=last_play, inline=True)
        e.add_field(name="Achievements", value=f"{fmt(unlocked)}/{fmt(total_ach)} ({ach_pct})", inline=True)
        if bm_prof:
            e.add_field(name="BM online", value="Yes" if bm_online else "No", inline=True)
            if bm_online and bm_srv:
                e.add_field(name="BM server", value=bm_srv, inline=True)
        if not stats_ok:
            e.add_field(name="Detailed stats", value="Private / unavailable")
            return await inter.followup.send(embed=e, ephemeral=True)
        kills, deaths = st["kill_player"], st["death_player"]
        kd = f"{kills/deaths:.2f}" if deaths else ("∞" if kills else "N/A")
        e.add_field(name="K/D", value=kd, inline=True)
        await inter.followup.send(embed=e, ephemeral=True)

    async def _resolve(self, raw: str):
        if raw.isdigit() and len(raw) >= 16:
            return raw
        m = PROFILE_RE.search(raw)
        if not m:
            return None
        vanity = m.group(1)
        if vanity.isdigit():
            return vanity
        url = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/" \
              f"?key={STEAM_API_KEY}&vanityurl={vanity}"
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
        eac = any("eac" in (f or "").lower() for f in flags)
        names = [n.get("name", "Unknown") for n in prof["attributes"].get("names", [])[::-1]]
        return prof, bans, eac, names

    async def _bm_sessions(self, pid: str):
        url = "https://api.battlemetrics.com/sessions?" \
              f"filter[player]={pid}&page[size]=100&include=server&sort=-start"
        async with aiohttp.ClientSession() as ses, ses.get(url, headers=BM_HEADERS) as r:
            data = await r.json()
        sess = data.get("data", [])
        srv_name = {i["id"]: i["attributes"]["name"] for i in data.get("included", []) if i["type"] == "server"}
        online = False; current = None
        if sess and sess[0]["attributes"]["end"] is None:
            online = True
            sid = sess[0]["relationships"]["server"]["data"]["id"]
            current = srv_name.get(sid, "Unknown")
        return sess, online, current, len(sess), []

    async def _playtime_and_persona(self, sid: str):
        async with aiohttp.ClientSession() as ses:
            url1 = f"https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?key={STEAM_API_KEY}&steamid={sid}"
            async with ses.get(url1) as r:
                og = await r.json()
        g = next((x for x in og.get("response", {}).get("games", []) if x["appid"] == APPID_RUST), None)
        total_h = g["playtime_forever"] // 60 if g else 0
        two_w_h = g.get("playtime_2weeks", 0) // 60 if g else 0
        async with aiohttp.ClientSession() as ses:
            url2 = f"https://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v1/?key={STEAM_API_KEY}&steamid={sid}"
            async with ses.get(url2) as r:
                rp = await r.json()
        recent = next((x for x in rp.get("response", {}).get("games", []) if x["appid"] == APPID_RUST), None)
        last_play = dt.datetime.utcfromtimestamp(recent["playtime_at"]).strftime("%Y-%m-%d") if recent and "playtime_at" in recent else "Unknown"
        async with aiohttp.ClientSession() as ses:
            url3 = f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key={STEAM_API_KEY}&steamids={sid}"
            async with ses.get(url3) as r:
                prof = (await r.json())["response"]["players"][0]
        return total_h, two_w_h, last_play, prof

    async def _achievements(self, sid: str):
        async with aiohttp.ClientSession() as ses:
            url = f"https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v1/?key={STEAM_API_KEY}&steamid={sid}&appid={APPID_RUST}"
            async with ses.get(url) as r:
                ach = await r.json()
        if not ach.get("playerstats", {}).get("success"):
            return "Private", "N/A", "N/A"
        lst = ach["playerstats"]["achievements"]
        unlocked = sum(1 for a in lst if a["achieved"])
        total = len(lst)
        pct = f"{unlocked / total * 100:.1f}%"
        return unlocked, total, pct

    async def _rust_stats(self, sid: str):
        async with aiohttp.ClientSession() as ses:
            url = f"https://api.steampowered.com/ISteamUserStats/GetUserStatsForGame/v2/?key={STEAM_API_KEY}&steamid={sid}&appid={APPID_RUST}"
            async with ses.get(url) as r:
                data = await r.json()
        lst = data.get("playerstats", {}).get("stats")
        if not lst:
            return False, {}
        raw = {s["name"]: s["value"] for s in lst}
        def sum_pref(p): return sum(v for k, v in raw.items() if k.startswith(p))
        def get(*vs, _sum=False, _scale=1):
            if _sum:
                return int(sum_pref(v[:-1]) if vs[0].endswith("*") else sum(raw.get(v, 0) for v in vs) / _scale)
            for v in vs:
                val = sum_pref(v[:-1]) if v.endswith("*") else raw.get(v, 0)
                if val:
                    return int(val / _scale)
            return 0
        bullets_fired = get("bullet_fired") + get("shotgun_fired")
        bullets_hit = get("bullet_hit_*", "shotgun_hit_*", _sum=True)
        arrows_fired = get("arrow_fired", "arrows_shot")
        arrows_hit = get("arrow_hit_*", _sum=True)
        headshots = get("headshot", "headshots")
        stats = {
            "kill_player": get("kill_player"), "death_player": get("deaths", "death_player"),
            "shots_fired": bullets_fired, "shots_hit": bullets_hit,
            "arrow_fired": arrows_fired, "arrow_hit": arrows_hit, "headshot_hits": headshots,
            "kill_scientist": get("kill_scientist"), "kill_bear": get("kill_bear"),
            "kill_wolf": get("kill_wolf"), "kill_boar": get("kill_boar"),
            "kill_deer": get("kill_stag"), "death_fall": get("death_fall"),
            "death_suicide": get("death_suicide", "death_selfinflicted"),
        }
        return True, stats

    async def _level_games_friends(self, sid: str):
        lvl = games = friends = None
        g_list = []
        async with aiohttp.ClientSession() as ses:
            try:
                async with ses.get(f"https://api.steampowered.com/IPlayerService/GetSteamLevel/v1/?key={STEAM_API_KEY}&steamid={sid}") as r:
                    lvl = (await r.json())["response"].get("player_level")
            except: pass
            try:
                async with ses.get(f"https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?key={STEAM_API_KEY}&steamid={sid}&include_appinfo=1") as r:
                    data = await r.json()
                    games = data["response"].get("game_count")
                    g_list = data["response"].get("games", [])
            except: pass
            try:
                async with ses.get(f"https://api.steampowered.com/ISteamUser/GetFriendList/v1/?key={STEAM_API_KEY}&steamid={sid}") as r:
                    friends = len((await r.json()).get("friendslist", {}).get("friends", []))
            except: pass
        g_list.sort(key=lambda x: x.get("playtime_forever", 0), reverse=True)
        top_games = [{"name": g["name"], "playtime": g["playtime_forever"] // 60} for g in g_list[:5]]
        return lvl, games, friends, top_games

    async def _rust_hours(self, sid: str):
        async with aiohttp.ClientSession() as ses:
            try:
                url = f"https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?key={STEAM_API_KEY}&steamid={sid}"
                async with ses.get(url) as r:
                    og = await r.json()
                for g in og["response"]["games"]:
                    if g["appid"] == 252490:
                        return g["playtime_forever"] // 60, g.get("playtime_2weeks", 0) // 60
            except: pass
        return None, None

    async def _profile_comments(self, sid: str):
        try:
            async with aiohttp.ClientSession() as ses:
                url = f"https://steamcommunity.com/profiles/{sid}/allcomments?xml=1"
                async with ses.get(url) as r:
                    text = await r.text()
            comments = re.findall(r"<comment thread='[^']+'>(.*?)</comment>", text, re.DOTALL)
            return [re.sub("<.*?>", "", c).strip() for c in comments if c.strip()]
        except:
            return []

    async def _rustbans_info(self, sid: str):
        try:
            async with aiohttp.ClientSession() as ses:
                async with ses.get(f"https://rustbans.com/api/v2/ban/{sid}") as r:
                    if r.status == 200:
                        data = await r.json()
                        if data.get("banned"):
                            return "Banned", data.get("reason"), data.get("timestamp", "")[:10]
        except:
            pass
        return None, None, None

    async def _steamrep_info(self, sid: str):
        try:
            async with aiohttp.ClientSession() as ses:
                async with ses.get(f"https://steamrep.com/api/beta4/reputation/{sid}?json=1") as r:
                    if r.status == 200:
                        data = await r.json()
                        if data.get("reputation", {}).get("summary"):
                            return data["reputation"]["summary"]
        except:
            pass
        return None

async def setup(bot: commands.Bot, db=None):
    await bot.add_cog(StatsCog(bot))