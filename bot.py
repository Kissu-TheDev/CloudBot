import os
import io
import json
import asyncio
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import UserNotParticipant, FloodWait
from motor.motor_asyncio import AsyncIOMotorClient

# Pyrogram ke apne FAQ ka suggestion: agar "client started but nothing happens"
# jaisa issue ho, INFO-level logging on karke dekho — network/socket errors
# yahi dikhte hain jo normal print() statements catch nahi karte.
logging.basicConfig(level=logging.INFO)

# ==========================================
# 🔧 ENV LOAD
# ==========================================
load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
MONGODB_URL = os.environ.get("MONGODB_URL")
ADMIN_ID = os.environ.get("ADMIN_ID")
DB_CHANNEL_ID = os.environ.get("DB_CHANNEL_ID")

missing = [name for name, val in {
    "BOT_TOKEN": BOT_TOKEN, "API_ID": API_ID, "API_HASH": API_HASH,
    "MONGODB_URL": MONGODB_URL, "ADMIN_ID": ADMIN_ID
}.items() if not val]
if missing:
    raise SystemExit(f"❌ .env me ye missing hai: {', '.join(missing)}")

API_ID = int(API_ID)
ADMIN_ID = int(ADMIN_ID)  # Super-admin — sirf ye doosre admins add/remove kar sakta hai, .env se aata hai

app = Client("KissuCloudBot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

db_client = AsyncIOMotorClient(MONGODB_URL)
db = db_client["KissuDB"]
settings_col = db["settings"]
tokens_col = db["tokens"]
users_col = db["users"]  # broadcast ke liye track karte hain kaun kaun /start kar chuka hai
admins_col = db["admins"]  # super-admin ke alawa jo extra admins add kiye gaye hain: { _id: user_id }
banned_col = db["banned_users"]  # { _id: user_id, banned_at: datetime, reason: str|None }

PAGE_SIZE = 10
pending_action = {}  # { user_id: "awaiting_xxx" }
BOT_USERNAME = None  # startup pe app.get_me() se fill hoga, deep-link banane ke liye
redemption_times = {}  # { user_id: [timestamp1, timestamp2, ...] } — rate-limit sliding window ke liye
rate_limit_blocks = {}  # { user_id: blocked_until_timestamp } — fixed hard-cooldown ke liye
_admin_ids_cache = None  # set of admin ids, lazily filled + invalidated on add/remove
_config_cache = None  # (config_dict, fetched_at_timestamp) — chhota TTL cache taaki har handler baar-baar DB na maare
_CONFIG_CACHE_TTL = 5  # seconds

# Rate-limit defaults — sab kuch /admin se customize ho sakta hai (config me store hote hain)
DEFAULT_RATELIMIT_COUNT = 3        # kitni baar redeem karne ke baad cooldown lage
DEFAULT_RATELIMIT_WINDOW = 60      # kitne seconds ke andar wo count hona chahiye
DEFAULT_RATELIMIT_WAIT = 30        # cooldown kitni der ka ho (seconds)
DEFAULT_RATELIMIT_MESSAGE = "Wait {s}s...."  # {s} me remaining seconds fill hota hai

# ==========================================
# 💬 DEFAULT CUSTOMIZABLE MESSAGES
# Ye saare keys admin panel se edit ho sakte hain.
# "extra" ek list hai - trigger pe in sabhi messages ko bhi bhejega (order me).
# ==========================================
DEFAULT_MESSAGES = {
    "welcome": {"text": "🧑‍💻", "extra": []},
    "verified": {"text": "🪪", "extra": []},
    "not_joined": {"text": "🗝️", "extra": []},
    "restricted": {"text": "❗️", "extra": []},
    "admin_welcome": {"text": "✅", "extra": []},
    "sending": {"text": "📤", "extra": []},
    "invalid_token": {"text": "❌", "extra": []},
    "ratelimit": {"text": "🤖", "extra": []},
    "banned": {"text": "🚫", "extra": []},
}


# ==========================================
# ⏱ Time parser
# ==========================================
def parse_time(time_str):
    if not time_str or len(time_str) < 2:
        return None
    time_dict = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    unit = time_str[-1].lower()
    number_part = time_str[:-1]
    if unit in time_dict and number_part.isdigit():
        return int(number_part) * time_dict[unit]
    return None


async def get_config():
    """5-second TTL cache ke saath — pehle almost har handler apne se config fetch
    kar raha tha, kabhi ek hi request ke andar 2-3 baar. Isse Mongo round-trips
    kaafi kam ho jaate hain, aur settings change hote hi max 5s me naya reflect ho jaata."""
    global _config_cache
    now = datetime.now().timestamp()
    if _config_cache is not None and (now - _config_cache[1]) < _CONFIG_CACHE_TTL:
        return _config_cache[0]
    config = await settings_col.find_one({"_id": "config"}) or {}
    _config_cache = (config, now)
    return config


def invalidate_config_cache():
    """Jab bhi settings_col me koi write ho, isko call karo taaki agla read stale na mile."""
    global _config_cache
    _config_cache = None


async def set_config(fields):
    """settings_col.update_one({"$set": fields}) + cache invalidate, ek jagah se —
    taaki koi bhi settings-write path cache-invalidate karna na bhoole."""
    await settings_col.update_one({"_id": "config"}, {"$set": fields}, upsert=True)
    invalidate_config_cache()


async def replace_config(new_doc):
    new_doc["_id"] = "config"
    await settings_col.replace_one({"_id": "config"}, new_doc, upsert=True)
    invalidate_config_cache()


async def get_all_admin_ids():
    """Super-admin (.env) + DB me add kiye gaye admins, dono milake ek set return karta hai. Cached."""
    global _admin_ids_cache
    if _admin_ids_cache is None:
        extra = [doc["_id"] async for doc in admins_col.find({})]
        _admin_ids_cache = {ADMIN_ID, *extra}
    return _admin_ids_cache


def invalidate_admin_cache():
    global _admin_ids_cache
    _admin_ids_cache = None


async def is_admin_user(user_id):
    ids = await get_all_admin_ids()
    return user_id in ids


async def is_banned(user_id):
    return await banned_col.find_one({"_id": user_id}) is not None


async def _admin_filter_func(_, __, update):
    """Pyrogram custom filter — DB-backed admin list check karta hai (super-admin +
    add kiye gaye extra admins), taaki static filters.user(ADMIN_ID) ki jagah
    dynamic multi-admin support kaam kare."""
    user = update.from_user
    if not user:
        return False
    return await is_admin_user(user.id)


admin_filter = filters.create(_admin_filter_func)


async def _superadmin_filter_func(_, __, update):
    """Sirf .env wala ADMIN_ID — extra admins add/remove karna sirf super-admin ka kaam hai."""
    user = update.from_user
    return bool(user) and user.id == ADMIN_ID


superadmin_filter = filters.create(_superadmin_filter_func)


async def get_db_channel_id():
    config = await get_config()
    if config.get("db_channel_id"):
        return int(config["db_channel_id"])
    if DB_CHANNEL_ID:
        return int(DB_CHANNEL_ID)
    return None


async def is_fsub_joined(client, user_id):
    config = await get_config()
    fsub_id = config.get("fsub_id")
    if not fsub_id:
        return True
    try:
        await client.get_chat_member(int(fsub_id), user_id)
        return True
    except UserNotParticipant:
        return False
    except Exception as e:
        # Fail-CLOSED: koi bhi error (bot admin nahi hai, wrong channel ID, etc.)
        # ka matlab hai "verify nahi ho paya" -> user ko not-joined treat karo.
        # Ye zaroori hai warna ek misconfiguration se FSUB silently bypass ho jaata.
        print(f"⚠️ FSUB check fail hua (fail-closed treat kar rahe hain): {e}")
        return False


async def get_message(key):
    """Custom message uthata hai DB se, warna default use karta hai."""
    config = await get_config()
    custom = config.get("messages", {}).get(key)
    if custom:
        return custom
    return DEFAULT_MESSAGES.get(key, {"text": "", "extra": []})


async def send_custom(client, chat_id, key, reply_markup=None):
    """Custom message + extras bhejta hai. Main text ka Message object return karta hai (edit ke liye)."""
    msg_data = await get_message(key)
    main = await client.send_message(chat_id, msg_data["text"], reply_markup=reply_markup)
    for extra_text in msg_data.get("extra", []):
        await client.send_message(chat_id, extra_text)
        await asyncio.sleep(0.3)
    return main


# ==========================================
# 🔢 MULTI-RANGE TOKEN PARSER
# Format: "4-8 20-25 VIP" ya single file bhi: "4-8 15 VIP"
# Aakhri part naam hai, baaki sab ranges/numbers hain.
# ==========================================
def parse_ranges(parts):
    """Returns (file_ids_list, error_message_or_None)"""
    file_ids = []
    for part in parts:
        if "-" in part:
            bits = part.split("-")
            if len(bits) != 2 or not (bits[0].isdigit() and bits[1].isdigit()):
                return None, f"❌ `{part}` ek valid range nahi hai (format: start-end)."
            start, end = int(bits[0]), int(bits[1])
            if end < start:
                return None, f"❌ `{part}` me end, start se chota hai."
            file_ids.extend(range(start, end + 1))
        elif part.isdigit():
            file_ids.append(int(part))
        else:
            return None, f"❌ `{part}` samajh nahi aaya (number ya range hona chahiye)."
    # Duplicates hata ke sorted order me rakho
    return sorted(set(file_ids)), None


# ==========================================
# 🛠 ADMIN PANEL
# ==========================================

async def admin_panel_markup(user_id=None):
    """user_id diya jaaye to "Manage Admins" button sirf super-admin ko dikhega —
    baaki admins ko wo option nahi milega."""
    config = await get_config()
    protect_status = "🟢 ON" if config.get("content_protection") else "🔴 OFF"
    rows = [
        # --- Setup ---
        [InlineKeyboardButton("📢 Set FSUB", callback_data="panel_setfsub"),
         InlineKeyboardButton("🗄 Set DB Channel", callback_data="panel_setdb")],
        [InlineKeyboardButton("⏱ Set Timer", callback_data="panel_settimer"),
         InlineKeyboardButton("🗑 Auto-Delete", callback_data="panel_autodelete")],
        # --- Tokens ---
        [InlineKeyboardButton("🔑 Generate Token", callback_data="panel_gentoken"),
         InlineKeyboardButton("❌ Revoke Token", callback_data="panel_revoke")],
        [InlineKeyboardButton("🔍 Search/List Tokens", callback_data="panel_search")],
        # --- Users ---
        [InlineKeyboardButton("🚫 Ban / Unban User", callback_data="panel_banmenu")],
    ]
    if user_id is None or user_id == ADMIN_ID:
        rows.append([InlineKeyboardButton("👑 Manage Admins", callback_data="panel_admins")])
    rows += [
        # --- Settings ---
        [InlineKeyboardButton(f"🔒 Content Protection: {protect_status}", callback_data="panel_toggleprotect")],
        [InlineKeyboardButton("🤖 Rate-Limit Settings", callback_data="panel_ratelimit")],
        [InlineKeyboardButton("✏️ Edit Messages", callback_data="panel_editmsg")],
        [InlineKeyboardButton("📣 Broadcast", callback_data="panel_broadcast"),
         InlineKeyboardButton("🐞 Debug", callback_data="panel_debug")],
        # --- Data ---
        [InlineKeyboardButton("📤 Export Settings", callback_data="panel_export"),
         InlineKeyboardButton("📥 Import Settings", callback_data="panel_import")],
        [InlineKeyboardButton("🚪 Quit", callback_data="panel_quit")],
    ]
    return InlineKeyboardMarkup(rows)


def edit_msg_markup():
    keys = list(DEFAULT_MESSAGES.keys())
    rows = []
    for k in keys:
        rows.append([InlineKeyboardButton(k, callback_data=f"editmsg_{k}")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="panel_back")])
    return InlineKeyboardMarkup(rows)


# ==========================================
# 🚫 BAN / UNBAN — menu + list view + JSON export
# ==========================================
def ban_menu_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Ban User (by ID)", callback_data="ban_add")],
        [InlineKeyboardButton("➖ Unban User (by ID)", callback_data="ban_remove")],
        [InlineKeyboardButton("📋 List Banned Users", callback_data="ban_list_0")],
        [InlineKeyboardButton("📤 Export Ban List (JSON)", callback_data="ban_export")],
        [InlineKeyboardButton("🔙 Back", callback_data="panel_back")],
    ])


async def build_ban_list_view(offset=0):
    cursor = banned_col.find({}).sort("banned_at", -1)
    all_banned = [b async for b in cursor]

    if not all_banned:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="panel_banmenu")]])
        return "📋 Koi bhi user banned nahi hai.", markup

    page = all_banned[offset: offset + PAGE_SIZE]
    lines = ["📋 **Banned Users:**\n"]
    for b in page:
        reason = f" — {b['reason']}" if b.get("reason") else ""
        lines.append(f"`{b['_id']}`{reason}")

    nav_row = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton("⏮ Prev", callback_data=f"ban_list_{max(0, offset - PAGE_SIZE)}"))
    if offset + PAGE_SIZE < len(all_banned):
        nav_row.append(InlineKeyboardButton("Next ⏭", callback_data=f"ban_list_{offset + PAGE_SIZE}"))

    rows = []
    if nav_row:
        rows.append(nav_row)
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="panel_banmenu")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


# ==========================================
# 👑 MANAGE ADMINS — super-admin only
# ==========================================
async def build_admin_list_view():
    extra_admins = [doc["_id"] async for doc in admins_col.find({})]
    lines = [f"👑 **Admins**\n\n**Super-Admin:** `{ADMIN_ID}` (env)\n"]
    if extra_admins:
        lines.append("**Extra Admins:**")
        for aid in extra_admins:
            lines.append(f"`{aid}`")
    else:
        lines.append("Koi extra admin add nahi kiya hai.")

    rows = [
        [InlineKeyboardButton("➕ Add Admin (by ID)", callback_data="adm_add")],
        [InlineKeyboardButton("➖ Remove Admin (by ID)", callback_data="adm_remove")],
        [InlineKeyboardButton("🔙 Back", callback_data="panel_back")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


# ==========================================
# ⏱ SET TIMER / 🗑 AUTO-DELETE — button menus
# Presets seedha save hote hain; "Custom" purane text-prompt flow
# (awaiting_settimer / awaiting_autodelete) me gira deta hai.
# ==========================================
def settimer_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("30m", callback_data="stmr_30m"),
         InlineKeyboardButton("1h", callback_data="stmr_1h"),
         InlineKeyboardButton("6h", callback_data="stmr_6h"),
         InlineKeyboardButton("1d", callback_data="stmr_1d")],
        [InlineKeyboardButton("✏️ Custom", callback_data="stmr_custom")],
        [InlineKeyboardButton("🔙 Back", callback_data="panel_back")],
    ])


def autodelete_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("10m", callback_data="adel_10m"),
         InlineKeyboardButton("1h", callback_data="adel_1h"),
         InlineKeyboardButton("6h", callback_data="adel_6h")],
        [InlineKeyboardButton("🔴 Off", callback_data="adel_off"),
         InlineKeyboardButton("✏️ Custom", callback_data="adel_custom")],
        [InlineKeyboardButton("🔙 Back", callback_data="panel_back")],
    ])


# ==========================================
# ❌ REVOKE TOKEN — button list (typing ki zaroorat nahi)
# ==========================================
async def build_revoke_list_view(offset=0):
    now = datetime.now()
    cursor = tokens_col.find({"revoked": False, "expiry_time": {"$gt": now}}).sort("expiry_time", 1)
    all_tokens = [t async for t in cursor]

    if not all_tokens:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="panel_back")]])
        return "❌ Revoke karne ke liye koi active token nahi hai.", markup

    page = all_tokens[offset: offset + PAGE_SIZE]
    rows = []
    for t in page:
        used = t.get("used_count", 0)
        limit = t.get("usage_limit")
        usage_text = f"{used}/{limit} uses" if limit is not None else f"{used} uses"
        label = f"{t['token_id']} ({len(t['files'])} files, {usage_text})"
        rows.append([InlineKeyboardButton(label, callback_data=f"rvk_{t['token_id']}")])

    nav_row = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton("⏮ Prev", callback_data=f"rvkpage_{max(0, offset - PAGE_SIZE)}"))
    if offset + PAGE_SIZE < len(all_tokens):
        nav_row.append(InlineKeyboardButton("Next ⏭", callback_data=f"rvkpage_{offset + PAGE_SIZE}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("🔙 Back", callback_data="panel_back")])
    text = f"❌ **Revoke Token** — jo revoke karna hai chuno ({len(all_tokens)} active):"
    return text, InlineKeyboardMarkup(rows)


# ==========================================
# 🔑 GENERATE TOKEN WIZARD — button menus per step
# Typing sirf 4 jagah: files, naam, limit-value, expiry-value.
# Baaki sab (limit-choice, expiry-choice, auto-delete, link, confirm) buttons se.
# ==========================================
def gtf_limit_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔢 Set Limit", callback_data="gtf_setlimit"),
         InlineKeyboardButton("⏭ Skip (Unlimited)", callback_data="gtf_skiplimit")],
        [InlineKeyboardButton("🔙 Cancel", callback_data="gtf_cancel")],
    ])


def gtf_expiry_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏱ Set Expiry", callback_data="gtf_setexpiry"),
         InlineKeyboardButton("⏭ Skip (Global Default)", callback_data="gtf_skipexpiry")],
        [InlineKeyboardButton("🔙 Cancel", callback_data="gtf_cancel")],
    ])


def gtf_autodelete_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Force ON", callback_data="gtf_adon"),
         InlineKeyboardButton("🔴 Force OFF", callback_data="gtf_adoff")],
        [InlineKeyboardButton("⏭ Skip (Global Setting)", callback_data="gtf_adskip")],
        [InlineKeyboardButton("🔙 Cancel", callback_data="gtf_cancel")],
    ])


def gtf_link_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Yes", callback_data="gtf_linkyes"),
         InlineKeyboardButton("🚫 No", callback_data="gtf_linkno")],
        [InlineKeyboardButton("🔙 Cancel", callback_data="gtf_cancel")],
    ])


def gtf_confirm_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Generate Token", callback_data="gtf_generate")],
        [InlineKeyboardButton("🔙 Cancel", callback_data="gtf_cancel")],
    ])


def gtf_cancel_only_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="gtf_cancel")]])


def gtf_summary_text(data):
    limit_text = f"{data['usage_limit']} uses" if data.get("usage_limit") else "Unlimited"
    expiry_text = data.get("expiry_display") or "Global default"
    ad = data.get("auto_delete_override")
    ad_text = "Global setting follow hogi" if ad is None else ("Force ON" if ad else "Force OFF")
    link_text = "Haan" if data.get("want_link") else "Nahi"
    return (
        "🔑 **Naya Token — Confirm**\n\n"
        f"**Files:** {len(data['file_ids'])}\n"
        f"**Naam:** {data['name']}\n"
        f"**Usage Limit:** {limit_text}\n"
        f"**Expiry:** {expiry_text}\n"
        f"**Auto-Delete:** {ad_text}\n"
        f"**Deep-Link:** {link_text}\n\n"
        "Sab sahi hai?"
    )


@app.on_message(filters.command("start") & filters.private & admin_filter)
async def admin_start(client, message):
    """Admin ke liye /start alag hai - customizable admin_welcome message + panel button."""
    await users_col.update_one(
        {"_id": message.from_user.id},
        {"$set": {"_id": message.from_user.id, "first_seen": datetime.now()}},
        upsert=True
    )
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("🛠 Open Admin Panel", callback_data="panel_open")]])
    await send_custom(client, message.chat.id, "admin_welcome", reply_markup=buttons)


@app.on_message(filters.command("admin") & admin_filter)
async def admin_panel(client, message):
    await message.reply_text(
        "🛠 **Admin Panel** — neeche se option chuno:",
        reply_markup=await admin_panel_markup(message.from_user.id)
    )


@app.on_callback_query(filters.regex(r"^panel_") & admin_filter)
async def panel_callback(client, callback_query):
    action = callback_query.data.split("_", 1)[1]
    user_id = callback_query.from_user.id

    if action == "open":
        return await callback_query.message.edit_text(
            "🛠 **Admin Panel** — neeche se option chuno:", reply_markup=await admin_panel_markup(user_id)
        )

    if action == "quit":
        pending_action.pop(user_id, None)
        return await callback_query.message.delete()

    if action == "back":
        pending_action.pop(user_id, None)
        return await callback_query.message.edit_text(
            "🛠 **Admin Panel** — neeche se option chuno:", reply_markup=await admin_panel_markup(user_id)
        )

    if action == "toggleprotect":
        config = await get_config()
        new_value = not config.get("content_protection", False)
        await set_config({"content_protection": new_value})
        await callback_query.answer(f"Content Protection {'ON' if new_value else 'OFF'} kar diya.")
        return await callback_query.message.edit_text(
            "🛠 **Admin Panel** — neeche se option chuno:", reply_markup=await admin_panel_markup(user_id)
        )

    if action == "ratelimit":
        config = await get_config()
        count = config.get("ratelimit_count", DEFAULT_RATELIMIT_COUNT)
        window = config.get("ratelimit_window", DEFAULT_RATELIMIT_WINDOW)
        wait = config.get("ratelimit_wait", DEFAULT_RATELIMIT_WAIT)
        msg_template = config.get("ratelimit_message", DEFAULT_RATELIMIT_MESSAGE)
        text = (
            f"🤖 **Rate-Limit Settings**\n\n"
            f"**Count:** {count} baar\n"
            f"**Window:** {window}s ke andar\n"
            f"**Cooldown:** {wait}s\n"
            f"**Message:** `{msg_template}`\n\n"
            f"Matlab: {window}s ke andar {count} baar redeem kiya to agla attempt {wait}s ke liye block hoga."
        )
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔢 Count Badlo", callback_data="rl_setcount"),
             InlineKeyboardButton("⏱ Window Badlo", callback_data="rl_setwindow")],
            [InlineKeyboardButton("⏳ Cooldown Badlo", callback_data="rl_setwait"),
             InlineKeyboardButton("✏️ Message Badlo", callback_data="rl_setmessage")],
            [InlineKeyboardButton("🔙 Back", callback_data="panel_back")],
        ])
        await callback_query.answer()
        return await callback_query.message.edit_text(text, reply_markup=buttons)

    if action == "editmsg":
        return await callback_query.message.edit_text(
            "✏️ Kaunsa message edit karna hai?", reply_markup=edit_msg_markup()
        )

    if action == "debug":
        config = await get_config()
        total_users = await users_col.count_documents({})
        total_tokens = await tokens_col.count_documents({})
        active_tokens = await tokens_col.count_documents({"revoked": False, "expiry_time": {"$gt": datetime.now()}})
        db_channel_id = await get_db_channel_id()
        db_status = f"`{db_channel_id}`" if db_channel_id else "Not set ❌"

        fsub_id = config.get("fsub_id")
        if not fsub_id:
            fsub_status = "Not set ❌"
        else:
            # Fail-closed hone ke baad ye check zaroori hai: agar bot us channel ka
            # admin nahi hai to FSUB "set" dikhega but practically sab users block ho rahe honge.
            try:
                await client.get_chat_member(int(fsub_id), "me")
                fsub_status = f"`{fsub_id}` — ✅ Bot admin hai, working"
            except Exception as e:
                fsub_status = f"`{fsub_id}` — 🔴 BROKEN! Bot admin nahi hai ya access nahi ({e}). Sab users block ho rahe honge!"

        debug_text = (
            f"🐞 **Debug Info**\n\n"
            f"👥 Total users: `{total_users}`\n"
            f"🔑 Total tokens (all time): `{total_tokens}`\n"
            f"✅ Active tokens: `{active_tokens}`\n"
            f"🗄 DB Channel: {db_status}\n"
            f"📢 FSUB: {fsub_status}\n"
            f"🔒 Content Protection: {'ON' if config.get('content_protection') else 'OFF'}\n"
            f"🗑 Auto-Delete: {config.get('auto_delete_seconds', 0)}s\n"
            f"⏱ Default Timer: `{config.get('default_timer', '1h')}`\n"
        )
        await callback_query.answer()
        return await callback_query.message.edit_text(
            debug_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="panel_back")]])
        )

    if action == "search":
        now = datetime.now()
        cursor = tokens_col.find({"revoked": False, "expiry_time": {"$gt": now}}).sort("expiry_time", 1)
        lines = ["🔍 **Active Tokens:**\n"]
        count = 0
        async for t in cursor:
            count += 1
            used = t.get("used_count", 0)
            limit = t.get("usage_limit")
            limit_str = f"{used}/{limit}" if limit else f"{used}/∞"
            remaining = t["expiry_time"] - now
            hours_left = remaining.total_seconds() / 3600
            lines.append(
                f"`{t['token_id']}` — {len(t['files'])} files, used: {limit_str}, expires in {hours_left:.1f}h"
            )
        if count == 0:
            lines.append("_Koi active token nahi hai._")
        await callback_query.answer()
        return await callback_query.message.edit_text(
            "\n".join(lines), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="panel_back")]])
        )

    if action == "broadcast":
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Users ko bhejo", callback_data="bcast_users")],
            [InlineKeyboardButton("📢 Channel(s) pe bhejo", callback_data="bcast_channels")],
            [InlineKeyboardButton("🔙 Back", callback_data="panel_back")],
        ])
        await callback_query.answer()
        return await callback_query.message.edit_text("📣 Broadcast kaha bhejna hai?", reply_markup=buttons)

    if action == "export":
        config = await get_config()
        config.pop("_id", None)
        json_str = json.dumps(config, indent=2, default=str)
        await callback_query.answer()
        return await callback_query.message.edit_text(
            f"📤 **Settings Export:**\n\n```json\n{json_str}\n```",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="panel_back")]])
        )

    if action == "import":
        pending_action[user_id] = "awaiting_import"
        back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="panel_back")]])
        await callback_query.answer()
        return await callback_query.message.edit_text(
            "📥 Wo JSON paste karke bhej jo pehle export kiya tha:", reply_markup=back_btn
        )

    if action == "settimer":
        await callback_query.answer()
        return await callback_query.message.edit_text(
            "⏱ Default token expiry time chuno:", reply_markup=settimer_markup()
        )

    if action == "autodelete":
        await callback_query.answer()
        return await callback_query.message.edit_text(
            "🗑 Files kitni der baad auto-delete ho, chuno:", reply_markup=autodelete_markup()
        )

    if action == "gentoken":
        pending_action[user_id] = {"flow": "gentoken", "step": "files", "data": {}}
        await callback_query.answer()
        return await callback_query.message.edit_text(
            "🔑 **Naya Token — Step 1/6**\n\n"
            "File ID(s)/range bhej (space se separate multiple):\n\n"
            "**Single file:** `101`\n**Range:** `101-112`\n**Multi-range:** `4-8 20-25`",
            reply_markup=gtf_cancel_only_markup()
        )

    if action == "revoke":
        text, markup = await build_revoke_list_view(offset=0)
        await callback_query.answer()
        return await callback_query.message.edit_text(text, reply_markup=markup)

    if action == "banmenu":
        await callback_query.answer()
        return await callback_query.message.edit_text(
            "🚫 **Ban / Unban User**\n\nNeeche se chuno:",
            reply_markup=ban_menu_markup()
        )

    if action == "admins":
        if user_id != ADMIN_ID:
            return await callback_query.answer("❌ Ye sirf super-admin ke liye hai.", show_alert=True)
        text, markup = await build_admin_list_view()
        await callback_query.answer()
        return await callback_query.message.edit_text(text, reply_markup=markup)

    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="panel_back")]])

    prompts = {
        "setfsub": "📢 Pehle FSUB **channel ID** bhej (e.g. `-1001234567890`):",
        "setdb": "🗄 DB channel ki ID bhej (e.g. `-1001234567890`):",
    }
    pending_action[user_id] = f"awaiting_{action}"
    await callback_query.answer()
    await callback_query.message.edit_text(prompts[action], reply_markup=back_btn)


@app.on_callback_query(filters.regex(r"^stmr_") & admin_filter)
async def settimer_callback(client, callback_query):
    value = callback_query.data.split("_", 1)[1]
    user_id = callback_query.from_user.id

    if value == "custom":
        pending_action[user_id] = "awaiting_settimer"
        back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="panel_back")]])
        await callback_query.answer()
        return await callback_query.message.edit_text(
            "⏱ Default token expiry time bhej (e.g. `1h`, `30m`, `1d`):", reply_markup=back_btn
        )

    await set_config({"default_timer": value})
    await callback_query.answer(f"✅ Default timer set: {value}")
    await callback_query.message.edit_text(
        "🛠 **Admin Panel** — neeche se option chuno:", reply_markup=await admin_panel_markup(user_id)
    )


@app.on_callback_query(filters.regex(r"^adel_") & admin_filter)
async def autodelete_callback(client, callback_query):
    value = callback_query.data.split("_", 1)[1]
    user_id = callback_query.from_user.id

    if value == "custom":
        pending_action[user_id] = "awaiting_autodelete"
        back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="panel_back")]])
        await callback_query.answer()
        return await callback_query.message.edit_text(
            "🗑 Files kitni der baad auto-delete ho (e.g. `10m`, `1h`) bhej:", reply_markup=back_btn
        )

    if value == "off":
        await set_config({"auto_delete_seconds": 0})
        await callback_query.answer("✅ Auto-delete band kar diya.")
    else:
        seconds = parse_time(value)
        await set_config({"auto_delete_seconds": seconds})
        await callback_query.answer(f"✅ Auto-delete set: {value}")

    await callback_query.message.edit_text(
        "🛠 **Admin Panel** — neeche se option chuno:", reply_markup=await admin_panel_markup(user_id)
    )


@app.on_callback_query(filters.regex(r"^rvkpage_") & admin_filter)
async def revoke_page_callback(client, callback_query):
    offset = int(callback_query.data.split("_", 1)[1])
    text, markup = await build_revoke_list_view(offset=offset)
    await callback_query.answer()
    await callback_query.message.edit_text(text, reply_markup=markup)


# ==========================================
# 🚫 BAN / UNBAN — callbacks
# ==========================================
@app.on_callback_query(filters.regex(r"^ban_") & admin_filter)
async def ban_callback(client, callback_query):
    action = callback_query.data.split("_", 1)[1]
    user_id = callback_query.from_user.id
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="panel_banmenu")]])

    if action == "add":
        pending_action[user_id] = "awaiting_ban_add"
        await callback_query.answer()
        return await callback_query.message.edit_text(
            "🚫 Ban karne ke liye **user ID** bhej. Reason bhi de sakta hai (optional):\n\n`123456789 spam kar raha`",
            reply_markup=back_btn
        )

    if action == "remove":
        pending_action[user_id] = "awaiting_ban_remove"
        await callback_query.answer()
        return await callback_query.message.edit_text("➖ Unban karne ke liye **user ID** bhej:", reply_markup=back_btn)

    if action == "export":
        all_banned = [b async for b in banned_col.find({})]
        if not all_banned:
            await callback_query.answer("Banned list khaali hai.", show_alert=True)
            return
        export_data = [
            {"user_id": b["_id"], "banned_at": str(b.get("banned_at", "")), "reason": b.get("reason")}
            for b in all_banned
        ]
        json_str = json.dumps(export_data, indent=2, ensure_ascii=False)
        await callback_query.answer()
        # JSON file bhejte hain, bada list ho to text-message me fit nahi hoga.
        file_bytes = io.BytesIO(json_str.encode("utf-8"))
        file_bytes.name = "banned_users.json"
        await client.send_document(callback_query.message.chat.id, file_bytes, caption=f"📤 {len(all_banned)} banned users.")
        return

    if action.startswith("list_"):
        offset = int(action.split("_", 1)[1])
        text, markup = await build_ban_list_view(offset=offset)
        await callback_query.answer()
        return await callback_query.message.edit_text(text, reply_markup=markup)


# ==========================================
# 👑 MANAGE ADMINS — callbacks (super-admin only)
# ==========================================
@app.on_callback_query(filters.regex(r"^adm_") & admin_filter)
async def admin_manage_callback(client, callback_query):
    action = callback_query.data.split("_", 1)[1]
    user_id = callback_query.from_user.id
    if user_id != ADMIN_ID:
        return await callback_query.answer("❌ Ye sirf super-admin ke liye hai.", show_alert=True)

    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="panel_admins")]])

    if action == "add":
        pending_action[user_id] = "awaiting_adm_add"
        await callback_query.answer()
        return await callback_query.message.edit_text("➕ Naye admin ki **user ID** bhej:", reply_markup=back_btn)

    if action == "remove":
        pending_action[user_id] = "awaiting_adm_remove"
        await callback_query.answer()
        return await callback_query.message.edit_text("➖ Hatane wale admin ki **user ID** bhej:", reply_markup=back_btn)


@app.on_callback_query(filters.regex(r"^rvk_") & admin_filter)
async def revoke_select_callback(client, callback_query):
    token_id = callback_query.data.split("_", 1)[1]
    token_data = await tokens_col.find_one({"token_id": token_id})
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm Revoke", callback_data=f"rvkc_{token_id}")],
        [InlineKeyboardButton("🔙 Cancel", callback_data="panel_revoke")],
    ])
    await callback_query.answer()
    if not token_data:
        return await callback_query.message.edit_text(
            f"⚠️ Sure? `{token_id}` revoke karna hai?", reply_markup=buttons
        )
    used = token_data.get("used_count", 0)
    limit = token_data.get("usage_limit")
    usage_text = f"{used}/{limit}" if limit is not None else f"{used} (unlimited limit)"
    text = (
        f"⚠️ **Revoke Token?**\n\n"
        f"**Token:** `{token_id}`\n"
        f"**Files:** {len(token_data.get('files', []))}\n"
        f"**Used:** {usage_text}\n\n"
        f"Sure?"
    )
    await callback_query.message.edit_text(text, reply_markup=buttons)


@app.on_callback_query(filters.regex(r"^rvkc_") & admin_filter)
async def revoke_confirm_callback(client, callback_query):
    token_id = callback_query.data.split("_", 1)[1]
    user_id = callback_query.from_user.id
    result = await tokens_col.update_one({"token_id": token_id}, {"$set": {"revoked": True}})
    if result.matched_count == 0:
        await callback_query.answer("❌ Token mila hi nahi.", show_alert=True)
    else:
        await callback_query.answer("✅ Token revoke ho gaya.")
    await callback_query.message.edit_text(
        "🛠 **Admin Panel** — neeche se option chuno:", reply_markup=await admin_panel_markup(user_id)
    )


@app.on_callback_query(filters.regex(r"^gtf_") & admin_filter)
async def gentoken_wizard_callback(client, callback_query):
    action_key = callback_query.data.split("_", 1)[1]
    user_id = callback_query.from_user.id
    state = pending_action.get(user_id)

    if action_key == "cancel":
        pending_action.pop(user_id, None)
        await callback_query.answer("❌ Cancel kar diya.")
        return await callback_query.message.edit_text(
            "🛠 **Admin Panel** — neeche se option chuno:", reply_markup=await admin_panel_markup(user_id)
        )

    if not isinstance(state, dict) or state.get("flow") != "gentoken":
        return await callback_query.answer("⚠️ Session expire ho gaya, `/admin` se dobara try kar.", show_alert=True)

    data = state["data"]

    if action_key == "skiplimit":
        data["usage_limit"] = None
        pending_action[user_id] = {"flow": "gentoken", "step": "expiry_choice", "data": data}
        await callback_query.answer()
        return await callback_query.message.edit_text(
            "🔑 **Step 4/6** — Is token ki custom expiry chahiye?", reply_markup=gtf_expiry_markup()
        )

    if action_key == "setlimit":
        pending_action[user_id] = {"flow": "gentoken", "step": "limit_value", "data": data}
        await callback_query.answer()
        return await callback_query.message.edit_text(
            "🔢 Kitni baar tak use ho sake, number bhej:", reply_markup=gtf_cancel_only_markup()
        )

    if action_key == "skipexpiry":
        data["expiry_override_seconds"] = None
        data["expiry_display"] = None
        pending_action[user_id] = {"flow": "gentoken", "step": "autodelete_choice", "data": data}
        await callback_query.answer()
        return await callback_query.message.edit_text(
            "🔑 **Step 5/6** — Auto-delete override karna hai?", reply_markup=gtf_autodelete_markup()
        )

    if action_key == "setexpiry":
        pending_action[user_id] = {"flow": "gentoken", "step": "expiry_value", "data": data}
        await callback_query.answer()
        return await callback_query.message.edit_text(
            "⏱ Time bhej (e.g. `1h`, `30m`, `2d`):", reply_markup=gtf_cancel_only_markup()
        )

    if action_key in ("adon", "adoff", "adskip"):
        data["auto_delete_override"] = True if action_key == "adon" else (False if action_key == "adoff" else None)
        pending_action[user_id] = {"flow": "gentoken", "step": "link_choice", "data": data}
        await callback_query.answer()
        return await callback_query.message.edit_text(
            "🔑 **Step 6/6** — Deep-link bhi chahiye?", reply_markup=gtf_link_markup()
        )

    if action_key in ("linkyes", "linkno"):
        data["want_link"] = (action_key == "linkyes")
        pending_action[user_id] = {"flow": "gentoken", "step": "confirm", "data": data}
        await callback_query.answer()
        return await callback_query.message.edit_text(
            gtf_summary_text(data), reply_markup=gtf_confirm_markup()
        )

    if action_key == "generate":
        config = await get_config()
        if data.get("expiry_override_seconds") is not None:
            seconds = data["expiry_override_seconds"]
            expiry_display = data["expiry_display"]
        else:
            expiry_display = config.get("default_timer", "1h")
            seconds = parse_time(expiry_display) or 3600

        token_id = f"Kissu-{data['name']}"
        await tokens_col.update_one(
            {"token_id": token_id},
            {"$set": {
                "token_id": token_id,
                "expiry_time": datetime.now() + timedelta(seconds=seconds),
                "files": data["file_ids"],
                "revoked": False,
                "usage_limit": data.get("usage_limit"),
                "used_count": 0,
                "auto_delete_override": data.get("auto_delete_override"),
            }},
            upsert=True
        )

        limit_text = f"{data['usage_limit']} uses" if data.get("usage_limit") else "Unlimited"
        ad = data.get("auto_delete_override")
        delete_text = "Global setting follow hogi" if ad is None else ("ON" if ad else "OFF")

        reply_text = (
            f"🔥 **Token Generated!**\n\n**Token:** `{token_id}`\n"
            f"**Files:** {len(data['file_ids'])}\n**Expires in:** {expiry_display}\n"
            f"**Usage limit:** {limit_text}\n**Auto-delete:** {delete_text}"
        )
        if data.get("want_link"):
            username = await get_bot_username()
            if username:
                deep_link = f"https://t.me/{username}?start={token_id}"
                reply_text += f"\n**Link:** {deep_link}"
            else:
                reply_text += "\n⚠️ Link nahi ban paya — bot username fetch nahi ho saka."

        pending_action.pop(user_id, None)
        await callback_query.answer("✅ Token generate ho gaya!")
        return await callback_query.message.edit_text(reply_text)


@app.on_callback_query(filters.regex(r"^editmsg_") & admin_filter)
async def editmsg_callback(client, callback_query):
    key = callback_query.data.split("_", 1)[1]
    user_id = callback_query.from_user.id
    pending_action[user_id] = f"awaiting_editmsg_{key}"
    current = await get_message(key)
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="panel_editmsg")]])
    await callback_query.answer()
    await callback_query.message.reply_text(
        f"✏️ **{key}** ka naya text bhej.\n\n"
        f"Current: `{current['text']}`\n"
        f"Extra messages: {len(current.get('extra', []))}\n\n"
        f"Agar extra message bhi add karni hai to naya text ke baad `|||` daal ke likh:\n"
        f"`MainText|||ExtraMsg1|||ExtraMsg2`",
        reply_markup=back_btn
    )


@app.on_callback_query(filters.regex(r"^rl_") & admin_filter)
async def ratelimit_callback(client, callback_query):
    field = callback_query.data.split("_", 1)[1]  # setcount / setwindow / setwait / setmessage
    user_id = callback_query.from_user.id
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="panel_ratelimit")]])

    prompts = {
        "setcount": "🔢 Kitni baar redeem karne ke baad limit lage, number bhej (e.g. `3`):",
        "setwindow": "⏱ Kitne seconds ke andar wo count hona chahiye, number bhej (e.g. `60`):",
        "setwait": "⏳ Cooldown kitni der ka ho, seconds me bhej (e.g. `30`):",
        "setmessage": "✏️ Naya message bhej. `{s}` likhne se wahan remaining seconds fill hoga (e.g. `Wait {s}s....`):",
    }
    pending_action[user_id] = f"awaiting_rl_{field}"
    await callback_query.answer()
    await callback_query.message.edit_text(prompts[field], reply_markup=back_btn)


@app.on_callback_query(filters.regex(r"^bcast_") & admin_filter)
async def bcast_callback(client, callback_query):
    kind = callback_query.data.split("_", 1)[1]  # "users" ya "channels"
    user_id = callback_query.from_user.id
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="panel_broadcast")]])

    if kind == "users":
        pending_action[user_id] = "awaiting_bcast_users"
        await callback_query.answer()
        return await callback_query.message.reply_text(
            "👥 Jo message sabhi users ko bhejna hai wo type kar:", reply_markup=back_btn
        )

    elif kind == "channels":
        config = await get_config()
        channels = config.get("broadcast_channels", [])
        if not channels:
            pending_action[user_id] = "awaiting_addchannel"
            await callback_query.answer()
            return await callback_query.message.reply_text(
                "📢 Abhi koi channel add nahi hai. Pehle channel ki ID bhej (e.g. `-1001234567890`):",
                reply_markup=back_btn
            )
        pending_action[user_id] = "awaiting_bcast_channels"
        await callback_query.answer()
        return await callback_query.message.reply_text(
            f"📢 {len(channels)} channel(s) me broadcast hoga. Message type kar.\n\n"
            f"Naya channel add karne ke liye `/addchannel <id>` bhej pehle.",
            reply_markup=back_btn
        )


@app.on_message(filters.command("addchannel") & admin_filter)
async def add_broadcast_channel(client, message):
    if len(message.command) < 2:
        return await message.reply_text("Format: `/addchannel <channel_id>`")
    channel_id = message.command[1]
    try:
        int(channel_id)
    except ValueError:
        return await message.reply_text("❌ Ye ID nahi lag rahi.")
    config = await get_config()
    channels = config.get("broadcast_channels", [])
    if channel_id in channels:
        return await message.reply_text("⚠️ Ye channel already list me hai.")
    channels.append(channel_id)
    await set_config({"broadcast_channels": channels})
    await message.reply_text(f"✅ Channel add ho gaya. Total: {len(channels)}\n\n⚠️ Bot ko us channel me admin banana mat bhoolna.")


@app.on_message(filters.command("search") & admin_filter)
async def search_tokens_command(client, message):
    now = datetime.now()
    cursor = tokens_col.find({"revoked": False, "expiry_time": {"$gt": now}}).sort("expiry_time", 1)
    lines = ["🔍 **Active Tokens:**\n"]
    count = 0
    async for t in cursor:
        count += 1
        used = t.get("used_count", 0)
        limit = t.get("usage_limit")
        limit_str = f"{used}/{limit}" if limit else f"{used}/∞"
        remaining = t["expiry_time"] - now
        hours_left = remaining.total_seconds() / 3600
        lines.append(
            f"`{t['token_id']}` — {len(t['files'])} files, used: {limit_str}, expires in {hours_left:.1f}h"
        )
    if count == 0:
        lines.append("_Koi active token nahi hai._")
    await message.reply_text("\n".join(lines))


# ==========================================
# 📩 ADMIN'S FOLLOW-UP REPLIES
# ==========================================

async def handle_gentoken_wizard_text(client, message, user_id, action):
    """Generate Token wizard ke 4 typed steps: files, naam, limit-value, expiry-value.
    Baaki sab steps (limit/expiry choice, auto-delete, link, confirm) buttons se
    handle hote hain gentoken_wizard_callback me."""
    step = action["step"]
    data = action["data"]
    text = message.text.strip()

    if step == "files":
        parts = text.split()
        file_ids, error = parse_ranges(parts)
        if error:
            return await message.reply_text(f"{error} — dobara bhej.")
        if not file_ids:
            return await message.reply_text("❌ Koi valid file ID nahi mili — dobara bhej.")
        data["file_ids"] = file_ids
        pending_action[user_id] = {"flow": "gentoken", "step": "name", "data": data}
        return await message.reply_text(
            f"✅ {len(file_ids)} file(s) mil gayi.\n\n🔑 **Step 2/6** — Token ka naam bhej:",
            reply_markup=gtf_cancel_only_markup()
        )

    elif step == "name":
        bits = text.split()
        name = bits[0] if bits else ""
        if not name:
            return await message.reply_text("❌ Naam khali nahi ho sakta — dobara bhej.")
        data["name"] = name
        pending_action[user_id] = {"flow": "gentoken", "step": "limit_choice", "data": data}
        return await message.reply_text(
            "🔑 **Step 3/6** — Usage limit lagani hai?", reply_markup=gtf_limit_markup()
        )

    elif step == "limit_value":
        if not text.isdigit() or int(text) <= 0:
            return await message.reply_text("❌ Ye ek valid positive number nahi hai — dobara bhej.")
        data["usage_limit"] = int(text)
        pending_action[user_id] = {"flow": "gentoken", "step": "expiry_choice", "data": data}
        return await message.reply_text(
            f"✅ Limit set: {text} uses.\n\n🔑 **Step 4/6** — Is token ki custom expiry chahiye?",
            reply_markup=gtf_expiry_markup()
        )

    elif step == "expiry_value":
        seconds = parse_time(text)
        if seconds is None:
            return await message.reply_text("❌ Format galat hai. Use: 10m, 1h, 2d — dobara bhej.")
        data["expiry_override_seconds"] = seconds
        data["expiry_display"] = text
        pending_action[user_id] = {"flow": "gentoken", "step": "autodelete_choice", "data": data}
        return await message.reply_text(
            f"✅ Expiry set: {text}.\n\n🔑 **Step 5/6** — Auto-delete override karna hai?",
            reply_markup=gtf_autodelete_markup()
        )


@app.on_message(filters.private & filters.text & admin_filter & ~filters.command([
    "start", "admin", "addchannel", "search"
]))
async def handle_admin_pending(client, message):
    user_id = message.from_user.id
    action = pending_action.get(user_id)
    if not action:
        return

    if isinstance(action, dict) and action.get("flow") == "gentoken":
        return await handle_gentoken_wizard_text(client, message, user_id, action)

    text = message.text.strip()

    if action == "awaiting_setfsub":
        try:
            int(text)
        except ValueError:
            return await message.reply_text("❌ Ye ID nahi lag rahi. Number bhej (e.g. `-1001234567890`) — dobara try kar.")
        await set_config({"fsub_id": text})
        pending_action[user_id] = "awaiting_setfsublink"
        return await message.reply_text("✅ ID save ho gayi. Ab channel ka **invite/join link** bhej (e.g. `https://t.me/teraChannel`):")

    elif action == "awaiting_setfsublink":
        if not (text.startswith("https://t.me/") or text.startswith("t.me/")):
            return await message.reply_text("❌ Ye valid link nahi lag raha. `https://t.me/...` format me bhej.")
        await set_config({"fsub_link": text})
        await message.reply_text(f"✅ FSUB poora set ho gaya!\n**ID:** saved\n**Link:** {text}\n\n⚠️ Bot ko us channel me admin banana mat bhoolna, warna join-check kaam nahi karega.")

    elif action == "awaiting_setdb":
        await set_config({"db_channel_id": text})
        await message.reply_text(f"✅ DB channel set ho gaya: `{text}`")

    elif action == "awaiting_settimer":
        if parse_time(text) is None:
            return await message.reply_text("❌ Format galat hai. Use: 10s, 5m, 1h, 1d — dobara bhej.")
        await set_config({"default_timer": text})
        await message.reply_text(f"✅ Default timer set ho gaya: `{text}`")

    elif action == "awaiting_autodelete":
        if text.lower() == "off":
            await set_config({"auto_delete_seconds": 0})
            await message.reply_text("✅ Auto-delete band kar diya.")
        else:
            seconds = parse_time(text)
            if seconds is None:
                return await message.reply_text("❌ Format galat hai. Use: 10m, 1h, ya `off` — dobara bhej.")
            await set_config({"auto_delete_seconds": seconds})
            await message.reply_text(f"✅ Files ab {text} baad auto-delete hongi.")

    elif action == "awaiting_rl_setcount":
        if not text.isdigit() or int(text) < 1:
            return await message.reply_text("❌ Ye ek valid positive number nahi hai — dobara bhej.")
        await set_config({"ratelimit_count": int(text)})
        await message.reply_text(f"✅ Rate-limit count set ho gaya: {text} baar")

    elif action == "awaiting_rl_setwindow":
        if not text.isdigit() or int(text) < 1:
            return await message.reply_text("❌ Ye ek valid positive number nahi hai (seconds me) — dobara bhej.")
        await set_config({"ratelimit_window": int(text)})
        await message.reply_text(f"✅ Rate-limit window set ho gaya: {text}s")

    elif action == "awaiting_rl_setwait":
        if not text.isdigit() or int(text) < 1:
            return await message.reply_text("❌ Ye ek valid positive number nahi hai (seconds me) — dobara bhej.")
        await set_config({"ratelimit_wait": int(text)})
        await message.reply_text(f"✅ Rate-limit cooldown set ho gaya: {text}s")

    elif action == "awaiting_rl_setmessage":
        await set_config({"ratelimit_message": text})
        await message.reply_text(f"✅ Rate-limit message set ho gaya:\n`{text}`")

    elif action.startswith("awaiting_editmsg_"):
        key = action.replace("awaiting_editmsg_", "")
        bits = text.split("|||")
        main_text = bits[0].strip()
        extras = [b.strip() for b in bits[1:] if b.strip()]
        config = await get_config()
        messages = config.get("messages", {})
        messages[key] = {"text": main_text, "extra": extras}
        await set_config({"messages": messages})
        await message.reply_text(f"✅ `{key}` update ho gaya.\nMain: {main_text}\nExtras: {len(extras)}")

    elif action == "awaiting_import":
        try:
            cleaned = text.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.strip("`")
                if cleaned.lower().startswith("json"):
                    cleaned = cleaned[4:]
            imported = json.loads(cleaned)
            if not isinstance(imported, dict):
                raise ValueError("JSON ek object hona chahiye")
            await replace_config(imported)
            await message.reply_text("✅ Settings import ho gayi! `/admin` se check kar le.")
        except Exception as e:
            return await message.reply_text(f"❌ JSON parse nahi hua: {e}\nDobara sahi JSON bhej.")

    elif action == "awaiting_bcast_users":
        all_users = users_col.find({})
        sent_count = 0
        failed_count = 0
        async for user in all_users:
            try:
                await client.send_message(user["_id"], text)
                sent_count += 1
                await asyncio.sleep(0.3)
            except FloodWait as fw:
                await asyncio.sleep(fw.value + 1)
                try:
                    await client.send_message(user["_id"], text)
                    sent_count += 1
                except Exception:
                    failed_count += 1
            except Exception:
                failed_count += 1  # user ne bot block kiya ho sakta hai, ya deactivated ho
        await message.reply_text(f"📣 Broadcast complete!\n✅ Sent: {sent_count}\n❌ Failed: {failed_count}")

    elif action == "awaiting_bcast_channels":
        config = await get_config()
        channels = config.get("broadcast_channels", [])
        sent_count = 0
        failed_count = 0
        for ch_id in channels:
            try:
                await client.send_message(int(ch_id), text)
                sent_count += 1
                await asyncio.sleep(0.5)
            except FloodWait as fw:
                await asyncio.sleep(fw.value + 1)
                try:
                    await client.send_message(int(ch_id), text)
                    sent_count += 1
                except Exception:
                    failed_count += 1
            except Exception as e:
                failed_count += 1
                await message.reply_text(f"⚠️ Channel `{ch_id}` me error: {e}")
        await message.reply_text(f"📣 Broadcast complete!\n✅ Sent: {sent_count}\n❌ Failed: {failed_count}")

    elif action == "awaiting_addchannel":
        try:
            int(text)
        except ValueError:
            return await message.reply_text("❌ Ye ID nahi lag rahi — dobara bhej.")
        config = await get_config()
        channels = config.get("broadcast_channels", [])
        if text not in channels:
            channels.append(text)
            await set_config({"broadcast_channels": channels})
        await message.reply_text(f"✅ Channel add ho gaya. Ab broadcast me message bhej.\n\n⚠️ Bot ko us channel me admin banana mat bhoolna.")
        pending_action[user_id] = "awaiting_bcast_channels"
        return

    elif action == "awaiting_ban_add":
        parts = text.split(maxsplit=1)
        try:
            target_id = int(parts[0])
        except (ValueError, IndexError):
            return await message.reply_text("❌ Valid user ID bhej (number). Optional reason space ke baad: `123456 spam kar raha`")
        reason = parts[1] if len(parts) > 1 else None
        await banned_col.update_one(
            {"_id": target_id},
            {"$set": {"banned_at": datetime.now(), "reason": reason}},
            upsert=True
        )
        # FSUB channel se turant kick — agar member hai to, warna silently ignore.
        config = await get_config()
        fsub_id = config.get("fsub_id")
        kicked_note = ""
        if fsub_id:
            try:
                await client.ban_chat_member(int(fsub_id), target_id)
                kicked_note = " Aur FSUB channel se bhi kick kar diya."
            except Exception:
                kicked_note = " (FSUB se kick nahi ho paya — shayad member nahi tha ya bot ke paas permission nahi hai.)"
        await message.reply_text(f"🚫 User `{target_id}` ban ho gaya.{kicked_note}")

    elif action == "awaiting_ban_remove":
        try:
            target_id = int(text)
        except ValueError:
            return await message.reply_text("❌ Valid user ID bhej (number) — dobara try kar.")
        result = await banned_col.delete_one({"_id": target_id})
        if result.deleted_count == 0:
            await message.reply_text(f"❌ `{target_id}` banned list me mila hi nahi.")
        else:
            await message.reply_text(f"✅ User `{target_id}` unban ho gaya.")

    elif action == "awaiting_adm_add":
        if user_id != ADMIN_ID:
            return  # Extra safety — button khud super-admin ko hi dikhta hai, par text-input bypass na ho sake isliye yahan bhi check.
        try:
            target_id = int(text)
        except ValueError:
            return await message.reply_text("❌ Valid user ID bhej (number) — dobara try kar.")
        await admins_col.update_one({"_id": target_id}, {"$set": {"_id": target_id}}, upsert=True)
        invalidate_admin_cache()
        await message.reply_text(f"👑 User `{target_id}` ab admin hai.")

    elif action == "awaiting_adm_remove":
        if user_id != ADMIN_ID:
            return
        try:
            target_id = int(text)
        except ValueError:
            return await message.reply_text("❌ Valid user ID bhej (number) — dobara try kar.")
        if target_id == ADMIN_ID:
            return await message.reply_text("❌ Super-admin ko remove nahi kar sakta.")
        result = await admins_col.delete_one({"_id": target_id})
        invalidate_admin_cache()
        if result.deleted_count == 0:
            await message.reply_text(f"❌ `{target_id}` admin list me mila hi nahi.")
        else:
            await message.reply_text(f"✅ `{target_id}` ab admin nahi raha.")

    if action != "awaiting_setfsub":
        pending_action.pop(user_id, None)


# ==========================================
# 📤 FILE SENDING (+ AUTO-DELETE)
# ==========================================

async def schedule_delete(client, chat_id, message_id, delay_seconds):
    await asyncio.sleep(delay_seconds)
    try:
        await client.delete_messages(chat_id, message_id)
    except Exception:
        pass


async def send_batch(client, chat_id, token_data, offset):
    db_channel_id = await get_db_channel_id()
    if not db_channel_id:
        return await client.send_message(chat_id, "❌ DB Channel set nahi hai. Admin ko batao.")

    config = await get_config()
    protect = config.get("content_protection", False)
    global_auto_delete_seconds = config.get("auto_delete_seconds", 0)

    # Per-token override: True = force ON, False = force OFF, None = global setting follow karo
    override = token_data.get("auto_delete_override")
    if override is False:
        auto_delete_seconds = 0
    elif override is True:
        # Global seconds agar 0/band hai to bhi |T| ka matlab kuch hona chahiye —
        # isliye global value use karo agar set hai, warna 1 hour default fallback.
        auto_delete_seconds = global_auto_delete_seconds if global_auto_delete_seconds > 0 else 3600
    else:
        auto_delete_seconds = global_auto_delete_seconds

    all_files = token_data["files"]
    batch = all_files[offset: offset + PAGE_SIZE]
    if not batch:
        return await client.send_message(chat_id, "❌ Aur files nahi hain.")

    for msg_id in batch:
        try:
            sent = await client.copy_message(
                chat_id=chat_id, from_chat_id=db_channel_id, message_id=msg_id,
                protect_content=protect
            )
            if auto_delete_seconds > 0:
                asyncio.create_task(schedule_delete(client, chat_id, sent.id, auto_delete_seconds))
            await asyncio.sleep(0.7)
        except FloodWait as fw:
            # Telegram ne rate-limit lagaya - jitna bola utna wait karke retry karo
            await asyncio.sleep(fw.value + 1)
            try:
                sent = await client.copy_message(
                    chat_id=chat_id, from_chat_id=db_channel_id, message_id=msg_id,
                    protect_content=protect
                )
                if auto_delete_seconds > 0:
                    asyncio.create_task(schedule_delete(client, chat_id, sent.id, auto_delete_seconds))
            except Exception as e:
                await send_custom(client, chat_id, "restricted")
        except Exception as e:
            await send_custom(client, chat_id, "restricted")

    next_offset = offset + PAGE_SIZE
    total_files = len(all_files)
    if next_offset < total_files:
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton(
            "Next ⏭", callback_data=f"next_{token_data['token_id']}_{next_offset}"
        )]])
        await client.send_message(chat_id, f"({min(next_offset, total_files)}/{total_files} files sent)", reply_markup=buttons)


@app.on_callback_query(filters.regex(r"^next_"))
async def next_batch_callback(client, callback_query):
    parts = callback_query.data.split("_")
    offset = int(parts[-1])
    token = "_".join(parts[1:-1])

    token_data = await tokens_col.find_one({"token_id": token})
    if not token_data or token_data.get("revoked"):
        return await callback_query.answer("❌ Token revoke ho chuka hai.", show_alert=True)
    if datetime.now() > token_data["expiry_time"]:
        return await callback_query.answer("⏳ Token expired ho chuka hai.", show_alert=True)

    await callback_query.answer("Sending next batch...")
    await send_batch(client, callback_query.message.chat.id, token_data, offset=offset)


# ==========================================
# 🚀 USER FLOW (non-admin): /start -> welcome + Join/Verify -> verified -> token -> sending
# ==========================================

async def check_rate_limit(user_id):
    """Sliding-window detection + fixed hard-cooldown enforcement.

    Purana bug: (1) wait_seconds hamesha fixed DEFAULT_RATELIMIT_WAIT tha, actual
    remaining cooldown nahi — ab blocked_until - now se exact countdown milta hai.
    (2) record_redemption sirf successful attempt pe chalta tha, blocked attempt pe
    nahi — isse spam karne pe history clear ho jaati thi aur cooldown lagne ke
    turant baad hi dobara burst allowed ho jaata tha. Ab har attempt (block ho ya
    na ho) record hoti hai, taaki cooldown ke dauraan spam se limit reset na ho.

    Returns (is_blocked: bool, wait_seconds: int) — jab blocked hai to wait_seconds
    hamesha exact remaining time hai, kabhi bhi fixed config value nahi.
    """
    now = datetime.now().timestamp()

    # Pehle se hard-blocked hai? Naya window-check ya record karne ki zaroorat nahi —
    # cooldown ke dauran attempts count karna faltu hai, blocked_until hi authority hai.
    blocked_until = rate_limit_blocks.get(user_id)
    if blocked_until and now < blocked_until:
        return True, int(blocked_until - now) + 1

    config = await get_config()
    count_limit = config.get("ratelimit_count", DEFAULT_RATELIMIT_COUNT)
    window = config.get("ratelimit_window", DEFAULT_RATELIMIT_WINDOW)
    wait_seconds = config.get("ratelimit_wait", DEFAULT_RATELIMIT_WAIT)

    history = redemption_times.get(user_id, [])
    history = [t for t in history if now - t < window]  # window ke bahar wale purane timestamps hata do
    history.append(now)  # is attempt ko bhi record karo — chahe ye block ho ya na ho
    redemption_times[user_id] = history

    if len(history) >= count_limit:
        # Limit cross hui — ab fixed hard-cooldown lagao. Jab tak ye khatam na ho,
        # sliding window dobara check nahi hogi (upar wala early-return isko handle karta hai).
        rate_limit_blocks[user_id] = now + wait_seconds
        return True, wait_seconds

    return False, wait_seconds


async def redeem_token(client, message, token):
    """Token check + file delivery — plain-text token aur deep-link (/start token) dono se call hota hai."""
    user_id = message.from_user.id

    if await is_banned(user_id):
        return  # Banned user ko silently ignore karo — koi feedback nahi ki bot exist karta hai.

    is_limited, wait_seconds = await check_rate_limit(user_id)
    if is_limited:
        config = await get_config()
        emoji_msg = await get_message("ratelimit")  # editable "🤖" — admin panel se change ho sakta hai
        wait_template = config.get("ratelimit_message", DEFAULT_RATELIMIT_MESSAGE)
        wait_text = wait_template.replace("{s}", str(wait_seconds))
        await message.reply_text(emoji_msg["text"])
        return await message.reply_text(wait_text)

    if not await is_fsub_joined(client, user_id):
        config = await get_config()
        fsub_link = config.get("fsub_link")
        buttons = None
        if fsub_link:
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 Join Channel", url=fsub_link)],
                [InlineKeyboardButton("✅ Verify", callback_data="verify_fsub")],
            ])
        return await send_custom(client, message.chat.id, "not_joined", reply_markup=buttons)

    token_data = await tokens_col.find_one({"token_id": token})

    if not token_data or token_data.get("revoked") or datetime.now() > token_data["expiry_time"]:
        return await send_custom(client, message.chat.id, "invalid_token")

    usage_limit = token_data.get("usage_limit")
    used_count = token_data.get("used_count", 0)
    if usage_limit is not None and used_count >= usage_limit:
        return await message.reply_text("❌")

    await tokens_col.update_one({"token_id": token}, {"$inc": {"used_count": 1}})

    await send_custom(client, message.chat.id, "sending")
    await send_batch(client, message.chat.id, token_data, offset=0)


@app.on_message(filters.command("start") & filters.private & ~admin_filter)
async def user_start(client, message):
    if await is_banned(message.from_user.id):
        return await send_custom(client, message.chat.id, "banned")

    await users_col.update_one(
        {"_id": message.from_user.id},
        {"$set": {"_id": message.from_user.id, "first_seen": datetime.now()}},
        upsert=True
    )

    # Deep-link se aaya hai to /start ke saath token bhi hoga: /start Kissu-Cutie
    if len(message.command) > 1:
        token = message.command[1].strip()
        if token.startswith("Kissu-"):
            return await redeem_token(client, message, token)

    config = await get_config()
    fsub_link = config.get("fsub_link")

    if fsub_link and not await is_fsub_joined(client, message.from_user.id):
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Join Channel", url=fsub_link)],
            [InlineKeyboardButton("✅ Verify", callback_data="verify_fsub")],
        ])
        return await send_custom(client, message.chat.id, "welcome", reply_markup=buttons)

    await send_custom(client, message.chat.id, "verified")


@app.on_callback_query(filters.regex(r"^verify_fsub$"))
async def verify_fsub_callback(client, callback_query):
    user_id = callback_query.from_user.id
    if await is_banned(user_id):
        return await callback_query.answer("🚫 Tu banned hai.", show_alert=True)
    if await is_fsub_joined(client, user_id):
        await callback_query.answer("✅ Verified!")
        msg_data = await get_message("verified")
        await callback_query.message.edit_text(msg_data["text"])
        for extra_text in msg_data.get("extra", []):
            await client.send_message(callback_query.message.chat.id, extra_text)
    else:
        await callback_query.answer("❌ Abhi bhi join nahi kiya hai. Pehle join kar.", show_alert=True)


@app.on_message(filters.private & filters.text & filters.regex(r"^Kissu-") & ~admin_filter)
async def handle_token_input(client, message):
    token = message.text.strip()
    await redeem_token(client, message, token)


async def get_bot_username():
    """BOT_USERNAME ko lazily fetch karta hai (pehli baar chahiye hone par),
    phir cache kar leta hai — taaki har baar get_me() call na karna pade."""
    global BOT_USERNAME
    if BOT_USERNAME is None:
        me = await app.get_me()
        BOT_USERNAME = me.username
    return BOT_USERNAME


if __name__ == "__main__":
    print("KissuCloudBot is alive!")
    # app.run() (bina argument ke) Pyrogram ka apna signal-safe start+idle+stop hai.
    # Railway jaisi platforms restart/health-check ke liye SIGTERM bhejti rehti hain,
    # aur manual app.start()+idle()+app.stop() pattern us signal par
    # "attached to a different loop" RuntimeError deta hai. Plain app.run() isse
    # sahi tarike se, Pyrogram ke apne signal handlers ke saath, handle karta hai.
    app.run()
