# Giveaways now in seperate file
# ──────────────────────────────────────────────────────────────────────
#  ctfobot2.0.py   –  CTFO Discord bot / registration system
# ──────────────────────────────────────────────────────────────────────
import os, sys, asyncio, signal, json
from datetime import datetime, timedelta, timezone, date
from random import choice
from typing import Dict, Any

import discord, asyncpg
from discord import app_commands
from discord.ext import commands, tasks
from importlib import import_module

# ══════════════════════════════════════════════════════════════════════
#                             CONFIGURATION
# ══════════════════════════════════════════════════════════════════════
BOT_TOKEN      = os.getenv("BOT_TOKEN")
DATABASE_URL   = os.getenv("DATABASE_URL")
GUILD_ID       = int(os.getenv("GUILD_ID", "1377035207777194005"))
ACTIVE_MEMBER_ROLE_ID = 1403337937722019931
FEEDBACK_CH    = 1413188006499586158
MEMBER_FORM_CH = 1378118620873494548
WARNING_CH_ID  = 1398657081338237028
WELCOME_CHANNEL_ID = 1398659438960971876
APPLICATION_CH_ID  = 1378081331686412468
UNCOMPLETED_APP_ROLE_ID = 1390143545066917931   # “Uncompleted application”
COMPLETED_APP_ROLE_ID   = 1398708167525011568   # “Completed application”
INACTIVE_ROLE_ID = 1416864151829221446          # “Inactive”  role
INACTIVE_CH_ID   = 1416865404860502026          # #inactive-players channel
RECRUITMENT_ID  = 1410659214959054988
ACCEPT_ROLE_ID  = 1377075930144571452
REGION_ROLE_IDS = {
    "North America": 1411364406096433212,
    "Europe":        1411364744484491287,
    "Asia":          1411364982117105684,
    "Other":         1411365034440921260,
}
FOCUS_ROLE_IDS = {
    "Farming":      1379918816871448686,
    "Base Sorting": 1400849292524130405,
    "Building":     1380233086544908428,
    "Electricity":  1380233234675400875,
    "PvP":          1408687710159245362,
}


TEMP_BAN_SECONDS    = 7 * 24 * 60 * 60
GIVEAWAY_ROLE_ID    = 1403337937722019931
GIVEAWAY_CH_ID      = 1413929735658016899
CODES_CH_ID         = 1398667158237483138
EMBED_TITLE         = "🎉 GIVEAWAY 🎉"
PROMOTE_STREAK      = 3
INACTIVE_AFTER_DAYS = 5
WARN_BEFORE_DAYS    = INACTIVE_AFTER_DAYS - 1

ADMIN_ID        = 1377103244089622719
ELECTRICIAN_ID  = 1380233234675400875
GROUP_LEADER_ID = 1377077466513932338
PLAYER_MGMT_ID  = 1377084533706588201
TRUSTED_ID      = 1400584430900219935

CODE_NAMES = ["Master", "Guest", "Electrician", "Other"]

# staff roles that give weekly bonus tickets
STAFF_BONUS_ROLE_IDS = {
    ADMIN_ID,
    GROUP_LEADER_ID,
    PLAYER_MGMT_ID,
    RECRUITMENT_ID,
}

BOOST_BONUS_PER_WEEK  = 3
STAFF_BONUS_PER_WEEK  = 3
STREAK_BONUS_PER_SET  = 3      # every 3-day set gives +3

# ══════════════════════════════════════════════════════════════════════
#                             BOT / INTENTS
# ══════════════════════════════════════════════════════════════════════
class CTFBot(commands.Bot):
    last_anonymous_time: dict[int, datetime]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_anonymous_time = {}

# ── instantiate your custom bot ──
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = CTFBot(command_prefix="!", intents=intents)

async def ensure_member_cache(guild: discord.Guild):
    """
    Make sure the bot’s member cache is filled so role.members works.
    Does nothing if the cache is already populated.
    """
    if guild.large and len(guild._members) == 0:      # type: ignore
        # Force a guild chunk – this populates role.members
        await guild.chunk(cache=True)

# ══════════════════════════════════════════════════════════════════════
#                                DATABASE
# ══════════════════════════════════════════════════════════════════════
class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    # ────────────────────────────────────────────────────────────────
    #  CONNECTION / SCHEMA
    # ────────────────────────────────────────────────────────────────
    async def connect(self):
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
        await self.init_tables()

    async def init_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
    CREATE TABLE IF NOT EXISTS codes (
        name   TEXT PRIMARY KEY,
        pin    VARCHAR(4) NOT NULL,
        public BOOLEAN     NOT NULL DEFAULT FALSE
    );
    CREATE TABLE IF NOT EXISTS reviewers (user_id BIGINT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS activity  (
        user_id BIGINT PRIMARY KEY,
        streak  INTEGER,
        date    DATE,
        warned  BOOLEAN,
        last    TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS giveaways (
        id         SERIAL PRIMARY KEY,
        channel_id BIGINT,
        message_id BIGINT,
        prize      TEXT,
        start_ts   BIGINT,
        end_ts     BIGINT,
        active     BOOLEAN,
        note       TEXT
    );
    -- in case the column is missing on an old table:
    ALTER TABLE giveaways
        ADD COLUMN IF NOT EXISTS start_ts BIGINT;
    
    ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS note TEXT;

    CREATE TABLE IF NOT EXISTS member_forms (
        id         SERIAL PRIMARY KEY,
        user_id    BIGINT,
        created_at TIMESTAMP DEFAULT now(),
        data       JSONB,
        status     TEXT NOT NULL DEFAULT 'pending'
    );
    CREATE TABLE IF NOT EXISTS staff_applications (
        id         SERIAL PRIMARY KEY,
        user_id    BIGINT,
        role       TEXT,
        message_id BIGINT,
        status     TEXT NOT NULL DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS inactive_members (
        user_id  BIGINT PRIMARY KEY,
        until_ts BIGINT
    );
    ALTER TABLE member_forms
        ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';
    """)

    # ────────────────────────────────────────────────────────────────
    #  CODES helpers
    # ────────────────────────────────────────────────────────────────
    async def get_codes(self, *, only_public: bool = False):
        q = "SELECT name, pin, public FROM codes"
        if only_public:
            q += " WHERE public=TRUE"
        q += " ORDER BY name"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q)
            return {r["name"]: (r["pin"], r["public"]) for r in rows}

    async def add_code(self, name: str, pin: str, public: bool):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO codes (name, pin, public)
                VALUES ($1,$2,$3)
                ON CONFLICT(name) DO UPDATE SET pin=$2, public=$3
                """,
                name, pin, public
            )

    async def edit_code(self, name: str, pin: str, public: bool | None = None):
        async with self.pool.acquire() as conn:
            if public is None:
                await conn.execute(
                    "UPDATE codes SET pin=$2 WHERE name=$1",
                    name, pin
                )
            else:
                await conn.execute(
                    "UPDATE codes SET pin=$2, public=$3 WHERE name=$1",
                    name, pin, public
                )

    async def remove_code(self, name: str):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM codes WHERE name=$1", name)

    # ────────────────────────────────────────────────────────────────
    #  REVIEWERS
    # ────────────────────────────────────────────────────────────────
    async def get_reviewers(self):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM reviewers")
            return {r["user_id"] for r in rows}

    async def add_reviewer(self, uid: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO reviewers (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
                uid
            )

    async def remove_reviewer(self, uid: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM reviewers WHERE user_id=$1", uid)

    # ────────────────────────────────────────────────────────────────
    #  ACTIVITY
    # ────────────────────────────────────────────────────────────────
    async def get_activity(self, uid: int):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM activity WHERE user_id=$1", uid
            )
            return dict(row) if row else None

    async def set_activity(self, uid, streak, date_, warned, last):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO activity (user_id, streak, date, warned, last)
                VALUES ($1,$2,$3,$4,$5)
                ON CONFLICT(user_id) DO UPDATE
                  SET streak=$2, date=$3, warned=$4, last=$5
                """,
                uid, streak, date_, warned, last
            )

    async def get_all_activity(self):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM activity")
            return {r["user_id"]: dict(r) for r in rows}
        
    # ───────────────── Inactive role helpers ─────────────────
    async def add_inactive(self, uid: int, until_ts: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO inactive_members (user_id, until_ts) "
                "VALUES ($1,$2) "
                "ON CONFLICT (user_id) DO UPDATE SET until_ts=$2",
                uid, until_ts
            )

    async def remove_inactive(self, uid: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM inactive_members WHERE user_id=$1", uid)

    async def get_expired_inactive(self, now_ts: int):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM inactive_members WHERE until_ts <= $1",
                now_ts
            )
            return [dict(r) for r in rows]

    # ────────────────────────────────────────────────────────────────
    #  MEMBER FORMS
    # ────────────────────────────────────────────────────────────────
    async def add_member_form(self, uid, data: dict, message_id: int = None):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO member_forms (user_id, data, message_id, status)
                VALUES ($1,$2,$3,'pending')
                """,
                uid, json.dumps(data), message_id
            )

    async def update_member_form_status(self, message_id: int, status: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE member_forms SET status=$1 WHERE message_id=$2",
                status, message_id
            )

    async def get_pending_member_forms(self):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM member_forms
                WHERE status='pending' AND message_id IS NOT NULL
            """)
            return [dict(r) for r in rows]
        
    # ────────────────────────────────────────────────────────────────
    #  STAFF APPLICATIONS
    # ────────────────────────────────────────────────────────────────
    async def add_staff_app(self, uid: int, role: str, msg_id: int):
        """Insert a new staff-application row."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO staff_applications (user_id, role, message_id)"
                " VALUES ($1,$2,$3)",
                uid, role, msg_id
            )

    async def update_staff_app_status(self, msg_id: int, status: str):
        """Mark an application message as accepted / denied."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE staff_applications SET status=$1 WHERE message_id=$2",
                status, msg_id
            )

    async def get_pending_staff_apps(self):
        """Return all still-pending staff applications."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM staff_applications WHERE status='pending'"
            )
            return [dict(r) for r in rows]

db = Database(DATABASE_URL)

async def remove_duplicate_welcomes(channel: discord.TextChannel, user: discord.Member, welcome_marker: str):
    """
    Delete duplicate welcome messages mentioning the user in the channel,
    keeping only the most recent.
    """
    matches = []
    async for message in channel.history(limit=20):
        if (
            message.author == channel.guild.me and
            welcome_marker in message.content and
            user.mention in message.content
        ):
            matches.append(message)
    if len(matches) > 1:
        matches.sort(key=lambda m: m.created_at, reverse=True)
        for msg in matches[1:]:
            try:
                await msg.delete()
            except Exception:
                pass

#================Resume member forms=====================
async def resume_member_forms():
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        print("[resume_member_forms] Guild not found.")
        return
    channel = guild.get_channel(MEMBER_FORM_CH)
    if not channel:
        print("[resume_member_forms] Member form channel not found.")
        return

    forms = await db.get_pending_member_forms()
    for form in forms:
        msg_id = form.get("message_id")
        raw = form.get("data") or {}
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except Exception as e:
                print(f"[resume_member_forms] Could not decode data: {raw} ({e})")
                continue
        else:
            data = raw
        region = data.get("region")
        focus = data.get("focus")
        user_id = form.get("user_id")
        if not all([msg_id, region, focus, user_id]):
            continue
        try:
            # Optionally: check if message still exists
            await channel.fetch_message(msg_id)
        except discord.NotFound:
            print(f"[resume_member_forms] Message {msg_id} not found, skipping.")
            continue
        view = ActionView(guild, user_id, region, focus)
        try:
            bot.add_view(view, message_id=msg_id)
            print(f"[resume_member_forms] Restored ActionView for form message {msg_id}")
        except Exception as e:
            print(f"[resume_member_forms] Error restoring view for {msg_id}: {e}")
    
# ══════════════════════════════════════════════════════════════════════
#                        UTILITIES  /  EMBEDS
# ══════════════════════════════════════════════════════════════════════
def build_codes_embed(codes: dict[str, tuple[str, bool]]) -> discord.Embed:
    """
    Build embed listing all codes.
    codes = {name: (pin, public)}
    """
    e = discord.Embed(
        title="🔑 Access Codes",
        description="Codes with 🔒 are **private** (not returned by /codes list).",
        colour=discord.Color.blue()
    )
    if not codes:
        e.description += "\n\n*No codes configured yet.*"
    else:
        for name, (pin, pub) in codes.items():
            lock = "" if pub else " 🔒"
            e.add_field(name=f"{name}{lock}", value=f"`{pin}`", inline=False)
    e.set_footer(text="Last updated")
    return e


async def update_codes_message(bot: commands.Bot, codes: dict) -> None:
    """
    Keep exactly ONE "🔑 Access Codes" embed in the codes channel.
    If it exists, edit it; otherwise create it and remember the ID.
    """
    channel: discord.TextChannel | None = bot.get_channel(CODES_CH_ID)
    if channel is None:
        print("[codes] codes channel not found!")
        return

    store_path = "/data/codes_msg_id.txt"
    msg_id: int | None = None

    # ---------- 1) try stored ID ----------
    if os.path.exists(store_path):
        try:
            msg_id = int(open(store_path, "r").read().strip())
            msg = await channel.fetch_message(msg_id)
        except (ValueError, discord.NotFound):
            msg = None
    else:
        msg = None

    # ---------- 2) search history if needed ----------
    if msg is None:
        async for m in channel.history(limit=100):
            if (
                m.author == bot.user                     # by this bot
                and m.embeds
                and m.embeds[0].title
                and m.embeds[0].title.startswith("🔑 Access Codes")
            ):
                msg = m
                msg_id = m.id
                # rewrite the cache file so next time we fetch directly
                os.makedirs("/data", exist_ok=True)
                with open(store_path, "w") as fp:
                    fp.write(str(msg_id))
                break

    embed = build_codes_embed(codes)

    # ---------- 3) edit existing or send new ----------
    if msg:
        await msg.edit(embed=embed)
    else:
        msg = await channel.send(embed=embed)
        os.makedirs("/data", exist_ok=True)
        with open(store_path, "w") as fp:
            fp.write(str(msg.id))

    print(f"[codes] embed updated (message id {msg.id})")

# ──────────────────────────────────────────────────────────────────────
#  Background listener – refresh codes embed when DB sends NOTIFY
# ──────────────────────────────────────────────────────────────────────
async def listen_for_code_changes() -> None:
    """
    Dedicated connection LISTENing on ‘codes_changed’.
    When a NOTIFY arrives, reload the table and refresh the embed.
    """
    conn: asyncpg.Connection = await asyncpg.connect(DATABASE_URL)

    async def refresh_embed() -> None:
        try:
            codes = await db.get_codes()
            await update_codes_message(bot, codes)
            print(f"[codes_changed] embed refreshed at {datetime.utcnow()}")
        except Exception as exc:
            print("[codes_changed] error while refreshing embed:", exc)

    async def _listener(*_):              # (conn, pid, channel, payload)
        await refresh_embed()             # run directly; it’s already a coro

    await conn.add_listener("codes_changed", _listener)
    print("[codes_changed] listener attached")

    try:
        while not bot.is_closed():
            await asyncio.sleep(3600)     # keep task alive
    finally:
        await conn.close()

# ══════════════════════════════════════════════════════════════════════
async def is_admin_or_reviewer(inter: discord.Interaction) -> bool:
    reviewers = await db.get_reviewers()
    return inter.user.guild_permissions.administrator or inter.user.id in reviewers

# ═══════════════════════════  ACTIVITY TRACKER  ═══════════════════════
async def mark_active(member: discord.Member):
    """Record activity for a member and handle auto-promotion."""
    if member.bot:
        return

    today = date.today()
    rec = await db.get_activity(member.id)

    if not rec:                                         # first-ever activity
        streak, warned = 1, False
    else:
        if rec["date"] != today:                        # new calendar day
            yesterday = rec["date"] + timedelta(days=1)
            streak = rec["streak"] + 1 if yesterday == today else 1
            warned = False                              # clear warning flag
        else:                                           # same day, no change
            streak, warned = rec["streak"], rec["warned"]

    await db.set_activity(member.id, streak, today, warned,
                          datetime.now(timezone.utc))

    # ───────── PROMOTION ─────────
    if streak >= PROMOTE_STREAK:
        role = member.guild.get_role(ACTIVE_MEMBER_ROLE_ID)
        if role and role not in member.roles:
            try:
                await member.add_roles(role, reason="Reached activity streak")
                print(f"[PROMOTE] {member} promoted (streak {streak})")
            except discord.Forbidden:
                print(f"[PROMOTE] Missing perms to add role to {member}")

@bot.event
async def on_message(m: discord.Message):
    if m.guild and not m.author.bot:
        await mark_active(m.author)

@bot.event
async def on_voice_state_update(member, before, after):
    if (
        member.guild.id == GUILD_ID
        and not member.bot
        and not before.channel
        and after.channel
    ):
        await mark_active(member)

@tasks.loop(hours=24)
async def activity_maintenance() -> None:
    """
    Daily maintenance:
      • promote / demote based on activity streak
      • warn users about impending demotion
      • kick users idle ≥14 days (unless exempt)
      • auto-remove “Inactive” role when its period elapses and
        reset that member’s activity so the kick timer restarts.
    """
    await bot.wait_until_ready()

    guild = bot.get_guild(GUILD_ID)
    role_active   = guild.get_role(ACTIVE_MEMBER_ROLE_ID) if guild else None
    role_inactive = guild.get_role(INACTIVE_ROLE_ID)      if guild else None
    warn_ch       = guild.get_channel(WARNING_CH_ID) if guild else None
    today         = date.today()

    if not guild or not role_active:
        print("[activity] guild or Active-Member role missing – cycle skipped")
        return

    records  = await db.get_all_activity()        # {uid: dict}
    promoted = demoted = warned_n = kicked = unmarked = 0

    for uid, rec in records.items():
        member = guild.get_member(uid)
        if not member or member.bot:
            continue

        # ───────── PROMOTE ─────────
        if rec["streak"] >= PROMOTE_STREAK and role_active not in member.roles:
            try:
                await member.add_roles(role_active, reason="Reached activity streak")
                promoted += 1
            except discord.Forbidden:
                print(f"[PROMOTE] Missing perms for {member}")

        # ───────── INACTIVITY CALCS ─────────
        days_idle      = (today - rec["date"]).days
        already_warned = rec["warned"]

        # warn 1 day before demotion
        if (
            days_idle == WARN_BEFORE_DAYS
            and role_active in member.roles
            and not already_warned
        ):
            if warn_ch:
                await warn_ch.send(
                    f"{member.mention} You’ve been inactive for {days_idle} days — "
                    "please pop in or you’ll lose your role."
                )
            await db.set_activity(uid, rec["streak"], rec["date"], True, rec["last"])
            warned_n += 1

        # demote after INACTIVE_AFTER_DAYS
        if days_idle >= INACTIVE_AFTER_DAYS and role_active in member.roles:
            try:
                await member.remove_roles(role_active, reason="Inactive > 5 days")
                demoted += 1
            except discord.Forbidden:
                print(f"[DEMOTE] Missing perms for {member}")
            await db.set_activity(uid, 0, rec["date"], False, rec["last"])

        # ───────── 14-DAY KICK (unless exempt) ─────────
        
        exempt = (
            member.guild_permissions.administrator
            or (role_inactive in member.roles if role_inactive else False)
            or (role_active in member.roles)
            or any(r.id in (GROUP_LEADER_ID, PLAYER_MGMT_ID, RECRUITMENT_ID, ADMIN_ID)
                   for r in member.roles)            # ← Recruitment added
        )

        if not exempt and days_idle >= 14:
            try:
                await guild.kick(
                    member,
                    reason=f"Inactivity ≥14 days (last activity {rec['date']})"
                )
                kicked += 1
                ch = guild.get_channel(INACTIVE_CH_ID)
                if ch:
                    await ch.send(f"👢 {member} was kicked for 14-day inactivity.")
            except discord.Forbidden:
                print(f"[KICK] Missing perms to kick {member}")

    # ───────── Remove expired “Inactive” roles ─────────
    now_ts  = int(datetime.now(timezone.utc).timestamp())
    expired = await db.get_expired_inactive(now_ts)

    for row in expired:                              # [{'user_id', 'until_ts'}]
        member = guild.get_member(row["user_id"])
        if not member:
            await db.remove_inactive(row["user_id"])
            continue

        if role_inactive and role_inactive in member.roles:
            try:
                await member.remove_roles(role_inactive, reason="Inactive period elapsed")
                unmarked += 1
            except discord.Forbidden:
                print(f"[INACTIVE] Can't remove role from {member}")

        # reset activity so they start fresh
        await db.set_activity(
            member.id,               # uid
            0,                       # streak
            today,                   # date
            False,                   # warned
            datetime.now(timezone.utc)
        )
        await db.remove_inactive(member.id)

        ch = guild.get_channel(INACTIVE_CH_ID)
        if ch:
            await ch.send(f"🔔 {member.mention} is no longer marked Inactive — welcome back!")

    print(
        f"[activity] cycle: +{promoted} promoted, "
        f"{warned_n} warned, –{demoted} demoted, "
        f"👢{kicked} kicked, 🔔{unmarked} inactive-role removed"
    )
# ============Welcome Message==============

@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return  # ignore bots

    guild = member.guild
    welcome = guild.get_channel(WELCOME_CHANNEL_ID)
    apply_ch = guild.get_channel(APPLICATION_CH_ID)

    # 1. Add uncompleted application role
    role = guild.get_role(UNCOMPLETED_APP_ROLE_ID)
    if role and role not in member.roles:
        try:
            await member.add_roles(role, reason="Joined – application not started")
        except discord.Forbidden:
            print(f"[JOIN] Missing perms to add role to {member}")
        except Exception as e:
            print(f"[JOIN] Error adding role: {e}")

    # 2. Send welcome message and deduplicate
    if welcome and apply_ch:
        msg = (
            f"👋 **Welcome {member.mention}!**\n"
            f"To join CTFO, please run **`/memberform`** "
            f"in {apply_ch.mention} and fill out the quick application.\n"
            "If you have any questions, just ask a mod.  Enjoy your stay!"
        )
        try:
            await welcome.send(msg)
        except Exception as e:
            print(f"[WELCOME] Error sending welcome message: {e}")
        try:
            await remove_duplicate_welcomes(welcome, member, "👋 **Welcome")
        except Exception as e:
            print(f"[WELCOME] Error deduplicating: {e}")
    else:
        print("Welcome or application channel missing!")

# ══════════════════════════════════════════════════════════════════════
#  STAFF APPLICATION SYSTEM  (persistent, button-paginated)
# ══════════════════════════════════════════════════════════════════════
from datetime import datetime, timezone

# ───────────────────────  CONSTANTS  ───────────────────────
STAFF_APPLICATION_CH_ID = 1410649548837093436      # channel where apps go
ADMIN_ROLE_ID           = 1377103244089622719      # ping on new app

STAFF_ROLE_IDS: dict[str, int] = {
    "Group Leader":      1377077466513932338,
    "Player Management": 1377084533706588201,
    "Recruitment":       1410659214959054988,
}

# ───────────────────────  QUESTIONS  ───────────────────────
# tuple = (label, style, required)  LABEL **≤45 chars**
STAFF_QUESTION_SETS: dict[str, list[tuple[str, discord.TextStyle, bool]]] = {
    "Group Leader": [
        ("What group are you looking to lead?",          discord.TextStyle.short,     True),
        ("Why do you want to be a group leader?",        discord.TextStyle.paragraph, True),
        ("What makes you a good fit for this role?",     discord.TextStyle.paragraph, True),
        ("How many Rust hours do you have?",             discord.TextStyle.short,     True),
        ("How many hours a week are you available?",     discord.TextStyle.short,     True),
        ("When are you most active?",                    discord.TextStyle.short,     True),
        ("What time-zone are you in?",                   discord.TextStyle.short,     True),
        ("How would you rate your in-game skills?",      discord.TextStyle.short,     True),
        ("How old are you?",                             discord.TextStyle.short,     True),
    ],

    "Player Management": [
        ("Why do you want to join player management?",        discord.TextStyle.paragraph, True),
        ("What makes you good for this role?",                discord.TextStyle.paragraph, True),
        ("Describe your leadership skills.",                  discord.TextStyle.paragraph, True),
        ("How would you handle breaking of rules?",           discord.TextStyle.paragraph, True),
        ("How would you handle an unpopular decision?",       discord.TextStyle.paragraph, True),
        ("How would you handle an irritating player?",        discord.TextStyle.paragraph, True),
        ("What would you do if you felt annoyed?",            discord.TextStyle.paragraph, True),
        ("What time-zone are you in?",                        discord.TextStyle.short,     True),
        ("How many hours a week are you active?",             discord.TextStyle.short,     True),
        ("When are you most active?",                         discord.TextStyle.short,     True),
        ("How old are you?",                                  discord.TextStyle.short,     True),
    ],

    "Recruitment": [
        ("Why do you want this role?",                        discord.TextStyle.paragraph, True),
        ("What time-zone are you in?",                        discord.TextStyle.short,     True),
        ("When are you most active?",                         discord.TextStyle.short,     True),
        ("Are you banned from any Rust discords?",            discord.TextStyle.short,     True),
        ("How old are you?",                                  discord.TextStyle.short,     True),
        ("If someone you rejected messages you, what do?",    discord.TextStyle.paragraph, True),
    ],
}

# ───────────────────────  UI CLASSES  ───────────────────────
class StaffRoleSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(StaffRoleSelect())

class StaffRoleSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Select the staff role you’d like to apply for…",
            options=[discord.SelectOption(label=r, value=r) for r in STAFF_QUESTION_SETS]
        )

    async def callback(self, inter: discord.Interaction):
        await inter.response.send_modal(
            StaffApplicationModal(self.values[0], 0, [])
        )

# ─────────────────────  Continue button  ─────────────────────
class ContinueView(discord.ui.View):
    def __init__(self, role: str, next_idx: int, collected: list[tuple[str, str]]):
        super().__init__(timeout=300)
        self.role, self.next_idx, self.collected = role, next_idx, collected

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary, emoji="➡️")
    async def continue_btn(self, inter: discord.Interaction, _):
        await inter.response.send_modal(
            StaffApplicationModal(self.role, self.next_idx, list(self.collected))
        )
        for c in self.children:
            c.disabled = True
        await inter.message.edit(view=self)

# ─────────────────── multi-page modal (≤5 inputs) ───────────────────
class StaffApplicationModal(discord.ui.Modal):
    def __init__(self, role: str, idx: int, collected: list[tuple[str, str]]):
        super().__init__(title=f"{role} Application")
        self.role, self.idx, self.collected = role, idx, collected

        qset = STAFF_QUESTION_SETS[role][idx : idx + 5]
        for q, style, req in qset:
            assert len(q) <= 45, f"Modal label >45 chars: {q}"
            self.add_item(
                discord.ui.TextInput(
                    label=q,
                    style=style,
                    required=req,
                    max_length=100 if style is discord.TextStyle.short else 4000
                )
            )

    async def on_submit(self, inter: discord.Interaction):
        # cache answers from this page
        for comp in self.children:                                    # type: ignore
            label_txt = getattr(comp, "label", None) or comp._underlying.label
            self.collected.append((label_txt, comp.value))            # type: ignore

        next_idx = self.idx + 5
        if next_idx < len(STAFF_QUESTION_SETS[self.role]):
            await inter.response.send_message(
                "Page saved — click **Continue** to answer the next set:",
                view=ContinueView(self.role, next_idx, list(self.collected)),
                ephemeral=True
            )
            return

        # ───────── all questions answered: build embed ─────────
        review_ch = inter.guild.get_channel(STAFF_APPLICATION_CH_ID)
        if not review_ch:
            return await inter.response.send_message("Review channel missing.", ephemeral=True)

        embed = (
            discord.Embed(
                title=f"{self.role} Application",
                colour=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc)
            )
            .set_author(name=str(inter.user), icon_url=inter.user.display_avatar.url)
        )
        for i, (q, a) in enumerate(self.collected, 1):
            embed.add_field(name=f"{i}. {q}", value=a or "N/A", inline=False)
        embed.set_footer(text=f"User ID: {inter.user.id}")

        view = StaffApplicationActionView(inter.guild, inter.user.id, self.role)
        msg  = await review_ch.send(f"<@&{ADMIN_ROLE_ID}>", embed=embed, view=view)

        await db.add_staff_app(inter.user.id, self.role, msg.id)
        await inter.response.send_message("✅ Your staff application was submitted.", ephemeral=True)

# ─────────────────── reviewer action buttons ───────────────────
class StaffApplicationActionView(discord.ui.View):
    def __init__(self, guild: discord.Guild, applicant_id: int, role: str):
        super().__init__(timeout=None)
        self.guild, self.applicant_id, self.role = guild, applicant_id, role

    async def _authorised(self, member: discord.Member) -> bool:
        return (member.guild_permissions.administrator
                or any(r.id in STAFF_ROLE_IDS.values() for r in member.roles))

    async def _notify(self, txt: str):
        user = await safe_fetch(self.guild, self.applicant_id)
        if user:
            try:    await user.send(txt)
            except discord.Forbidden: pass

    async def _finish(self, inter: discord.Interaction, colour: discord.Colour):
        emb = inter.message.embeds[0]
        emb.colour = colour
        for c in self.children: c.disabled = True
        await inter.message.edit(embed=emb, view=self)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, inter: discord.Interaction, _):
        if not await self._authorised(inter.user):
            return await inter.response.send_message("Not authorised.", ephemeral=True)

        applicant = await safe_fetch(self.guild, self.applicant_id)
        if not applicant:
            return await inter.response.send_message("Applicant left.", ephemeral=True)

        role_obj = self.guild.get_role(STAFF_ROLE_IDS[self.role])
        if not role_obj:
            return await inter.response.send_message("Role missing.", ephemeral=True)

        try:    await applicant.add_roles(role_obj, reason="Staff application accepted")
        except discord.Forbidden:
            return await inter.response.send_message("Cannot add role.", ephemeral=True)

        await db.update_staff_app_status(inter.message.id, "accepted")
        await inter.response.send_message(f"{applicant.mention} accepted ✅", ephemeral=True)
        await self._finish(inter, discord.Color.green())
        await self._notify(f"🎉 You have been **accepted** as **{self.role}**!")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="⛔")
    async def deny(self, inter: discord.Interaction, _):
        if not await self._authorised(inter.user):
            return await inter.response.send_message("Not authorised.", ephemeral=True)

        await db.update_staff_app_status(inter.message.id, "denied")
        await inter.response.send_message("Application denied ⛔", ephemeral=True)
        await self._finish(inter, discord.Color.red())
        await self._notify(f"❌ Your application for **{self.role}** was **denied**.")

# ──────────────────────  slash command  ──────────────────────
@bot.tree.command(name="staffapply", description="Apply for a staff position")
async def staffapply(inter: discord.Interaction):
    await inter.response.send_message(
        "Select the staff role you’d like to apply for:",
        view=StaffRoleSelectView(),
        ephemeral=True
    )

# ───────────── restore persistent views on reboot ─────────────
async def resume_staff_applications():
    guild = bot.get_guild(GUILD_ID)
    if not guild: return
    channel = guild.get_channel(STAFF_APPLICATION_CH_ID)
    if not channel: return

    for row in await db.get_pending_staff_apps():      # [{'user_id', 'role', 'message_id'}]
        try:    await channel.fetch_message(row["message_id"])
        except discord.NotFound: continue
        bot.add_view(
            StaffApplicationActionView(guild, row["user_id"], row["role"]),
            message_id=row["message_id"]
        )
        print(f"[resume_staff_apps] Restored view for {row['message_id']}")

# ───────────────────────────── /inactive ─────────────────────────────
# Anyone can run it; always applies to the caller them-self.
PERIOD_CHOICES: list[tuple[str, int]] = [
    ("1 week", 7),
    ("2 weeks", 14),
    ("1 month", 30),
    ("2 months", 60),
]

@bot.tree.command(
    name="inactive",
    description="Mark yourself as temporarily inactive"
)
@app_commands.choices(
    period=[app_commands.Choice(name=n, value=d) for n, d in PERIOD_CHOICES]
)
@app_commands.describe(
    period="How long you will be inactive",
    reason="Reason for inactivity (required)"
)
async def inactive_cmd(
    inter:  discord.Interaction,
    period: app_commands.Choice[int],
    reason: str
):
    guild = inter.guild
    if guild is None:                               # safety (shouldn’t happen)
        return await inter.response.send_message(
            "This command can only be used in a guild.", ephemeral=True
        )

    member   = guild.get_member(inter.user.id) or inter.user  # Member object
    role     = guild.get_role(INACTIVE_ROLE_ID)
    channel  = guild.get_channel(INACTIVE_CH_ID)

    if role is None or channel is None:
        return await inter.response.send_message(
            "Inactive role or channel not configured.", ephemeral=True
        )

    until_ts = int(datetime.now(timezone.utc).timestamp()) + period.value * 86_400

    # give role
    try:
        await member.add_roles(role, reason=f"Inactive – {reason} ({period.name})")
    except discord.Forbidden:
        return await inter.response.send_message(
            "I don’t have permission to add that role.", ephemeral=True
        )

    # DB entry / update
    await db.add_inactive(member.id, until_ts)

    # log to channel
    await channel.send(
        embed=(
            discord.Embed(
                title="📴 Member marked Inactive",
                description=(
                    f"{member.mention} has set themselves to **Inactive** "
                    f"for **{period.name}**.\n"
                    f"**Reason:** {reason}"
                ),
                colour=discord.Color.light_gray(),
            )
            .set_footer(text=f"Until <t:{until_ts}:R>")
        )
    )

    await inter.response.send_message(
        f"You are now marked as inactive for **{period.name}**.", ephemeral=True
    )

# ══════════════════════════════════════════════════════════════════════
#  LEAVE / BAN ANNOUNCEMENTS (no @mentions)
# ══════════════════════════════════════════════════════════════════════

LEAVE_BAN_CH_ID = 1404955868054814761   # change if you prefer a different channel

@bot.event
async def on_member_remove(member: discord.Member):
    """
    Fires when someone leaves the guild (voluntarily or kick).
    """
    channel = member.guild.get_channel(LEAVE_BAN_CH_ID)
    if channel:
        # member -> "Username#1234"
        await channel.send(f"👋 **{member}** has left the server.")

@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    """
    Fires when someone is banned. (Does NOT trigger for temp-bans placed
    via the member-form deny, because that uses guild.ban and so will fire.)
    """
    channel = guild.get_channel(LEAVE_BAN_CH_ID)
    if channel:
        await channel.send(f"⛔ **{user}** has been banned from the server.")

# ═══════════════════════════  /codes  COMMANDS  ═══════════════════════

# ──────────────────────────────────────────────────────────────────────
#  /generatecode  – get a random 4-digit pin (ephemeral)
# ──────────────────────────────────────────────────────────────────────
from random import randint

@bot.tree.command(name="generatecode", description="Generate a random 4-digit code")
async def generate_code(inter: discord.Interaction):
    pin = str(randint(0, 9999)).zfill(4)   # always 4 digits, leading zeros allowed
    await inter.response.send_message(
        f"Your random code: `{pin}`",
        ephemeral=True
    )

class CodesCog(commands.Cog):
    def __init__(self, bot_, db_):
        self.bot, self.db = bot_, db_

    codes_group = app_commands.Group(
        name="codes",
        description="Manage / view access codes"
    )

    # ----------  /codes add  ----------
    @codes_group.command(name="add", description="Add a new code")
    @app_commands.describe(
        name="Code name",
        pin="4-digit number",
        public="Visible to everyone with /codes list?"
    )
    async def codes_add(
        self,
        inter: discord.Interaction,
        name: str,
        pin: str,
        public: bool = False
    ):
        if not await is_admin_or_reviewer(inter):
            return await inter.response.send_message("Permission denied.",
                                                     ephemeral=True)

        codes = await self.db.get_codes()
        if name in codes:
            return await inter.response.send_message(
                "A code with that name already exists. Use /codes edit.",
                ephemeral=True
            )
        if not (pin.isdigit() and len(pin) == 4):
            return await inter.response.send_message("PIN must be 4 digits.",
                                                     ephemeral=True)

        await self.db.add_code(name, pin, public)
        await update_codes_message(self.bot, await self.db.get_codes())
        await inter.response.send_message(
            f"Added **{name}** (`{pin}`) {'(public)' if public else '(private)'}.",
            ephemeral=True
        )

    # ----------  /codes edit  ----------
    @codes_group.command(name="edit", description="Modify an existing code")
    @app_commands.describe(
        name="Existing code name",
        pin="New 4-digit pin",
        public="Leave blank to keep current visibility"
    )
    async def codes_edit(
        self,
        inter: discord.Interaction,
        name: str,
        pin: str,
        public: bool | None = None
    ):
        if not await is_admin_or_reviewer(inter):
            return await inter.response.send_message("Permission denied.",
                                                     ephemeral=True)

        codes = await self.db.get_codes()
        if name not in codes:
            return await inter.response.send_message("No such code.",
                                                     ephemeral=True)
        if not (pin.isdigit() and len(pin) == 4):
            return await inter.response.send_message("PIN must be 4 digits.",
                                                     ephemeral=True)

        await self.db.edit_code(name, pin, public)
        await update_codes_message(self.bot, await self.db.get_codes())
        await inter.response.send_message("Code updated.", ephemeral=True)

    # ----------  /codes remove  ----------
    @codes_group.command(name="remove", description="Delete a code")
    @app_commands.describe(name="Code name to remove")
    async def codes_remove(self, inter: discord.Interaction, name: str):
        if not await is_admin_or_reviewer(inter):
            return await inter.response.send_message("Permission denied.",
                                                     ephemeral=True)

        codes = await self.db.get_codes()
        if name not in codes:
            return await inter.response.send_message("No such code.",
                                                     ephemeral=True)

        await self.db.remove_code(name)
        await update_codes_message(self.bot, await self.db.get_codes())
        await inter.response.send_message("Code removed.", ephemeral=True)

    # ----------  /codes list  ----------
    @codes_group.command(name="list", description="Show public codes")
    async def codes_list(self, inter: discord.Interaction):
        pub = await self.db.get_codes(only_public=True)
        if not pub:
            return await inter.response.send_message(
                "No public codes currently.",
                ephemeral=True
            )
        lines = [f"• **{n}**: `{pin}`" for n, (pin, _) in pub.items()]
        await inter.response.send_message("\n".join(lines), ephemeral=True)


codes_cog = CodesCog(bot, db)
bot.tree.add_command(codes_cog.codes_group)

# ───────── Reviewer helper commands ─────────
@bot.tree.command(name="addreviewer")
async def add_reviewer(i: discord.Interaction, member: discord.Member):
    if not i.user.guild_permissions.administrator:
        await i.response.send_message("No permission.", ephemeral=True)
        return
    await db.add_reviewer(member.id)
    await i.response.send_message("Added.", ephemeral=True)

@bot.tree.command(name="removereviewer")
async def remove_reviewer(i: discord.Interaction, member: discord.Member):
    if not i.user.guild_permissions.administrator:
        await i.response.send_message("No permission.", ephemeral=True)
        return
    await db.remove_reviewer(member.id)
    await i.response.send_message("Removed.", ephemeral=True)

@bot.tree.command(name="reviewers")
async def list_reviewers(i: discord.Interaction):
    reviewers = await db.get_reviewers()
    txt = ", ".join(f"<@{u}>" for u in reviewers) or "None."
    await i.response.send_message(txt, ephemeral=True)

# ═══════════════════════════════ FEEDBACK ══════════════════════════════
@bot.tree.command(name="feedback")
@app_commands.describe(message="Your feedback", anonymous="Send anonymously?")
async def feedback(inter: discord.Interaction, message: str, anonymous: bool):
    ch = bot.get_channel(FEEDBACK_CH)
    if not ch:
        return await inter.response.send_message("Channel missing.", ephemeral=True)

    now = datetime.now(timezone.utc)
    last = bot.last_anonymous_time.get(inter.user.id)

    if anonymous and last and now - last < timedelta(days=1):
        rem = timedelta(days=1) - (now - last)
        h, r = divmod(rem.seconds, 3600)
        m, _ = divmod(r, 60)
        return await inter.response.send_message(
            f"One anonymous msg per 24 h. Retry in {rem.days} d {h} h {m} m.",
            ephemeral=True,
        )

    if anonymous:
        bot.last_anonymous_time[inter.user.id] = now
        embed = (
            discord.Embed(
                title="Anonymous Feedback",
                description=message,
                colour=discord.Color.light_gray(),
            ).set_footer(text="Sent anonymously")
        )
    else:
        embed = (
            discord.Embed(
                title="Feedback", description=message, colour=discord.Color.blue()
            ).set_author(name=str(inter.user), icon_url=inter.user.display_avatar.url)
        )

    await ch.send(embed=embed)
    await inter.response.send_message("Thanks!", ephemeral=True)

# ═══════════════════════ REGISTRATION WORKFLOW ════════════════════════

def opts(*lbl: str) -> list[discord.SelectOption]:
    """Helper to build SelectOption lists quickly."""
    return [discord.SelectOption(label=l, value=l) for l in lbl]

# ──────────────────────────── MemberRegistrationView ───────────────────────────
class MemberRegistrationView(discord.ui.View):
    """
    First view the user sees.  Presents five dropdowns; when all are chosen
    the Submit button appears (in a separate ephemeral message).
    """
    def __init__(self):
        super().__init__(timeout=300)          # 5-minute timeout
        self.data: Dict[str, str] = {}         # answers
        self.user: discord.User | None = None  # who’s filling the form
        self.start_msg:  discord.Message | None = None
        self.submit_msg: discord.Message | None = None
        self.submit_sent: bool = False

    # -------------- Start button --------------
    @discord.ui.button(label="Start Registration",
                       style=discord.ButtonStyle.primary)
    async def start(self, inter: discord.Interaction, _):
        self.user = inter.user
        self.clear_items()                     # remove the Start button
        # add all dropdowns
        self.add_item(SelectAge(self))
        self.add_item(SelectRegion(self))
        self.add_item(SelectBans(self))
        self.add_item(SelectFocus(self))
        self.add_item(SelectSkill(self))
        # send the view back
        await inter.response.send_message(
            "Fill each dropdown – **Submit** appears when all done.",
            view=self,
            ephemeral=True
        )
        self.start_msg = await inter.original_response()

# ───────────────────────── SubmitView (ephemeral, has one button) ──────────────
class SubmitView(discord.ui.View):
    def __init__(self, v: MemberRegistrationView):
        super().__init__(timeout=300)
        self.v = v

    @discord.ui.button(label="Submit", style=discord.ButtonStyle.success)
    async def submit(self, inter: discord.Interaction, _):
        await inter.response.send_modal(FinalRegistrationModal(self.v))

# ────────────────────────────── Base dropdown class ────────────────────────────
class _BaseSelect(discord.ui.Select):
    def __init__(self, v: MemberRegistrationView, key: str, **kw):
        self.v, self.key = v, key
        super().__init__(**kw)

    async def callback(self, inter: discord.Interaction):
        self.v.data[self.key] = self.values[0]      # store answer
        self.placeholder = self.values[0]           # show selection
        await inter.response.edit_message(view=self.v)

        # if all required keys are present & Submit not yet shown → show it
        if (not self.v.submit_sent
                and all(k in self.v.data for k in
                        ("age", "region", "bans", "focus", "skill"))):
            self.v.submit_sent = True
            self.v.submit_msg = await inter.followup.send(
                "All set – click **Submit** :",
                view=SubmitView(self.v),
                ephemeral=True,
                wait=True
            )

# ────────────────────── concrete dropdowns ──────────────────────
class SelectAge(_BaseSelect):
    def __init__(self, v): super().__init__(
        v, "age", placeholder="Age",
        options=opts("12-14", "15-17", "18-21", "21+"))

class SelectRegion(_BaseSelect):
    def __init__(self, v): super().__init__(
        v, "region", placeholder="Region",
        options=opts("North America", "Europe", "Asia", "Other"))

class SelectBans(_BaseSelect):
    def __init__(self, v): super().__init__(
        v, "bans", placeholder="Any bans?",
        options=opts("Yes", "No"))

class SelectFocus(_BaseSelect):
    def __init__(self, v): super().__init__(
        v, "focus", placeholder="Main focus",
        options=opts("PvP", "Farming", "Base Sorting",
                     "Building", "Electricity"))

class SelectSkill(_BaseSelect):
    def __init__(self, v): super().__init__(
        v, "skill", placeholder="Skill level",
        options=opts("Beginner", "Intermediate", "Advanced", "Expert"))

# ───────────────────────────── ActionView (Accept / Deny) ──────────────────────
class ActionView(discord.ui.View):
    """
    Added to every embed posted in MEMBER_FORM_CH.
    Lets reviewers accept or deny an applicant.
    """
    def __init__(self, guild: discord.Guild, uid: int, region: str, focus: str):
        super().__init__(timeout=None)
        self.guild, self.uid, self.region, self.focus = guild, uid, region, focus

    # helper to fetch reviewers set (async!)
    async def _reviewers(self) -> set[int]:
        return await db.get_reviewers()

    # -------------- Accept button --------------
    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅", custom_id="memberform_accept")
    async def accept(self, inter, _):
        try:
            if (inter.user.id not in await self._reviewers()
                and not inter.user.guild_permissions.manage_roles):
                return await inter.response.send_message(
                    "Not authorised.", ephemeral=True)

            # Fetch the member to accept
            member = await safe_fetch(self.guild, self.uid)
            if not member:
                return await inter.response.send_message(
                    "Member not found.", ephemeral=True)

            # Collect all roles to add
            roles = [
                r for r in (
                    self.guild.get_role(ACCEPT_ROLE_ID),
                    self.guild.get_role(REGION_ROLE_IDS.get(self.region, 0)),
                    self.guild.get_role(FOCUS_ROLE_IDS.get(self.focus, 0)),
                ) if r
            ]
            if not roles:
                return await inter.response.send_message(
                    "Some roles are missing.", ephemeral=True)

            try:
                await member.add_roles(*roles, reason="Application accepted")
            except discord.Forbidden:
                return await inter.response.send_message(
                    "Missing permissions to add roles.", ephemeral=True)

            # Remove application roles
            try:
                unc = self.guild.get_role(UNCOMPLETED_APP_ROLE_ID)
                comp = self.guild.get_role(COMPLETED_APP_ROLE_ID)
                cleanup = [r for r in (unc, comp) if r and r in member.roles]
                if cleanup:
                    await member.remove_roles(*cleanup, reason="Application accepted")
            except discord.Forbidden:
                print(f"[ACCEPT] Can't remove app roles from {member}")

            await inter.response.send_message(
                f"{member.mention} accepted ✅", ephemeral=True)
            await db.update_member_form_status(inter.message.id, "accepted")

            # Change embed color to green
            embed = inter.message.embeds[0]
            embed.color = discord.Color.green()
            await inter.message.edit(embed=embed)

            # Disable buttons
            for c in self.children:
                c.disabled = True
            await inter.message.edit(view=self)
        except Exception as exc:
            print(f"[ACCEPT BUTTON ERROR] {type(exc).__name__}: {exc}")
            try:
                if not inter.response.is_done():
                    await inter.response.send_message(
                        f"Error: {exc}", ephemeral=True)
                else:
                    await inter.followup.send(
                        f"Error: {exc}", ephemeral=True)
            except Exception as exc2:
                print(f"Could not send error message: {exc2}")

    # -------------- Deny button --------------
    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="⛔", custom_id="memberform_deny")
    async def deny(self, inter: discord.Interaction, _):
        if (inter.user.id not in await self._reviewers()
                and not inter.user.guild_permissions.ban_members):
            return await inter.response.send_message(
                "Not authorised.", ephemeral=True)

        member = await safe_fetch(self.guild, self.uid)
        if not member:
            return await inter.response.send_message(
                "Member not found.", ephemeral=True)

        await self.guild.ban(
            member,
            reason="Application denied – 7-day temp-ban",
            delete_message_seconds=0
        )
        await inter.response.send_message(
            f"{member.mention} denied ⛔", ephemeral=True)
        await db.update_member_form_status(inter.message.id, "denied")

        embed = inter.message.embeds[0]
        embed.color = discord.Color.red()
        await inter.message.edit(embed=embed)

        for c in self.children:
            c.disabled = True
        await inter.message.edit(view=self)

        # schedule unban
        async def unban_later():
            await asyncio.sleep(TEMP_BAN_SECONDS)
            try:
                await self.guild.unban(
                    discord.Object(id=self.uid),
                    reason="Temp ban expired"
                )
            except discord.HTTPException:
                pass
        asyncio.create_task(unban_later())
# ───────────────────────── helper to fetch a member safely ─────────────────────
async def safe_fetch(guild: discord.Guild, uid: int) -> discord.Member | None:
    try:
        return await guild.fetch_member(uid)
    except (discord.NotFound, discord.HTTPException):
        return None

# ───────────────────────────── FinalRegistrationModal ──────────────────────────
class FinalRegistrationModal(discord.ui.Modal):
    """
    Final modal that collects the long-answer questions and then posts the
    embed for reviewers.
    """
    ban_expl: discord.ui.TextInput | None
    gender:   discord.ui.TextInput | None
    referral: discord.ui.TextInput | None

    def __init__(self, v: MemberRegistrationView):
        needs_ban = v.data.get("bans") == "Yes"
        super().__init__(title="More Details" if needs_ban else "Additional Info")
        self.v = v

        # core fields
        self.steam = discord.ui.TextInput(
            label="Steam Profile Link",
            placeholder="https://steamcommunity.com/…",
            required=True
        )
        self.hours = discord.ui.TextInput(
            label="Hours in Rust", required=True)
        self.heard = discord.ui.TextInput(
            label="Where did you hear about us?", required=True)

        self.ban_expl = self.gender = self.referral = None

        if needs_ban:
            self.ban_expl = discord.ui.TextInput(
                label="Ban Explanation",
                style=discord.TextStyle.paragraph,
                required=True
            )
            self.referral = discord.ui.TextInput(
                label="Referral (optional)", required=False)
            components = (
                self.steam, self.hours, self.heard,
                self.ban_expl, self.referral
            )
        else:
            self.referral = discord.ui.TextInput(
                label="Referral (optional)", required=False)
            self.gender = discord.ui.TextInput(
                label="Gender (optional)", required=False)
            components = (
                self.steam, self.hours, self.heard,
                self.referral, self.gender
            )

        for c in components:
            self.add_item(c)

    # -------------------------- submit handler --------------------------
    async def on_submit(self, inter: discord.Interaction):
        d, user = self.v.data, (self.v.user or inter.user)

        # ------------ build embed ------------
        e = (
            discord.Embed(
                title="📋 NEW MEMBER REGISTRATION",
                colour=discord.Color.gold(),
                timestamp=inter.created_at
            )
            .set_author(name=str(user), icon_url=user.display_avatar.url)
            .set_thumbnail(url=user.display_avatar.url)
        )
        e.add_field(name="\u200b", value="\u200b", inline=False)
        e.add_field(name="👤 User",   value=user.mention, inline=False)
        e.add_field(name="🔗 Steam",  value=self.steam.value, inline=False)
        e.add_field(name="🗓️ Age",   value=d["age"],    inline=True)
        e.add_field(name="🌍 Region", value=d["region"], inline=True)
        e.add_field(name="🚫 Bans",   value=d["bans"],   inline=True)
        if d["bans"] == "Yes" and self.ban_expl:
            e.add_field(name="📝 Ban Explanation",
                        value=self.ban_expl.value, inline=False)
        e.add_field(name="🎯 Focus",  value=d["focus"], inline=True)
        e.add_field(name="⭐ Skill",  value=d["skill"], inline=True)
        e.add_field(name="⏱️ Hours", value=self.hours.value, inline=True)
        e.add_field(name="📢 Heard about us",
                    value=self.heard.value, inline=False)
        e.add_field(name="🤝 Referral",
                    value=self.referral.value if self.referral else "N/A",
                    inline=True)
        if self.gender:
            e.add_field(name="⚧️ Gender",
                        value=self.gender.value or "N/A",
                        inline=True)
        e.add_field(name="\u200b", value="\u200b", inline=False)

        # ------------ DB save ------------
        await db.add_member_form(user.id, {
            "age": d["age"],
            "region": d["region"],
            "bans": d["bans"],
            "ban_explanation": self.ban_expl.value if self.ban_expl else None,
            "focus": d["focus"],
            "skill": d["skill"],
            "steam": self.steam.value,
            "hours": self.hours.value,
            "heard": self.heard.value,
            "referral": self.referral.value if self.referral else None,
            "gender": self.gender.value if self.gender else None
        })

        # ------------ swap application roles ------------
        try:
            applicant = await inter.guild.fetch_member(user.id)
            unc = inter.guild.get_role(UNCOMPLETED_APP_ROLE_ID)
            comp = inter.guild.get_role(COMPLETED_APP_ROLE_ID)

            if comp and comp not in applicant.roles:
                await applicant.add_roles(
                    comp, reason="Application submitted")
            if unc and unc in applicant.roles:
                await applicant.remove_roles(
                    unc, reason="Application submitted")
        except discord.Forbidden:
            print(f"[FORM] Can't modify roles for {applicant}")

        # ------------ send to reviewer channel ------------
        form_msg = await inter.client.get_channel(MEMBER_FORM_CH).send(
            embed=e,
            view=ActionView(inter.guild, user.id,
                            d["region"], d["focus"])
        )
        await db.add_member_form(
            user.id,
            {
                "age": d["age"],
                "region": d["region"],
                "bans": d["bans"],
                "ban_explanation": self.ban_expl.value if self.ban_expl else None,
                "focus": d["focus"],
                "skill": d["skill"],
                "steam": self.steam.value,
                "hours": self.hours.value,
                "heard": self.heard.value,
                "referral": self.referral.value if self.referral else None,
                "gender": self.gender.value if self.gender else None
            },
            message_id=form_msg.id
        )

        # ------------ acknowledge user ------------
        await inter.response.send_message(
            "Registration submitted – thank you!", ephemeral=True
        )

        done = await inter.original_response()

        # clean up ephemeral helper messages
        async def tidy():
            await asyncio.sleep(2)
            for m in (self.v.start_msg, self.v.submit_msg, done):
                try:
                    if m:
                        await m.delete()
                except discord.HTTPException:
                    pass
        asyncio.create_task(tidy())

# ───────────────────────────── /memberform slash command ───────────────────────
@bot.tree.command(name="memberform", description="Start member registration")
async def memberform(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    # Do slow work, then:
    await inter.followup.send(
        "Click below to begin registration:",
        view=MemberRegistrationView(),
        ephemeral=True
    )

# ════════════════════════ on_ready & startup ══════════════════════════
@bot.event
async def on_ready():
    await db.connect()
    print(f"Logged in as {bot.user} ({bot.user.id})")

    guild_obj = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild_obj)
    await bot.tree.sync(guild=guild_obj)
    print("Slash-commands synced")

    await update_codes_message(bot, await db.get_codes())

    await resume_member_forms()
    await resume_staff_applications()
    await (import_module("cogs.giveaways").setup)(bot, db)

    bot.loop.create_task(listen_for_code_changes())

    if not activity_maintenance.is_running():
        activity_maintenance.start()

    print("Giveaways resumed – code-listener running")

# ══════════════════════════════════════════════════════════════════════
#  ENTRY POINTS  – safe for import OR direct execution
# ══════════════════════════════════════════════════════════════════════
async def _run_bot():
    """Internal helper used by main()."""
    await bot.start(BOT_TOKEN)

def main() -> None:
    """
    • If called from a script with no running loop → starts asyncio.run().
    • If called from code that is already inside a loop (FastAPI) →
      schedules bot.start() on that loop.
    """
    if not BOT_TOKEN or not DATABASE_URL:
        raise RuntimeError("Set BOT_TOKEN and DATABASE_URL environment variables!")

    try:
        loop = asyncio.get_running_loop()   # are we already inside a loop?
    except RuntimeError:
        # No loop yet → normal CLI usage
        asyncio.run(_run_bot())
    else:
        # Loop exists → schedule bot.start() in background
        loop.create_task(_run_bot())

# Standard guard so the bot still starts when executed directly
if __name__ == "__main__":
    main()