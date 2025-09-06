from __future__ import annotations

import os, sys, json, asyncio, signal, discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta, timezone, date
from typing import Dict, Set, Any
from random import choice

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot("!", intents=intents)

# ══════════════════════════════════════════════════════════════════
#  Configuration (IDs can stay hard-coded; secrets via env vars)
# ══════════════════════════════════════════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID  = int(os.getenv("GUILD_ID", 1377035207777194005))

FEEDBACK_CH    = 1413188006499586158
MEMBER_FORM_CH = 1413672763108888636
WARNING_CH_ID  = 1398657081338237028

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
EMBED_TITLE          = "🎉 GIVEAWAY 🎉"
FOOTER_END_TAG       = "END:"
FOOTER_PRIZE_TAG     = "PRIZE:"

PROMOTE_STREAK       = 3
INACTIVE_AFTER_DAYS  = 5
WARN_BEFORE_DAYS     = INACTIVE_AFTER_DAYS - 1

DATA_DIR = os.getenv("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)

REVIEW_FILE   = os.path.join(DATA_DIR, "reviewers.json")
ACTIVITY_FILE = os.path.join(DATA_DIR, "activity.json")
CODES_FILE    = os.path.join(DATA_DIR, "codes.json")

# ══════════════════════════════════════════════════════════════════
#  CODE SYSTEM: Role-based Codes Storage & UI
# ══════════════════════════════════════════════════════════════════

def load_codes() -> dict:
    if os.path.isfile(CODES_FILE):
        try:
            with open(CODES_FILE, "r", encoding="utf8") as fp:
                return json.load(fp)
        except (OSError, json.JSONDecodeError):
            pass
    return {}

def save_codes(codes: dict):
    try:
        with open(CODES_FILE, "w", encoding="utf8") as fp:
            json.dump(codes, fp)
    except OSError:
        pass

codes: dict = load_codes()

def has_code_manager_perms(member: discord.Member):
    return member.guild_permissions.administrator

# ══ UI CLASSES FOR /codes ═════════════════════════════════════════

class RoleMultiSelect(discord.ui.Select):
    def __init__(self, roles, placeholder, min_values=1, max_values=5):
        options = [discord.SelectOption(label=role.name, value=str(role.id)) for role in roles]
        super().__init__(placeholder=placeholder, min_values=min_values, max_values=max_values, options=options)

class LabelRoleSelect(discord.ui.Select):
    def __init__(self, roles, placeholder):
        options = [discord.SelectOption(label=role.name, value=str(role.id)) for role in roles]
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)

class CodesMenuView(discord.ui.View):
    def __init__(self, author: discord.Member):
        super().__init__(timeout=300)
        self.author = author

        self.add_item(ViewCodesButton())
        if has_code_manager_perms(author):
            self.add_item(AddCodeButton())
            self.add_item(UpdateCodeButton())
            self.add_item(RemoveCodeButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This menu is not for you.", ephemeral=True)
            return False
        return True

class ViewCodesButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="View My Codes", style=discord.ButtonStyle.primary, custom_id="view_codes")

    async def callback(self, interaction: discord.Interaction):
        user_roles = {str(r.id) for r in interaction.user.roles}
        found = []
        for label_id, data in codes.items():
            if user_roles & set(data.get("viewers", [])):
                role = interaction.guild.get_role(int(label_id))
                found.append(f"**{role.name if role else label_id}**: `{data['value']}`")
        msg = "Your codes:\n" + "\n".join(found) if found else "You don't have any codes assigned to your roles."
        await interaction.response.send_message(msg, ephemeral=True)

class AddCodeButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Add Code", style=discord.ButtonStyle.success, custom_id="add_code")

    async def callback(self, interaction: discord.Interaction):
        all_roles = [role for role in interaction.guild.roles if not role.is_bot_managed() and role.name != "@everyone"]
        await interaction.response.send_message("Select the label role for the code:", view=AddCodeLabelView(all_roles), ephemeral=True)

class AddCodeLabelView(discord.ui.View):
    def __init__(self, roles):
        super().__init__(timeout=60)
        self.add_item(LabelRoleSelect(roles, "Select label role"))

    @discord.ui.select(cls=LabelRoleSelect)
    async def select_label(self, interaction: discord.Interaction, select: LabelRoleSelect):
        label_role_id = select.values[0]
        all_roles = [role for role in interaction.guild.roles if not role.is_bot_managed() and role.name != "@everyone"]
        await interaction.response.edit_message(content="Now select roles that can view this code:", view=AddCodeView(label_role_id, all_roles))

class AddCodeView(discord.ui.View):
    def __init__(self, label_role_id, roles):
        super().__init__(timeout=60)
        self.label_role_id = label_role_id
        self.selected_viewers = []
        self.add_item(RoleMultiSelect(roles, "Select roles who can view", min_values=1, max_values=10))
        self.code_value = discord.ui.TextInput(label="Code Value", style=discord.TextStyle.short)
        self.add_item(self.code_value)

    @discord.ui.select(cls=RoleMultiSelect)
    async def select_viewers(self, interaction: discord.Interaction, select: RoleMultiSelect):
        self.selected_viewers = select.values
        await interaction.response.defer()

    @discord.ui.button(label="Submit", style=discord.ButtonStyle.green)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        label_role_id = self.label_role_id
        viewers = self.selected_viewers
        code_value = self.code_value.value
        if not viewers or not code_value:
            return await interaction.response.send_message("Please select viewers and enter a code value.", ephemeral=True)
        if label_role_id in codes:
            return await interaction.response.send_message("Code already exists for that label. Use update.", ephemeral=True)
        codes[label_role_id] = {"value": code_value, "viewers": list(viewers)}
        save_codes(codes)
        role_names = ", ".join(
            f"**{interaction.guild.get_role(int(r)).name}**" for r in viewers if interaction.guild.get_role(int(r))
        )
        await interaction.response.send_message(
            f"Code for **{interaction.guild.get_role(int(label_role_id)).name}** added. Viewable by: {role_names}", ephemeral=True
        )
        await interaction.delete_original_response()

class UpdateCodeButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Update Code", style=discord.ButtonStyle.primary, custom_id="update_code")

    async def callback(self, interaction: discord.Interaction):
        label_roles = [interaction.guild.get_role(int(rid)) for rid in codes.keys() if interaction.guild.get_role(int(rid))]
        if not label_roles:
            return await interaction.response.send_message("No codes exist yet.", ephemeral=True)
        await interaction.response.send_message("Select a code to update:", view=UpdateCodeLabelView(label_roles), ephemeral=True)

class UpdateCodeLabelView(discord.ui.View):
    def __init__(self, label_roles):
        super().__init__(timeout=60)
        self.add_item(LabelRoleSelect(label_roles, "Select code to update"))

    @discord.ui.select(cls=LabelRoleSelect)
    async def select_label(self, interaction: discord.Interaction, select: LabelRoleSelect):
        label_role_id = select.values[0]
        all_roles = [role for role in interaction.guild.roles if not role.is_bot_managed() and role.name != "@everyone"]
        current_code = codes[label_role_id]["value"]
        await interaction.response.edit_message(content=f"Now select new viewers and code value (current: `{current_code}`):", view=UpdateCodeView(label_role_id, all_roles))

class UpdateCodeView(discord.ui.View):
    def __init__(self, label_role_id, roles):
        super().__init__(timeout=60)
        self.label_role_id = label_role_id
        self.selected_viewers = []
        self.add_item(RoleMultiSelect(roles, "Select new viewer roles", min_values=1, max_values=10))
        self.code_value = discord.ui.TextInput(label="New Code Value", style=discord.TextStyle.short)
        self.add_item(self.code_value)

    @discord.ui.select(cls=RoleMultiSelect)
    async def select_viewers(self, interaction: discord.Interaction, select: RoleMultiSelect):
        self.selected_viewers = select.values
        await interaction.response.defer()

    @discord.ui.button(label="Submit", style=discord.ButtonStyle.green)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        label_role_id = self.label_role_id
        viewers = self.selected_viewers
        code_value = self.code_value.value
        if not viewers or not code_value:
            return await interaction.response.send_message("Please select viewers and enter a code value.", ephemeral=True)
        if label_role_id not in codes:
            return await interaction.response.send_message("No code exists for that label. Use add.", ephemeral=True)
        role_names = ", ".join(
            f"**{interaction.guild.get_role(int(r)).name}**" for r in viewers if interaction.guild.get_role(int(r))
        )
        await interaction.response.send_message(
            f"Are you sure you want to update code for **{interaction.guild.get_role(int(label_role_id)).name}** to `{code_value}` (viewable by: {role_names})?",
            ephemeral=True,
            view=ConfirmUpdateView(label_role_id, viewers, code_value)
        )

class ConfirmUpdateView(discord.ui.View):
    def __init__(self, label_role_id, viewers, code_value):
        super().__init__(timeout=30)
        self.label_role_id = label_role_id
        self.viewers = viewers
        self.code_value = code_value

    @discord.ui.button(label="Yes, update", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        codes[self.label_role_id] = {"value": self.code_value, "viewers": list(self.viewers)}
        save_codes(codes)
        role_names = ", ".join(
            f"**{interaction.guild.get_role(int(r)).name}**" for r in self.viewers if interaction.guild.get_role(int(r))
        )
        await interaction.response.edit_message(content=f"Code updated. Now viewable by: {role_names}", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Update cancelled.", view=None)

class RemoveCodeButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Remove Code", style=discord.ButtonStyle.danger, custom_id="remove_code")

    async def callback(self, interaction: discord.Interaction):
        label_roles = [interaction.guild.get_role(int(rid)) for rid in codes.keys() if interaction.guild.get_role(int(rid))]
        if not label_roles:
            return await interaction.response.send_message("No codes exist yet.", ephemeral=True)
        await interaction.response.send_message("Select a code to remove:", view=RemoveCodeView(label_roles), ephemeral=True)

class RemoveCodeView(discord.ui.View):
    def __init__(self, label_roles):
        super().__init__(timeout=60)
        self.add_item(LabelRoleSelect(label_roles, "Select code to remove"))

    @discord.ui.select(cls=LabelRoleSelect)
    async def select_label(self, interaction: discord.Interaction, select: LabelRoleSelect):
        label_role_id = select.values[0]
        if label_role_id not in codes:
            return await interaction.response.send_message("No code exists for that label.", ephemeral=True)
        role_name = interaction.guild.get_role(int(label_role_id)).name
        await interaction.response.send_message(
            f"Are you sure you want to remove the code for **{role_name}**?",
            ephemeral=True,
            view=ConfirmRemoveView(label_role_id, role_name)
        )

class ConfirmRemoveView(discord.ui.View):
    def __init__(self, label_role_id, role_name):
        super().__init__(timeout=30)
        self.label_role_id = label_role_id
        self.role_name = role_name

    @discord.ui.button(label="Yes, remove", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        del codes[self.label_role_id]
        save_codes(codes)
        await interaction.response.edit_message(content=f"Code for **{self.role_name}** has been removed.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Removal cancelled.", view=None)

@bot.tree.command(name="codes", description="Show code management UI")
async def codes_command(inter: discord.Interaction):
    await inter.response.send_message(
        "Manage/view codes:",
        view=CodesMenuView(inter.user),
        ephemeral=True
    )

# ══════════════════════════════════════════════════════════════════
#  Reviewer List helpers
# ══════════════════════════════════════════════════════════════════
bot.review_team = set()  # type: Set[int]
bot.last_anonymous_time = {}  # type: Dict[int, datetime]
bot.giveaway_stop_events = {}  # type: Dict[int, asyncio.Event]

def load_reviewers() -> None:
    if os.path.isfile(REVIEW_FILE):
        try:
            with open(REVIEW_FILE, "r", encoding="utf8") as fp:
                bot.review_team |= {int(x) for x in json.load(fp)}
        except (OSError, json.JSONDecodeError):
            pass

def save_reviewers() -> None:
    try:
        with open(REVIEW_FILE, "w", encoding="utf8") as fp:
            json.dump(list(bot.review_team), fp)
    except OSError:
        pass

load_reviewers()

# ══════════════════════════════════════════════════════════════════
#  ACTIVITY TRACKER (load/save)
# ══════════════════════════════════════════════════════════════════
activity: Dict[str, Dict[str, Any]] = {}

def load_activity() -> None:
    global activity
    if os.path.isfile(ACTIVITY_FILE):
        try:
            with open(ACTIVITY_FILE, "r", encoding="utf8") as fp:
                activity = json.load(fp)
        except (OSError, json.JSONDecodeError):
            activity = {}

def save_activity() -> None:
    try:
        with open(ACTIVITY_FILE, "w", encoding="utf8") as fp:
            json.dump(activity, fp)
    except OSError:
        pass

load_activity()

def _dump_all() -> None:
    save_reviewers()
    save_activity()
    save_codes(codes)

def _graceful_exit() -> None:
    print("Signal received – saving JSON files and shutting down …")
    try:
        _dump_all()
    finally:
        sys.exit(0)

async def _periodic_autosave() -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(60)
        _dump_all()

# ========== ACTIVITY LOGIC ==========

def mark_active(member: discord.Member) -> None:
    if member.bot:
        return

    today = date.today().isoformat()
    rec = activity.setdefault(
        str(member.id), {"streak": 0, "date": today, "warned": False}
    )

    if rec["date"] != today:
        yesterday = date.fromisoformat(rec["date"]) + timedelta(days=1)
        rec["streak"] = rec["streak"] + 1 if yesterday.isoformat() == today else 1
        rec["date"] = today
        rec["warned"] = False

    rec["last"] = datetime.now(timezone.utc).timestamp()
    save_activity()

    role = member.guild.get_role(GIVEAWAY_ROLE_ID)
    if role and rec["streak"] >= PROMOTE_STREAK and role not in member.roles:
        asyncio.create_task(
            member.add_roles(role, reason=f"{PROMOTE_STREAK}-day activity streak")
        )

def _cutoff(days: int) -> float:
    return datetime.now(timezone.utc).timestamp() - days * 86400

def members_to_warn(guild: discord.Guild):
    warn_cut = _cutoff(WARN_BEFORE_DAYS)
    kick_cut = _cutoff(INACTIVE_AFTER_DAYS)
    role = guild.get_role(GIVEAWAY_ROLE_ID)
    if not role:
        return []

    out = []
    for m in role.members:
        info = activity.get(str(m.id), {})
        last = float(info.get("last", 0))
        if last < warn_cut <= last or info.get("warned"):
            continue
        if last < warn_cut and last >= kick_cut:
            out.append(m)
    return out

def members_to_demote(guild: discord.Guild):
    kick_cut = _cutoff(INACTIVE_AFTER_DAYS)
    role = guild.get_role(GIVEAWAY_ROLE_ID)
    if not role:
        return []
    return [
        m
        for m in role.members
        if float(activity.get(str(m.id), {}).get("last", 0)) < kick_cut
    ]

async def daily_activity_check():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(timezone.utc)
        nxt = (now + timedelta(days=1)).replace(
            hour=4, minute=0, second=0, microsecond=0
        )
        await asyncio.sleep((nxt - now).total_seconds())

        guild = bot.get_guild(GUILD_ID)
        if not guild:
            continue

        warn_ch = guild.get_channel(WARNING_CH_ID)
        role = guild.get_role(GIVEAWAY_ROLE_ID)

        for m in members_to_warn(guild):
            if warn_ch:
                try:
                    await warn_ch.send(
                        f"{m.mention} you have been inactive **{WARN_BEFORE_DAYS} days**. "
                        f"You will lose {role.mention} tomorrow."
                    )
                except discord.HTTPException:
                    pass
            activity[str(m.id)]["warned"] = True

        removed = 0
        for m in members_to_demote(guild):
            try:
                await m.remove_roles(role, reason="inactive 5 days")
                removed += 1
            except discord.Forbidden:
                pass

        if removed and warn_ch:
            await warn_ch.send(
                f"Removed {role.mention} from **{removed}** inactive member(s)."
            )

        save_activity()

@bot.event
async def on_message(msg: discord.Message):
    if msg.guild and not msg.author.bot:
        mark_active(msg.author)

@bot.event
async def on_voice_state_update(member, before, after):
    if (
        member.guild.id == GUILD_ID
        and not member.bot
        and not before.channel
        and after.channel
    ):
        mark_active(member)

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

async def resume_giveaways():
    g = bot.get_guild(GUILD_ID)
    ch = g.get_channel(GIVEAWAY_CH_ID) if g else None
    if not g or not ch:
        return

    async for msg in ch.history(limit=200):
        if msg.author.id != bot.user.id or not msg.embeds:
            continue
        e = msg.embeds[0]
        if e.title != EMBED_TITLE or not e.footer:
            continue
        f = e.footer.text.strip("|")
        if FOOTER_END_TAG not in f:
            continue
        end_ts = int(f.split("|", 1)[0].replace(FOOTER_END_TAG, ""))
        prize = f.split("|", 1)[1].replace(FOOTER_PRIZE_TAG, "")
        if end_ts <= int(datetime.now(timezone.utc).timestamp()):
            continue
        stop = asyncio.Event()
        bot.giveaway_stop_events[msg.id] = stop
        v = GiveawayControl(g, ch.id, msg.id, prize, stop)
        bot.add_view(v, message_id=msg.id)
        asyncio.create_task(run_giveaway(g, ch.id, msg.id, prize, end_ts, stop))

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
    asyncio.create_task(run_giveaway(g, ch.id, msg.id, prize, end_ts, stop))
    await inter.response.send_message(
        f"Giveaway started in {ch.mention}.", ephemeral=True
    )

# ========== FEEDBACK ==========

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

# ========== REVIEWER COMMANDS ==========

def is_admin(i: discord.Interaction) -> bool:
    return i.user.guild_permissions.administrator or i.user.id == bot.owner_id

@bot.tree.command(name="addreviewer")
@app_commands.check(is_admin)
async def add_reviewer(i: discord.Interaction, member: discord.Member):
    bot.review_team.add(member.id)
    save_reviewers()
    await i.response.send_message("Added.", ephemeral=True)

@bot.tree.command(name="removereviewer")
@app_commands.check(is_admin)
async def remove_reviewer(i: discord.Interaction, member: discord.Member):
    bot.review_team.discard(member.id)
    save_reviewers()
    await i.response.send_message("Removed.", ephemeral=True)

@bot.tree.command(name="reviewers")
@app_commands.check(is_admin)
async def list_reviewers(i: discord.Interaction):
    txt = ", ".join(f"<@{u}>" for u in bot.review_team) or "None."
    await i.response.send_message(txt, ephemeral=True)

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

    def authorised(self, u, perm):
        return u.id in bot.review_team or getattr(u.guild_permissions, perm)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, inter, _):
        if not self.authorised(inter.user, "manage_roles"):
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
        if not self.authorised(inter.user, "ban_members"):
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

# ══════════════════════════════════════════════════════════════════
#  READY HANDLER, AUTOSAVE, ETC (unchanged)
# ══════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")

    # daily activity checker
    if not getattr(bot, "_activity_task_started", False):
        asyncio.create_task(daily_activity_check())
        bot._activity_task_started = True

    # autosave task
    if not getattr(bot, "_autosave_started", False):
        bot.loop.create_task(_periodic_autosave())
        bot._autosave_started = True

    # register signal handlers (loop is running now)
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            bot.loop.add_signal_handler(_sig, _graceful_exit)
        except (NotImplementedError, RuntimeError):
            signal.signal(_sig, lambda *_: _graceful_exit())

    # slash-command sync
    guild_obj = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild_obj)
    await bot.tree.sync(guild=guild_obj)
    print("Slash-commands synced")

    await resume_giveaways()
    print("Giveaways resumed")

# ══════════════════════════════════════════════════════════════════
#  RUN
# ══════════════════════════════════════════════════════════════════
if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN environment variable!")

bot.run(BOT_TOKEN)