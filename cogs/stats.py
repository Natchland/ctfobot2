# cogs/stats.py  –  FULL production version  (2024-09-23)
# This file contains every line required by the original cog
# plus the fixes discussed in the support thread.
from __future__ import annotations

# ────────────────────────── stdlib & 3rd-party ──────────────────────────
import os, re, json, statistics, datetime
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

# ────────────────────────────────────────────────────────────────
### BASELINE SECTION – put this once near the top of your cog
# ────────────────────────────────────────────────────────────────

RAW_SAMPLES: list[dict] = [
    # 1
    {"kill_scientist":354,"kill_bear":55,"kill_wolf":39,"kill_boar":83,"kill_deer":12,"kill_horse":65,"death_suicide":682,"death_fall":24,"harvest_wood":2362390,"harvest_stones":1403171,"harvest_metal_ore":2964557,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":197998,"acq_scrap":282686,"acq_cloth":17461,"acq_leather":7734,"build_place":37247,"build_upgrade":10749,"barrels":26798,"bps":1070,"pipes":1980,"wires":762,"waves":213,"horse_miles":86,"horses_ridden":65,"calories":914399,"water":276831,"map_open":189793,"inv_open":443857,"items_crafted":22261,"shots_fired":609332,"shots_hit":253134,"arrow_fired":5637,"arrow_hit":2870,"headshot_hits":13704,"kill_player":3178,"death_player":3436,"_hours":3511},
    # 2
    {"kill_scientist":177,"kill_bear":26,"kill_wolf":17,"kill_boar":29,"kill_deer":5,"kill_horse":66,"death_suicide":186,"death_fall":10,"harvest_wood":609213,"harvest_stones":723570,"harvest_metal_ore":1054506,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":32355,"acq_scrap":59674,"acq_cloth":14140,"acq_leather":2432,"build_place":42145,"build_upgrade":17988,"barrels":14600,"bps":886,"pipes":322,"wires":826,"waves":34,"horse_miles":16,"horses_ridden":66,"calories":241643,"water":75164,"map_open":40893,"inv_open":139486,"items_crafted":6758,"shots_fired":7692,"shots_hit":4204,"arrow_fired":983,"arrow_hit":477,"headshot_hits":743,"kill_player":511,"death_player":1115,"_hours":1361},
    # 3
    {"kill_scientist":150,"kill_bear":4,"kill_wolf":11,"kill_boar":24,"kill_deer":7,"kill_horse":4,"death_suicide":112,"death_fall":1,"harvest_wood":89169,"harvest_stones":76421,"harvest_metal_ore":507135,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":10907,"acq_scrap":13242,"acq_cloth":2684,"acq_leather":1141,"build_place":3041,"build_upgrade":2730,"barrels":4066,"bps":267,"pipes":0,"wires":15,"waves":0,"horse_miles":3,"horses_ridden":4,"calories":138684,"water":40459,"map_open":8922,"inv_open":48483,"items_crafted":2389,"shots_fired":42379,"shots_hit":19476,"arrow_fired":988,"arrow_hit":533,"headshot_hits":2057,"kill_player":470,"death_player":443,"_hours":407},
    # 4
    {"kill_scientist":7274,"kill_bear":554,"kill_wolf":288,"kill_boar":1010,"kill_deer":63,"kill_horse":231,"death_suicide":2799,"death_fall":201,"harvest_wood":559046,"harvest_stones":560009,"harvest_metal_ore":6190220,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":189473,"acq_scrap":217267,"acq_cloth":22015,"acq_leather":4009,"build_place":213313,"build_upgrade":73828,"barrels":46328,"bps":3881,"pipes":656,"wires":1772,"waves":513,"horse_miles":1655,"horses_ridden":231,"calories":1322232,"water":384341,"map_open":78908,"inv_open":635818,"items_crafted":30236,"shots_fired":244968,"shots_hit":117231,"arrow_fired":9018,"arrow_hit":6484,"headshot_hits":19610,"kill_player":12363,"death_player":4277,"_hours":4505},
    # 5
    {"kill_scientist":77,"kill_bear":30,"kill_wolf":23,"kill_boar":60,"kill_deer":44,"kill_horse":118,"death_suicide":328,"death_fall":14,"harvest_wood":571628,"harvest_stones":248131,"harvest_metal_ore":659782,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":34781,"acq_scrap":62898,"acq_cloth":13846,"acq_leather":5828,"build_place":2979,"build_upgrade":2754,"barrels":6998,"bps":213,"pipes":0,"wires":39,"waves":5,"horse_miles":36,"horses_ridden":118,"calories":245011,"water":108200,"map_open":29552,"inv_open":107325,"items_crafted":1705,"shots_fired":21212,"shots_hit":12931,"arrow_fired":1830,"arrow_hit":903,"headshot_hits":719,"kill_player":406,"death_player":1357,"_hours":895},
    # 6
    {"kill_scientist":508,"kill_bear":70,"kill_wolf":32,"kill_boar":117,"kill_deer":27,"kill_horse":49,"death_suicide":135,"death_fall":8,"harvest_wood":122734,"harvest_stones":137361,"harvest_metal_ore":1842790,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":43915,"acq_scrap":36475,"acq_cloth":6891,"acq_leather":1203,"build_place":10966,"build_upgrade":9572,"barrels":11272,"bps":1139,"pipes":153,"wires":668,"waves":29,"horse_miles":83,"horses_ridden":49,"calories":191708,"water":52369,"map_open":15614,"inv_open":92130,"items_crafted":6765,"shots_fired":10528,"shots_hit":5882,"arrow_fired":824,"arrow_hit":559,"headshot_hits":1305,"kill_player":753,"death_player":522,"_hours":560},
    # 7
    {"kill_scientist":127,"kill_bear":25,"kill_wolf":32,"kill_boar":51,"kill_deer":2,"kill_horse":74,"death_suicide":267,"death_fall":11,"harvest_wood":392618,"harvest_stones":316605,"harvest_metal_ore":892631,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":28336,"acq_scrap":71849,"acq_cloth":6066,"acq_leather":1883,"build_place":8435,"build_upgrade":6904,"barrels":7956,"bps":771,"pipes":0,"wires":0,"waves":42,"horse_miles":34,"horses_ridden":74,"calories":336570,"water":134404,"map_open":32268,"inv_open":118623,"items_crafted":3116,"shots_fired":57902,"shots_hit":27645,"arrow_fired":2431,"arrow_hit":1341,"headshot_hits":1756,"kill_player":730,"death_player":1093,"_hours":1076},
    # 8
    {"kill_scientist":94,"kill_bear":36,"kill_wolf":23,"kill_boar":85,"kill_deer":31,"kill_horse":111,"death_suicide":239,"death_fall":21,"harvest_wood":299206,"harvest_stones":406888,"harvest_metal_ore":1013881,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":33126,"acq_scrap":39103,"acq_cloth":25362,"acq_leather":5173,"build_place":23754,"build_upgrade":13041,"barrels":7606,"bps":353,"pipes":328,"wires":1398,"waves":222,"horse_miles":22,"horses_ridden":111,"calories":316711,"water":85832,"map_open":28185,"inv_open":160681,"items_crafted":10638,"shots_fired":20496,"shots_hit":10767,"arrow_fired":1098,"arrow_hit":571,"headshot_hits":1421,"kill_player":638,"death_player":1102,"_hours":1005},
    # 9
    {"kill_scientist":1538,"kill_bear":74,"kill_wolf":85,"kill_boar":148,"kill_deer":14,"kill_horse":132,"death_suicide":236,"death_fall":5,"harvest_wood":61552,"harvest_stones":124968,"harvest_metal_ore":2466700,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":28056,"acq_scrap":25135,"acq_cloth":1982,"acq_leather":700,"build_place":18518,"build_upgrade":14925,"barrels":16192,"bps":1333,"pipes":221,"wires":477,"waves":66,"horse_miles":224,"horses_ridden":132,"calories":589583,"water":156402,"map_open":65653,"inv_open":261916,"items_crafted":7612,"shots_fired":39702,"shots_hit":22799,"arrow_fired":3299,"arrow_hit":1844,"headshot_hits":4655,"kill_player":2031,"death_player":1083,"_hours":1383},
    # 10
    {"kill_scientist":2942,"kill_bear":192,"kill_wolf":103,"kill_boar":286,"kill_deer":115,"kill_horse":84,"death_suicide":796,"death_fall":77,"harvest_wood":401773,"harvest_stones":405135,"harvest_metal_ore":5359024,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":328675,"acq_scrap":236977,"acq_cloth":11583,"acq_leather":2603,"build_place":106074,"build_upgrade":45983,"barrels":37434,"bps":4999,"pipes":206,"wires":244,"waves":671,"horse_miles":126,"horses_ridden":84,"calories":1429121,"water":473881,"map_open":155438,"inv_open":638920,"items_crafted":18922,"shots_fired":209520,"shots_hit":127249,"arrow_fired":7372,"arrow_hit":4373,"headshot_hits":12376,"kill_player":7000,"death_player":4515,"_hours":3254},
    # 11
    {"kill_scientist":212,"kill_bear":34,"kill_wolf":37,"kill_boar":92,"kill_deer":15,"kill_horse":33,"death_suicide":184,"death_fall":6,"harvest_wood":2064470,"harvest_stones":899024,"harvest_metal_ore":1011734,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":14565,"acq_scrap":10602,"acq_cloth":41203,"acq_leather":14470,"build_place":1372,"build_upgrade":1576,"barrels":10064,"bps":317,"pipes":0,"wires":14,"waves":10,"horse_miles":29,"horses_ridden":33,"calories":232643,"water":70585,"map_open":23424,"inv_open":113051,"items_crafted":2508,"shots_fired":10883,"shots_hit":5711,"arrow_fired":995,"arrow_hit":652,"headshot_hits":799,"kill_player":689,"death_player":616,"_hours":738},
    # 12
    {"kill_scientist":259,"kill_bear":54,"kill_wolf":61,"kill_boar":136,"kill_deer":74,"kill_horse":70,"death_suicide":320,"death_fall":2,"harvest_wood":195700,"harvest_stones":290142,"harvest_metal_ore":1091375,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":24847,"acq_scrap":21259,"acq_cloth":8975,"acq_leather":2347,"build_place":72973,"build_upgrade":17626,"barrels":10098,"bps":493,"pipes":34,"wires":292,"waves":4,"horse_miles":41,"horses_ridden":70,"calories":266304,"water":117404,"map_open":23023,"inv_open":99896,"items_crafted":6156,"shots_fired":60589,"shots_hit":35985,"arrow_fired":8192,"arrow_hit":3780,"headshot_hits":2227,"kill_player":1145,"death_player":1728,"_hours":980},
    # 13
    {"kill_scientist":80,"kill_bear":7,"kill_wolf":4,"kill_boar":26,"kill_deer":1,"kill_horse":275,"death_suicide":61,"death_fall":1,"harvest_wood":416652,"harvest_stones":356796,"harvest_metal_ore":241345,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":4340,"acq_scrap":3175,"acq_cloth":10406,"acq_leather":2997,"build_place":509,"build_upgrade":605,"barrels":2282,"bps":15,"pipes":0,"wires":0,"waves":3,"horse_miles":7,"horses_ridden":275,"calories":62326,"water":17612,"map_open":3040,"inv_open":26637,"items_crafted":540,"shots_fired":4990,"shots_hit":2850,"arrow_fired":233,"arrow_hit":96,"headshot_hits":272,"kill_player":207,"death_player":290,"_hours":165},
    # 14
    {"kill_scientist":18,"kill_bear":9,"kill_wolf":9,"kill_boar":11,"kill_deer":2,"kill_horse":5,"death_suicide":11,"death_fall":1,"harvest_wood":91199,"harvest_stones":131170,"harvest_metal_ore":204296,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":1979,"acq_scrap":2221,"acq_cloth":2986,"acq_leather":1037,"build_place":609,"build_upgrade":466,"barrels":1732,"bps":20,"pipes":0,"wires":0,"waves":1,"horse_miles":8,"horses_ridden":5,"calories":44906,"water":12002,"map_open":1605,"inv_open":11889,"items_crafted":359,"shots_fired":2129,"shots_hit":1222,"arrow_fired":227,"arrow_hit":102,"headshot_hits":163,"kill_player":80,"death_player":148,"_hours":91},
    # 15
    {"kill_scientist":20,"kill_bear":4,"kill_wolf":3,"kill_boar":18,"kill_deer":3,"kill_horse":182,"death_suicide":69,"death_fall":9,"harvest_wood":103288,"harvest_stones":40823,"harvest_metal_ore":350620,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":14851,"acq_scrap":28422,"acq_cloth":878,"acq_leather":1033,"build_place":2624,"build_upgrade":1683,"barrels":4932,"bps":232,"pipes":1669,"wires":3401,"waves":0,"horse_miles":14,"horses_ridden":182,"calories":83077,"water":24560,"map_open":6929,"inv_open":38328,"items_crafted":2931,"shots_fired":2822,"shots_hit":1919,"arrow_fired":167,"arrow_hit":82,"headshot_hits":72,"kill_player":67,"death_player":262,"_hours":596},
    # 16
    {"kill_scientist":5914,"kill_bear":73,"kill_wolf":56,"kill_boar":261,"kill_deer":29,"kill_horse":2115,"death_suicide":600,"death_fall":11,"harvest_wood":1774328,"harvest_stones":928261,"harvest_metal_ore":312497,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":18345,"acq_scrap":39335,"acq_cloth":58659,"acq_leather":16666,"build_place":40607,"build_upgrade":11907,"barrels":12252,"bps":804,"pipes":63,"wires":149,"waves":25,"horse_miles":280,"horses_ridden":2115,"calories":489067,"water":156255,"map_open":23920,"inv_open":118232,"items_crafted":5484,"shots_fired":122665,"shots_hit":58707,"arrow_fired":7151,"arrow_hit":3577,"headshot_hits":14446,"kill_player":6461,"death_player":1727,"_hours":761},
    # 17
    {"kill_scientist":373,"kill_bear":46,"kill_wolf":26,"kill_boar":59,"kill_deer":8,"kill_horse":266,"death_suicide":145,"death_fall":10,"harvest_wood":328148,"harvest_stones":456950,"harvest_metal_ore":1791943,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":33950,"acq_scrap":30473,"acq_cloth":9211,"acq_leather":4933,"build_place":50198,"build_upgrade":14947,"barrels":5776,"bps":830,"pipes":335,"wires":808,"waves":73,"horse_miles":32,"horses_ridden":266,"calories":264662,"water":90732,"map_open":38546,"inv_open":171193,"items_crafted":6471,"shots_fired":22908,"shots_hit":12084,"arrow_fired":2096,"arrow_hit":1056,"headshot_hits":1675,"kill_player":914,"death_player":1063,"_hours":711},
    # 18
    {"kill_scientist":1503,"kill_bear":164,"kill_wolf":167,"kill_boar":388,"kill_deer":95,"kill_horse":181,"death_suicide":679,"death_fall":22,"harvest_wood":2488693,"harvest_stones":1878775,"harvest_metal_ore":1121361,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":100601,"acq_scrap":71940,"acq_cloth":103517,"acq_leather":38881,"build_place":21072,"build_upgrade":14789,"barrels":31764,"bps":983,"pipes":0,"wires":75,"waves":8,"horse_miles":219,"horses_ridden":181,"calories":1154754,"water":895653,"map_open":85639,"inv_open":357509,"items_crafted":8262,"shots_fired":280702,"shots_hit":113993,"arrow_fired":19525,"arrow_hit":8795,"headshot_hits":13599,"kill_player":4174,"death_player":4826,"_hours":2258},
    # 19
    {"kill_scientist":754,"kill_bear":129,"kill_wolf":107,"kill_boar":215,"kill_deer":81,"kill_horse":105,"death_suicide":1874,"death_fall":32,"harvest_wood":726685,"harvest_stones":325399,"harvest_metal_ore":3182498,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":363139,"acq_scrap":234022,"acq_cloth":25275,"acq_leather":9316,"build_place":70992,"build_upgrade":46432,"barrels":19190,"bps":1692,"pipes":151,"wires":522,"waves":178,"horse_miles":143,"horses_ridden":105,"calories":1852308,"water":595864,"map_open":86312,"inv_open":853846,"items_crafted":30230,"shots_fired":755531,"shots_hit":277956,"arrow_fired":17509,"arrow_hit":8121,"headshot_hits":17674,"kill_player":9430,"death_player":9065,"_hours":4194},
    # 20
    {"kill_scientist":105,"kill_bear":10,"kill_wolf":12,"kill_boar":24,"kill_deer":2,"kill_horse":17,"death_suicide":64,"death_fall":1,"harvest_wood":21972,"harvest_stones":31930,"harvest_metal_ore":142886,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":4556,"acq_scrap":6244,"acq_cloth":1969,"acq_leather":582,"build_place":728,"build_upgrade":724,"barrels":2502,"bps":672,"pipes":0,"wires":12,"waves":0,"horse_miles":6,"horses_ridden":17,"calories":52617,"water":28933,"map_open":3566,"inv_open":19844,"items_crafted":1254,"shots_fired":42886,"shots_hit":16490,"arrow_fired":623,"arrow_hit":274,"headshot_hits":1363,"kill_player":277,"death_player":262,"_hours":252},
    # 21
    {"kill_scientist":2186,"kill_bear":140,"kill_wolf":117,"kill_boar":158,"kill_deer":16,"kill_horse":229,"death_suicide":518,"death_fall":39,"harvest_wood":358730,"harvest_stones":635622,"harvest_metal_ore":2111269,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":171723,"acq_scrap":256875,"acq_cloth":26341,"acq_leather":4391,"build_place":121514,"build_upgrade":63315,"barrels":24652,"bps":2982,"pipes":1,"wires":280,"waves":483,"horse_miles":310,"horses_ridden":229,"calories":1189694,"water":466139,"map_open":103053,"inv_open":504075,"items_crafted":29209,"shots_fired":663120,"shots_hit":259016,"arrow_fired":3547,"arrow_hit":1812,"headshot_hits":22469,"kill_player":7803,"death_player":3118,"_hours":2797},
    # 22
    {"kill_scientist":38,"kill_bear":3,"kill_wolf":12,"kill_boar":18,"kill_deer":0,"kill_horse":124,"death_suicide":76,"death_fall":4,"harvest_wood":669959,"harvest_stones":419266,"harvest_metal_ore":366584,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":6810,"acq_scrap":14132,"acq_cloth":14639,"acq_leather":4296,"build_place":1556,"build_upgrade":1634,"barrels":6310,"bps":168,"pipes":4,"wires":14,"waves":34,"horse_miles":3,"horses_ridden":124,"calories":135077,"water":146203,"map_open":22242,"inv_open":52607,"items_crafted":2925,"shots_fired":9061,"shots_hit":3226,"arrow_fired":352,"arrow_hit":158,"headshot_hits":533,"kill_player":185,"death_player":492,"_hours":328},
    # 23
    {"kill_scientist":90,"kill_bear":13,"kill_wolf":11,"kill_boar":18,"kill_deer":9,"kill_horse":7,"death_suicide":264,"death_fall":9,"harvest_wood":29331,"harvest_stones":109020,"harvest_metal_ore":852785,"harvest_hq_metal_ore":0,"harvest_sulfur_ore":0,"acq_lowgrade":23538,"acq_scrap":23944,"acq_cloth":3789,"acq_leather":254,"build_place":25772,"build_upgrade":8394,"barrels":3588,"bps":70,"pipes":0,"wires":121,"waves":40,"horse_miles":19,"horses_ridden":7,"calories":215783,"water":101466,"map_open":15590,"inv_open":100174,"items_crafted":3460,"shots_fired":57964,"shots_hit":30508,"arrow_fired":2660,"arrow_hit":1552,"headshot_hits":2014,"kill_player":1221,"death_player":1532,"_hours":842}
]

def _build_baseline(samples: list[dict]) -> dict[str, dict]:
    """
    Turn RAW_SAMPLES into {stat: {mean, sd}} (per-hour).
    Keep stats with ≥3 samples and players with ≥10 h.
    """
    per_key: dict[str, list[float]] = {}
    for s in samples:
        hrs = s.get("_hours", 0)
        if hrs < 10:
            continue
        for k, v in s.items():
            if k == "_hours": 
                continue
            per_key.setdefault(k, []).append(v / hrs)

    baseline: dict[str, dict] = {}
    for k, lst in per_key.items():
        if len(lst) < 3:             # need enough samples
            continue
        mu = statistics.mean(lst)
        sd = statistics.stdev(lst) if len(lst) > 1 else 0.001
        baseline[k] = {"mean": mu, "sd": sd}

    baseline["_meta"] = {
        "generated": datetime.datetime.utcnow().isoformat(timespec="seconds"),
        "source_samples": len(samples)
    }
    return baseline

BASELINE: dict = _build_baseline(RAW_SAMPLES)

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
        last_play = (datetime.datetime.utcfromtimestamp(recent["playtime_at"])
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
        now = datetime.datetime.utcnow()
        created = (datetime.datetime.utcfromtimestamp(prof.get("timecreated", 0))
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

    # ────────────────────────────────────────────────────────────────
    ### /stats rust  –  baseline-aware version
    # ────────────────────────────────────────────────────────────────
    @stats.command(name="rust", description="Rust hours & detailed risk assessment")
    @app_commands.describe(steamid="SteamID64 or profile URL")
    async def rust_stats(self, inter: discord.Interaction, steamid: str):

        if not STEAM_API_KEY:
            return await inter.response.send_message(
                "Steam API key not configured on this bot.", ephemeral=True
            )
        await inter.response.defer(ephemeral=True)

        sid = await self._resolve(steamid)
        if not sid:
            return await inter.followup.send("Unable to resolve SteamID.",
                                            ephemeral=True)

        # ─── top-level activity ────────────────────────────────────
        tot_h, twk_h, last_play, profile = await self._playtime_and_persona(sid)
        ach_unl, ach_tot, ach_pct = await self._achievements(sid)

        pres_steam = "Yes" if profile.get("gameid") == str(APPID_RUST) else "No"
        bm_prof, *_ = await self._bm_info(sid)
        bm_online = bm_sessions = "N/A"
        if bm_prof:
            _, online, _, sessions, _ = await self._bm_sessions(bm_prof["id"])
            bm_online   = "Yes" if online else "No"
            bm_sessions = sessions

        # ─── detailed stats ────────────────────────────────────────
        ok, st = await self._rust_stats(sid)
        if not ok:
            return await inter.followup.send(
                "Detailed stats are private / unavailable.", ephemeral=True
            )

        # helpers & derived PvP numbers
        fmt = lambda n: "N/A" if n in (None, 0, "N/A") else f"{n:,}"
        b_fired, b_hit = st["shots_fired"], st["shots_hit"]
        a_fired, a_hit = st["arrow_fired"], st["arrow_hit"]

        bullet_acc = b_hit / b_fired if b_fired else 0
        arrow_acc  = a_hit / a_fired if a_fired else 0
        head_acc   = st["headshot_hits"] / b_hit if b_hit else 0
        kills, deaths = st["kill_player"], st["death_player"]
        kd = kills / deaths if deaths else (999 if kills else 0)

        # ─── risk assessment  (baseline z-scores + PvP caps) ───────
        BIG_Z, MID_Z, MIN_H = 3.5, 2.5, 10
        score, flags = 0, []

        if tot_h and tot_h >= MIN_H:
            for key, val in st.items():
                if key not in BASELINE: 
                    continue
                mu, sd = BASELINE[key]["mean"], BASELINE[key]["sd"]
                if sd < 1e-6: 
                    continue
                z = (val / tot_h - mu) / sd
                if z >= BIG_Z:
                    flags.append(f"🔴 {key} per-h very high (z={z:.1f})")
                    score += 2
                elif z <= -BIG_Z:
                    flags.append(f"🔴 {key} per-h very low (z={z:.1f})")
                    score += 2
                elif z >= MID_Z:
                    flags.append(f"⚠️ {key} per-h high (z={z:.1f})")
                    score += 1
                elif z <= -MID_Z:
                    flags.append(f"⚠️ {key} per-h low (z={z:.1f})")
                    score += 1

        def pvp_cap(name: str, val: float, thresh: float):
            nonlocal score
            if val >= thresh:
                flags.append(f"⚠️ {name} {val:.2f} ≥ {thresh}")
                score += 2
        pvp_cap("Bullet acc.", bullet_acc, 0.45)
        pvp_cap("Head-shot acc.", head_acc, 0.40)
        pvp_cap("Arrow acc.", arrow_acc, 0.60)
        pvp_cap("K/D", kd, 5.0)

        risk, colour = (
            ("🔴  HIGH RISK",     discord.Color.red())    if score >= 15 else
            ("🟠  MODERATE RISK", discord.Color.orange()) if score >= 7  else
            ("🟢  LOW RISK",      discord.Color.green())
        )

        # ─── embed ─────────────────────────────────────────────────
        e = (
            discord.Embed(
                title=f"Rust stats – [{profile.get('personaname')}]",
                url=profile.get("profileurl"),
                colour=colour,
                description=f"{risk}\n\n" +
                            (" ".join(flags) if flags else "No risk indicators triggered.")
            )
            .set_footer(text=f"SteamID64: {sid}  |  Score: {score}")
        )
        if profile.get("avatarfull"):
            e.set_thumbnail(url=profile["avatarfull"])

        # blocks (summary / PvP / PvE / resources / misc)
        summary = "\n".join([
            f"Total hrs  : {fmt(tot_h)}",
            f"2-wks hrs  : {fmt(twk_h)}",
            f"Last played: {last_play}",
            f"Achievement: {ach_unl}/{ach_tot} ({ach_pct})",
            f"Steam pres.: {pres_steam}",
            f"BM pres.   : {bm_online}",
            f"BM sessions: {fmt(bm_sessions)}",
        ])
        e.add_field(name="Summary", value=f"```ini\n{summary}\n```", inline=False)

        pvp_blk = "\n".join([
            f"Kills  : {fmt(kills)}",
            f"Deaths : {fmt(deaths)}   (K/D {kd:.2f})",
            f"Bullets: {fmt(b_hit)} / {fmt(b_fired)} ({bullet_acc*100:4.1f} %)",
            f"Head-shot acc.: {head_acc*100:4.1f} %",
            f"Arrows : {fmt(a_hit)} / {fmt(a_fired)} ({arrow_acc*100:4.1f} %)",
        ])
        e.add_field(name="PvP", value=f"```ini\n{pvp_blk}\n```", inline=False)

        pve = "\n".join([
            f"Scientists: {fmt(st['kill_scientist'])}",
            f"Bears     : {fmt(st['kill_bear'])}",
            f"Wolves    : {fmt(st['kill_wolf'])}",
            f"Boars     : {fmt(st['kill_boar'])}",
            f"Deer      : {fmt(st['kill_deer'])}",
            f"Horses    : {fmt(st['kill_horse'])}",
        ])
        other_deaths = "\n".join([
            f"Suicides: {fmt(st['death_suicide'])}",
            f"Falling : {fmt(st['death_fall'])}",
        ])
        e.add_field(name="PvE kills", value=f"```ini\n{pve}\n```", inline=True)
        e.add_field(name="Other deaths", value=f"```ini\n{other_deaths}\n```",
                    inline=True)
        e.add_field(name="\u200b", value="\u200b", inline=False)

        nodes = "\n".join([
            f"Wood      : {fmt(st['harvest_wood'])}",
            f"Stone     : {fmt(st['harvest_stones'])}",
            f"Metal ore : {fmt(st['harvest_metal_ore'])}",
            f"HQ ore    : {fmt(st['harvest_hq_metal_ore'])}",
            f"Sulfur ore: {fmt(st['harvest_sulfur_ore'])}",
        ])
        pickups = "\n".join([
            f"Low-grade : {fmt(st['acq_lowgrade'])}",
            f"Scrap     : {fmt(st['acq_scrap'])}",
            f"Cloth     : {fmt(st['acq_cloth'])}",
            f"Leather   : {fmt(st['acq_leather'])}",
        ])
        build = "\n".join([
            f"Blocks placed : {fmt(st['build_place'])}",
            f"Blocks upgrade: {fmt(st['build_upgrade'])}",
            f"Barrels broken: {fmt(st['barrels'])}",
            f"BPs learned   : {fmt(st['bps'])}",
        ])
        e.add_field(name="Resources (nodes)",
                    value=f"```ini\n{nodes}\n```", inline=True)
        e.add_field(name="Resources (pick-ups)",
                    value=f"```ini\n{pickups}\n```", inline=True)
        e.add_field(name="Building / Loot",
                    value=f"```ini\n{build}\n```", inline=True)
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
        ui = "\n".join([
            f"Calories : {fmt(st['calories'])}",
            f"Water    : {fmt(st['water'])}",
            f"Map opens: {fmt(st['map_open'])}",
            f"Inv opens: {fmt(st['inv_open'])}",
            f"Crafted  : {fmt(st['items_crafted'])}",
        ])
        e.add_field(name="Electric / Social",
                    value=f"```ini\n{social}\n```", inline=True)
        e.add_field(name="Horses",
                    value=f"```ini\n{horses}\n```", inline=True)
        e.add_field(name="Consumption / UI",
                    value=f"```ini\n{ui}\n```", inline=True)

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