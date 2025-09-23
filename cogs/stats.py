# cogs/stats.py  –  FULL production version  (2024-09-23)
# This file contains every line required by the original cog
# plus the fixes discussed in the support thread.
from __future__ import annotations

# ────────────────────────── stdlib & 3rd-party ──────────────────────────
import os
import json
import re
import datetime as dt
import aiohttp
import cachetools
import discord
from discord import app_commands
from discord.ext import commands

# ════════════════════════════════════════
#               CONFIG
# ════════════════════════════════════════
STEAM_API_KEY = os.getenv("STEAM_API_KEY")            # ← MUST be set
BM_TOKEN      = os.getenv("BATTLEMETRICS_TOKEN", "")
BM_HEADERS    = {"Authorization": f"Bearer {BM_TOKEN}"} if BM_TOKEN else {}

APPID_RUST = 252490

PROFILE_RE   = re.compile(r"https?://steamcommunity\.com/(?:profiles|id)/([^/]+)")
PLAYER_CACHE = cachetools.TTLCache(maxsize=1_000, ttl=300)   # 5-minute cache

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

# ════════════════════════════════════════
#               COG
# ════════════════════════════════════════
class StatsCog(commands.Cog):
    """
    /check …   – risk / ban look-ups  
    /stats …   – Rust statistics
    """

    check = app_commands.Group(name="check",  description="Look-ups & checks")
    stats = app_commands.Group(name="stats",  description="Game statistics")

    def __init__(self, bot: commands.Bot):
        self.bot = bot                # no manual add_command – discord.py does it

    async def _achievements(self, sid: str):
        """
        Return unlocked-count, total-count, percentage-string.
        """
        async with aiohttp.ClientSession() as ses:
            url = ("https://api.steampowered.com/ISteamUserStats/"
                   f"GetPlayerAchievements/v1/?key={STEAM_API_KEY}"
                   f"&steamid={sid}&appid={APPID_RUST}")
            async with ses.get(url) as r:
                data = await r.json()

        ps = data.get("playerstats", {})
        if not ps.get("success"):
            return 0, 0, "N/A"

        lst = ps.get("achievements", [])
        unlocked = sum(1 for a in lst if a["achieved"])
        total    = len(lst)
        pct      = f"{unlocked/total*100:.1f}%"
        return unlocked, total, pct

    async def _playtime_and_persona(self, sid: str):
        """Return total-hrs, 2-wk-hrs, last-played-date, player-summary-dict"""
        async with aiohttp.ClientSession() as ses:
            # total / 2-week hours
            url1 = ("https://api.steampowered.com/IPlayerService/"
                    f"GetOwnedGames/v1/?key={STEAM_API_KEY}&steamid={sid}")
            async with ses.get(url1) as r:
                og = await r.json()
        g = next((x for x in og.get("response", {}).get("games", [])
                  if x["appid"] == APPID_RUST), None)
        total_h = g["playtime_forever"] // 60 if g else 0
        two_w_h = g.get("playtime_2weeks", 0) // 60 if g else 0

        # date last played
        async with aiohttp.ClientSession() as ses:
            url2 = ("https://api.steampowered.com/IPlayerService/"
                    f"GetRecentlyPlayedGames/v1/?key={STEAM_API_KEY}&steamid={sid}")
            async with ses.get(url2) as r:
                rp = await r.json()
        recent = next((x for x in rp.get("response", {}).get("games", [])
                       if x["appid"] == APPID_RUST), None)
        last_play = (dt.datetime.utcfromtimestamp(recent["playtime_at"])
                     .strftime("%Y-%m-%d")
                     if recent and "playtime_at" in recent else "Unknown")

        # persona / avatar / profile-url
        async with aiohttp.ClientSession() as ses:
            url3 = ("https://api.steampowered.com/ISteamUser/"
                    f"GetPlayerSummaries/v2/?key={STEAM_API_KEY}&steamids={sid}")
            async with ses.get(url3) as r:
                prof = (await r.json())["response"]["players"][0]

        return total_h, two_w_h, last_play, prof

    # ────────────────────────────────
    #   /check help
    # ────────────────────────────────
    @check.command(name="help", description="Explain risk flags")
    async def check_help(self, inter: discord.Interaction):
        txt = "\n".join(f"{k} — {v}" for k, v in RISK_FLAG_EXPLANATIONS.items())
        await inter.response.send_message(txt, ephemeral=True)

    # ────────────────────────────────
    #   /check player
    # ────────────────────────────────
    @check.command(name="player", description="Steam profile & ban check")
    @app_commands.describe(steamid="SteamID64 or profile URL")
    async def check_player(self, inter: discord.Interaction, steamid: str):

        if not STEAM_API_KEY:
            return await inter.response.send_message(
                "Steam API key is not configured on this bot.", ephemeral=True
            )

        await inter.response.defer(ephemeral=True)

        # ───── SteamID resolution ─────
        sid = await self._resolve(steamid)
        if not sid:
            return await inter.followup.send(
                "Unable to resolve SteamID.", ephemeral=True
            )

        # ───── fetch (with 5-min cache) ─────
        if sid in PLAYER_CACHE:
            (bans, prof, lvl, game_cnt, friend_cnt, top_games,
             bm_prof, bm_bans, eac, names,
             rb_status, rb_reason, rb_date,
             sr_status, rust_h, two_w_h,
             comments, patterns) = PLAYER_CACHE[sid]
        else:
            bans, prof = await self._steam_bans_and_profile(sid)
            lvl, game_cnt, friend_cnt, top_games = \
                await self._level_games_friends(sid)
            bm_prof, bm_bans, eac, names = await self._bm_info(sid)
            rb_status, rb_reason, rb_date = await self._rustbans_info(sid)
            sr_status = await self._steamrep_info(sid)
            rust_h, two_w_h = await self._rust_hours(sid)
            comments = await self._profile_comments(sid)
            patterns = [n for n in names
                        if re.search(r"(alt|smurf|rust|\d{5,})", n, re.I)]
            PLAYER_CACHE[sid] = (bans, prof, lvl, game_cnt, friend_cnt,
                                 top_games, bm_prof, bm_bans, eac, names,
                                 rb_status, rb_reason, rb_date,
                                 sr_status, rust_h, two_w_h,
                                 comments, patterns)

        # ───── risk / flag analysis ─────
        now = dt.datetime.utcnow()
        created = (dt.datetime.utcfromtimestamp(prof.get("timecreated", 0))
                   if prof.get("timecreated") else None)
        age = (now - created).days if created else None

        total_bans   = (bans.get("NumberOfVACBans", 0) or 0) + \
                       (bans.get("NumberOfGameBans", 0) or 0)
        has_any_ban  = bans.get("VACBanned") or total_bans

        recent_ban   = has_any_ban and bans.get("DaysSinceLastBan", 9999) <= 90
        very_recent  = has_any_ban and bans.get("DaysSinceLastBan", 9999) <= 14
        low_lvl      = lvl is not None and lvl < 6
        low_games    = game_cnt is not None and game_cnt < 3
        rust_only    = (game_cnt is not None and game_cnt <= 2
                        and top_games and top_games[0]["name"].lower() == "rust")
        low_friends  = friend_cnt is not None and friend_cnt < 3
        private      = prof.get("communityvisibilitystate", 3) != 3
        default_av   = prof.get("avatarfull", "").endswith("/avatar.jpg")
        many_names   = len(names) >= 3
        multi_bans   = total_bans > 1
        suspicious_name = bool(patterns)
        fast_rust    = (rust_h is not None and age is not None
                        and rust_h > 100 and age < 30)

        flags: list[str] = []
        if private:         flags.append("🔒 Private profile")
        if default_av:      flags.append("👤 Default avatar")
        if age is not None and age < 30: flags.append("🆕 New account")
        if low_lvl:         flags.append("⬇️ Low Steam level")
        if low_games:       flags.append("🎮 Few games")
        if low_friends:     flags.append("👥 Few friends")
        if very_recent:     flags.append("⚠️ Very recent ban")
        elif recent_ban:    flags.append("⚠️ Recent ban")
        if multi_bans:      flags.append("⚠️ Multiple bans")
        if bm_bans:         flags.append("🔴 BattleMetrics ban")
        if eac:             flags.append("🔴 EAC ban")
        if rb_status:       flags.append("🔴 RustBans ban")
        if sr_status:       flags.append("⚠️ SteamRep flagged")
        if many_names:      flags.append("✏️ Frequent name changes")
        if suspicious_name: flags.append("🕵️‍♂️ Suspicious name")
        if rust_only:       flags.append("🕹️ Rust-only account")
        if fast_rust:       flags.append("⏳ High Rust hrs (fast)")

        # ───── numeric score ─────
        score = 0
        if private:                        score += 2
        if default_av:                     score += 1
        if age is not None:
            score += 5 if age < 7 else 3 if age < 30 else 0
        if low_lvl:                        score += 2
        if lvl and lvl > 50:               score -= 2
        if low_games:                      score += 3
        if game_cnt and game_cnt > 100:    score -= 2
        if low_friends:                    score += 2
        if friend_cnt and friend_cnt > 100:score -= 1
        if very_recent:                    score += 8
        elif recent_ban:                   score += 5
        if multi_bans:                     score += 3
        if bm_bans:                        score += 5
        if eac:                            score += 5
        if rb_status:                      score += 5
        if sr_status:                      score += 5
        if many_names:                     score += 1
        if suspicious_name:                score += 2
        if rust_only:                      score += 2
        if fast_rust:                      score += 2
        score = max(score, 0)

        risk, colour = (
            ("🔴  HIGH RISK",     discord.Color.red())     if score >= 12 else
            ("🟠  MODERATE RISK", discord.Color.orange())  if score >= 5  else
            ("🟢  LOW RISK",      discord.Color.green())
        )

        # ───── embed skeleton ─────
        e = discord.Embed(
                title=prof.get("personaname", "Unknown"),
                url=prof.get("profileurl"),
                colour=colour,
                description=f"{risk}\n\n{' '.join(flags) or 'No immediate risk factors.'}"
            ).set_footer(text=f"SteamID64: {sid}  |  Score: {score}")
        if prof.get("avatarfull"):
            e.set_thumbnail(url=prof["avatarfull"])

        fmt = lambda n: f"{n:,}" if n is not None else "N/A"

        # ───── neat blocks ─────
        # Account block
        account_block = "\n".join([
            f"Created : {created.strftime('%Y-%m-%d') if created else 'N/A'}",
            f"Age     : {age} d" if age is not None else "Age     : N/A",
            f"Level   : {fmt(lvl)}",
            f"Games   : {fmt(game_cnt)}",
            ("Friends : " +
             ("Private" if friend_cnt is None else fmt(friend_cnt))),
            f"Status  : {'Private' if private else 'Public'}",
        ])
        e.add_field(name="Account", value=f"```ini\n{account_block}\n```",
                    inline=False)

        # Activity block
        activity_block = "\n".join([
            f"Rust hours  : {fmt(rust_h)}",
            f"2-weeks hrs : {fmt(two_w_h)}",
        ])
        e.add_field(name="Activity", value=f"```ini\n{activity_block}\n```",
                    inline=False)

        # Top games (already hours)
        if top_games:
            tg_list = "\n".join(
                f"{g['name'][:25]:25}  {g['playtime']:>6,} h"
                for g in top_games[:5]
            )
            e.add_field(name="Top games (hours)",
                        value=f"```ini\n{tg_list}\n```",
                        inline=False)

        # Bans / reputation
        ban_lines = [
            f"VAC             : {'Yes' if bans['VACBanned'] else 'No'} "
            f"({bans['NumberOfVACBans']})",
            f"Game bans       : {bans['NumberOfGameBans']}",
            f"Comm ban        : {'Yes' if bans['CommunityBanned'] else 'No'}",
            f"Trade ban       : {bans['EconomyBan'].capitalize()}",
        ]
        if eac is not None:
            ban_lines.append(f"EAC ban         : {'Yes' if eac else 'No'}")
        ban_lines.append(
            f"BattleMetrics   : "
            f"{len(bm_bans)} ban(s)" if bm_bans else "BattleMetrics   : None")
        ban_lines.append(
            f"RustBans        : {rb_status or 'None'}")
        ban_lines.append(
            f"SteamRep        : {sr_status or 'Clean'}")
        e.add_field(name="Bans / reputation",
                    value=f"```ini\n" + "\n".join(ban_lines) + "\n```",
                    inline=False)

        # BattleMetrics details (if any bans) – separate block to avoid clutter
        if bm_prof:
            bm_url = f"https://www.battlemetrics.com/rcon/players/{bm_prof['id']}"
            if bm_bans:
                bm_text = f"[Profile]({bm_url}) — **{len(bm_bans)} ban(s)**\n"
                for b in bm_bans[:3]:
                    org    = (b['attributes'].get('organization', {})
                              .get('name') or 'Org')
                    reason = b['attributes'].get('reason') or 'No reason'
                    date   = (b['attributes'].get('timestamp') or '')[:10]
                    bm_text += f"• {org}: {reason} ({date})\n"
                if len(bm_bans) > 3:
                    bm_text += f"…and {len(bm_bans)-3} more"
            else:
                bm_text = f"[Profile]({bm_url}) — no bans"
            e.add_field(name="BattleMetrics details", value=bm_text, inline=False)

        # Previous names / comments
        if names:
            e.add_field(name="Previous names",
                        value="\n".join(names[:10]), inline=False)
        if comments:
            e.add_field(name="Profile comments",
                        value="\n".join(comments[:5]), inline=False)

        # Glossary (only for flags present)
        if flags:
            glossary = "\n".join(f"{f} — {RISK_FLAG_EXPLANATIONS[f]}"
                                 for f in flags)
            e.add_field(name="Flag glossary", value=glossary, inline=False)

        # Links
        links = [
            f"[Steam]({prof.get('profileurl')})",
            (f"[BattleMetrics](https://www.battlemetrics.com/rcon/players/"
             f"{bm_prof['id']})" if bm_prof else None),
            f"[RustBans](https://rustbans.com/lookup/{sid})",
            f"[SteamDB](https://steamdb.info/calculator/{sid}/)",
            f"[SteamRep](https://steamrep.com/profiles/{sid})",
        ]
        e.add_field(name="Links",
                    value=" | ".join(l for l in links if l),
                    inline=False)

        await inter.followup.send(embed=e, ephemeral=True)

    # ────────────────────────────────
    #   /dump raw stats
    # ────────────────────────────────
    @app_commands.command(name="dump_rust_raw", description="DM-dump raw stats with hours")
    @app_commands.describe(steamid="SteamID64 or profile URL")
    async def dump_rust_raw(self, inter: discord.Interaction, steamid: str):
        await inter.response.defer(ephemeral=True)
        sid = await self._resolve(steamid)
        if not sid:
            return await inter.followup.send("SteamID could not be resolved.", ephemeral=True)
        ok, raw = await self._rust_stats(sid)
        if not ok:
            return await inter.followup.send("Stats private / unavailable.", ephemeral=True)
        # Fetch total Rust hours (lifetime)
        tot_h, *_ = await self._rust_hours(sid)
        if tot_h is not None:
            raw["_hours"] = tot_h
        await inter.followup.send(
            "Copy & save this JSON for baseline analysis:\n"
            f"```json\n{json.dumps(raw, indent=2)}```",
            ephemeral=True
        )

    # ────────────────────────────────
    #   /stats rust
    # ────────────────────────────────
    @stats.command(name="rust", description="Rust hours & detailed, tidy embed")
    @app_commands.describe(steamid="SteamID64 or profile URL")
    async def rust_stats(self, inter: discord.Interaction, steamid: str):

        if not STEAM_API_KEY:
            return await inter.response.send_message(
                "Steam API key not configured on this bot.", ephemeral=True
            )

        await inter.response.defer(ephemeral=True)

        sid = await self._resolve(steamid)
        if not sid:
            return await inter.followup.send(
                "Unable to resolve SteamID.", ephemeral=True
            )

        # ───── top-level data ─────
        tot_h, twk_h, last_play, profile = \
            await self._playtime_and_persona(sid)
        ach_unl, ach_tot, ach_pct = await self._achievements(sid)

        # presence
        pres_steam = "Yes" if profile.get("gameid") == str(APPID_RUST) else "No"

        bm_prof, *_ = await self._bm_info(sid)
        bm_online = bm_sessions = "N/A"
        if bm_prof:
            _, online, _, sessions, _ = await self._bm_sessions(bm_prof["id"])
            bm_online   = "Yes" if online else "No"
            bm_sessions = sessions

        # ───── raw stats ─────
        ok, st = await self._rust_stats(sid)
        if not ok:
            return await inter.followup.send(
                "Detailed stats are private / unavailable.", ephemeral=True
            )

        # ───── derived numbers ─────
        fmt = lambda n: "N/A" if n in (None, 0, "N/A") else f"{n:,}"

        bullets_fired = st["shots_fired"]
        bullets_hit   = st["shots_hit"]
        arrows_fired  = st["arrow_fired"]
        arrows_hit    = st["arrow_hit"]

        bullet_acc = f"{bullets_hit / bullets_fired * 100:4.1f}%" \
                    if bullets_fired else "0 %"
        arrow_acc  = f"{arrows_hit / arrows_fired * 100:4.1f}%" \
                    if arrows_fired else "0 %"
        head_acc   = (f"{st['headshot_hits']/bullets_hit*100:4.1f}%"
                    if bullets_hit else "0 %")

        kills   = st["kill_player"]
        deaths  = st["death_player"]
        kd      = f"{kills/deaths:.2f}" if deaths else ("∞" if kills else "0")

        # ───── embed ─────
        colour = 0x2F3136  # dark-grey Discord BG
        e = (
            discord.Embed(
                title=f"Rust stats – [{profile.get('personaname')}]",
                url=profile.get("profileurl"),
                colour=colour
            )
            .set_footer(text=f"SteamID64: {sid}")
        )
        if profile.get("avatarfull"):
            e.set_thumbnail(url=profile["avatarfull"])

        # ───── neat blocks ─────
        summary = "\n".join([
            f"Total hrs  : {fmt(tot_h)}",
            f"2-wks hrs  : {fmt(twk_h)}",
            f"Last played: {last_play}",
            f"Achievement: {ach_unl}/{ach_tot} ({ach_pct})",
            f"Steam pres.: {pres_steam}",
            f"BM pres.   : {bm_online}",
            f"BM sessions: {fmt(bm_sessions)}",
        ])
        e.add_field(name="Summary",
                    value=f"```ini\n{summary}\n```",
                    inline=False)

        pvp = "\n".join([
            f"Kills  : {fmt(kills)}",
            f"Deaths : {fmt(deaths)}  (K/D {kd})",
            f"Bullets: {fmt(bullets_hit)} / {fmt(bullets_fired)}  ({bullet_acc})",
            f"Head-shot acc.: {head_acc}",
            f"Arrows : {fmt(arrows_hit)} / {fmt(arrows_fired)}  ({arrow_acc})",
        ])
        e.add_field(name="PvP",
                    value=f"```ini\n{pvp}\n```",
                    inline=False)

        kills_pve = "\n".join([
            f"Scientists:  {fmt(st['kill_scientist'])}",
            f"Bears     :  {fmt(st['kill_bear'])}",
            f"Wolves    :  {fmt(st['kill_wolf'])}",
            f"Boars     :  {fmt(st['kill_boar'])}",
            f"Deer      :  {fmt(st['kill_deer'])}",
            f"Horses    :  {fmt(st['kill_horse'])}",
        ])
        deaths_misc = "\n".join([
            f"Suicides:  {fmt(st['death_suicide'])}",
            f"Falling :  {fmt(st['death_fall'])}",
        ])
        e.add_field(name="PvE kills",
                    value=f"```ini\n{kills_pve}\n```",
                    inline=True)
        e.add_field(name="Other deaths",
                    value=f"```ini\n{deaths_misc}\n```",
                    inline=True)
        e.add_field(name="\u200b", value="\u200b", inline=False)  # spacer

        nodes = "\n".join([
            f"Wood      : {fmt(st['harvest_wood'])}",
            f"Stone     : {fmt(st['harvest_stones'])}",
            f"Metal ore : {fmt(st['harvest_metal_ore'])}",
            f"HQ ore    : {fmt(st['harvest_hq_metal_ore'])}",
            f"Sulfur ore: {fmt(st['harvest_sulfur_ore'])}",
        ])
        pickups = "\n".join([
            f"Low-grade: {fmt(st['acq_lowgrade'])}",
            f"Scrap    : {fmt(st['acq_scrap'])}",
            f"Cloth    : {fmt(st['acq_cloth'])}",
            f"Leather  : {fmt(st['acq_leather'])}",
        ])
        build_loot = "\n".join([
            f"Blocks placed : {fmt(st['build_place'])}",
            f"Blocks upgrade: {fmt(st['build_upgrade'])}",
            f"Barrels broken: {fmt(st['barrels'])}",
            f"BPs learned   : {fmt(st['bps'])}",
        ])
        e.add_field(name="Resources (nodes)",
                    value=f"```ini\n{nodes}\n```",
                    inline=True)
        e.add_field(name="Resources (pick-ups)",
                    value=f"```ini\n{pickups}\n```",
                    inline=True)
        e.add_field(name="Building / Loot",
                    value=f"```ini\n{build_loot}\n```",
                    inline=True)
        e.add_field(name="\u200b", value="\u200b", inline=False)

        social = "\n".join([
            f"Wires conn.: {fmt(st['wires'])}",
            f"Pipes conn.: {fmt(st['pipes'])}",
            f"Friendly waves: {fmt(st['waves'])}",
        ])
        horses = "\n".join([
            f"Miles ridden : {fmt(st['horse_miles'])}",
            f"Horses ridden: {fmt(st['horses_ridden'])}",
        ])
        consum = "\n".join([
            f"Calories : {fmt(st['calories'])}",
            f"Water    : {fmt(st['water'])}",
            f"Map opens: {fmt(st['map_open'])}",
            f"Inv opens: {fmt(st['inv_open'])}",
            f"Crafted  : {fmt(st['items_crafted'])}",
        ])
        e.add_field(name="Electric / Social",
                    value=f"```ini\n{social}\n```",
                    inline=True)
        e.add_field(name="Horses",
                    value=f"```ini\n{horses}\n```",
                    inline=True)
        e.add_field(name="Consumption / UI",
                    value=f"```ini\n{consum}\n```",
                    inline=True)

        await inter.followup.send(embed=e, ephemeral=True)

    # ═══════════════════════ helper methods ════════════════════════
    async def _resolve(self, raw: str):
        if raw.isdigit() and len(raw) >= 16:
            return raw
        m = PROFILE_RE.search(raw)
        if not m:
            return None
        vanity = m.group(1)
        if vanity.isdigit():
            return vanity
        url = ("https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
               f"?key={STEAM_API_KEY}&vanityurl={vanity}")
        async with aiohttp.ClientSession() as s, s.get(url) as r:
            data = await r.json()
        return data["response"].get("steamid")

    async def _steam_bans_and_profile(self, sid: str):
        async with aiohttp.ClientSession() as ses:
            url_b = ("https://api.steampowered.com/ISteamUser/GetPlayerBans/v1/"
                     f"?key={STEAM_API_KEY}&steamids={sid}")
            url_p = ("https://api.steampowered.com/ISteamUser/"
                     f"GetPlayerSummaries/v2/?key={STEAM_API_KEY}&steamids={sid}")
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
            url = (f"https://api.battlemetrics.com/bans?"
                   f"filter[player]={pid}&sort=-timestamp")
            async with ses.get(url, headers=BM_HEADERS) as r:
                bans = (await r.json()).get("data", [])
        flags = prof["attributes"].get("flags", [])
        eac   = any("eac" in (f or "").lower() for f in flags)
        names = [n.get("name", "Unknown")
                 for n in prof["attributes"].get("names", [])[::-1]]
        return prof, bans, eac, names

    async def _bm_sessions(self, pid: str):
        url = ("https://api.battlemetrics.com/sessions?"
               f"filter[player]={pid}&page[size]=100&include=server&sort=-start")
        async with aiohttp.ClientSession() as ses, ses.get(url, headers=BM_HEADERS) as r:
            data = await r.json()
        sess = data.get("data", [])
        srv_name = {i["id"]: i["attributes"]["name"]
                    for i in data.get("included", [])
                    if i["type"] == "server"}
        online = False
        current = None
        if sess and sess[0]["attributes"]["end"] is None:
            online = True
            sid = sess[0]["relationships"]["server"]["data"]["id"]
            current = srv_name.get(sid, "Unknown")
        return sess, online, current, len(sess), []

    async def _level_games_friends(self, sid: str):
        lvl = games = friends = None
        g_list = []
        async with aiohttp.ClientSession() as ses:
            try:
                async with ses.get(
                    "https://api.steampowered.com/IPlayerService/"
                    f"GetSteamLevel/v1/?key={STEAM_API_KEY}&steamid={sid}"
                ) as r:
                    lvl = (await r.json())["response"].get("player_level")
            except: pass
            try:
                async with ses.get(
                    "https://api.steampowered.com/IPlayerService/"
                    f"GetOwnedGames/v1/?key={STEAM_API_KEY}&steamid={sid}"
                    "&include_appinfo=1"
                ) as r:
                    data = await r.json()
                    games  = data["response"].get("game_count")
                    g_list = data["response"].get("games", [])
            except: pass
            try:
                async with ses.get(
                    "https://api.steampowered.com/ISteamUser/"
                    f"GetFriendList/v1/?key={STEAM_API_KEY}&steamid={sid}"
                ) as r:
                    friends = len((await r.json())
                                  .get("friendslist", {}).get("friends", []))
            except: pass
        g_list.sort(key=lambda x: x.get("playtime_forever", 0), reverse=True)
        top_games = [{"name": g["name"],
                      "playtime": g["playtime_forever"] // 60}
                     for g in g_list[:5]]
        return lvl, games, friends, top_games

    async def _rust_hours(self, sid: str):
        async with aiohttp.ClientSession() as ses:
            try:
                url = ("https://api.steampowered.com/IPlayerService/"
                       f"GetOwnedGames/v1/?key={STEAM_API_KEY}&steamid={sid}")
                async with ses.get(url) as r:
                    og = await r.json()
                for g in og["response"]["games"]:
                    if g["appid"] == APPID_RUST:
                        return (g["playtime_forever"] // 60,
                                g.get("playtime_2weeks", 0) // 60)
            except: pass
        return None, None

    async def _profile_comments(self, sid: str):
        try:
            async with aiohttp.ClientSession() as ses:
                url = f"https://steamcommunity.com/profiles/{sid}/allcomments?xml=1"
                async with ses.get(url) as r:
                    text = await r.text()
            comments = re.findall(
                r"<comment thread='[^']+'>(.*?)</comment>",
                text, re.DOTALL)
            return [re.sub("<.*?>", "", c).strip()
                    for c in comments if c.strip()]
        except: return []

    async def _rustbans_info(self, sid: str):
        try:
            async with aiohttp.ClientSession() as ses:
                async with ses.get(
                    f"https://rustbans.com/api/v2/ban/{sid}"
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        if data.get("banned"):
                            return ("Banned",
                                    data.get("reason"),
                                    data.get("timestamp", "")[:10])
        except: pass
        return None, None, None

    async def _steamrep_info(self, sid: str):
        try:
            async with aiohttp.ClientSession() as ses:
                async with ses.get(
                    f"https://steamrep.com/api/beta4/reputation/{sid}?json=1"
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        if data.get("reputation", {}).get("summary"):
                            return data["reputation"]["summary"]
        except: pass
        return None

    # ════════════════ LONG _rust_stats helper (exactly as supplied) ════════════════
    async def _rust_stats(self, sid: str):
        """
        Return (ok: bool, stats: dict[str,int])
        Implements every rule from the specification you supplied.
        """
        async with aiohttp.ClientSession() as ses:
            url = ("https://api.steampowered.com/ISteamUserStats/"
                   f"GetUserStatsForGame/v2/?key={STEAM_API_KEY}&steamid={sid}&appid={APPID_RUST}")
            async with ses.get(url) as r:
                data = await r.json()

        raw_list = data.get("playerstats", {}).get("stats")
        if not raw_list:
            return False, {}
        raw = {s["name"]: s["value"] for s in raw_list}

        # helpers
        def _sum_prefix(pre: str) -> int:
            return sum(v for k, v in raw.items() if k.startswith(pre))

        def get(*vars: str, _sum=False, _scale=1):
            if _sum:
                tot = 0
                for v in vars:
                    tot += _sum_prefix(v[:-1]) if v.endswith("*") else raw.get(v, 0)
                return int(tot / _scale)
            for v in vars:
                val = _sum_prefix(v[:-1]) if v.endswith("*") else raw.get(v, 0)
                if val:
                    return int(val / _scale)
            return 0

        # combat
        bullets_fired = get("bullet_fired") + get("shotgun_fired")
        bullets_hit   = get("bullet_hit_*", "shotgun_hit_*", _sum=True)
        arrows_fired  = get("arrow_fired", "arrows_shot")
        arrows_hit    = get("arrow_hit_*", _sum=True)
        headshots     = get("headshot", "headshots")
        kills_player  = get("kill_player")
        deaths_player = get("death_player", "deaths")

        stats = {
            "kill_scientist": get("kill_scientist"),
            "kill_bear":      get("kill_bear"),
            "kill_wolf":      get("kill_wolf"),
            "kill_boar":      get("kill_boar"),
            "kill_deer":      get("kill_stag"),
            "kill_horse":     get("horse_mounted_count"),
            "death_suicide":  get("death_suicide", "death_selfinflicted"),
            "death_fall":     get("death_fall"),
        }

        # resources – nodes
        stats.update({
            "harvest_wood":   get("harvested_wood",  "harvest.wood"),
            "harvest_stones": get("harvested_stones","harvest.stones"),
            "harvest_metal_ore": get("acquired_metal.ore",
                                      "harvest.metal_ore", _sum=True),
            "harvest_hq_metal_ore": 0,
            "harvest_sulfur_ore":    0,
        })

        # resources – pick-ups
        stats.update({
            "acq_lowgrade": get("acquired_lowgradefuel"),
            "acq_scrap":    get("acquired_scrap"),
            "acq_cloth":    get("harvested_cloth","acquired_cloth","acquired_cloth.item"),
            "acq_leather":  get("harvested_leather","acquired_leather","acquired_leather.item"),
        })

        # building / loot / social
        stats.update({
            "build_place":   get("placed_blocks","building_blocks_placed",
                                 "buildings_placed","structure_built"),
            "build_upgrade": get("upgraded_blocks","building_blocks_upgraded",
                                 "buildings_upgraded","structure_upgrade"),
            "barrels":       get("destroyed_barrels","destroyed_barrel*", _sum=True),
            "bps":           get("blueprint_studied"),
            "pipes":         get("pipes_connected"),
            "wires":         get("wires_connected","tincanalarms_wired"),
            "waves":         get("gesture_wave_count","waved_at_players","gesture_wave"),
        })

        metres = get("horse_distance_ridden", _sum=True)
        km     = get("horse_distance_ridden_km")
        miles  = (metres / 1609.344) if metres else (km * 0.621371)
        stats.update({
            "horse_miles":   int(miles),
            "horses_ridden": get("horse_mounted_count"),
            "calories":      get("calories_consumed"),
            "water":         get("water_consumed"),
            "map_open":      get("MAP_OPENED","map_opened","map_open"),
            "inv_open":      get("INVENTORY_OPENED","inventory_opened"),
            "items_crafted": get("CRAFTING_OPENED","items_crafted","crafted_items"),
        })

        # core combat numbers
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

# ════════════════════════════════════════
#            public entry-point
# ════════════════════════════════════════
async def setup(bot: commands.Bot, db=None):
    await bot.add_cog(StatsCog(bot))