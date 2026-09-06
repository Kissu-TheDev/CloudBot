import os
import json
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import UserNotParticipant, FloodWait
from motor.motor_asyncio import AsyncIOMotorClient

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
BOT_USERNAME = None  # startup pe app.get_me() se fill hoga, deep-link banane ke liye

# ==========================================
# 💬 DEFAULT CUSTOMIZABLE MESSAGES
# Ye saare keys admin panel se edit ho sakte hain.
# "extra" ek list hai - trigger pe in sabhi messages ko bhi bhejega (order me).
# ==========================================
DEFAULT_MESSAGES = {
    "welcome": {"text": "🧑‍💻", "extra": []},
    "verified": {"text": "🪪", "extra": []},
    "sending": {"text": "📤", "extra": []},
    "invalid_token": {"text": "❌", "extra": []},
    "not_joined": {"text": "❌ Pehle channel join karo.", "extra": []},
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
# 🏷 TOKEN OPTIONS PARSER (naam + optional markers)
# Format: "Cutie |L:99| |E:2h| |T| ?LINK"  (order matter nahi karta markers ka)
#  - naam: pehla plain word (koi marker match na kare)
#  - |L:99| : usage limit (optional, default = unlimited)
#  - |E:2h| : per-token expiry (optional, default = admin ka global default_timer)
#  - |T| ya |F| : auto-delete override (optional, default = admin ka global setting)
#  - ?LINK : deep-link bhi generate karo (optional, default = sirf token)
# ==========================================
def parse_token_options(parts):
    """
    parts: naam + baad ke saare tokens (jaise ["Cutie", "|L:99|", "|E:2h|", "|T|", "?LINK"])
    Returns: (name, usage_limit, expiry_seconds, auto_delete_override, want_link, error_message_or_None)
      expiry_seconds: int (custom) ya None (None = global default_timer follow karo)
      auto_delete_override: True / False / None (None = global setting follow karo)
    """
    name = None
    usage_limit = None
    expiry_seconds = None
    auto_delete_override = None
    want_link = False

    for part in parts:
        if part == "?LINK":
            want_link = True
        elif part == "|T|":
            auto_delete_override = True
        elif part == "|F|":
            auto_delete_override = False
        elif part.startswith("|L:") and part.endswith("|"):
            num_str = part[3:-1]
            if not num_str.isdigit():
                return None, None, None, None, False, f"❌ `{part}` me limit number nahi hai (format: `|L:99|`)."
            usage_limit = int(num_str)
        elif part.startswith("|E:") and part.endswith("|"):
            time_str = part[3:-1]
            seconds = parse_time(time_str)
            if seconds is None:
                return None, None, None, None, False, f"❌ `{part}` me time format galat hai (format: `|E:2h|`, `|E:30m|`, `|E:1d|`)."
            expiry_seconds = seconds
        elif part.startswith("|") or part.startswith("?"):
            return None, None, None, None, False, f"❌ `{part}` samajh nahi aaya. Valid markers: `|L:number|`, `|E:time|`, `|T|`, `|F|`, `?LINK`."
        else:
            if name is not None:
                return None, None, None, None, False, f"❌ Do naam mile (`{name}` aur `{part}`) — sirf ek naam allowed hai."
            name = part

    if name is None:
        return None, None, None, None, False, "❌ Koi naam nahi mila — naam zaroori hai (files ke baad)."

    return name, usage_limit, expiry_seconds, auto_delete_override, want_link, None


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


@app.on_message(filters.command("start") & filters.private & filters.user(ADMIN_ID))
async def admin_start(client, message):
    """Admin ke liye /start alag hai - seedha 👾 + panel button."""
    await users_col.update_one(
        {"_id": message.from_user.id},
        {"$set": {"_id": message.from_user.id, "first_seen": datetime.now()}},
        upsert=True
    )
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("🛠 Open Admin Panel", callback_data="panel_open")]])
    await message.reply_text("👾", reply_markup=buttons)


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

    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="panel_back")]])

    prompts = {
        "setfsub": "📢 Pehle FSUB **channel ID** bhej (e.g. `-1001234567890`):",
        "setdb": "🗄 DB channel ki ID bhej (e.g. `-1001234567890`):",
        "settimer": "⏱ Default token expiry time bhej (e.g. `1h`, `30m`, `1d`):",
        "autodelete": "🗑 Files kitni der baad auto-delete ho (e.g. `10m`, `1h`). Band karne ke liye `off` bhej:",
        "gentoken": (
            "🔑 Format me bhej (ranges/numbers, phir naam, phir optional markers — kisi bhi order me):\n\n"
            "**Single file:** `101 CuteGirl`\n"
            "**Range:** `101-112 CuteGirl`\n"
            "**Multi-range:** `4-8 20-25 VIP`\n"
            "**Usage limit ke saath:** `4-8 20-25 VIP |L:50|`\n"
            "**Custom expiry:** `4-8 20-25 VIP |E:2h|` (`|E:30m|`, `|E:1d|` bhi chalega) — na doge to global default timer follow hoga\n"
            "**Auto-delete override:** `4-8 20-25 VIP |T|` (ON) ya `|F|` (OFF) — na doge to global setting follow hogi\n"
            "**Deep-link bhi chahiye:** `4-8 20-25 VIP ?LINK`\n"
            "**Sab ek saath:** `4-8 20-25 VIP |L:50| |E:2h| |T| ?LINK`\n\n"
            "_Limit na do to unlimited use hoga. Marker order matter nahi karta._"
        ),
        "revoke": "❌ Jo token revoke karna hai uska naam bhej (e.g. `Kissu-CuteGirl`):",
    }
    pending_action[user_id] = f"awaiting_{action}"
    await callback_query.answer()
    await callback_query.message.reply_text(prompts[action], reply_markup=back_btn)


@app.on_callback_query(filters.regex(r"^editmsg_") & filters.user(ADMIN_ID))
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

@app.on_message(filters.private & filters.text & filters.user(ADMIN_ID) & ~filters.command([
    "start", "admin", "addchannel", "search"
]))
async def handle_admin_pending(client, message):
    user_id = message.from_user.id
    action = pending_action.get(user_id)
    if not action:
        return

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

    elif action == "awaiting_gentoken":
        parts = text.split()
        if len(parts) < 2:
            return await message.reply_text("❌ Kam se kam ek number/range aur naam chahiye — dobara bhej.")

        # File-ID parts hamesha shuru me hote hain (numbers/ranges). Pehla part jo
        # number/range nahi hai, wahi se naam+markers ka section shuru hota hai.
        def looks_like_range_part(p):
            if p.isdigit():
                return True
            bits = p.split("-")
            return len(bits) == 2 and bits[0].isdigit() and bits[1].isdigit()

        split_index = len(parts)
        for i, p in enumerate(parts):
            if not looks_like_range_part(p):
                split_index = i
                break

        range_parts = parts[:split_index]
        rest_parts = parts[split_index:]

        if not range_parts:
            return await message.reply_text("❌ Koi file ID/range nahi mila — dobara bhej.")
        if not rest_parts:
            return await message.reply_text("❌ Naam nahi mila — dobara bhej.")

        file_ids, error = parse_ranges(range_parts)
        if error:
            return await message.reply_text(f"{error} — dobara bhej.")
        if not file_ids:
            return await message.reply_text("❌ Koi valid file ID nahi mili — dobara bhej.")

        name, usage_limit, expiry_override_seconds, auto_delete_override, want_link, opt_error = parse_token_options(rest_parts)
        if opt_error:
            return await message.reply_text(f"{opt_error} — dobara bhej.")

        config = await get_config()
        expiry_marker = next((p for p in rest_parts if p.startswith("|E:") and p.endswith("|")), None)
        if expiry_override_seconds is not None and expiry_marker:
            seconds = expiry_override_seconds
            expiry_display = expiry_marker[3:-1]
        else:
            expiry_display = config.get("default_timer", "1h")
            seconds = parse_time(expiry_display) or 3600

        token_id = f"Kissu-{name}"
        await tokens_col.update_one(
            {"token_id": token_id},
            {"$set": {
                "token_id": token_id,
                "expiry_time": datetime.now() + timedelta(seconds=seconds),
                "files": file_ids,
                "revoked": False,
                "usage_limit": usage_limit,
                "used_count": 0,
                "auto_delete_override": auto_delete_override,  # None = global setting follow karo
            }},
            upsert=True
        )
        limit_text = f"{usage_limit} uses" if usage_limit else "Unlimited"
        if auto_delete_override is None:
            delete_text = "Global setting follow hogi"
        else:
            delete_text = "ON" if auto_delete_override else "OFF"

        reply_text = (
            f"🔥 **Token Generated!**\n\n**Token:** `{token_id}`\n"
            f"**Files:** {len(file_ids)}\n**Expires in:** {expiry_display}\n"
            f"**Usage limit:** {limit_text}\n**Auto-delete:** {delete_text}"
        )
        if want_link:
            if BOT_USERNAME:
                deep_link = f"https://t.me/{BOT_USERNAME}?start={token_id}"
                reply_text += f"\n**Link:** {deep_link}"
            else:
                reply_text += "\n⚠️ Link nahi ban paya — bot username load nahi hua abhi tak."

        await message.reply_text(reply_text)


    elif action == "awaiting_revoke":
        token_id = text if text.startswith("Kissu-") else f"Kissu-{text}"
        result = await tokens_col.update_one({"token_id": token_id}, {"$set": {"revoked": True}})
        if result.matched_count == 0:
            await message.reply_text(f"❌ Token `{token_id}` mila hi nahi.")
        else:
            await message.reply_text(f"✅ Token `{token_id}` revoke kar diya gaya.")

    elif action.startswith("awaiting_editmsg_"):
        key = action.replace("awaiting_editmsg_", "")
        bits = text.split("|||")
        main_text = bits[0].strip()
        extras = [b.strip() for b in bits[1:] if b.strip()]
        config = await get_config()
        messages = config.get("messages", {})
        messages[key] = {"text": main_text, "extra": extras}
        await settings_col.update_one({"_id": "config"}, {"$set": {"messages": messages}}, upsert=True)
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
                await client.send_message(chat_id, f"⚠️ File ID {msg_id} bhejne me error (retry ke baad bhi): {e}")
        except Exception as e:
            await client.send_message(chat_id, f"⚠️ File ID {msg_id} bhejne me error: {e}")

    next_offset = offset + PAGE_SIZE
    total_files = len(all_files)
    if next_offset < total_files:
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton(
            "Next ⏭", callback_data=f"next_{token_data['token_id']}_{next_offset}"
        )]])
        await client.send_message(chat_id, f"({min(next_offset, total_files)}/{total_files} files sent)", reply_markup=buttons)

    if auto_delete_seconds > 0:
        unit_label = f"{auto_delete_seconds // 60}m" if auto_delete_seconds >= 60 else f"{auto_delete_seconds}s"
        await client.send_message(chat_id, f"⚠️ Ye files {unit_label} me delete ho jayengi, jaldi save kar lo.")


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

async def redeem_token(client, message, token):
    """Token check + file delivery — plain-text token aur deep-link (/start token) dono se call hota hai."""
    user_id = message.from_user.id

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
        return await message.reply_text("❌ Ye token apni usage limit tak pahuch chuka hai.")

    await tokens_col.update_one({"token_id": token}, {"$inc": {"used_count": 1}})

    await send_custom(client, message.chat.id, "sending")
    await send_batch(client, message.chat.id, token_data, offset=0)


@app.on_message(filters.command("start") & filters.private & ~filters.user(ADMIN_ID))
async def user_start(client, message):
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
    if await is_fsub_joined(client, user_id):
        await callback_query.answer("✅ Verified!")
        msg_data = await get_message("verified")
        await callback_query.message.edit_text(msg_data["text"])
        for extra_text in msg_data.get("extra", []):
            await client.send_message(callback_query.message.chat.id, extra_text)
    else:
        await callback_query.answer("❌ Abhi bhi join nahi kiya hai. Pehle join kar.", show_alert=True)


@app.on_message(filters.private & filters.text & filters.regex(r"^Kissu-") & ~filters.user(ADMIN_ID))
async def handle_token_input(client, message):
    token = message.text.strip()
    await redeem_token(client, message, token)


async def main():
    global BOT_USERNAME
    await app.start()
    me = await app.get_me()
    BOT_USERNAME = me.username
    print(f"KissuCloudBot is alive! (@{BOT_USERNAME})")
    await idle()
    await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
