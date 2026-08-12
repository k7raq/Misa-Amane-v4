from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("premier-league-lb")

DATA_FILE = Path(os.getenv("DATA_FILE", "data.json"))
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)

OWNER_ROLE_ID = int(os.getenv("OWNER_ROLE_ID", "1533730510423982090"))
EDITOR_ROLE_ID = int(os.getenv("EDITOR_ROLE_ID", "1524622437788942497"))
REFEREE_ROLE_ID = int(os.getenv("REFEREE_ROLE_ID", "1524626691475898420"))
SUPERVISOR_ROLE_ID = int(os.getenv("SUPERVISOR_ROLE_ID", "1524639500301635756"))

PROFILE_CHANNEL_ID = int(os.getenv("CHANNEL_PROFILE", "1536311663089684520"))
APPROVAL_CHANNEL_ID = int(os.getenv("CHANNEL_SET_APPROVAL", "1536315633937948723"))
ANNOUNCEMENT_CHANNEL_ID = int(os.getenv("CHANNEL_ANNOUNCEMENT", "1524646213214015578"))
MISTAKE_CHANNEL_ID = int(os.getenv("CHANNEL_MISTAKE", "1536329062111510588"))
SCORE_CHANNEL_ID = int(os.getenv("CHANNEL_SCORE", "152464626919932313"))
CHALLENGE_LOG_CHANNEL_ID = int(os.getenv("CHANNEL_CHALLENGE_LOG", "1536790782197899364"))
CHALLENGE_CATEGORY_ID = int(os.getenv("CHALLENGE_CATEGORY_ID", "0") or 0)
SET_PING_ROLE_ID = int(os.getenv("SET_PING_ROLE_ID", "1524622534690082836"))

REGIONS = ("AS", "EU", "NA", "SA", "OC")
BOARDS = ("overall", "mobile")
SPOTS = tuple(range(1, 11))
RANGE = 1
PROTECTION_SECONDS = 3 * 86400
COOLDOWN_SECONDS = 3 * 86400
DEFENDER_LOSS_COOLDOWN = 86400

OVERALL_ROLE_SETS = [
    {1524639286010577037, 1524639266171261058},
    {1524639250316791879},
    {1524639237809377330},
]
MOBILE_ROLE_SETS = [
    {1524639266171261058, 1524639298333180045, 1524639352951410739},
    {1524639266171261058, 1524639298333180045, 1524639338921459892},
] + OVERALL_ROLE_SETS

INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.guilds = True
bot = commands.Bot(command_prefix="!", intents=INTENTS)


def empty_data() -> dict:
    return {
        "boards": {},
        "profiles": {},
        "sets": {},
        "challenges": {},
        "next_set_id": 1,
        "challenge_panel_messages": {},
    }


def load_data() -> dict:
    if not DATA_FILE.exists():
        return empty_data()
    try:
        data = json.loads(DATA_FILE.read_text("utf-8"))
    except Exception:
        log.exception("Could not read data.json")
        return empty_data()
    base = empty_data()
    base.update(data)
    # Migrate old board lists into the new structure.
    for key, value in list(base["boards"].items()):
        if isinstance(value, list):
            base["boards"][key] = {"spots": [None if x is None else {"user_id": int(x), "stage": ""} for x in value], "message_ids": [], "channel_id": 0}
        elif isinstance(value, dict):
            spots = value.get("spots", [None] * 10)
            fixed = []
            for x in spots[:10]:
                if x is None:
                    fixed.append(None)
                elif isinstance(x, dict):
                    fixed.append({"user_id": int(x["user_id"]), "stage": str(x.get("stage", ""))})
                else:
                    fixed.append({"user_id": int(x), "stage": ""})
            while len(fixed) < 10:
                fixed.append(None)
            value["spots"] = fixed
            value.setdefault("message_ids", [])
            value.setdefault("channel_id", 0)
    return base


DATA = load_data()


def save() -> None:
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(DATA, indent=2), encoding="utf-8")
    tmp.replace(DATA_FILE)


def now() -> int:
    return int(time.time())


def has_role(member: discord.Member, role_id: int) -> bool:
    return any(r.id == role_id for r in member.roles)


def is_owner(member: discord.Member) -> bool:
    return has_role(member, OWNER_ROLE_ID)


def is_editor(member: discord.Member) -> bool:
    return is_owner(member) or has_role(member, EDITOR_ROLE_ID)


def is_supervisor(member: discord.Member) -> bool:
    return is_owner(member) or has_role(member, SUPERVISOR_ROLE_ID)


def is_referee(member: discord.Member) -> bool:
    return is_supervisor(member) or has_role(member, REFEREE_ROLE_ID)


def role_allowed(member: discord.Member, sets: list[set[int]]) -> bool:
    owned = {r.id for r in member.roles}
    return any(req <= owned for req in sets)


def board_key(board: str, region: str) -> str:
    return f"{board.lower()}:{region.upper()}"


def ensure_board(board: str, region: str) -> dict:
    key = board_key(board, region)
    b = DATA["boards"].setdefault(key, {"spots": [None] * 10, "message_ids": [], "channel_id": 0})
    if "spots" not in b:
        b["spots"] = [None] * 10
    while len(b["spots"]) < 10:
        b["spots"].append(None)
    b["spots"] = b["spots"][:10]
    b.setdefault("message_ids", [])
    b.setdefault("channel_id", 0)
    return b


def get_profile(user_id: int) -> Optional[dict]:
    return DATA["profiles"].get(str(user_id))


def entry_user(entry: Optional[dict]) -> Optional[int]:
    return int(entry["user_id"]) if entry else None


def find_player(board: str, region: str, user_id: int) -> Optional[int]:
    for i, entry in enumerate(ensure_board(board, region)["spots"], 1):
        if entry_user(entry) == user_id:
            return i
    return None


def status_for_profile(profile: Optional[dict]) -> str:
    if not profile:
        return "CHALLENGEABLE"
    cd = int(profile.get("cooldown_until", 0) or 0)
    prot = int(profile.get("protection_until", 0) or 0)
    changed = False
    if cd and cd <= now():
        profile["cooldown_until"] = 0
        cd = 0
        changed = True
    if prot and prot <= now():
        profile["protection_until"] = 0
        prot = 0
        changed = True
    if changed:
        save()
    if cd > now():
        return "COOLDOWN"
    if prot > now():
        return "PROTECTION"
    return "CHALLENGEABLE"


def status_text(status: str) -> str:
    return {"CHALLENGEABLE": "Challengeable", "PROTECTION": "Protection", "COOLDOWN": "Cooldown"}.get(status, status.title())


def display_name(guild: discord.Guild, user_id: int) -> str:
    p = get_profile(user_id)
    if p and p.get("nickname"):
        return str(p["nickname"])
    m = guild.get_member(user_id)
    return m.display_name if m else f"Player {user_id}"


def profile_avatar(user_id: int) -> str:
    p = get_profile(user_id)
    return str(p.get("avatar_url", "")) if p else ""


async def roblox_lookup(username: str) -> tuple[str, str]:
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post("https://users.roblox.com/v1/usernames/users", json={"usernames": [username], "excludeBannedUsers": False}) as r:
            if r.status != 200:
                raise ValueError("Roblox lookup failed. Try again.")
            data = await r.json()
        users = data.get("data", [])
        if not users:
            raise ValueError("That Roblox username was not found.")
        uid = users[0]["id"]
        canonical = users[0]["name"]
        async with session.get("https://thumbnails.roblox.com/v1/users/avatar-headshot", params={"userIds": uid, "size": "150x150", "format": "Png", "isCircular": "false"}) as r:
            if r.status != 200:
                raise ValueError("Roblox avatar lookup failed. Try again.")
            td = await r.json()
        avatar = (td.get("data") or [{}])[0].get("imageUrl", "")
        if not avatar:
            raise ValueError("Roblox avatar could not be found.")
        return canonical, avatar


def board_title(board: str, region: str) -> str:
    return f"{board.title()} Leaderboard • {region.upper()}"


def requirement_sets(board: str) -> list[set[int]]:
    return MOBILE_ROLE_SETS if board == "mobile" else OVERALL_ROLE_SETS


def profile_warning() -> str:
    return "⚠️ Giving fake information can lead to a **1 month LB ban**. Please provide accurate information."


def spot_embed(guild: discord.Guild, board: str, region: str, spot: int) -> discord.Embed:
    entry = ensure_board(board, region)["spots"][spot - 1]
    if entry is None:
        e = discord.Embed(title=f"#{spot} Vacant", description="`| Vacant |`\n`<<< | • Vacant • | >>>`\n**Country:** —\n**Stage:** —")
        e.set_footer(text=f"{board.title()} • {region} • Vacant spot")
        return e
    uid = entry_user(entry)
    p = get_profile(uid) or {}
    status = status_for_profile(p)
    nick = p.get("nickname", display_name(guild, uid))
    roblox = p.get("roblox_username", "—")
    country = p.get("country_flag", "—")
    stage = entry.get("stage", "") or "—"
    e = discord.Embed(title=f"#{spot} {nick}")
    e.description = (
        f"`| {nick} |`\n"
        f"`<<< | {roblox} • | >>>`\n"
        f"**Country:** {country}\n"
        f"**Stage:** {stage}\n"
        f"**Status:** {status_text(status)}\n"
        f"**Discord:** <@{uid}>"
    )
    av = profile_avatar(uid)
    if av:
        e.set_thumbnail(url=av)
    e.set_footer(text=f"{board.title()} • {region} • Spot #{spot}")
    return e


async def fetch_text_channel(guild: discord.Guild, channel_id: int) -> Optional[discord.TextChannel]:
    ch = guild.get_channel(channel_id)
    if isinstance(ch, discord.TextChannel):
        return ch
    try:
        ch = await guild.fetch_channel(channel_id)
        return ch if isinstance(ch, discord.TextChannel) else None
    except Exception:
        return None


async def refresh_lb(board: str, region: str, guild: discord.Guild) -> None:
    b = ensure_board(board, region)
    ids = list(b.get("message_ids", []))
    channel = await fetch_text_channel(guild, int(b.get("channel_id", 0))) if b.get("channel_id") else None
    if not channel or len(ids) != 10:
        return
    new_ids = []
    for spot, mid in enumerate(ids, 1):
        try:
            msg = await channel.fetch_message(int(mid))
            await msg.edit(embed=spot_embed(guild, board, region, spot))
            new_ids.append(msg.id)
        except Exception:
            new_ids.append(mid)
    b["message_ids"] = new_ids
    save()


async def refresh_all_lbs(guild: discord.Guild) -> None:
    for key in list(DATA["boards"]):
        try:
            board, region = key.split(":", 1)
            await refresh_lb(board, region, guild)
        except Exception:
            log.exception("Could not refresh %s", key)


async def log_event(guild: discord.Guild, title: str, description: str, file_bytes: bytes | None = None, filename: str = "transcript.txt"):
    ch = await fetch_text_channel(guild, CHALLENGE_LOG_CHANNEL_ID)
    if not ch:
        return
    e = discord.Embed(title=title, description=description[:4000])
    if file_bytes is None:
        await ch.send(embed=e)
    else:
        await ch.send(embed=e, file=discord.File(io.BytesIO(file_bytes), filename=filename))


async def save_profile_message(guild: discord.Guild, profile: dict) -> None:
    ch = await fetch_text_channel(guild, PROFILE_CHANNEL_ID)
    if not ch:
        return
    e = discord.Embed(title=f"Player Profile — ID {profile['profile_id']:03d}")
    e.add_field(name="Nickname", value=profile["nickname"], inline=False)
    e.add_field(name="Discord", value=f"<@{profile['user_id']}>\n`{profile['discord_id']}`", inline=False)
    e.add_field(name="Roblox", value=profile["roblox_username"], inline=False)
    e.add_field(name="Country", value=profile["country_flag"], inline=False)
    e.add_field(name="Warning", value=profile_warning(), inline=False)
    e.set_footer(text=f"Registered {profile.get('registered_at', '—')} UTC")
    if profile.get("avatar_url"):
        e.set_thumbnail(url=profile["avatar_url"])
    old_id = profile.get("profile_message_id")
    if old_id:
        try:
            old = await ch.fetch_message(int(old_id))
            await old.edit(embed=e)
            return
        except Exception:
            pass
    msg = await ch.send(embed=e)
    profile["profile_message_id"] = msg.id
    save()


class ProfileModal(discord.ui.Modal, title="Create LB Profile"):
    nickname = discord.ui.TextInput(label="Nickname for the LB", max_length=32)
    discord_id = discord.ui.TextInput(label="Discord ID", max_length=25)
    roblox_username = discord.ui.TextInput(label="Roblox Username", max_length=32)
    country_flag = discord.ui.TextInput(label="Country Flag", max_length=8)

    def __init__(self, action: str, board: str, region: str, spot: int):
        super().__init__()
        self.action = action
        self.board = board
        self.region = region
        self.spot = spot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if str(self.discord_id).strip() != str(interaction.user.id):
            await interaction.followup.send("The Discord ID must be your own Discord ID.", ephemeral=True)
            return
        try:
            canonical, avatar = await roblox_lookup(str(self.roblox_username).strip())
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        if get_profile(interaction.user.id):
            await interaction.followup.send("You already have a profile. Please use the challenge panel again.", ephemeral=True)
            return
        next_id = max([int(p.get("profile_id", 0)) for p in DATA["profiles"].values()] + [0]) + 1
        p = {
            "profile_id": next_id, "user_id": interaction.user.id, "nickname": str(self.nickname).strip(),
            "discord_id": str(self.discord_id).strip(), "roblox_username": canonical,
            "country_flag": str(self.country_flag).strip(), "avatar_url": avatar,
            "protection_until": 0, "cooldown_until": 0, "profile_message_id": None,
            "registered_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        }
        DATA["profiles"][str(interaction.user.id)] = p
        save()
        await save_profile_message(interaction.guild, p)
        if self.action == "claim":
            b = ensure_board(self.board, self.region)
            current = find_player(self.board, self.region, interaction.user.id)
            if b["spots"][self.spot - 1] is not None or (current is not None and self.spot >= current):
                await interaction.followup.send("Profile created, but your original spot is no longer available.", ephemeral=True)
                return
            if current is not None:
                b["spots"][current - 1] = None
            b["spots"][self.spot - 1] = {"user_id": interaction.user.id, "stage": ""}
            save()
            await refresh_lb(self.board, self.region, interaction.guild)
            await interaction.followup.send(f"Profile created and **#{self.spot}** has been claimed.", ephemeral=True)
            return
        # Challenge action after first-time profile creation.
        spots = ensure_board(self.board, self.region)["spots"]
        opponent = entry_user(spots[self.spot - 1])
        if opponent is None:
            await interaction.followup.send("Profile created, but that spot is no longer occupied.", ephemeral=True)
            return
        await create_challenge_channel(interaction.guild, self.board, self.region, interaction.user.id, opponent, find_player(self.board, self.region, interaction.user.id), self.spot)
        await interaction.followup.send("Profile created and the private challenge channel has been created.", ephemeral=True)


async def claim_spot(interaction: discord.Interaction, board: str, region: str, spot: int) -> bool:
    member = interaction.user
    b = ensure_board(board, region)
    spots = b["spots"]
    if spots[spot - 1] is not None:
        await interaction.response.send_message("That spot is no longer vacant.", ephemeral=True)
        return False
    current = find_player(board, region, member.id)
    if current is not None and spot >= current:
        await interaction.response.send_message("You cannot claim a spot below or equal to your current spot.", ephemeral=True)
        return False
    if not role_allowed(member, requirement_sets(board)):
        await interaction.response.send_message("You don't meet the requirement for this LB.", ephemeral=True)
        return False
    if not get_profile(member.id):
        await interaction.response.send_modal(ProfileModal("claim", board, region, spot))
        return False
    if current is not None:
        spots[current - 1] = None
    spots[spot - 1] = {"user_id": member.id, "stage": ""}
    save()
    await refresh_lb(board, region, interaction.guild)
    await interaction.response.send_message(f"You claimed **#{spot}** on **{board_title(board, region)}**.", ephemeral=True)
    return True


async def continue_after_profile(interaction: discord.Interaction, action: str, board: str, region: str, spot: int) -> None:
    if action == "claim":
        b = ensure_board(board, region)
        current = find_player(board, region, interaction.user.id)
        if b["spots"][spot - 1] is not None or (current is not None and spot >= current):
            await interaction.response.send_message("Your original spot is no longer available.", ephemeral=True)
            return
        if current is not None:
            b["spots"][current - 1] = None
        b["spots"][spot - 1] = {"user_id": interaction.user.id, "stage": ""}
        save()
        await refresh_lb(board, region, interaction.guild)
        await interaction.response.send_message(f"Profile created and **#{spot}** has been claimed.", ephemeral=True)
        return
    await create_challenge_from_interaction(interaction, board, region, spot)


class ActionSelect(discord.ui.Select):
    def __init__(self, board: str, region: str):
        self.board = board
        self.region = region
        options = []
        spots = ensure_board(board, region)["spots"]
        for n, entry in enumerate(spots, 1):
            if entry is None:
                label = f"#{n} • Vacant"
                desc = "Claim this vacant spot"
            else:
                uid = entry_user(entry)
                label = f"#{n} • {display_name_cached(uid)}"
                desc = "Challenge this player"
            options.append(discord.SelectOption(label=label[:100], value=str(n), description=desc[:100]))
        super().__init__(placeholder="Choose a spot", options=options)

    async def callback(self, interaction: discord.Interaction):
        spot = int(self.values[0])
        entry = ensure_board(self.board, self.region)["spots"][spot - 1]
        if entry is None:
            await claim_spot(interaction, self.board, self.region, spot)
            return
        uid = entry_user(entry)
        if uid == interaction.user.id:
            await interaction.response.send_message("That's your own spot.", ephemeral=True)
            return
        current = find_player(self.board, self.region, interaction.user.id)
        if current is not None and spot >= current:
            await interaction.response.send_message("You can only challenge a player above you.", ephemeral=True)
            return
        if current is None:
            occupied = ensure_board(self.board, self.region)["spots"]
            if any(x is None for x in occupied):
                await interaction.response.send_message("There is a vacant spot available for you to claim. Claim a vacant spot first.", ephemeral=True)
                return
            if spot not in (9, 10):
                await interaction.response.send_message("An unranked player may only challenge **#9 or #10** when the LB is full.", ephemeral=True)
                return
        await create_challenge_from_interaction(interaction, self.board, self.region, spot)


def display_name_cached(uid: int) -> str:
    p = get_profile(uid)
    return str(p.get("nickname")) if p and p.get("nickname") else f"Player {uid}"


class RegionView(discord.ui.View):
    def __init__(self, board: str):
        super().__init__(timeout=300)
        for r in REGIONS:
            self.add_item(RegionButton(board, r))


class RegionButton(discord.ui.Button):
    def __init__(self, board: str, region: str):
        super().__init__(label=region, style=discord.ButtonStyle.secondary)
        self.board, self.region = board, region

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"**{board_title(self.board, self.region)}**\nChoose a vacant spot to claim or an occupied spot to challenge.",
            view=SpotView(self.board, self.region),
            ephemeral=True,
        )


class SpotView(discord.ui.View):
    def __init__(self, board: str, region: str):
        super().__init__(timeout=300)
        self.add_item(ActionSelect(board, region))


class BoardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(BoardButton("Overall", "overall"))
        self.add_item(BoardButton("Mobile", "mobile"))


class BoardButton(discord.ui.Button):
    def __init__(self, label: str, board: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary, custom_id=f"pllb:board:{board}")
        self.board = board

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"**{self.board.title()} LB**\nChoose a region.",
            view=RegionView(self.board),
            ephemeral=True,
        )


async def send_challenge_panel(channel: discord.TextChannel) -> discord.Message:
    e = discord.Embed(
        title="Premier League • Challenge Panel",
        description=(
            "**PLAYER LEADERBOARD**\n\n"
            "Choose **Overall** or **Mobile**, then choose a region and a spot.\n\n"
            "• Vacant spot → Claim it if you meet the LB requirement.\n"
            "• Occupied spot above you → Challenge it.\n"
            "• Challenge range: **1 spot**. Further challenges can be accepted, dodged, or autoed by the challenged player.\n"
            "• Protection → can dodge.\n"
            "• Cooldown → cannot be challenged.\n\n"
            "Your profile is requested only the first time you claim or challenge."
        ),
    )
    msg = await channel.send(embed=e, view=BoardView())
    DATA["challenge_panel_messages"][str(msg.id)] = channel.id
    save()
    return msg


async def create_challenge_from_interaction(interaction: discord.Interaction, board: str, region: str, opponent_spot: int):
    spots = ensure_board(board, region)["spots"]
    opponent = entry_user(spots[opponent_spot - 1])
    if opponent is None:
        await interaction.response.send_message("That spot is vacant now.", ephemeral=True)
        return
    challenger = interaction.user.id
    challenger_spot = find_player(board, region, challenger)
    if status_for_profile(get_profile(challenger)) == "COOLDOWN":
        await interaction.response.send_message("You are on cooldown and cannot challenge.", ephemeral=True)
        return
    op_status = status_for_profile(get_profile(opponent))
    if op_status == "COOLDOWN":
        await interaction.response.send_message("That player is on cooldown and cannot be challenged.", ephemeral=True)
        return
    if challenger_spot is not None and opponent_spot >= challenger_spot:
        await interaction.response.send_message("You can only challenge a player above you.", ephemeral=True)
        return
    if challenger_spot is None and any(x is None for x in spots):
        await interaction.response.send_message("Claim an available vacant spot first.", ephemeral=True)
        return
    if challenger_spot is None and opponent_spot not in (9, 10):
        await interaction.response.send_message("An unranked player may only challenge #9 or #10 when the LB is full.", ephemeral=True)
        return
    if not get_profile(challenger):
        await interaction.response.send_modal(ProfileModal("challenge", board, region, opponent_spot))
        return
    await interaction.response.send_message("Creating the private challenge channel...", ephemeral=True)
    await create_challenge_channel(interaction.guild, board, region, challenger, opponent, challenger_spot, opponent_spot)


async def create_challenge_channel(guild: discord.Guild, board: str, region: str, challenger: int, opponent: int, challenger_spot: Optional[int], opponent_spot: int):
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    for uid in (challenger, opponent):
        m = guild.get_member(uid)
        if m:
            overwrites[m] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    for rid in (REFEREE_ROLE_ID, SUPERVISOR_ROLE_ID, OWNER_ROLE_ID):
        role = guild.get_role(rid)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    category = guild.get_channel(CHALLENGE_CATEGORY_ID) if CHALLENGE_CATEGORY_ID else None
    name = re.sub(r"[^a-z0-9-]", "-", f"challenge-{display_name_cached(challenger)}-{display_name_cached(opponent)}".lower())[:90]
    ch = await guild.create_text_channel(name, overwrites=overwrites, category=category)
    cid = str(ch.id)
    out_of_range = challenger_spot is not None and opponent_spot < challenger_spot - RANGE
    protected = status_for_profile(get_profile(opponent)) == "PROTECTION"
    DATA["challenges"][cid] = {
        "channel_id": ch.id, "board": board, "region": region,
        "challenger": challenger, "opponent": opponent,
        "challenger_spot": challenger_spot, "opponent_spot": opponent_spot,
        "out_of_range": out_of_range, "created_at": now(), "resolved": False,
    }
    save()
    e = discord.Embed(title="Challenge Discussion")
    e.description = (
        f"**{display_name_cached(challenger)}** challenges **{display_name_cached(opponent)}**\n"
        f"Leaderboard: **{board.title()} • {region}**\n"
        f"Challenger spot: **#{challenger_spot or 'Unranked'}**\n"
        f"Challenged spot: **#{opponent_spot}**\n\n"
        "Discuss the challenge here. When the result is ready, continue with the appropriate option."
    )
    if out_of_range or protected:
        view = DefenseView(cid)
    else:
        view = FightAutoView(cid)
    await ch.send(content=f"<@{challenger}> <@{opponent}>", embed=e, view=view)
    await log_event(guild, "Private Challenge Created", f"Channel: <#{ch.id}>\nChallenger: <@{challenger}>\nChallenged: <@{opponent}>\nLeaderboard: {board.title()} • {region}\nChallenger spot: #{challenger_spot or 'Unranked'}\nChallenged spot: #{opponent_spot}\nOut of range: {out_of_range}\nProtection: {protected}")


class FightAutoView(discord.ui.View):
    def __init__(self, cid: str):
        super().__init__(timeout=None)
        self.add_item(FightButton(cid))
        self.add_item(AutoButton(cid))


class DefenseView(discord.ui.View):
    def __init__(self, cid: str):
        super().__init__(timeout=None)
        self.add_item(AcceptButton(cid))
        self.add_item(DodgeButton(cid))
        self.add_item(AutoButton(cid))


def challenge_from(cid: str) -> Optional[dict]:
    return DATA["challenges"].get(str(cid))


class FightButton(discord.ui.Button):
    def __init__(self, cid: str):
        super().__init__(label="Fight", style=discord.ButtonStyle.success, custom_id=f"pllb:fight:{cid}")
        self.cid = cid

    async def callback(self, interaction: discord.Interaction):
        c = challenge_from(self.cid)
        if not c:
            await interaction.response.send_message("This challenge is no longer active.", ephemeral=True)
            return
        if interaction.user.id not in (c["challenger"], c["opponent"]) and not is_referee(interaction.user):
            await interaction.response.send_message("You don't have access to this action.", ephemeral=True)
            return
        await log_event(interaction.guild, "Challenge Action", f"Channel: <#{c['channel_id']}>\nAction: Fight\nBy: <@{interaction.user.id}>")
        await interaction.response.send_message("Fight selected. A referee/supervisor can now announce the result with `/scoreannouncement`.", ephemeral=True)


class AcceptButton(discord.ui.Button):
    def __init__(self, cid: str):
        super().__init__(label="Accept", style=discord.ButtonStyle.success, custom_id=f"pllb:accept:{cid}")
        self.cid = cid

    async def callback(self, interaction: discord.Interaction):
        c = challenge_from(self.cid)
        if not c or interaction.user.id != c["opponent"]:
            await interaction.response.send_message("Only the challenged player can accept.", ephemeral=True)
            return
        await log_event(interaction.guild, "Challenge Action", f"Channel: <#{c['channel_id']}>\nAction: Accept\nBy: <@{interaction.user.id}>")
        await interaction.response.send_message("Challenge accepted. The fight can proceed.", ephemeral=True)
        await interaction.channel.send(f"<@{c['challenger']}> **The opponent accepted the challenge.**")


class DodgeButton(discord.ui.Button):
    def __init__(self, cid: str):
        super().__init__(label="Dodge", style=discord.ButtonStyle.danger, custom_id=f"pllb:dodge:{cid}")
        self.cid = cid

    async def callback(self, interaction: discord.Interaction):
        c = challenge_from(self.cid)
        if not c or interaction.user.id != c["opponent"]:
            await interaction.response.send_message("Only the challenged player can dodge.", ephemeral=True)
            return
        c["delete_reason"] = "Premier League Bot deleted this channel because the challenge was dodged."
        defender_profile = get_profile(c["opponent"])
        if defender_profile:
            defender_profile["protection_until"] = 0
            defender_profile["cooldown_until"] = now() + DEFENDER_LOSS_COOLDOWN
        save()
        await refresh_lb(c["board"], c["region"], interaction.guild)
        await log_event(interaction.guild, "Challenge Action", f"Channel: <#{c['channel_id']}>\nAction: Dodge\nBy: <@{interaction.user.id}>\nPenalty: 1-day cooldown")
        await interaction.response.send_message("Challenge dodged. A 1-day cooldown has been applied.", ephemeral=True)
        await interaction.channel.send(f"<@{c['challenger']}> **The opponent dodged this challenge.**")


class AutoButton(discord.ui.Button):
    def __init__(self, cid: str):
        super().__init__(label="Auto", style=discord.ButtonStyle.secondary, custom_id=f"pllb:auto:{cid}")
        self.cid = cid

    async def callback(self, interaction: discord.Interaction):
        c = challenge_from(self.cid)
        if not c:
            await interaction.response.send_message("This challenge is no longer active.", ephemeral=True)
            return
        if interaction.user.id not in (c["challenger"], c["opponent"]) and not is_referee(interaction.user):
            await interaction.response.send_message("You don't have access to this action.", ephemeral=True)
            return
        await interaction.response.send_modal(AutoReasonModal(self.cid))


class AutoReasonModal(discord.ui.Modal, title="Auto Result"):
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, cid: str):
        super().__init__()
        self.cid = cid

    async def on_submit(self, interaction: discord.Interaction):
        c = challenge_from(self.cid)
        if not c:
            await interaction.response.send_message("This challenge is no longer active.", ephemeral=True)
            return
        # If the challenged player clicks Auto, challenger wins. Otherwise challenged wins.
        winner = c["challenger"] if interaction.user.id == c["opponent"] else c["opponent"]
        c["delete_reason"] = "Premier League Bot deleted this channel after the auto result."
        await log_event(interaction.guild, "Challenge Action", f"Channel: <#{c['channel_id']}>\nAction: Auto\nBy: <@{interaction.user.id}>\nReason: {str(self.reason)}")
        await finish_challenge(interaction.guild, c, winner, True, str(self.reason), None, None, None)
        await interaction.response.send_message("Auto result recorded. The LB has been updated.", ephemeral=True)


async def finish_challenge(guild: discord.Guild, c: dict, winner: int, auto: bool, reason: str, score: Optional[str], referee: Optional[str], set_id: Optional[int]):
    if c.get("resolved"):
        return
    board, region = c["board"], c["region"]
    b = ensure_board(board, region)
    spots = b["spots"]
    challenger, defender = c["challenger"], c["opponent"]
    challenger_spot = find_player(board, region, challenger)
    defender_spot = find_player(board, region, defender)
    if defender_spot is None:
        raise ValueError("The challenged player's spot no longer exists.")
    if winner not in (challenger, defender):
        raise ValueError("Winner must be one of the two players.")

    old_challenger_spot = challenger_spot
    old_defender_spot = defender_spot
    if winner == challenger:
        if challenger_spot is None:
            spots[defender_spot - 1] = {"user_id": challenger, "stage": ""}
        else:
            spots[challenger_spot - 1], spots[defender_spot - 1] = spots[defender_spot - 1], spots[challenger_spot - 1]
    # Defender win means no rank movement.

    wp = get_profile(winner)
    cp = get_profile(challenger)
    dp = get_profile(defender)
    if winner == defender:
        if dp:
            dp["protection_until"] = now() + PROTECTION_SECONDS
            dp["cooldown_until"] = 0
        if cp:
            cp["cooldown_until"] = now() + COOLDOWN_SECONDS
            cp["protection_until"] = 0
    else:
        # Challenger wins: challenger gets nothing; defeated defender gets 1-day CD.
        if wp:
            wp["protection_until"] = 0
            wp["cooldown_until"] = 0
        if dp:
            dp["cooldown_until"] = now() + DEFENDER_LOSS_COOLDOWN
            dp["protection_until"] = 0

    c["resolved"] = True
    c["winner"] = winner
    c["auto"] = auto
    c["reason"] = reason
    c["score"] = score
    c["referee"] = referee
    c["set_id"] = set_id
    c["resolved_at"] = now()
    c["delete_reason"] = "Premier League Bot deleted this channel after the result was recorded."
    if c.get("channel_id") and str(c["channel_id"]) in DATA["challenges"]:
        original = DATA["challenges"][str(c["channel_id"])]
        original.update({"resolved": True, "winner": winner, "auto": auto, "score": score, "reason": reason, "referee": referee, "set_id": set_id, "resolved_at": now(), "delete_reason": c["delete_reason"]})
    save()
    await refresh_lb(board, region, guild)

    new_challenger_spot = find_player(board, region, challenger)
    new_defender_spot = find_player(board, region, defender)
    score_ch = await fetch_text_channel(guild, SCORE_CHANNEL_ID)
    if score_ch:
        e = discord.Embed(title="Premier League • Score Announcement")
        e.add_field(name="Leaderboard", value=f"{board.title()} • {region}", inline=True)
        if set_id is not None:
            e.add_field(name="Set ID", value=f"#{set_id}", inline=True)
        if DATA["sets"].get(str(set_id or ""), {}).get("ft"):
            e.add_field(name="FT", value=DATA["sets"][str(set_id)]["ft"], inline=True)
        e.add_field(name="Winner", value=f"<@{winner}>", inline=True)
        if not auto and score:
            e.add_field(name="Score", value=score, inline=True)
        if not auto and referee:
            e.add_field(name="Referee", value=referee, inline=True)
        if auto:
            e.add_field(name="Reason", value=reason or "—", inline=False)
        if winner == challenger:
            movement = f"#{old_defender_spot} ({display_name(guild, defender)}) goes to → #{old_challenger_spot or 'Unranked'}\n#{old_challenger_spot or 'Unranked'} ({display_name(guild, challenger)}) goes to → #{old_defender_spot}"
        else:
            movement = f"#{old_defender_spot} ({display_name(guild, defender)}) stays at → #{old_defender_spot}\n#{old_challenger_spot or 'Unranked'} ({display_name(guild, challenger)}) stays at → #{old_challenger_spot or 'Unranked'}"
        e.add_field(name="Rank Movement", value=movement, inline=False)
        if winner == defender:
            e.add_field(name="Status", value=f"<@{defender}>: **3d Protection**\n<@{challenger}>: **3d Cooldown**", inline=False)
        else:
            e.add_field(name="Status", value=f"<@{defender}>: **1d Cooldown**\n<@{challenger}>: **No status**", inline=False)
        msg = await score_ch.send(embed=e)
        if set_id is not None and str(set_id) in DATA["sets"]:
            DATA["sets"][str(set_id)]["score_message_id"] = msg.id
    await log_event(guild, "Challenge Result Logged", f"Channel: <#{c['channel_id']}>\nLeaderboard: {board.title()} • {region}\nChallenger: <@{challenger}> (#{old_challenger_spot or 'Unranked'})\nChallenged: <@{defender}> (#{old_defender_spot})\nWinner: <@{winner}>\nAuto: {auto}\nScore: {score or '—'}\nReferee: {referee or '—'}\nSet ID: {set_id or '—'}\nReason: {reason or '—'}")
    save()


async def delete_challenge(channel_id: int, reason: str):
    ch = bot.get_channel(channel_id)
    if not isinstance(ch, discord.TextChannel):
        DATA["challenges"].pop(str(channel_id), None)
        save()
        return
    transcript = []
    try:
        async for m in ch.history(limit=None, oldest_first=True):
            transcript.append(f"[{m.created_at.isoformat()}] {m.author} ({m.author.id}): {m.content}")
    except Exception:
        transcript.append("[Transcript collection failed]")
    DATA["challenges"].pop(str(channel_id), None)
    save()
    await log_event(ch.guild, "Challenge Channel Deleted", f"Channel: #{ch.name}\n{reason}", "\n".join(transcript).encode("utf-8"), f"{ch.name}-transcript.txt")
    try:
        await ch.delete(reason=reason)
    except Exception:
        pass


class SetApprovalView(discord.ui.View):
    def __init__(self, sid: int):
        super().__init__(timeout=None)
        self.add_item(ApproveSetButton(sid))
        self.add_item(RejectSetButton(sid))


class ApproveSetButton(discord.ui.Button):
    def __init__(self, sid: int):
        super().__init__(label="Approve", style=discord.ButtonStyle.success, custom_id=f"pllb:set:approve:{sid}")
        self.sid = sid

    async def callback(self, interaction: discord.Interaction):
        if not is_supervisor(interaction.user):
            await interaction.response.send_message("Only the Ranking Supervisor can approve sets.", ephemeral=True)
            return
        s = DATA["sets"].get(str(self.sid))
        if not s or s.get("status") != "pending":
            await interaction.response.send_message("This set is no longer pending.", ephemeral=True)
            return
        s["status"] = "approved"
        s["approval_message_id"] = None
        save()
        ch = await fetch_text_channel(interaction.guild, ANNOUNCEMENT_CHANNEL_ID)
        if ch:
            e = discord.Embed(title=f"Set Announcement • #{self.sid}")
            e.add_field(name="Leaderboard", value=f"{s['board'].title()} • {s['region']}", inline=True)
            e.add_field(name="Set", value=f"<@{s['player1']}> #{s['player1_spot']} vs <@{s['player2']}> #{s['player2_spot']}", inline=False)
            e.add_field(name="Time (GMT)", value=s["time_gmt"], inline=True)
            e.add_field(name="FT", value=s["ft"], inline=True)
            msg = await ch.send(content=f"<@&{SET_PING_ROLE_ID}>", embed=e)
            s["announcement_message_id"] = msg.id
        save()
        try:
            await interaction.message.delete()
        except Exception:
            pass
        await interaction.response.send_message(f"Set #{self.sid} approved and announced.", ephemeral=True)


class RejectSetButton(discord.ui.Button):
    def __init__(self, sid: int):
        super().__init__(label="Reject", style=discord.ButtonStyle.danger, custom_id=f"pllb:set:reject:{sid}")
        self.sid = sid

    async def callback(self, interaction: discord.Interaction):
        if not is_supervisor(interaction.user):
            await interaction.response.send_message("Only the Ranking Supervisor can reject sets.", ephemeral=True)
            return
        s = DATA["sets"].get(str(self.sid))
        if not s or s.get("status") != "pending":
            await interaction.response.send_message("This set is no longer pending.", ephemeral=True)
            return
        s["status"] = "rejected"
        save()
        ch = await fetch_text_channel(interaction.guild, MISTAKE_CHANNEL_ID)
        if ch:
            await ch.send(f"<@{s['referee']}> **Set #{self.sid} was rejected.** Ranking Supervisor: please explain the mistake here.")
        try:
            await interaction.message.delete()
        except Exception:
            pass
        await interaction.response.send_message(f"Set #{self.sid} rejected.", ephemeral=True)


@bot.tree.command(name="sendchallenge", description="Send the main leaderboard challenge panel.")
async def sendchallenge(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Owner role required.", ephemeral=True)
        return
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Use this command in a text channel.", ephemeral=True)
        return
    await send_challenge_panel(interaction.channel)
    await interaction.response.send_message("Challenge panel sent.", ephemeral=True)


@bot.tree.command(name="createlb", description="Create and send a 10-spot leaderboard.")
@app_commands.describe(board="overall/mobile", region="AS/EU/NA/SA/OC")
@app_commands.choices(board=[app_commands.Choice(name="Overall", value="overall"), app_commands.Choice(name="Mobile", value="mobile")], region=[app_commands.Choice(name=x, value=x) for x in REGIONS])
async def createlb(interaction: discord.Interaction, board: str, region: str):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Owner role required.", ephemeral=True)
        return
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Use this command in a text channel.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    board, region = board.lower(), region.upper()
    b = ensure_board(board, region)
    # If this LB already exists in a channel, refresh it instead of creating duplicate cards.
    if b.get("message_ids") and b.get("channel_id") == interaction.channel.id:
        await refresh_lb(board, region, interaction.guild)
        await interaction.followup.send(f"{board_title(board, region)} refreshed.", ephemeral=True)
        return
    ids = []
    for spot in SPOTS:
        msg = await interaction.channel.send(embed=spot_embed(interaction.guild, board, region, spot))
        ids.append(msg.id)
    b["message_ids"] = ids
    b["channel_id"] = interaction.channel.id
    save()
    await interaction.followup.send(f"{board_title(board, region)} created with 10 spots.", ephemeral=True)


@bot.tree.command(name="setannouncement", description="Submit a set for Ranking Supervisor approval.")
@app_commands.describe(board="Overall or Mobile", region="Region", player1="Player 1 Discord ID", player1_spot="Player 1 spot", player2="Player 2 Discord ID", player2_spot="Player 2 spot", time_gmt="Day and time in GMT", ft="FT value such as FT7")
@app_commands.choices(board=[app_commands.Choice(name="Overall", value="overall"), app_commands.Choice(name="Mobile", value="mobile")], region=[app_commands.Choice(name=x, value=x) for x in REGIONS])
async def setannouncement(interaction: discord.Interaction, board: str, region: str, player1: str, player1_spot: int, player2: str, player2_spot: int, time_gmt: str, ft: str):
    if not is_referee(interaction.user):
        await interaction.response.send_message("Referee/Ranking Supervisor access required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        p1, p2 = int(player1), int(player2)
    except ValueError:
        await interaction.followup.send("Player IDs must be Discord user IDs.", ephemeral=True)
        return
    if player1_spot not in SPOTS or player2_spot not in SPOTS or player1_spot == player2_spot:
        await interaction.followup.send("Invalid player spots.", ephemeral=True)
        return
    b = ensure_board(board, region)
    if entry_user(b["spots"][player1_spot - 1]) != p1 or entry_user(b["spots"][player2_spot - 1]) != p2:
        await interaction.followup.send("The players/spots do not match the current LB.", ephemeral=True)
        return
    challenge_ctx = DATA["challenges"].get(str(interaction.channel.id)) if interaction.channel else None
    if challenge_ctx and {challenge_ctx.get("challenger"), challenge_ctx.get("opponent")} != {p1, p2}:
        await interaction.followup.send("The players do not match the players in this challenge channel.", ephemeral=True)
        return
    sid = int(DATA["next_set_id"])
    DATA["next_set_id"] = sid + 1
    DATA["sets"][str(sid)] = {
        "id": sid, "board": board, "region": region, "player1": p1, "player1_spot": player1_spot,
        "player2": p2, "player2_spot": player2_spot, "time_gmt": time_gmt.strip(), "ft": ft.strip(),
        "referee": interaction.user.id, "challenger": challenge_ctx.get("challenger") if challenge_ctx else p1,
        "defender": challenge_ctx.get("opponent") if challenge_ctx else p2,
        "challenge_channel_id": challenge_ctx.get("channel_id") if challenge_ctx else 0,
        "status": "pending", "created_at": now(),
        "approval_message_id": None, "announcement_message_id": None, "score_message_id": None,
    }
    save()
    ch = await fetch_text_channel(interaction.guild, APPROVAL_CHANNEL_ID)
    if ch:
        e = discord.Embed(title=f"Set Approval • ID #{sid}", description="Ranking Supervisor approval required.")
        e.add_field(name="Leaderboard", value=f"{board.title()} • {region}", inline=True)
        e.add_field(name="Set", value=f"<@{p1}> #{player1_spot} vs <@{p2}> #{player2_spot}", inline=False)
        e.add_field(name="Time (GMT)", value=time_gmt, inline=True)
        e.add_field(name="FT", value=ft, inline=True)
        msg = await ch.send(embed=e, view=SetApprovalView(sid))
        DATA["sets"][str(sid)]["approval_message_id"] = msg.id
        save()
    await interaction.followup.send(f"Set **#{sid}** submitted for approval.", ephemeral=True)


@bot.tree.command(name="scoreannouncement", description="Announce a set result and update the LB.")
@app_commands.describe(winner="Winner Discord ID", auto="true or false", set_id="Global set ID", score="Manual score such as 5-3", referee="Optional referee", reason="Required only for auto")
async def scoreannouncement(interaction: discord.Interaction, winner: str, auto: str, set_id: int, score: Optional[str] = None, referee: Optional[str] = None, reason: Optional[str] = None):
    if not is_referee(interaction.user):
        await interaction.response.send_message("Referee/Ranking Supervisor access required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    s = DATA["sets"].get(str(set_id))
    if not s or s.get("status") != "approved":
        await interaction.followup.send("That set does not exist or is not approved/available for scoring.", ephemeral=True)
        return
    try:
        w = int(winner)
    except ValueError:
        await interaction.followup.send("Winner must be a Discord user ID.", ephemeral=True)
        return
    if w not in (s["player1"], s["player2"]):
        await interaction.followup.send("Winner must be one of the two players in this set.", ephemeral=True)
        return
    is_auto = auto.strip().lower() == "true"
    if auto.strip().lower() not in ("true", "false"):
        await interaction.followup.send("Auto must be true or false.", ephemeral=True)
        return
    if is_auto:
        if not reason or not reason.strip():
            await interaction.followup.send("Auto=true requires a reason.", ephemeral=True)
            return
        score = None
        referee = None
    else:
        if not score or not score.strip():
            await interaction.followup.send("Auto=false requires the score.", ephemeral=True)
            return
        score = score.strip()
        referee = referee.strip() if referee and referee.strip() else None
        reason = None
    challenger_id = int(s.get("challenger", s["player1"]))
    defender_id = int(s.get("defender", s["player2"]))
    challenger_spot = s["player1_spot"] if challenger_id == s["player1"] else s["player2_spot"]
    defender_spot = s["player2_spot"] if defender_id == s["player2"] else s["player1_spot"]
    c = {"channel_id": int(s.get("challenge_channel_id", 0) or 0), "board": s["board"], "region": s["region"], "challenger": challenger_id, "opponent": defender_id, "challenger_spot": challenger_spot, "opponent_spot": defender_spot, "resolved": False}
    try:
        await finish_challenge(interaction.guild, c, w, is_auto, reason or "", score, referee, set_id)
    except ValueError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return
    s["status"] = "completed"
    s["winner"] = w
    s["score"] = score
    s["auto"] = is_auto
    s["reason"] = reason
    s["referee_used"] = referee
    save()
    await interaction.followup.send(f"Set **#{set_id}** scored and the LB has been updated.", ephemeral=True)


async def update_shifted_set_message(guild: discord.Guild, s: dict, field: str, channel_id: int, old_id: int) -> None:
    mid = s.get(field)
    if not mid:
        return
    ch = await fetch_text_channel(guild, channel_id)
    if not ch:
        return
    try:
        msg = await ch.fetch_message(int(mid))
        if not msg.embeds:
            return
        old_embed = msg.embeds[0]
        e = discord.Embed.from_dict(old_embed.to_dict())
        e.title = (e.title or "").replace(f"#{old_id}", f"#{s['id']}")
        for f in e.fields:
            if f.name == "Set ID":
                f.value = f"#{s['id']}"
        await msg.edit(embed=e)
    except Exception:
        pass


@bot.tree.command(name="voidset", description="Void a set and shift every later global set ID back by 1.")
async def voidset(interaction: discord.Interaction, set_id: int):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Owner role required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    s = DATA["sets"].get(str(set_id))
    if not s:
        await interaction.followup.send("Set not found.", ephemeral=True)
        return
    for field, channel_id in (("score_message_id", SCORE_CHANNEL_ID), ("announcement_message_id", ANNOUNCEMENT_CHANNEL_ID), ("approval_message_id", APPROVAL_CHANNEL_ID)):
        mid = s.get(field)
        if mid:
            ch = await fetch_text_channel(interaction.guild, channel_id)
            if ch:
                try:
                    await (await ch.fetch_message(int(mid))).delete()
                except Exception:
                    pass
    later = sorted(int(k) for k in DATA["sets"] if int(k) > set_id)
    DATA["sets"].pop(str(set_id), None)
    for old in later:
        obj = DATA["sets"].pop(str(old))
        obj["id"] = old - 1
        DATA["sets"][str(old - 1)] = obj
    DATA["next_set_id"] = max([int(k) for k in DATA["sets"]] + [0]) + 1
    save()
    # Update visible IDs in all later set messages.
    for new_id in sorted(int(k) for k in DATA["sets"] if int(k) >= set_id):
        obj = DATA["sets"][str(new_id)]
        old_id = new_id + 1
        if obj.get("status") == "pending" and obj.get("approval_message_id"):
            # Approval buttons contain the old ID, so replace the message with a fresh approval view.
            ch = await fetch_text_channel(interaction.guild, APPROVAL_CHANNEL_ID)
            if ch:
                try:
                    old_msg = await ch.fetch_message(int(obj["approval_message_id"]))
                    await old_msg.delete()
                except Exception:
                    pass
                e = discord.Embed(title=f"Set Approval • ID #{new_id}", description="Ranking Supervisor approval required.")
                e.add_field(name="Leaderboard", value=f"{obj['board'].title()} • {obj['region']}", inline=True)
                e.add_field(name="Set", value=f"<@{obj['player1']}> #{obj['player1_spot']} vs <@{obj['player2']}> #{obj['player2_spot']}", inline=False)
                e.add_field(name="Time (GMT)", value=obj["time_gmt"], inline=True)
                e.add_field(name="FT", value=obj["ft"], inline=True)
                msg = await ch.send(embed=e, view=SetApprovalView(new_id))
                obj["approval_message_id"] = msg.id
        else:
            await update_shifted_set_message(interaction.guild, obj, "announcement_message_id", ANNOUNCEMENT_CHANNEL_ID, old_id)
            await update_shifted_set_message(interaction.guild, obj, "score_message_id", SCORE_CHANNEL_ID, old_id)
    save()
    ch = await fetch_text_channel(interaction.guild, ANNOUNCEMENT_CHANNEL_ID)
    if ch:
        await ch.send(f"**Set #{set_id} has been voided.** All set IDs after this set will get behind by 1.")
    await interaction.followup.send("Set voided and later global set IDs shifted back by 1.", ephemeral=True)


@bot.tree.command(name="deletechannel", description="Delete a challenge channel and save its transcript.")
async def deletechannel(interaction: discord.Interaction, name: str):
    if not (is_referee(interaction.user) or is_owner(interaction.user)):
        await interaction.response.send_message("Referee/Supervisor/Owner access required.", ephemeral=True)
        return
    target = None
    if name.isdigit():
        target = interaction.guild.get_channel(int(name))
    if target is None:
        target = discord.utils.get(interaction.guild.text_channels, name=name.lstrip("#"))
    if not isinstance(target, discord.TextChannel):
        await interaction.response.send_message("Channel not found.", ephemeral=True)
        return
    await interaction.response.send_message("Deleting channel and saving transcript.", ephemeral=True)
    await delete_challenge(target.id, f"Manually deleted by {interaction.user} ({interaction.user.id}).")


@bot.tree.command(name="editprofile", description="Edit a stored LB profile by profile ID.")
async def editprofile(interaction: discord.Interaction, profile_id: int, nickname: str, discord_id: str, roblox_username: str, country_flag: str):
    if not is_editor(interaction.user):
        await interaction.response.send_message("Editor role required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    target_key = None
    old = None
    for key, p in DATA["profiles"].items():
        if int(p.get("profile_id", 0)) == profile_id:
            target_key, old = key, p
            break
    if not old:
        await interaction.followup.send("Profile ID not found.", ephemeral=True)
        return
    try:
        canonical, avatar = await roblox_lookup(roblox_username)
        uid = int(target_key)
    except Exception as exc:
        await interaction.followup.send(f"Profile update failed: {exc}", ephemeral=True)
        return
    if str(discord_id).strip() != str(uid):
        await interaction.followup.send("The Discord ID must match the player stored in this profile.", ephemeral=True)
        return
    old.update({
        "nickname": nickname.strip(), "discord_id": discord_id.strip(),
        "roblox_username": canonical, "country_flag": country_flag.strip(), "avatar_url": avatar,
    })
    save()
    await save_profile_message(interaction.guild, old)
    await refresh_all_lbs(interaction.guild)
    await interaction.followup.send("Profile updated, Roblox avatar refreshed, and affected LBs refreshed.", ephemeral=True)


@bot.tree.command(name="deleteprofile", description="Delete a stored LB profile by profile ID.")
async def deleteprofile(interaction: discord.Interaction, profile_id: int):
    if not is_editor(interaction.user):
        await interaction.response.send_message("Editor role required.", ephemeral=True)
        return
    target_key = None
    profile = None
    for key, p in DATA["profiles"].items():
        if int(p.get("profile_id", 0)) == profile_id:
            target_key, profile = key, p
            break
    if not profile:
        await interaction.response.send_message("Profile ID not found.", ephemeral=True)
        return
    uid = int(target_key)
    if profile.get("profile_message_id"):
        ch = await fetch_text_channel(interaction.guild, PROFILE_CHANNEL_ID)
        if ch:
            try:
                await (await ch.fetch_message(int(profile["profile_message_id"]))).delete()
            except Exception:
                pass
    DATA["profiles"].pop(str(uid), None)
    for b in DATA["boards"].values():
        for i, entry in enumerate(b["spots"]):
            if entry_user(entry) == uid:
                b["spots"][i] = None
    save()
    await refresh_all_lbs(interaction.guild)
    await interaction.response.send_message(f"Profile #{profile_id:03d} deleted and all LB spots belonging to that player were cleared.", ephemeral=True)


@bot.tree.command(name="clearspot", description="Clear a leaderboard spot.")
@app_commands.choices(board=[app_commands.Choice(name="Overall", value="overall"), app_commands.Choice(name="Mobile", value="mobile")], region=[app_commands.Choice(name=x, value=x) for x in REGIONS])
async def clearspot(interaction: discord.Interaction, board: str, region: str, spot: int):
    if not is_editor(interaction.user):
        await interaction.response.send_message("Editor role required.", ephemeral=True)
        return
    if spot not in SPOTS:
        await interaction.response.send_message("Spot must be 1-10.", ephemeral=True)
        return
    ensure_board(board, region)["spots"][spot - 1] = None
    save()
    await refresh_lb(board, region, interaction.guild)
    await interaction.response.send_message(f"Cleared **#{spot}** on **{board_title(board, region)}**.", ephemeral=True)


@bot.tree.command(name="editspot", description="Edit the stage of an occupied leaderboard spot.")
@app_commands.choices(board=[app_commands.Choice(name="Overall", value="overall"), app_commands.Choice(name="Mobile", value="mobile")], region=[app_commands.Choice(name=x, value=x) for x in REGIONS])
async def editspot(interaction: discord.Interaction, board: str, region: str, spot: int, stage: str):
    if not is_editor(interaction.user):
        await interaction.response.send_message("Editor role required.", ephemeral=True)
        return
    if spot not in SPOTS:
        await interaction.response.send_message("Spot must be 1-10.", ephemeral=True)
        return
    b = ensure_board(board, region)
    if b["spots"][spot - 1] is None:
        await interaction.response.send_message("That spot is vacant. Place a player there through the LB claim flow first.", ephemeral=True)
        return
    b["spots"][spot - 1]["stage"] = stage.strip()
    save()
    await refresh_lb(board, region, interaction.guild)
    await interaction.response.send_message(f"Updated **#{spot}** stage to **{stage}**.", ephemeral=True)


@tasks.loop(minutes=1)
async def cleanup_challenges():
    cutoff = now() - 30 * 60
    for cid, c in list(DATA["challenges"].items()):
        if int(c.get("created_at", 0)) <= cutoff:
            reason = c.get("delete_reason", "Premier League Bot deleted this channel after 30 minutes.")
            await delete_challenge(int(cid), reason)


@tasks.loop(minutes=1)
async def expire_statuses():
    changed = False
    t = now()
    for p in DATA["profiles"].values():
        before = (p.get("protection_until", 0), p.get("cooldown_until", 0))
        if p.get("protection_until", 0) and p["protection_until"] <= t:
            p["protection_until"] = 0
        if p.get("cooldown_until", 0) and p["cooldown_until"] <= t:
            p["cooldown_until"] = 0
        if before != (p.get("protection_until", 0), p.get("cooldown_until", 0)):
            changed = True
    if changed:
        save()
        for guild in bot.guilds:
            await refresh_all_lbs(guild)


@bot.event
async def on_ready():
    if not expire_statuses.is_running():
        expire_statuses.start()
    if not cleanup_challenges.is_running():
        cleanup_challenges.start()
    # Restore persistent main challenge panels and pending approval buttons.
    for gid, channel_id in DATA.get("challenge_panel_messages", {}).items():
        try:
            guild = bot.get_channel(int(channel_id)).guild
            bot.add_view(BoardView(), message_id=int(gid))
        except Exception:
            pass
    for sid, s in DATA.get("sets", {}).items():
        if s.get("status") == "pending":
            mid = s.get("approval_message_id")
            if mid:
                try:
                    bot.add_view(SetApprovalView(int(sid)), message_id=int(mid))
                except Exception:
                    pass
    for cid, c in DATA.get("challenges", {}).items():
        if c.get("resolved"):
            continue
        try:
            if c.get("out_of_range") or status_for_profile(get_profile(c["opponent"])) == "PROTECTION":
                bot.add_view(DefenseView(cid))
            else:
                bot.add_view(FightAutoView(cid))
        except Exception:
            pass
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()
    log.info("Logged in as %s", bot.user)


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN is missing.")
    bot.run(TOKEN)
