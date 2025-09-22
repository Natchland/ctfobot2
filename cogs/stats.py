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
            url=prof.get("profileurl") or None,
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
    async def _rust_stats(self, sid: str):
        """
        Return (ok: bool, stats: dict[str, int])

        • understands every known variant of the counters the bot shows
        • treats names ending in “*” as a prefix-wildcard (bullet_hit_*, …)
        • returns integers only (no decimal places)
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

        # ───────── helper functions ─────────
        def _sum_prefix(prefix: str) -> int:
            return sum(v for k, v in raw.items() if k.startswith(prefix))

        def get(*variants: str, _sum=False, _scale=1):
            """
            · variants with a trailing '*' are treated as prefix-wildcards
            · if _sum=True  ➜ return the sum of ALL variants
            · otherwise     ➜ return first non-zero variant
            """
            if _sum:
                total = 0
                for var in variants:
                    total += _sum_prefix(var[:-1]) if var.endswith("*") else raw.get(var, 0)
                return int(total / _scale)

            for var in variants:
                val = _sum_prefix(var[:-1]) if var.endswith("*") else raw.get(var, 0)
                if val:
                    return int(val / _scale)
            return 0

        # ───────── combat ─────────
        bullets_fired = get("bullet_fired") + get("shotgun_fired")
        bullets_hit   = get("bullet_hit_*", "shotgun_hit_*", _sum=True)
        arrows_fired  = get("arrow_fired")
        arrows_hit    = get("arrow_hit_*", _sum=True)
        headshots     = get("headshots", "headshot")
        kills_player  = get("kill_player")
        deaths_player = get("death_player", "deaths")

        # ───────── animals / misc kills & deaths ─────────
        stats = {
            "kill_scientist": get("kill_scientist"),
            "kill_bear":      get("kill_bear"),
            "kill_wolf":      get("kill_wolf"),
            "kill_boar":      get("kill_boar"),
            "kill_deer":      get("kill_stag"),   # deer = stag in schema
            "kill_horse":     get("kill_horse"),
            "death_suicide":  get("death_suicide"),
            "death_fall":     get("death_fall"),
        }

        # ───────── resources (nodes) ─────────
        stats.update({
            "harvest_wood":  get("harvest.wood",  "harvested_wood"),
            "harvest_stones":get("harvest.stones","harvested_stones"),
            "harvest_metal_ore": get(
                "harvest.metal_ore", "harvest_metal_ore", "acquired_metal.ore", _sum=True
            ),
            "harvest_hq_metal_ore": get(
                "harvest.hq_metal_ore", "harvest_hq_metal_ore",
                "acquired_highqualitymetal.ore", "acquired_hq_metal_ore", _sum=True
            ),
            "harvest_sulfur_ore": get(
                "harvest.sulfur_ore", "harvest_sulfur_ore",
                "acquired_sulfur.ore", _sum=True
            ),
        })

        # ───────── resources (pick-ups) ─────────
        stats.update({
            "acq_lowgrade": get("acquired_lowgradefuel"),
            "acq_scrap":    get("acquired_scrap"),
            "acq_cloth":    get("acquired_cloth",   "acquired_cloth.item"),
            "acq_leather":  get("acquired_leather", "acquired_leather.item"),
        })

        # ───────── building / loot ─────────
        stats.update({
            "build_place":   get("building_blocks_placed",  "buildings_placed",
                                 "structure_built"),
            "build_upgrade": get("building_blocks_upgraded","buildings_upgraded",
                                 "structure_upgrade"),
            "barrels":       get("destroyed_barrel", "destroyed_barrel_town",
                                 "destroyed_barrel_snowball", _sum=True),
            "bps":           get("blueprint_studied", "blueprints_studied"),
            "pipes":         get("pipes_connected", "fluid_links_connected"),
            "wires":         get("wires_connected", "wiretool_connected"),
            "waves":         get("gesture_wave", "friendly_wave"),
        })

        # ───────── horse / consumption / UI ─────────
        stats.update({
            "horse_miles":   get("horse_distance", "horse_distance_travelled",
                                 _sum=True, _scale=1609.344),  # metres → miles
            "horses_ridden": get("horses_ridden", "horse_ridden"),
            "calories":      get("calories_consumed", "calories_consumed_total"),
            "water":         get("water_consumed",    "water_consumed_total"),
            "map_open":      get("map_opened", "opened_map", "map_open"),
            "inv_open":      get("inventory_opened", "opened_inventory",
                                 "inventory_open"),
            "items_crafted": get("items_crafted", "crafted_items"),
        })

        # ───────── core combat numbers ─────────
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

# ═══════════════════ setup loader ══════════════════
async def setup(bot: commands.Bot, db=None):
    await bot.add_cog(StatsCog(bot))