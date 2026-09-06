import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pyrogram import Client, filters, StopPropagation
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
ADMIN_ID = int(ADMIN_ID)

app = Client("KissuCloudBot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

db_client = AsyncIOMotorClient(MONGODB_URL)
db = db_client["KissuDB"]
settings_col = db["settings"]
tokens_col = db["tokens"]
users_col = db["users"]  # broadcast ke liye track karte hain kaun kaun /start kar chuka hai

PAGE_SIZE = 10
pending_action = {}  # { user_id: "awaiting_xxx" }
pending_language = {}  # { user_id: {"token": "Kissu-xyz" | None} } - language pick hone tak deep-link token yahan hold hota hai
cooldown_tracker = {}  # { user_id: {"attempts": int, "cooldown_until": datetime | None} } - token-submission cooldown, in-memory (bot restart pe reset ho jaata hai, acceptable)
BOT_USERNAME = None  # startup pe app.get_me() se fill hoga, deep-link banane ke liye

# ==========================================
# 💬 DEFAULT CUSTOMIZABLE MESSAGES
# Ye saare keys admin panel se edit ho sakte hain, aur ab HAR key language ke
# hisaab se nested hai: DEFAULT_MESSAGES[key][lang] = {"text": ..., "extra": [...]}.
# "extra" ek list hai - trigger pe in sabhi messages ko bhi bhejega (order me).
# ==========================================
DEFAULT_MESSAGES = {
    "welcome": {
        "hi": {
            "text": (
                "Namaste! 🎉 KissuCloudBot me aapka swagat hai.\n"
                "Yahan aap apne token ke zariye files prapt kar sakte hain.\n"
                "Bas apna token bhejein ya /start ke saath token daalein.\n"
                "Agar koi dikkat ho, to admin se contact karein."
            ),
            "extra": [],
        },
        "en": {
            "text": (
                "Welcome to KissuCloudBot.\n"
                "This platform enables you to retrieve files using your unique token.\n"
                "To begin, please submit your token via the /start command followed by the token string.\n"
                "Should you encounter any issues, kindly contact the administrator."
            ),
            "extra": [],
        },
    },
    "verified": {
        "hi": {"text": "✅ Token sahi hai! Ab aapki files bheji ja rahi hain...", "extra": []},
        "en": {"text": "✅ Token verified. Your files are now being delivered.", "extra": []},
    },
    "sending": {
        "hi": {"text": "📤 Aapki files bheji ja rahi hain... thoda intezar karein.", "extra": []},
        "en": {"text": "📤 Your files are being transmitted. Please wait a moment.", "extra": []},
    },
    "invalid_token": {
        "hi": {"text": "❌ Ye token galat hai ya expired ho chuka hai. Sahi token daalein.", "extra": []},
        "en": {"text": "❌ The token you provided is either invalid or has expired. Please verify and try again.", "extra": []},
    },
    "not_joined": {
        "hi": {"text": "⚠️ Is channel ko join karna zaroori hai! Pehle channel join karein, phir wapas try karein.", "extra": []},
        "en": {"text": "⚠️ Access requires membership in the designated channel. Please join the channel and then retry.", "extra": []},
    },
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
    return await settings_col.find_one({"_id": "config"}) or {}


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


async def get_message(key, lang="hi"):
    """Custom message uthata hai DB se (language ke hisaab se), warna default use karta hai.
    Agar requested lang custom me nahi mila to us key ke DEFAULT_MESSAGES se nikaalte hain,
    aur wo bhi na mile to 'hi' pe fallback karte hain."""
    config = await get_config()
    custom_key = config.get("messages", {}).get(key, {})
    if isinstance(custom_key, dict) and lang in custom_key:
        return custom_key[lang]
    defaults_for_key = DEFAULT_MESSAGES.get(key, {})
    return defaults_for_key.get(lang) or defaults_for_key.get("hi") or {"text": "", "extra": []}


async def send_custom(client, chat_id, key, lang="hi", reply_markup=None):
    """Custom message + extras bhejta hai (language-aware). Main text ka Message object
    return karta hai (edit/delete-tracking ke liye)."""
    msg_data = await get_message(key, lang)
    main = await client.send_message(chat_id, msg_data["text"], reply_markup=reply_markup)
    for extra_text in msg_data.get("extra", []):
        await client.send_message(chat_id, extra_text)
        await asyncio.sleep(0.3)
    return main


# ==========================================
# 🌐 LANGUAGE SELECTION — helpers
# ==========================================

async def get_user_language(user_id):
    """DB se language uthata hai. Default 'hi' agar set nahi hai."""
    user = await users_col.find_one({"_id": user_id})
    return (user or {}).get("language", "hi")


async def set_user_language(user_id, lang):
    await users_col.update_one({"_id": user_id}, {"$set": {"language": lang}}, upsert=True)


def language_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇮🇳 Hinglish", callback_data="lang_hi"),
         InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
    ])


async def show_language_picker(client, chat_id):
    """Language picker — ye text fixed hai, admin isse edit nahi kar sakta (DEFAULT_MESSAGES ka hissa nahi)."""
    await client.send_message(
        chat_id,
        "🌐 Choose your language / apni bhasha chunein:\n"
        "🇮🇳 Hinglish – casual, friendly\n"
        "🇬🇧 English – formal, professional",
        reply_markup=language_markup()
    )


async def send_welcome_or_verified(client, chat_id, user_id, lang):
    """FSUB-gated welcome/verified logic — /start (bina token ke) aur language-selection
    ke baad (agar deep-link token nahi tha) dono jagah se same cheez chahiye, isliye
    ek helper me nikal diya taaki dono jagah sync rahe."""
    config = await get_config()
    fsub_link = config.get("fsub_link")
    if fsub_link and not await is_fsub_joined(client, user_id):
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Join Channel", url=fsub_link)],
            [InlineKeyboardButton("✅ Verify", callback_data="verify_fsub")],
        ])
        return await send_custom(client, chat_id, "welcome", lang=lang, reply_markup=buttons)
    await send_custom(client, chat_id, "verified", lang=lang)


# ==========================================
# ⏱ TOKEN REDEMPTION COOLDOWN — helpers
# ==========================================

def is_cooldown_active(user_id):
    """Return (bool, remaining_seconds). Purely in-memory check — bot restart pe
    cooldown state reset ho jaata hai (acceptable, DB me persist karne ki zaroorat nahi)."""
    state = cooldown_tracker.get(user_id)
    if not state or not state.get("cooldown_until"):
        return False, 0
    remaining = (state["cooldown_until"] - datetime.now()).total_seconds()
    if remaining <= 0:
        # Cooldown khatam ho chuka - reset karo taaki agli baar fresh cycle chale
        cooldown_tracker[user_id] = {"attempts": 0, "cooldown_until": None}
        return False, 0
    return True, int(remaining) + 1  # round up taaki "0 sec baad" jaisa awkward message na dikhe


async def record_token_attempt(user_id):
    """Har token submission (valid ya invalid) is_cooldown_active check ke baad ye call karta hai.
    3 consecutive attempts pe cooldown lagta hai, phir attempts reset ho jaate hain."""
    state = cooldown_tracker.setdefault(user_id, {"attempts": 0, "cooldown_until": None})
    state["attempts"] += 1
    if state["attempts"] >= 3:
        duration = await get_cooldown_duration()
        state["cooldown_until"] = datetime.now() + timedelta(seconds=duration)
        state["attempts"] = 0


async def get_cooldown_duration():
    """Settings se cooldown duration (seconds) uthata hai, default 30."""
    config = await get_config()
    return config.get("cooldown_duration", 30)


async def set_cooldown_duration(seconds):
    await settings_col.update_one({"_id": "config"}, {"$set": {"cooldown_duration": seconds}}, upsert=True)


def cooldown_message(lang, remaining_seconds):
    """Cooldown active hone par dikhaya jaane wala message — fixed hai, admin-editable nahi."""
    if lang == "en":
        return f"⏳ Please wait before submitting another token. You may try again in {remaining_seconds} seconds."
    return f"⏳ thoda ruko bhai! {remaining_seconds} sec baad try karo."


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

async def admin_panel_markup():
    config = await get_config()
    protect_status = "🟢 ON" if config.get("content_protection") else "🔴 OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Set FSUB", callback_data="panel_setfsub"),
         InlineKeyboardButton("🗄 Set DB Channel", callback_data="panel_setdb")],
        [InlineKeyboardButton("⏱ Set Timer", callback_data="panel_settimer"),
         InlineKeyboardButton("🗑 Auto-Delete", callback_data="panel_autodelete")],
        [InlineKeyboardButton("⏱️ Cooldown Duration", callback_data="panel_cooldown")],
        [InlineKeyboardButton("🔑 Generate Token", callback_data="panel_gentoken"),
         InlineKeyboardButton("❌ Revoke Token", callback_data="panel_revoke")],
        [InlineKeyboardButton("🔍 Search/List Tokens", callback_data="panel_search")],
        [InlineKeyboardButton(f"🔒 Content Protection: {protect_status}", callback_data="panel_toggleprotect")],
        [InlineKeyboardButton("✏️ Edit Messages", callback_data="panel_editmsg")],
        [InlineKeyboardButton("📣 Broadcast", callback_data="panel_broadcast"),
         InlineKeyboardButton("🐞 Debug", callback_data="panel_debug")],
        [InlineKeyboardButton("📤 Export Settings", callback_data="panel_export"),
         InlineKeyboardButton("📥 Import Settings", callback_data="panel_import")],
        [InlineKeyboardButton("🚪 Quit", callback_data="panel_quit")],
    ])


def edit_msg_markup():
    keys = list(DEFAULT_MESSAGES.keys())
    rows = []
    for k in keys:
        rows.append([InlineKeyboardButton(k, callback_data=f"editmsg_{k}")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="panel_back")])
    return InlineKeyboardMarkup(rows)


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


def cooldown_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("30s", callback_data="cldn_30"),
         InlineKeyboardButton("1m", callback_data="cldn_60"),
         InlineKeyboardButton("5m", callback_data="cldn_300")],
        [InlineKeyboardButton("✏️ Custom", callback_data="cldn_custom")],
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
        label = f"{t['token_id']} ({len(t['files'])} files)"
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


@app.on_message(filters.command("start") & filters.private & filters.user(ADMIN_ID))
async def admin_start(client, message):
    """Admin ke liye /start alag hai - seedha 🔐 + panel button."""
    await users_col.update_one(
        {"_id": message.from_user.id},
        {"$set": {"_id": message.from_user.id, "first_seen": datetime.now()}},
        upsert=True
    )
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("🛠 Open Admin Panel", callback_data="panel_open")]])
    await message.reply_text("🔐", reply_markup=buttons)


@app.on_message(filters.command("admin") & filters.user(ADMIN_ID))
async def admin_panel(client, message):
    await message.reply_text("🛠 **Admin Panel** — neeche se option chuno:", reply_markup=await admin_panel_markup())


@app.on_callback_query(filters.regex(r"^panel_") & filters.user(ADMIN_ID))
async def panel_callback(client, callback_query):
    action = callback_query.data.split("_", 1)[1]
    user_id = callback_query.from_user.id

    if action == "open":
        return await callback_query.message.edit_text(
            "🛠 **Admin Panel** — neeche se option chuno:", reply_markup=await admin_panel_markup()
        )

    if action == "quit":
        pending_action.pop(user_id, None)
        return await callback_query.message.delete()

    if action == "back":
        pending_action.pop(user_id, None)
        return await callback_query.message.edit_text(
            "🛠 **Admin Panel** — neeche se option chuno:", reply_markup=await admin_panel_markup()
        )

    if action == "toggleprotect":
        config = await get_config()
        new_value = not config.get("content_protection", False)
        await settings_col.update_one({"_id": "config"}, {"$set": {"content_protection": new_value}}, upsert=True)
        await callback_query.answer(f"Content Protection {'ON' if new_value else 'OFF'} kar diya.")
        return await callback_query.message.edit_text(
            "🛠 **Admin Panel** — neeche se option chuno:", reply_markup=await admin_panel_markup()
        )

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
            f"⏱️ Cooldown Duration: {config.get('cooldown_duration', 30)}s\n"
        )
        await callback_query.answer()
        return await callback_query.message.reply_text(debug_text)

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
        return await callback_query.message.reply_text("\n".join(lines))

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
        return await callback_query.message.reply_text(f"📤 **Settings Export:**\n\n```json\n{json_str}\n```")

    if action == "import":
        pending_action[user_id] = "awaiting_import"
        back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="panel_back")]])
        await callback_query.answer()
        return await callback_query.message.reply_text(
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

    if action == "cooldown":
        await callback_query.answer()
        return await callback_query.message.edit_text(
            "⏱️ Token submission ke beech cooldown duration chuno:", reply_markup=cooldown_markup()
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

    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="panel_back")]])

    prompts = {
        "setfsub": "📢 Pehle FSUB **channel ID** bhej (e.g. `-1001234567890`):",
        "setdb": "🗄 DB channel ki ID bhej (e.g. `-1001234567890`):",
    }
    pending_action[user_id] = f"awaiting_{action}"
    await callback_query.answer()
    await callback_query.message.reply_text(prompts[action], reply_markup=back_btn)


@app.on_callback_query(filters.regex(r"^stmr_") & filters.user(ADMIN_ID))
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

    await settings_col.update_one({"_id": "config"}, {"$set": {"default_timer": value}}, upsert=True)
    await callback_query.answer(f"✅ Default timer set: {value}")
    await callback_query.message.edit_text(
        "🛠 **Admin Panel** — neeche se option chuno:", reply_markup=await admin_panel_markup()
    )


@app.on_callback_query(filters.regex(r"^adel_") & filters.user(ADMIN_ID))
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
        await settings_col.update_one({"_id": "config"}, {"$set": {"auto_delete_seconds": 0}}, upsert=True)
        await callback_query.answer("✅ Auto-delete band kar diya.")
    else:
        seconds = parse_time(value)
        await settings_col.update_one({"_id": "config"}, {"$set": {"auto_delete_seconds": seconds}}, upsert=True)
        await callback_query.answer(f"✅ Auto-delete set: {value}")

    await callback_query.message.edit_text(
        "🛠 **Admin Panel** — neeche se option chuno:", reply_markup=await admin_panel_markup()
    )


@app.on_callback_query(filters.regex(r"^cldn_") & filters.user(ADMIN_ID))
async def cooldown_callback(client, callback_query):
    value = callback_query.data.split("_", 1)[1]
    user_id = callback_query.from_user.id

    if value == "custom":
        pending_action[user_id] = "awaiting_cooldown"
        back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="panel_back")]])
        await callback_query.answer()
        return await callback_query.message.edit_text(
            "⏱️ Cooldown duration seconds me bhej (min 5):", reply_markup=back_btn
        )

    seconds = int(value)
    await set_cooldown_duration(seconds)
    await callback_query.answer(f"✅ Cooldown set: {seconds}s")
    await callback_query.message.edit_text(
        "🛠 **Admin Panel** — neeche se option chuno:", reply_markup=await admin_panel_markup()
    )


@app.on_callback_query(filters.regex(r"^rvkpage_") & filters.user(ADMIN_ID))
async def revoke_page_callback(client, callback_query):
    offset = int(callback_query.data.split("_", 1)[1])
    text, markup = await build_revoke_list_view(offset=offset)
    await callback_query.answer()
    await callback_query.message.edit_text(text, reply_markup=markup)


@app.on_callback_query(filters.regex(r"^rvk_") & filters.user(ADMIN_ID))
async def revoke_select_callback(client, callback_query):
    token_id = callback_query.data.split("_", 1)[1]
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm Revoke", callback_data=f"rvkc_{token_id}")],
        [InlineKeyboardButton("🔙 Cancel", callback_data="panel_revoke")],
    ])
    await callback_query.answer()
    await callback_query.message.edit_text(f"⚠️ Sure? `{token_id}` revoke karna hai?", reply_markup=buttons)


@app.on_callback_query(filters.regex(r"^rvkc_") & filters.user(ADMIN_ID))
async def revoke_confirm_callback(client, callback_query):
    token_id = callback_query.data.split("_", 1)[1]
    result = await tokens_col.update_one({"token_id": token_id}, {"$set": {"revoked": True}})
    if result.matched_count == 0:
        await callback_query.answer("❌ Token mila hi nahi.", show_alert=True)
    else:
        await callback_query.answer("✅ Token revoke ho gaya.")
    await callback_query.message.edit_text(
        "🛠 **Admin Panel** — neeche se option chuno:", reply_markup=await admin_panel_markup()
    )


@app.on_callback_query(filters.regex(r"^gtf_") & filters.user(ADMIN_ID))
async def gentoken_wizard_callback(client, callback_query):
    action_key = callback_query.data.split("_", 1)[1]
    user_id = callback_query.from_user.id
    state = pending_action.get(user_id)

    if action_key == "cancel":
        pending_action.pop(user_id, None)
        await callback_query.answer("❌ Cancel kar diya.")
        return await callback_query.message.edit_text(
            "🛠 **Admin Panel** — neeche se option chuno:", reply_markup=await admin_panel_markup()
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


@app.on_callback_query(filters.regex(r"^editmsg_(?!lang_)") & filters.user(ADMIN_ID))
async def editmsg_callback(client, callback_query):
    """Step 2/3: key select ho gaya -> ab language chuno (Hinglish ya English)."""
    key = callback_query.data.split("_", 1)[1]
    await callback_query.answer()

    if key not in DEFAULT_MESSAGES:
        return await callback_query.message.edit_text("❌ Message key not found.", reply_markup=edit_msg_markup())

    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("🇮🇳 Hinglish", callback_data=f"editmsg_lang_{key}_hi"),
         InlineKeyboardButton("🇬🇧 English", callback_data=f"editmsg_lang_{key}_en")],
        [InlineKeyboardButton("🔙 Back", callback_data="panel_editmsg")],
    ])
    await callback_query.message.edit_text(
        f"✏️ **{key}** — kaunsi language version edit karni hai?", reply_markup=buttons
    )


@app.on_callback_query(filters.regex(r"^editmsg_lang_") & filters.user(ADMIN_ID))
async def editmsg_lang_callback(client, callback_query):
    """Step 3/3: language chuni -> current text dikhao + naya text maango."""
    parts = callback_query.data.split("_")
    lang = parts[-1]
    key = "_".join(parts[2:-1])  # key me khud underscore ho sakta hai (e.g. invalid_token)
    user_id = callback_query.from_user.id

    pending_action[user_id] = {"flow": "editmsg", "key": key, "lang": lang, "step": "awaiting_text"}
    current = await get_message(key, lang)
    lang_label = "Hinglish" if lang == "hi" else "English"
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"editmsg_{key}")]])
    await callback_query.answer()
    await callback_query.message.edit_text(
        f"✏️ Current **{lang_label}** text for `{key}`:\n\n"
        f"`{current['text']}`\n"
        f"Extra messages: {len(current.get('extra', []))}\n\n"
        f"Naya text reply karke bhej. Extra message add karni ho to `|||` se separate kar:\n"
        f"`MainText|||ExtraMsg1|||ExtraMsg2`\n\n"
        f"/cancel se abort kar sakte ho.",
        reply_markup=back_btn
    )


@app.on_callback_query(filters.regex(r"^bcast_") & filters.user(ADMIN_ID))
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


@app.on_message(filters.command("addchannel") & filters.user(ADMIN_ID))
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
    await settings_col.update_one({"_id": "config"}, {"$set": {"broadcast_channels": channels}}, upsert=True)
    await message.reply_text(f"✅ Channel add ho gaya. Total: {len(channels)}\n\n⚠️ Bot ko us channel me admin banana mat bhoolna.")


@app.on_message(filters.command("search") & filters.user(ADMIN_ID))
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

async def handle_editmsg_wizard_text(client, message, user_id, action):
    """Edit Messages wizard ka typed step: naya text (aur optional extras) us key+lang
    ke liye DB me save karta hai."""
    key = action["key"]
    lang = action["lang"]
    text = message.text.strip()
    bits = text.split("|||")
    main_text = bits[0].strip()
    extras = [b.strip() for b in bits[1:] if b.strip()]

    config = await get_config()
    messages = config.get("messages", {})
    messages.setdefault(key, {})
    messages[key][lang] = {"text": main_text, "extra": extras}
    await settings_col.update_one({"_id": "config"}, {"$set": {"messages": messages}}, upsert=True)

    pending_action.pop(user_id, None)
    lang_label = "Hinglish" if lang == "hi" else "English"
    await message.reply_text(f"✅ `{key}` ({lang_label}) update ho gaya.\nMain: {main_text}\nExtras: {len(extras)}")


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


@app.on_message(filters.command("cancel") & filters.user(ADMIN_ID))
async def cancel_command(client, message):
    """Kisi bhi pending admin flow (editmsg, gentoken, etc.) ko cancel karta hai."""
    pending_action.pop(message.from_user.id, None)
    await message.reply_text("❌ Cancel kar diya.")


@app.on_message(filters.private & filters.text & filters.user(ADMIN_ID) & ~filters.command([
    "start", "admin", "addchannel", "search", "cancel"
]))
async def handle_admin_pending(client, message):
    user_id = message.from_user.id
    action = pending_action.get(user_id)
    if not action:
        return

    if isinstance(action, dict) and action.get("flow") == "gentoken":
        return await handle_gentoken_wizard_text(client, message, user_id, action)

    if isinstance(action, dict) and action.get("flow") == "editmsg":
        return await handle_editmsg_wizard_text(client, message, user_id, action)

    text = message.text.strip()

    if action == "awaiting_setfsub":
        try:
            int(text)
        except ValueError:
            return await message.reply_text("❌ Ye ID nahi lag rahi. Number bhej (e.g. `-1001234567890`) — dobara try kar.")
        await settings_col.update_one({"_id": "config"}, {"$set": {"fsub_id": text}}, upsert=True)
        pending_action[user_id] = "awaiting_setfsublink"
        return await message.reply_text("✅ ID save ho gayi. Ab channel ka **invite/join link** bhej (e.g. `https://t.me/teraChannel`):")

    elif action == "awaiting_setfsublink":
        if not (text.startswith("https://t.me/") or text.startswith("t.me/")):
            return await message.reply_text("❌ Ye valid link nahi lag raha. `https://t.me/...` format me bhej.")
        await settings_col.update_one({"_id": "config"}, {"$set": {"fsub_link": text}}, upsert=True)
        await message.reply_text(f"✅ FSUB poora set ho gaya!\n**ID:** saved\n**Link:** {text}\n\n⚠️ Bot ko us channel me admin banana mat bhoolna, warna join-check kaam nahi karega.")

    elif action == "awaiting_setdb":
        await settings_col.update_one({"_id": "config"}, {"$set": {"db_channel_id": text}}, upsert=True)
        await message.reply_text(f"✅ DB channel set ho gaya: `{text}`")

    elif action == "awaiting_settimer":
        if parse_time(text) is None:
            return await message.reply_text("❌ Format galat hai. Use: 10s, 5m, 1h, 1d — dobara bhej.")
        await settings_col.update_one({"_id": "config"}, {"$set": {"default_timer": text}}, upsert=True)
        await message.reply_text(f"✅ Default timer set ho gaya: `{text}`")

    elif action == "awaiting_autodelete":
        if text.lower() == "off":
            await settings_col.update_one({"_id": "config"}, {"$set": {"auto_delete_seconds": 0}}, upsert=True)
            await message.reply_text("✅ Auto-delete band kar diya.")
        else:
            seconds = parse_time(text)
            if seconds is None:
                return await message.reply_text("❌ Format galat hai. Use: 10m, 1h, ya `off` — dobara bhej.")
            await settings_col.update_one({"_id": "config"}, {"$set": {"auto_delete_seconds": seconds}}, upsert=True)
            await message.reply_text(f"✅ Files ab {text} baad auto-delete hongi.")

    elif action == "awaiting_cooldown":
        if not text.isdigit() or int(text) < 5:
            return await message.reply_text("❌ Minimum 5 seconds hona chahiye — dobara bhej.")
        await set_cooldown_duration(int(text))
        await message.reply_text(f"✅ Cooldown duration set ho gaya: {text}s")

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
            imported["_id"] = "config"
            await settings_col.replace_one({"_id": "config"}, imported, upsert=True)
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
            await settings_col.update_one({"_id": "config"}, {"$set": {"broadcast_channels": channels}}, upsert=True)
        await message.reply_text(f"✅ Channel add ho gaya. Ab broadcast me message bhej.\n\n⚠️ Bot ko us channel me admin banana mat bhoolna.")
        pending_action[user_id] = "awaiting_bcast_channels"
        return

    pending_action.pop(user_id, None) if action not in ("awaiting_setfsub",) else None


# ==========================================
# 📤 FILE SENDING (+ AUTO-DELETE)
# ==========================================

async def schedule_delete(client, chat_id, message_id, delay_seconds):
    if delay_seconds <= 0:
        return
    await asyncio.sleep(delay_seconds)
    try:
        await client.delete_messages(chat_id, message_id)
    except Exception:
        pass


async def send_batch(client, chat_id, token_data, offset, extra_message_ids=None):
    """extra_message_ids: pehle se bheje gaye messages (e.g. 'sending' status) jinhe is
    batch ke auto-delete cycle me shaamil karna hai — same delay follow karenge."""
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

    # Is batch ke sabhi messages (pichhla 'sending' status + files + progress + warning)
    # yahan track hote hain, taaki auto-delete FILES ke saath STATUS messages ko bhi
    # saaf kare — pehle sirf files delete hoti thi, status messages reh jaate the.
    sent_message_ids = list(extra_message_ids or [])

    for msg_id in batch:
        try:
            sent = await client.copy_message(
                chat_id=chat_id, from_chat_id=db_channel_id, message_id=msg_id,
                protect_content=protect
            )
            sent_message_ids.append(sent.id)
            await asyncio.sleep(0.7)
        except FloodWait as fw:
            # Telegram ne rate-limit lagaya - jitna bola utna wait karke retry karo
            await asyncio.sleep(fw.value + 1)
            try:
                sent = await client.copy_message(
                    chat_id=chat_id, from_chat_id=db_channel_id, message_id=msg_id,
                    protect_content=protect
                )
                sent_message_ids.append(sent.id)
            except Exception as e:
                warn = await client.send_message(chat_id, f"⚠️ File ID {msg_id} bhejne me error (retry ke baad bhi): {e}")
                sent_message_ids.append(warn.id)
        except Exception as e:
            warn = await client.send_message(chat_id, f"⚠️ File ID {msg_id} bhejne me error: {e}")
            sent_message_ids.append(warn.id)

    next_offset = offset + PAGE_SIZE
    total_files = len(all_files)
    if next_offset < total_files:
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton(
            "Next ⏭", callback_data=f"next_{token_data['token_id']}_{next_offset}"
        )]])
        progress_msg = await client.send_message(chat_id, f"({min(next_offset, total_files)}/{total_files} files sent)", reply_markup=buttons)
        sent_message_ids.append(progress_msg.id)

    if auto_delete_seconds > 0:
        unit_label = f"{auto_delete_seconds // 60}m" if auto_delete_seconds >= 60 else f"{auto_delete_seconds}s"
        warn_msg = await client.send_message(chat_id, f"⚠️ Ye files {unit_label} me delete ho jayengi, jaldi save kar lo.")
        sent_message_ids.append(warn_msg.id)

        # Files + status messages (sending/progress/warning) — sabko SAME delay ke saath schedule karo
        for mid in sent_message_ids:
            asyncio.create_task(schedule_delete(client, chat_id, mid, auto_delete_seconds))


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

async def redeem_token(client, user_id, chat_id, token, lang="hi"):
    """Token check + file delivery — plain-text token, deep-link (/start token), aur
    language-picker ke baad delayed redemption sabhi yahan se guzarte hain.
    Cooldown gate sabse pehle check hota hai (ye token-submission attempt bhi record karta
    hai), uske baad FSUB aur token validity."""
    active, remaining = is_cooldown_active(user_id)
    if active:
        return await client.send_message(chat_id, cooldown_message(lang, remaining))

    await record_token_attempt(user_id)

    if not await is_fsub_joined(client, user_id):
        config = await get_config()
        fsub_link = config.get("fsub_link")
        buttons = None
        if fsub_link:
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 Join Channel", url=fsub_link)],
                [InlineKeyboardButton("✅ Verify", callback_data="verify_fsub")],
            ])
        return await send_custom(client, chat_id, "not_joined", lang=lang, reply_markup=buttons)

    token_data = await tokens_col.find_one({"token_id": token})

    if not token_data or token_data.get("revoked") or datetime.now() > token_data["expiry_time"]:
        return await send_custom(client, chat_id, "invalid_token", lang=lang)

    usage_limit = token_data.get("usage_limit")
    used_count = token_data.get("used_count", 0)
    if usage_limit is not None and used_count >= usage_limit:
        return await client.send_message(chat_id, "❌ Ye token apni usage limit tak pahuch chuka hai.")

    await tokens_col.update_one({"token_id": token}, {"$inc": {"used_count": 1}})

    sending_msg = await send_custom(client, chat_id, "sending", lang=lang)
    await send_batch(client, chat_id, token_data, offset=0, extra_message_ids=[sending_msg.id])


@app.on_message(filters.private & ~filters.user(ADMIN_ID), group=-1)
async def pending_language_guard(client, message):
    """Agar user language-picker pending hai aur usne button dabane ke bajaye
    kuch aur (command/text) bhej diya, to wahi ignore karke picker dobara dikhao —
    normal handlers tak baat propagate nahi hone dete."""
    user_id = message.from_user.id
    if user_id in pending_language:
        await show_language_picker(client, message.chat.id)
        raise StopPropagation


@app.on_message(filters.command("start") & filters.private & ~filters.user(ADMIN_ID))
async def user_start(client, message):
    user_id = message.from_user.id
    existing_user = await users_col.find_one({"_id": user_id})
    await users_col.update_one(
        {"_id": user_id},
        {"$set": {"_id": user_id, "first_seen": datetime.now()}},
        upsert=True
    )

    # Naya user ya purana user jiske paas abhi "language" field nahi hai — dono ke liye
    # pehle language chunwao. Deep-link token (agar hai) language-choice tak pending_language me hold hota hai.
    if not existing_user or "language" not in existing_user:
        token = message.command[1].strip() if len(message.command) > 1 else None
        pending_language[user_id] = {"token": token}
        return await show_language_picker(client, message.chat.id)

    lang = existing_user.get("language", "hi")

    # Deep-link se aaya hai to /start ke saath token bhi hoga: /start Kissu-Cutie
    if len(message.command) > 1:
        token = message.command[1].strip()
        if token.startswith("Kissu-"):
            return await redeem_token(client, user_id, message.chat.id, token, lang)

    await send_welcome_or_verified(client, message.chat.id, user_id, lang)


@app.on_callback_query(filters.regex(r"^lang_(hi|en)$"))
async def language_callback(client, callback_query):
    """Language picker ka button tap — language save karta hai, phir pending token
    (agar deep-link se aaya tha) redeem karta hai ya normal welcome/verified dikhata hai."""
    user_id = callback_query.from_user.id
    lang = "hi" if callback_query.data == "lang_hi" else "en"
    chat_id = callback_query.message.chat.id

    await set_user_language(user_id, lang)
    pending = pending_language.pop(user_id, {})
    token = pending.get("token")

    await callback_query.answer("Language set! ✅")
    await callback_query.message.delete()

    if token and token.startswith("Kissu-"):
        await redeem_token(client, user_id, chat_id, token, lang)
    else:
        await send_welcome_or_verified(client, chat_id, user_id, lang)


@app.on_callback_query(filters.regex(r"^verify_fsub$"))
async def verify_fsub_callback(client, callback_query):
    user_id = callback_query.from_user.id
    if await is_fsub_joined(client, user_id):
        lang = await get_user_language(user_id)
        await callback_query.answer("✅ Verified!")
        msg_data = await get_message("verified", lang)
        await callback_query.message.edit_text(msg_data["text"])
        for extra_text in msg_data.get("extra", []):
            await client.send_message(callback_query.message.chat.id, extra_text)
    else:
        await callback_query.answer("❌ Abhi bhi join nahi kiya hai. Pehle join kar.", show_alert=True)


@app.on_message(filters.private & filters.text & filters.regex(r"^Kissu-") & ~filters.user(ADMIN_ID))
async def handle_token_input(client, message):
    lang = await get_user_language(message.from_user.id)
    token = message.text.strip()
    await redeem_token(client, message.from_user.id, message.chat.id, token, lang)


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
