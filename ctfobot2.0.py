import os
import sys
import asyncio
import signal
import discord
import asyncpg
import json
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta, timezone, date
from typing import Dict, Any
from random import choice

# ══ Configuration ═══════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GUILD_ID = int(os.getenv("GUILD_ID", "1377035207777194005"))
FEEDBACK_CH = 1413188006499586158
MEMBER_FORM_CH = 1413672763108888636
WARNING_CH_ID = 1398657081338237028

ACCEPT_ROLE_ID = 1377075930144571452
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
TEMP_BAN_SECONDS     = 7 * 24 * 60 * 60
GIVEAWAY_ROLE_ID     = 1403337937722019931
GIVEAWAY_CH_ID       = 1413929735658016899
CODES_CH_ID          = 1398667158237483138
EMBED_TITLE          = "🎉 GIVEAWAY 🎉"
FOOTER_END_TAG       = "END:"
FOOTER_PRIZE_TAG     = "PRIZE:"
PROMOTE_STREAK       = 3
INACTIVE_AFTER_DAYS  = 5
WARN_BEFORE_DAYS     = INACTIVE_AFTER_DAYS - 1

ADMIN_ID = 1377103244089622719
ELECTRICIAN_ID = 1380233234675400875
GROUP_LEADER_ID = 1377077466513932338
PLAYER_MGMT_ID = 1377084533706588201
TRUSTED_ID = 1400584430900219935

CODE_NAMES = ["Master", "Guest", "Electrician", "Other"]

# ══ Bot/intents ═════════════════════════════
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot("!", intents=intents)

bot.last_anonymous_time = {}
bot.giveaway_stop_events = {}

# ══ Database helpers ════════════════════════
class Database:
    def __init__(self, dsn):
        self.dsn = dsn
        self.pool = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
        await self.init_tables()

    async def init_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS codes (
                    name TEXT PRIMARY KEY,
                    pin VARCHAR(4) NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviewers (
                    user_id BIGINT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS activity (
                    user_id BIGINT PRIMARY KEY,
                    streak INTEGER,
                    date DATE,
                    warned BOOLEAN,
                    last TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS giveaways (
                    id SERIAL PRIMARY KEY,
                    channel_id BIGINT,
                    message_id BIGINT,
                    prize TEXT,
                    end_ts BIGINT,
                    active BOOLEAN
                );
                CREATE TABLE IF NOT EXISTS member_forms (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    created_at TIMESTAMP DEFAULT now(),
                    data JSONB
                );
            """)

    # Codes
    async def get_codes(self):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT name, pin FROM codes ORDER BY name")
            return {r['name']: r['pin'] for r in rows}

    async def add_code(self, name, pin):
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO codes (name, pin) VALUES ($1, $2)", name, pin)

    async def edit_code(self, name, pin):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE codes SET pin=$2 WHERE name=$1", name, pin)

    async def remove_code(self, name):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM codes WHERE name=$1", name)

    # Reviewers
    async def get_reviewers(self):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM reviewers")
            return set(r['user_id'] for r in rows)

    async def add_reviewer(self, user_id):
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO reviewers (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)

    async def remove_reviewer(self, user_id):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM reviewers WHERE user_id=$1", user_id)

    # Activity
    async def get_activity(self, user_id):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM activity WHERE user_id=$1", user_id)
            return dict(row) if row else None

    async def set_activity(self, user_id, streak, date, warned, last):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO activity (user_id, streak, date, warned, last)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id)
                DO UPDATE SET streak=$2, date=$3, warned=$4, last=$5
            """, user_id, streak, date, warned, last)

    async def get_all_activity(self):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM activity")
            return {r['user_id']: dict(r) for r in rows}

    # Giveaways
    async def add_giveaway(self, channel_id, message_id, prize, end_ts):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO giveaways (channel_id, message_id, prize, end_ts, active)
                VALUES ($1, $2, $3, $4, TRUE)
            """, channel_id, message_id, prize, end_ts)

    async def end_giveaway(self, message_id):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE giveaways SET active=FALSE WHERE message_id=$1", message_id)

    async def get_active_giveaways(self):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM giveaways WHERE active=TRUE")
            return [dict(r) for r in rows]

    # Member forms
    async def add_member_form(self, user_id, data):
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO member_forms (user_id, data) VALUES ($1, $2)", user_id, json.dumps(data))

    async def get_member_forms(self, user_id=None):
        async with self.pool.acquire() as conn:
            if user_id:
                rows = await conn.fetch("SELECT * FROM member_forms WHERE user_id=$1", user_id)
            else:
                rows = await conn.fetch("SELECT * FROM member_forms")
            return [dict(r) for r in rows]

db = Database(DATABASE_URL)

# ══ Codes Embed Utilities ═══════════════════
def build_codes_embed(codes: dict) -> discord.Embed:
    embed = discord.Embed(
        title="🔑 Access Codes",
        description="Below are the current access codes. Contact an admin if you need access.",
        color=discord.Color.blue(),
    )
    if codes:
        for name, pin in codes.items():
            embed.add_field(name=name, value=f"`{pin}`", inline=False)
    else:
        embed.description += "\n\n*No codes set yet.*"
    embed.set_footer(text="Code list is kept up to date by staff.")
    return embed

async def update_codes_message(bot, codes: dict):
    channel = bot.get_channel(CODES_CH_ID)
    if not channel:
        print("Codes channel missing!")
        return

    msg_id_file = "codes_msg_id.txt"
    msg_id = None
    if os.path.isfile(msg_id_file):
        with open(msg_id_file, "r") as f:
            try:
                msg_id = int(f.read().strip())
            except Exception:
                pass

    embed = build_codes_embed(codes)
    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed)
            return
        except Exception:
            pass

    msg = await channel.send(embed=embed)
    with open(msg_id_file, "w") as f:
        f.write(str(msg.id))

# ══ Reviewer List helpers (DB-backed) ══════
async def is_admin_or_reviewer(inter: discord.Interaction) -> bool:
    reviewers = await db.get_reviewers()
    return inter.user.guild_permissions.administrator or inter.user.id in reviewers

# ══ Activity Helper (DB-backed) ════════════
async def mark_active(member: discord.Member):
    if member.bot:
        return
    today = date.today()
    rec = await db.get_activity(member.id)
    if not rec:
        streak = 1
        warned = False
    else:
        if rec['date'] != today:
            lastdate = rec['date']
            yesterday = lastdate + timedelta(days=1)
            streak = rec['streak'] + 1 if yesterday == today else 1
            warned = False
        else:
            streak = rec['streak']
            warned = rec['warned']
    last = datetime.now(timezone.utc)
    await db.set_activity(member.id, streak, today, warned, last)

@bot.event
async def on_message(msg: discord.Message):
    if msg.guild and not msg.author.bot:
        await mark_active(msg.author)

@bot.event
async def on_voice_state_update(member, before, after):
    if (
        member.guild.id == GUILD_ID
        and not member.bot
        and not before.channel
        and after.channel
    ):
        await mark_active(member)

# ========== /codes command group ==========
class CodesCog(commands.Cog):
    def __init__(self, bot, db):
        self.bot = bot
        self.db = db

    codes_group = app_commands.Group(name="codes", description="Manage access codes")

    @codes_group.command(name="add", description="Add a new code")
    @app_commands.describe(name="Name of the code", pin="4-digit code (e.g. 1234)")
    async def codes_add(self, inter: discord.Interaction, name: str, pin: str):
        if not await is_admin_or_reviewer(inter):
            await inter.response.send_message("You need admin permission.", ephemeral=True)
            return

        codes = await self.db.get_codes()
        if name in codes:
            await inter.response.send_message("A code with that name already exists.", ephemeral=True)
            return
        if not (pin.isdigit() and len(pin) == 4):
            await inter.response.send_message("PIN must be a 4-digit number.", ephemeral=True)
            return
        await self.db.add_code(name, pin)
        codes = await self.db.get_codes()
        await update_codes_message(self.bot, codes)
        await inter.response.send_message(f"Added code `{name}: {pin}`.", ephemeral=True)

    @codes_group.command(name="edit", description="Edit the PIN for an existing code")
    @app_commands.describe(name="Name of the code", pin="New 4-digit code")
    async def codes_edit(self, inter: discord.Interaction, name: str, pin: str):
        if not await is_admin_or_reviewer(inter):
            await inter.response.send_message("You need admin permission.", ephemeral=True)
            return
        codes = await self.db.get_codes()
        if name not in codes:
            await inter.response.send_message("No such code.", ephemeral=True)
            return
        if not (pin.isdigit() and len(pin) == 4):
            await inter.response.send_message("PIN must be a 4-digit number.", ephemeral=True)
            return
        await self.db.edit_code(name, pin)
        codes = await self.db.get_codes()
        await update_codes_message(self.bot, codes)
        await inter.response.send_message(f"Updated code `{name}: {pin}`.", ephemeral=True)

    @codes_group.command(name="remove", description="Remove a code")
    @app_commands.describe(name="Name of the code to remove")
    async def codes_remove(self, inter: discord.Interaction, name: str):
        if not await is_admin_or_reviewer(inter):
            await inter.response.send_message("You need admin permission.", ephemeral=True)
            return
        codes = await self.db.get_codes()
        if name not in codes:
            await inter.response.send_message("No such code.", ephemeral=True)
            return
        await self.db.remove_code(name)
        codes = await self.db.get_codes()
        await update_codes_message(self.bot, codes)
        await inter.response.send_message(f"Removed code `{name}`.", ephemeral=True)

codes_cog = CodesCog(bot, db)
bot.tree.add_command(codes_cog.codes_group)

# Reviewer commands
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

# ========== Feedback command ==========
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

# ========== REGISTRATION WORKFLOW ==========

def opts(*lbl: str):
    return [discord.SelectOption(label=l, value=l) for l in lbl]

class MemberRegistrationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.data: Dict[str, str] = {}
        self.user: discord.User | None = None
        self.start_msg: discord.Message | None = None
        self.submit_msg: discord.Message | None = None
        self.submit_sent = False

    @discord.ui.button(label="Start Registration", style=discord.ButtonStyle.primary)
    async def start(self, inter: discord.Interaction, _):
        self.user = inter.user
        self.clear_items()
        self.add_item(SelectAge(self))
        self.add_item(SelectRegion(self))
        self.add_item(SelectBans(self))
        self.add_item(SelectFocus(self))
        self.add_item(SelectSkill(self))
        await inter.response.send_message(
            "Fill each dropdown – **Submit** appears when all done.",
            view=self,
            ephemeral=True,
        )
        self.start_msg = await inter.original_response()

class SubmitView(discord.ui.View):
    def __init__(self, v):
        super().__init__(timeout=300)
        self.v = v

    @discord.ui.button(label="Submit", style=discord.ButtonStyle.success)
    async def submit(self, inter: discord.Interaction, _):
        await inter.response.send_modal(FinalRegistrationModal(self.v))

class _BaseSelect(discord.ui.Select):
    def __init__(self, v, key, **kw):
        self.v, self.key = v, key
        super().__init__(**kw)

    async def callback(self, inter: discord.Interaction):
        self.v.data[self.key] = self.values[0]
        self.placeholder = self.values[0]
        await inter.response.edit_message(view=self.v)
        if not self.v.submit_sent and all(
            k in self.v.data for k in ("age", "region", "bans", "focus", "skill")
        ):
            self.v.submit_sent = True
            self.v.submit_msg = await inter.followup.send(
                "All set – click **Submit** :",
                view=SubmitView(self.v),
                ephemeral=True,
                wait=True,
            )

class SelectAge(_BaseSelect):
    def __init__(self, v):
        super().__init__(
            v,
            "age",
            placeholder="Age",
            options=opts("12-14", "15-17", "18-21", "21+"),
        )

class SelectRegion(_BaseSelect):
    def __init__(self, v):
        super().__init__(
            v,
            "region",
            placeholder="Region",
            options=opts("North America", "Europe", "Asia", "Other"),
        )

class SelectBans(_BaseSelect):
    def __init__(self, v):
        super().__init__(
            v,
            "bans",
            placeholder="Any bans?",
            options=opts("Yes", "No"),
        )

class SelectFocus(_BaseSelect):
    def __init__(self, v):
        super().__init__(
            v,
            "focus",
            placeholder="Main focus",
            options=opts(
                "PvP",
                "Farming",
                "Base Sorting",
                "Building",
                "Electricity",
            ),
        )

class SelectSkill(_BaseSelect):
    def __init__(self, v):
        super().__init__(
            v,
            "skill",
            placeholder="Skill level",
            options=opts("Beginner", "Intermediate", "Advanced", "Expert"),
        )

class ActionView(discord.ui.View):
    def __init__(self, guild, uid, region, focus):
        super().__init__(timeout=None)
        self.guild, self.uid, self.region, self.focus = guild, uid, region, focus

    @property
    def reviewers(self):
        return bot.loop.create_task(db.get_reviewers())

    def authorised(self, u, perm):
        return u.id in bot.loop.run_until_complete(db.get_reviewers()) or getattr(u.guild_permissions, perm)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, inter, _):
        reviewers = await db.get_reviewers()
        if not (inter.user.id in reviewers or inter.user.guild_permissions.manage_roles):
            return await inter.response.send_message("Not authorised.", ephemeral=True)

        member = await safe_fetch(self.guild, self.uid)
        if not member:
            return await inter.response.send_message("Member not found.", ephemeral=True)

        roles = [
            r
            for r in (
                self.guild.get_role(ACCEPT_ROLE_ID),
                self.guild.get_role(REGION_ROLE_IDS.get(self.region, 0)),
                self.guild.get_role(FOCUS_ROLE_IDS.get(self.focus, 0)),
            )
            if r
        ]

        if not roles:
            return await inter.response.send_message("Roles missing.", ephemeral=True)

        try:
            await member.add_roles(*roles, reason="Application accepted")
        except discord.Forbidden:
            return await inter.response.send_message("Missing perms.", ephemeral=True)

        await inter.response.send_message(f"{member.mention} accepted ✅", ephemeral=True)
        for c in self.children:
            c.disabled = True
        await inter.message.edit(view=self)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="⛔")
    async def deny(self, inter, _):
        reviewers = await db.get_reviewers()
        if not (inter.user.id in reviewers or inter.user.guild_permissions.ban_members):
            return await inter.response.send_message("Not authorised.", ephemeral=True)

        member = await safe_fetch(self.guild, self.uid)
        if not member:
            return await inter.response.send_message("Member not found.", ephemeral=True)

        await self.guild.ban(
            member,
            reason="Application denied – 7-day temp-ban",
            delete_message_seconds=0,
        )
        await inter.response.send_message(f"{member.mention} denied ⛔", ephemeral=True)
        for c in self.children:
            c.disabled = True
        await inter.message.edit(view=self)

        async def unban_later():
            await asyncio.sleep(TEMP_BAN_SECONDS)
            try:
                await self.guild.unban(
                    discord.Object(id=self.uid), reason="Temp ban expired"
                )
            except discord.HTTPException:
                pass

        asyncio.create_task(unban_later())

async def safe_fetch(guild, uid):
    try:
        return await guild.fetch_member(uid)
    except (discord.NotFound, discord.HTTPException):
        return None

class FinalRegistrationModal(discord.ui.Modal):
    ban_expl: discord.ui.TextInput | None
    gender: discord.ui.TextInput | None
    referral: discord.ui.TextInput | None

    def __init__(self, v):
        needs_ban = v.data.get("bans") == "Yes"
        super().__init__(title="More Details" if needs_ban else "Additional Info")
        self.v = v

        self.steam = discord.ui.TextInput(
            label="Steam Profile Link",
            placeholder="https://steamcommunity.com/…",
            required=True,
        )
        self.hours = discord.ui.TextInput(label="Hours in Rust", required=True)
        self.heard = discord.ui.TextInput(
            label="Where did you hear about us?", required=True
        )

        self.ban_expl = self.gender = self.referral = None
        if needs_ban:
            self.ban_expl = discord.ui.TextInput(
                label="Ban Explanation",
                style=discord.TextStyle.paragraph,
                required=True,
            )
            self.referral = discord.ui.TextInput(
                label="Referral (optional)", required=False
            )
            comps = (
                self.steam,
                self.hours,
                self.heard,
                self.ban_expl,
                self.referral,
            )
        else:
            self.referral = discord.ui.TextInput(
                label="Referral (optional)", required=False
            )
            self.gender = discord.ui.TextInput(
                label="Gender (optional)", required=False
            )
            comps = (
                self.steam,
                self.hours,
                self.heard,
                self.referral,
                self.gender,
            )

        for c in comps:
            self.add_item(c)

    async def on_submit(self, inter):
        d, user = self.v.data, (self.v.user or inter.user)

        e = (
            discord.Embed(
                title="📋 NEW MEMBER REGISTRATION",
                colour=discord.Color.gold(),
                timestamp=inter.created_at,
            )
            .set_author(name=str(user), icon_url=user.display_avatar.url)
            .set_thumbnail(url=user.display_avatar.url)
        )
        e.add_field(name="\u200b", value="\u200b", inline=False)
        e.add_field(name="👤 User", value=user.mention, inline=False)
        e.add_field(name="🔗 Steam", value=self.steam.value, inline=False)
        e.add_field(name="🗓️ Age", value=d["age"], inline=True)
        e.add_field(name="🌍 Region", value=d["region"], inline=True)
        e.add_field(name="🚫 Bans", value=d["bans"], inline=True)
        if d["bans"] == "Yes" and self.ban_expl:
            e.add_field(
                name="📝 Ban Explanation", value=self.ban_expl.value, inline=False
            )
        e.add_field(name="🎯 Focus", value=d["focus"], inline=True)
        e.add_field(name="⭐ Skill", value=d["skill"], inline=True)
        e.add_field(name="⏱️ Hours", value=self.hours.value, inline=True)
        e.add_field(
            name="📢 Heard about us", value=self.heard.value, inline=False
        )
        e.add_field(
            name="🤝 Referral",
            value=self.referral.value if self.referral else "N/A",
            inline=True,
        )
        if self.gender:
            e.add_field(
                name="⚧️ Gender",
                value=self.gender.value or "N/A",
                inline=True,
            )
        e.add_field(name="\u200b", value="\u200b", inline=False)

        # Save to persistent member_forms table
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

        await inter.client.get_channel(MEMBER_FORM_CH).send(
            embed=e,
            view=ActionView(inter.guild, user.id, d["region"], d["focus"]),
        )
        await inter.response.send_message(
            "Registration submitted – thank you!", ephemeral=True
        )
        done = await inter.original_response()

        async def tidy():
            await asyncio.sleep(2)
            for m in (self.v.start_msg, self.v.submit_msg, done):
                try:
                    if m:
                        await m.delete()
                except discord.HTTPException:
                    pass

        asyncio.create_task(tidy())

@bot.tree.command(name="memberform", description="Start member registration")
async def memberform(inter: discord.Interaction):
    await inter.response.send_message(
        "Click below to begin registration:", view=MemberRegistrationView(), ephemeral=True
    )

# ========== GIVEAWAYS ==========

def fmt_time(s: int) -> str:
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m {s}s" if m else f"{s}s"

def put_field(e: discord.Embed, idx: int, *, name: str, value: str, inline=False):
    if idx < len(e.fields):
        e.set_field_at(idx, name=name, value=value, inline=inline)
    else:
        while len(e.fields) < idx:
            e.add_field(name="\u200b", value="\u200b", inline=False)
        e.add_field(name=name, value=value, inline=inline)

def eligible(g: discord.Guild):
    r = g.get_role(GIVEAWAY_ROLE_ID)
    return [m for m in r.members if not m.bot] if r else []

class GiveawayControl(discord.ui.View):
    def __init__(self, g, ch_id, msg_id, prize, stop):
        super().__init__(timeout=None)
        self.g, self.ch, self.msg_id, self.prize, self.stop = (
            g,
            ch_id,
            msg_id,
            prize,
            stop,
        )

    def _admin(self, m):
        return m.guild_permissions.administrator or m.id == bot.owner_id

    @discord.ui.button(
        label="End & Draw", style=discord.ButtonStyle.success, emoji="🎰", custom_id="gw_end"
    )
    async def end(self, inter: discord.Interaction, _):
        if not self._admin(inter.user):
            return await inter.response.send_message(
                "Not authorised.", ephemeral=True
            )

        chan = self.g.get_channel(self.ch)
        msg = await chan.fetch_message(self.msg_id)
        win = choice(eligible(self.g)) if eligible(self.g) else None

        if win:
            await chan.send(
                embed=discord.Embed(
                    title=f"🎉 {self.prize} – WINNER 🎉",
                    description=f"Congrats {win.mention}! Enjoy **{self.prize}**!",
                    colour=discord.Color.gold(),
                )
            )
        else:
            await chan.send("No eligible entrants.")

        e = msg.embeds[0]
        put_field(e, 1, name="Time left", value="**ENDED**")
        put_field(e, 3, name="Eligible Entrants", value="Giveaway ended early.")
        e.color = discord.Color.dark_gray()
        await msg.edit(embed=e, view=None)
        self.stop.set()
        await inter.response.send_message("Ended.", ephemeral=True)
        await db.end_giveaway(self.msg_id)

    @discord.ui.button(
        label="Cancel", style=discord.ButtonStyle.danger, emoji="🛑", custom_id="gw_cancel"
    )
    async def cancel(self, inter: discord.Interaction, _):
        if not self._admin(inter.user):
            return await inter.response.send_message(
                "Not authorised.", ephemeral=True
            )

        chan = self.g.get_channel(self.ch)
        msg = await chan.fetch_message(self.msg_id)
        e = msg.embeds[0]
        put_field(e, 1, name="Time left", value="**CANCELLED**")
        put_field(e, 3, name="Eligible Entrants", value="Giveaway cancelled.")
        e.color = discord.Color.red()
        await msg.edit(embed=e, view=None)
        self.stop.set()
        await chan.send("Giveaway cancelled.")
        await inter.response.send_message("Cancelled.", ephemeral=True)
        await db.end_giveaway(self.msg_id)

async def run_giveaway(g, ch_id, msg_id, prize, end_ts, stop):
    chan = g.get_channel(ch_id)
    msg = await chan.fetch_message(msg_id)

    while not stop.is_set():
        rem = end_ts - int(datetime.now(timezone.utc).timestamp())
        if rem <= 0:
            break

        txt = "\n".join(m.mention for m in eligible(g)) or "*None yet*"
        e = msg.embeds[0]
        put_field(e, 1, name="Time left", value=f"**{fmt_time(rem)}**")
        put_field(e, 3, name="Eligible Entrants", value=txt)
        try:
            await msg.edit(embed=e)
        except discord.HTTPException:
            pass

        await asyncio.sleep(min(60, rem))

    if stop.is_set():
        return

    pool = eligible(g)
    if pool:
        await chan.send(
            embed=discord.Embed(
                title=f"🎉 {prize} – WINNER 🎉",
                description=f"Congratulations {choice(pool).mention}! You won **{prize}**!",
                colour=discord.Color.gold(),
            )
        )
    else:
        await chan.send("No eligible entrants.")
    await db.end_giveaway(msg_id)

async def resume_giveaways():
    g = bot.get_guild(GUILD_ID)
    ch = g.get_channel(GIVEAWAY_CH_ID) if g else None
    if not g or not ch:
        return

    # Start from DB
    for row in await db.get_active_giveaways():
        stop = asyncio.Event()
        bot.giveaway_stop_events[row['message_id']] = stop
        v = GiveawayControl(g, row['channel_id'], row['message_id'], row['prize'], stop)
        bot.add_view(v, message_id=row['message_id'])
        asyncio.create_task(run_giveaway(
            g, row['channel_id'], row['message_id'], row['prize'], row['end_ts'], stop
        ))

@bot.tree.command(name="giveaway", description="Start a giveaway")
@app_commands.check(lambda i: i.user.guild_permissions.administrator)
@app_commands.choices(
    duration=[
        app_commands.Choice(name="7 days", value=7),
        app_commands.Choice(name="14 days", value=14),
        app_commands.Choice(name="30 days", value=30),
    ]
)
@app_commands.describe(prize="Prize to give away")
async def giveaway(
    inter: discord.Interaction, duration: app_commands.Choice[int], prize: str
):
    g = inter.guild
    ch = g.get_channel(GIVEAWAY_CH_ID)
    role = g.get_role(GIVEAWAY_ROLE_ID)

    if not ch or not role:
        return await inter.response.send_message(
            "Giveaway channel/role missing.", ephemeral=True
        )

    end_ts = int(datetime.now(timezone.utc).timestamp()) + duration.value * 86400
    stop = asyncio.Event()
    embed = discord.Embed(title=EMBED_TITLE, colour=discord.Color.blurple())
    embed.add_field(name="Prize", value=f"**{prize}**", inline=False)
    embed.add_field(name="Time left", value=f"**{duration.name}**", inline=False)
    embed.add_field(
        name="Eligibility", value=f"Only {role.mention} can win.", inline=False
    )
    embed.add_field(name="Eligible Entrants", value="*Updating…*", inline=False)
    embed.set_footer(
        text=f"||{FOOTER_END_TAG}{end_ts}|{FOOTER_PRIZE_TAG}{prize}||"
    )

    v = GiveawayControl(g, ch.id, 0, prize, stop)
    msg = await ch.send(embed=embed, view=v)
    v.msg_id = v.message_id = msg.id
    bot.add_view(v, message_id=msg.id)
    await db.add_giveaway(ch.id, msg.id, prize, end_ts)
    asyncio.create_task(run_giveaway(g, ch.id, msg.id, prize, end_ts, stop))
    await inter.response.send_message(
        f"Giveaway started in {ch.mention}.", ephemeral=True
    )

# ========== on_ready and Signal Handlers ==========

@bot.event
async def on_ready():
    await db.connect()
    print(f"Logged in as {bot.user} ({bot.user.id})")

    # slash-command sync
    guild_obj = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild_obj)
    await bot.tree.sync(guild=guild_obj)
    print("Slash-commands synced")

    codes = await db.get_codes()
    await update_codes_message(bot, codes)

    await resume_giveaways()
    print("Giveaways resumed")

if not BOT_TOKEN or not DATABASE_URL:
    raise RuntimeError("Set BOT_TOKEN and DATABASE_URL environment variables!")

bot.run(BOT_TOKEN)