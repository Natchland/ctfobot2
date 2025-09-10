# ──────────────────────────────────────────────────────────────────────
#  ctfobot2.0.py   –  CTFO Discord bot + giveaway / registration system
# ──────────────────────────────────────────────────────────────────────
import os, sys, asyncio, signal, json
from datetime import datetime, timedelta, timezone, date
from random import choice
from typing import Dict, Any

import discord, asyncpg
from discord import app_commands
from discord.ext import commands, tasks

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
FOOTER_END_TAG      = "END:"
FOOTER_PRIZE_TAG    = "PRIZE:"
PROMOTE_STREAK      = 3
INACTIVE_AFTER_DAYS = 5
WARN_BEFORE_DAYS    = INACTIVE_AFTER_DAYS - 1

ADMIN_ID        = 1377103244089622719
ELECTRICIAN_ID  = 1380233234675400875
GROUP_LEADER_ID = 1377077466513932338
PLAYER_MGMT_ID  = 1377084533706588201
TRUSTED_ID      = 1400584430900219935

CODE_NAMES = ["Master", "Guest", "Electrician", "Other"]

# ══════════════════════════════════════════════════════════════════════
#                             BOT / INTENTS
# ══════════════════════════════════════════════════════════════════════
class CTFBot(commands.Bot):
    last_anonymous_time: dict[int, datetime]
    giveaway_stop_events: dict[int, asyncio.Event]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_anonymous_time = {}
        self.giveaway_stop_events = {}

# ── instantiate your custom bot ──
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = CTFBot(command_prefix="!", intents=intents)

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
                public BOOLEAN    NOT NULL DEFAULT FALSE
            );
            CREATE TABLE IF NOT EXISTS reviewers (
                user_id BIGINT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS activity (
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
                end_ts     BIGINT,
                active     BOOLEAN,
                note       TEXT
            );
            CREATE TABLE IF NOT EXISTS member_forms (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT,
                created_at TIMESTAMP DEFAULT now(),
                data       JSONB,
                status     TEXT NOT NULL DEFAULT 'pending'
            );

            ALTER TABLE member_forms
            ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';
            """)
            # ── NOTIFY for /codes edits → bot refresh ─────
            await conn.execute("""
            CREATE OR REPLACE FUNCTION notify_codes_changed()
            RETURNS trigger AS $$
            BEGIN
                PERFORM pg_notify('codes_changed', 'refresh');
                RETURN NEW;
            END; $$ LANGUAGE plpgsql;
            """)
            await conn.execute("""
            DROP TRIGGER IF EXISTS codes_changed_trigger ON codes;
            CREATE TRIGGER codes_changed_trigger
            AFTER INSERT OR UPDATE OR DELETE ON codes
            FOR EACH STATEMENT
            EXECUTE FUNCTION notify_codes_changed();
            """)
            # ── NOTIFY for /giveaways edits → bot refresh ──
            await conn.execute("""
            CREATE OR REPLACE FUNCTION notify_giveaways_changed()
            RETURNS trigger AS $$
            BEGIN
                PERFORM pg_notify('giveaways_changed', NEW.id::text);
                RETURN NEW;
            END; $$ LANGUAGE plpgsql;
            """)
            await conn.execute("""
            DROP TRIGGER IF EXISTS giveaways_changed_trigger ON giveaways;
            CREATE TRIGGER giveaways_changed_trigger
            AFTER UPDATE ON giveaways
              FOR EACH ROW
              WHEN (OLD.* IS DISTINCT FROM NEW.*)
            EXECUTE FUNCTION notify_giveaways_changed();
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

    # ────────────────────────────────────────────────────────────────
    #  GIVEAWAYS
    # ────────────────────────────────────────────────────────────────
    async def add_giveaway(self, ch_id, msg_id, prize, end_ts):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO giveaways
                (channel_id, message_id, prize, end_ts, active)
                VALUES ($1,$2,$3,$4,TRUE)
                """,
                ch_id, msg_id, prize, end_ts
            )

    async def end_giveaway(self, msg_id):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE giveaways SET active=FALSE WHERE message_id=$1",
                msg_id
            )

    async def get_active_giveaways(self):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM giveaways WHERE active=TRUE"
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
    Runs once per day.
    1. Grants the Active-Member role to users whose streak is high enough.
    2. Sends a warning message if they are about to lose the role.
    3. Removes the role after prolonged inactivity.
    """
    await bot.wait_until_ready()

    guild   = bot.get_guild(GUILD_ID)
    role    = guild.get_role(ACTIVE_MEMBER_ROLE_ID) if guild else None
    warn_ch = guild.get_channel(WARNING_CH_ID) if guild else None
    today   = date.today()

    if not guild or not role:
        print("[activity] Guild or role not found — skipping cycle")
        return

    records = await db.get_all_activity()           # {uid: dict}
    promoted = demoted = warned_n = 0

    for uid, rec in records.items():
        member = guild.get_member(uid)
        if not member or member.bot:
            continue

        # ---- 1) PROMOTE IF STREAK MET --------------------------------
        if rec["streak"] >= PROMOTE_STREAK and role not in member.roles:
            try:
                await member.add_roles(role, reason="Reached activity streak (cron)")
                promoted += 1
                print(f"[PROMOTE] {member} (streak {rec['streak']})")
            except discord.Forbidden:
                print(f"[PROMOTE] Missing perms for {member}")

        # ---- compute inactivity --------------------------------------
        days_idle = (today - rec["date"]).days
        already_warned = rec["warned"]

        # ---- 2) SEND WARNING ----------------------------------------
        if (
            days_idle == WARN_BEFORE_DAYS
            and role in member.roles
            and not already_warned
        ):
            if warn_ch:
                await warn_ch.send(
                    f"{member.mention} You’ve been inactive for "
                    f"{days_idle} days – please pop in or you’ll lose your role."
                )
            await db.set_activity(
                uid, rec["streak"], rec["date"], True, rec["last"]
            )
            warned_n += 1

        # ---- 3) DEMOTE ----------------------------------------------
        if days_idle >= INACTIVE_AFTER_DAYS and role in member.roles:
            try:
                await member.remove_roles(role, reason="Inactive > 5 days")
                demoted += 1
                print(f"[DEMOTE] {member} – inactive {days_idle} days")
            except discord.Forbidden:
                print(f"[DEMOTE] Missing perms for {member}")
            await db.set_activity(uid, 0, rec["date"], False, rec["last"])

    print(
        f"[activity] cycle complete: +{promoted} promoted, "
        f"{warned_n} warned, –{demoted} demoted"
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
# ═══════════════════════════  /codes  COMMANDS  ═══════════════════════
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

# ═══════════════════════════════ GIVEAWAYS ════════════════════════════
def fmt_time(seconds: int) -> str:
    """
    Turn a number of seconds into a compact human-readable string.
    Examples: 2d 4h, 3h 17m, 7m 12s, 4s
    """
    if seconds < 0:
        seconds = 0

    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    mins, seconds = divmod(seconds, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    if days or hours or mins:
        parts.append(f"{mins}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def put_field(e: discord.Embed, idx: int, *, name: str, value: str, inline=False):
    """Insert / replace an embed field at a given index."""
    if idx < len(e.fields):
        e.set_field_at(idx, name=name, value=value, inline=inline)
    else:
        while len(e.fields) < idx:
            e.add_field(name="\u200b", value="\u200b", inline=False)
        e.add_field(name=name, value=value, inline=inline)


def eligible(guild: discord.Guild):
    role = guild.get_role(GIVEAWAY_ROLE_ID)
    return [m for m in role.members if not m.bot] if role else []

# ═════════════════════ GIVEAWAY REFRESHER (panel-driven) ═════════════
async def refresh_giveaway_from_row(row: dict):
    """
    Patch the embed whenever the web panel updates a giveaway row.
    """
    guild = bot.get_guild(GUILD_ID)
    chan  = guild.get_channel(row["channel_id"]) if guild else None
    if not guild or not chan:
        return
    try:
        msg = await chan.fetch_message(row["message_id"])
    except discord.NotFound:
        return

    embed = msg.embeds[0]
    # Update prize field (index 0 in our embed)
    put_field(embed, 0, name="Prize", value=f"**{row['prize']}**", inline=False)

    now = int(datetime.now(timezone.utc).timestamp())
    remaining = row["end_ts"] - now

    if not row["active"]:
        put_field(embed, 1, name="Time left", value="**ENDED**", inline=False)
        embed.colour = discord.Color.dark_gray()
        await msg.edit(embed=embed, view=None)
        ev = bot.giveaway_stop_events.get(row["message_id"])
        if ev and not ev.is_set():
            ev.set()
        return

    # Still running
    put_field(embed, 1, name="Time left", value=f"**{fmt_time(remaining)}**", inline=False)
    embed.set_footer(text=f"{FOOTER_END_TAG}{row['end_ts']}|{FOOTER_PRIZE_TAG}{row['prize']}")
    await msg.edit(embed=embed)


async def listen_for_giveaway_changes():
    """
    LISTEN on Postgres channel 'giveaways_changed' so the bot reacts
    to edits coming from the FastAPI admin panel.
    """
    conn = await asyncpg.connect(DATABASE_URL)

    async def _listener(_c, _pid, _chan, payload):
        gid = int(payload)
        row = await db.pool.fetchrow("SELECT * FROM giveaways WHERE id=$1", gid)
        if row:
            await refresh_giveaway_from_row(dict(row))

    await conn.add_listener("giveaways_changed", _listener)
    print("[giveaways_changed] listener attached")

    try:
        while not bot.is_closed():
            await asyncio.sleep(3600)
    finally:
        await conn.close()


class GiveawayControl(discord.ui.View):
    def __init__(self, guild, ch_id, msg_id, prize, stop_event: asyncio.Event):
        super().__init__(timeout=None)
        self.guild, self.ch_id, self.msg_id, self.prize, self.stop = (
            guild, ch_id, msg_id, prize, stop_event
        )

    # ---------- helpers ----------
    def _admin(self, member: discord.Member) -> bool:
        return member.guild_permissions.administrator or member.id == bot.owner_id

    async def _finish(self, ended_text: str, colour: discord.Colour):
        chan = self.guild.get_channel(self.ch_id)
        msg  = await chan.fetch_message(self.msg_id)

        embed = msg.embeds[0]
        put_field(embed, 1, name="Time left", value=f"**{ended_text}**")
        put_field(embed, 3, name="Eligible Entrants",
                  value=f"Giveaway {ended_text.lower()}.")
        embed.color = colour
        await msg.edit(embed=embed, view=None)
        self.stop.set()
        await db.end_giveaway(self.msg_id)

    # ---------- buttons ----------
    @discord.ui.button(label="End & Draw", style=discord.ButtonStyle.success,
                       emoji="🎰", custom_id="gw_end")
    async def end(self, inter: discord.Interaction, _):
        if not self._admin(inter.user):
            return await inter.response.send_message("Not authorised.",
                                                     ephemeral=True)
        chan = self.guild.get_channel(self.ch_id)
        winner = choice(eligible(self.guild)) if eligible(self.guild) else None
        if winner:
            await chan.send(
                embed=discord.Embed(
                    title=f"🎉 {self.prize} – WINNER 🎉",
                    description=f"Congrats {winner.mention}! Enjoy **{self.prize}**!",
                    colour=discord.Color.gold(),
                )
            )
        else:
            await chan.send("No eligible entrants.")
        await self._finish("ENDED", discord.Color.dark_gray())
        await inter.response.send_message("Ended.", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger,
                       emoji="🛑", custom_id="gw_cancel")
    async def cancel(self, inter: discord.Interaction, _):
        if not self._admin(inter.user):
            return await inter.response.send_message("Not authorised.",
                                                     ephemeral=True)
        await self._finish("CANCELLED", discord.Color.red())
        await inter.response.send_message("Cancelled.", ephemeral=True)


async def run_giveaway(guild, ch_id, msg_id, prize, end_ts, stop):
    """
    Background task that updates the timer field and, when time is up,
    announces the winner (unless the giveaway was ended/cancelled manually).
    """
    channel = guild.get_channel(ch_id)
    if not channel:
        return

    try:
        message = await channel.fetch_message(msg_id)
    except discord.NotFound:
        return

    last_display = None  # cache to avoid needless edits

    while not stop.is_set():
        now = int(datetime.now(timezone.utc).timestamp())
        remaining = end_ts - now
        if remaining <= 0:
            break

        display = fmt_time(remaining)
        if display != last_display:
            last_display = display
            embed = message.embeds[0]

            entrants_txt = "\n".join(m.mention for m in eligible(guild)) or "*None yet*"
            put_field(embed, 1, name="Time left", value=f"**{display}**")
            put_field(embed, 3, name="Eligible Entrants", value=entrants_txt)

            try:
                await message.edit(embed=embed)
            except discord.HTTPException:
                pass

        # dynamic sleep: 30s (>5 min), 10s (5 min-10 s), 1s (≤10 s)
        sleep_for = 1 if remaining <= 10 else 10 if remaining <= 300 else 30
        await asyncio.sleep(sleep_for)

    # ---------- time expired ----------
    if stop.is_set():            # ended or cancelled manually
        return

    pool = eligible(guild)
    if pool:
        winner = choice(pool)
        await channel.send(
            embed=discord.Embed(
                title=f"🎉 {prize} – WINNER 🎉",
                description=f"Congratulations {winner.mention}! You won **{prize}**!",
                colour=discord.Color.gold(),
            )
        )
    else:
        await channel.send("No eligible entrants.")

    await db.end_giveaway(msg_id)


async def resume_giveaways():
    """
    Called in on_ready(): restores any active giveaways saved in the database.
    """
    guild = bot.get_guild(GUILD_ID)
    channel = guild.get_channel(GIVEAWAY_CH_ID) if guild else None
    if not guild or not channel:
        return

    for row in await db.get_active_giveaways():
        stop = asyncio.Event()
        bot.giveaway_stop_events[row['message_id']] = stop
        view = GiveawayControl(guild, row['channel_id'],
                               row['message_id'], row['prize'], stop)
        bot.add_view(view, message_id=row['message_id'])
        asyncio.create_task(
            run_giveaway(guild, row['channel_id'], row['message_id'],
                         row['prize'], row['end_ts'], stop)
        )


# ────────────────────────── /giveaway command ─────────────────────────
@bot.tree.command(name="giveaway", description="Start a giveaway")
@app_commands.check(lambda i: i.user.guild_permissions.administrator)
@app_commands.choices(
    duration=[
        app_commands.Choice(name="7 days",  value=7),
        app_commands.Choice(name="14 days", value=14),
        app_commands.Choice(name="30 days", value=30),
    ]
)
@app_commands.describe(prize="Prize to give away")
async def giveaway(inter: discord.Interaction,
                   duration: app_commands.Choice[int],
                   prize: str):
    # acknowledge right away to avoid 3-second limit
    await inter.response.defer(ephemeral=True)

    guild = inter.guild
    channel = guild.get_channel(GIVEAWAY_CH_ID)
    role    = guild.get_role(GIVEAWAY_ROLE_ID)
    if not channel or not role:
        return await inter.followup.send(
            "Giveaway channel or role missing.", ephemeral=True
        )

    end_ts = int(datetime.now(timezone.utc).timestamp()) + duration.value * 86_400
    stop   = asyncio.Event()

    embed = discord.Embed(title="🎉 GIVEAWAY 🎉",
                          colour=discord.Color.blurple())
    embed.add_field(name="Prize",             value=f"**{prize}**",
                    inline=False)
    embed.add_field(name="Time left",         value=f"**{duration.name}**",
                    inline=False)
    embed.add_field(name="Eligibility",
                    value=f"Only {role.mention} can win.",
                    inline=False)
    embed.add_field(name="Eligible Entrants", value="*Updating…*",
                    inline=False)
    embed.set_footer(text=f"||END:{end_ts}|PRIZE:{prize}||")

    view = GiveawayControl(guild, channel.id, 0, prize, stop)
    message = await channel.send(embed=embed, view=view)
    view.msg_id = view.message_id = message.id
    bot.add_view(view, message_id=message.id)

    await db.add_giveaway(channel.id, message.id, prize, end_ts)
    asyncio.create_task(
        run_giveaway(guild, channel.id, message.id, prize, end_ts, stop)
    )

    await inter.followup.send(
        f"Giveaway started in {channel.mention}.", ephemeral=True
    )
# ──────────────────────────────────────────────────────────────────────

# ════════════════════════ on_ready & startup ══════════════════════════
@bot.event
async def on_ready():
    await db.connect()
    print(f"Logged in as {bot.user} ({bot.user.id})")

    # sync slash-commands to the guild
    guild_obj = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild_obj)
    await bot.tree.sync(guild=guild_obj)
    print("Slash-commands synced")

    # initial /codes embed
    await update_codes_message(bot, await db.get_codes())

    # resume any giveaways stored in DB
    await resume_giveaways()

    await resume_member_forms()

    # start the LISTEN codes_changed background task
    bot.loop.create_task(listen_for_code_changes())
    bot.loop.create_task(listen_for_giveaway_changes())

    if not activity_maintenance.is_running():
        activity_maintenance.start()

    print("Giveaways resumed – code-listener running")

# ══════════════════════════════════════════════════════════════════════
if not BOT_TOKEN or not DATABASE_URL:
    raise RuntimeError("Set BOT_TOKEN and DATABASE_URL environment variables!")

if __name__ == "__main__":
    bot.run(BOT_TOKEN)