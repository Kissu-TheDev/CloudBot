# 🧑‍💻 KissuDrop

Ek Telegram bot jo **token-based file delivery** karta hai — ek private DB channel se files ko token ke through users tak bhejta hai, saath mein force-subscribe, auto-delete, per-token expiry, aur ek full admin panel ke saath.

> 📦 Naam **KissuDrop** isliye rakha gaya hai kyunki bot exactly yahi karta hai — token ke through files "drop" karta hai users tak. Ye actual "cloud storage" nahi hai: files kahin upload nahi hoti bot ke through, wo pehle se ek Telegram channel (DB Channel) mein padi hoti hain, bot sirf token ke basis pe unhe copy karke bhejta hai.

---

## 🇮🇳 Hinglish (Main)

### Ye bot karta kya hai

1. Admin apne ek private Telegram channel mein files daalta hai (ye "DB Channel" banta hai).
2. Admin bot ko bolta hai: "in file-IDs (ya range) ka token bana do" → bot ek token generate karta hai jaise `Kissu-CuteGirl`.
3. Wo token kisi user ko diya jata hai (link/message ke through, bot khud generate nahi karta link).
4. User bot ko `/start` karta hai, agar force-subscribe (FSUB) channel set hai to pehle wo join karna padta hai.
5. User token bhejta hai (jaise `Kissu-CuteGirl`) → bot DB channel se wo files copy karke user ko bhej deta hai.
6. Agar auto-delete on hai, to files kuch time baad user ke chat se khud delete ho jati hain.

### Features

- 🔑 **Token System** — single file, range (`4-8`), ya multiple ranges (`4-8 20-25`) ek hi token mein.
- 🔢 **Usage Limit** — token ko fix number of times use karne ki limit lagai ja sakti hai.
- 📢 **Force Subscribe (FSUB)** — user ko pehle ek channel join karna zaroori, warna files nahi milengi.
- 🗑 **Auto-Delete** — bheji gayi files ek set time ke baad khud delete ho jati hain.
- 🔒 **Content Protection** — files ko forward/save hone se rok sakte ho (Telegram ka `protect_content`).
- ✏️ **Custom Messages** — welcome, verified, sending, invalid-token, not-joined — sab admin panel se edit ho sakte hain (extra follow-up messages ke saath).
- 📣 **Broadcast** — sabhi users ko, ya specific channels ko ek saath message bhejo.
- 📤📥 **Export/Import Settings** — pura config JSON mein backup/restore kar sakte ho.
- 🐞 **Debug Panel** — total users, active tokens, DB/FSUB status ek jagah.
- 📄 **Pagination** — bade token (bahut saari files) automatically batches (10-10) mein bheje jaate hain, "Next ⏭" button ke saath.
- ⚠️ **FloodWait Handling** — Telegram rate-limit lagne pe khud wait karke retry karta hai.

### Tech Stack

| Cheez | Use |
|---|---|
| [Pyrogram](https://docs.pyrogram.org/) | Telegram MTProto client (bot framework) |
| [TgCrypto](https://github.com/pyrogram/tgcrypto) | Pyrogram ke liye fast encryption |
| [Motor](https://motor.readthedocs.io/) | Async MongoDB driver |
| MongoDB | Settings + tokens + users store karne ke liye |
| python-dotenv | `.env` file se config load karne ke liye |

### Setup

**1. Repo clone karo**
```bash
git clone https://github.com/Kissu-TheDev/KissuDrop.git
cd KissuDrop
```

**2. Dependencies install karo**
```bash
pip install -r requirements.txt
```

**3. `.env` file banao**

`.env.example` ko copy karke `.env` banao aur values bharo:
```bash
cp .env.example .env
```

| Variable | Kahan se milega | Zaroori? |
|---|---|---|
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) se naya bot banake | ✅ Haan |
| `API_ID` | [my.telegram.org](https://my.telegram.org) | ✅ Haan |
| `API_HASH` | [my.telegram.org](https://my.telegram.org) | ✅ Haan |
| `MONGODB_URL` | [MongoDB Atlas](https://www.mongodb.com/atlas) connection string | ✅ Haan |
| `ADMIN_ID` | Apni Telegram numeric user ID (e.g. via [@userinfobot](https://t.me/userinfobot)) | ✅ Haan |
| `DB_CHANNEL_ID` | Jis channel mein files store karoge uski ID | ⭕ Optional (baad mein `/admin` panel se bhi set ho sakti hai) |

**4. Bot ko run karo**
```bash
python bot.py
```

Terminal mein `KissuCloudBot is alive!` dikhega — matlab bot chalu ho gaya.

### Use Kaise Karein (Admin)

1. Bot ko apne DB channel mein **admin** banao (warna wo files copy nahi kar payega).
2. Bot ko `/start` karo apne (admin) account se → seedha admin panel ka button milega.
3. `/admin` se panel kabhi bhi khol sakte ho. Panel se:
   - **Set DB Channel** — jahan se files copy hongi.
   - **Set FSUB** — force-join channel ID + link.
   - **Generate Token** — format: `<ranges/files> <naam> [|L:limit|] [|E:time|] [|T| ya |F|] [?LINK]`
     - `01-19 21-25 Cutie` → sirf token banega, unlimited use, global default timer + global auto-delete setting follow hogi
     - `01-19 21-25 Cutie |L:99|` → 99 baar tak use ho sakta hai
     - `01-19 21-25 Cutie |E:2h|` → is token ki apni custom expiry (2 ghante), global default timer ignore
     - `01-19 21-25 Cutie |T|` → is token ki files ke liye auto-delete force ON (global setting ignore)
     - `01-19 21-25 Cutie |F|` → is token ki files auto-delete kabhi nahi hongi (global setting ignore)
     - `01-19 21-25 Cutie ?LINK` → token ke saath ek Telegram deep-link bhi milega jisse user click karke seedha bot mein token redeem kar sakta hai
     - Sab ek saath, kisi bhi order mein: `01-19 21-25 Cutie |L:99| |E:2h| |T| ?LINK`
     - Marker na do to us cheez ka default lagta hai: limit = unlimited, expiry = global default timer, auto-delete = global setting, link = nahi milega
   - **Set Timer** — token kitni der mein expire hoga (`1h`, `30m`, `1d`).
   - **Auto-Delete** — sent files kitni der baad delete ho.
4. Generated token (`Kissu-CuteGirl`) user ko manually bhejo — bot khud shareable link nahi banata, token hi share karna padta hai.

### ⚠️ Jaani-Maani Limitations (Honest Disclosure)

- **FSUB check ab fail-closed hai** ✅ (fixed): agar member-check karte waqt koi error aata hai (jaise bot us channel ka admin nahi hai), to ab user ko *not-joined* treat kiya jaata hai — bypass nahi hone diya jaata. **Trade-off:** agar bot galti se FSUB channel ka admin nahi raha, to sab users silently block ho jayenge (join-check har baar fail dikhega). Isliye `/admin` → **Debug** panel se time-time pe FSUB status check karte raho — ab wahan clearly dikhega agar bot ki permission tut gayi hai.
- **Single admin only**: `ADMIN_ID` ek hi ID leta hai — multiple admins ka built-in support nahi hai.
- **No rate-limiting on token guesses**: users jitni baar chahein galat token try kar sakte hain, koi cooldown/lockout nahi hai.
- Ye actual "cloud storage" nahi hai — sab kuch Telegram ke DB channel pe depend karta hai; wo channel delete/leave hui to sab tut jaayega.

### Folder Structure
```
KissuDrop/
├── bot.py            # Poora bot logic (single file)
├── requirements.txt  # Python dependencies
└── .env.example      # Config template
```

---

## 🇬🇧 English (Translation)

### What this bot does

1. Admin stores files in a private Telegram channel (the "DB Channel").
2. Admin tells the bot to generate a token for a given file ID / range → bot creates a token like `Kissu-CuteGirl`.
3. That token is shared with a user manually (the bot does not auto-generate shareable links).
4. The user runs `/start`; if a force-subscribe (FSUB) channel is configured, they must join it first.
5. The user sends the token → the bot copies the corresponding files from the DB channel and delivers them.
6. If auto-delete is enabled, delivered files are removed from the user's chat after a set delay.

### Features

- 🔑 **Token system** — supports single files, ranges (`4-8`), or multiple ranges (`4-8 20-25`) in one token.
- 🔢 **Usage limits** — cap how many times a token can be redeemed.
- 📢 **Force Subscribe (FSUB)** — gate file delivery behind joining a channel.
- 🗑 **Auto-delete** — delivered files self-delete after a configurable delay.
- 🔒 **Content protection** — optionally block forwarding/saving via Telegram's `protect_content`.
- ✏️ **Custom messages** — welcome, verified, sending, invalid-token, not-joined states are all editable from the admin panel, including extra follow-up messages.
- 📣 **Broadcast** — send a message to all users or to specific channels.
- 📤📥 **Export/import settings** — back up or restore the entire config as JSON.
- 🐞 **Debug panel** — total users, active tokens, DB/FSUB status at a glance.
- 📄 **Pagination** — large batches of files are sent in pages of 10, with a "Next ⏭" button.
- ⚠️ **FloodWait handling** — automatically waits and retries when Telegram rate-limits the bot.

### Tech Stack

| Component | Purpose |
|---|---|
| [Pyrogram](https://docs.pyrogram.org/) | Telegram MTProto client (bot framework) |
| [TgCrypto](https://github.com/pyrogram/tgcrypto) | Fast encryption backend for Pyrogram |
| [Motor](https://motor.readthedocs.io/) | Async MongoDB driver |
| MongoDB | Stores settings, tokens, and users |
| python-dotenv | Loads config from `.env` |

### Setup

**1. Clone the repo**
```bash
git clone https://github.com/Kissu-TheDev/KissuDrop.git
cd KissuDrop
```

**2. Install dependencies**
```bash
pip install -r requirements.txt
```

**3. Create your `.env` file**
```bash
cp .env.example .env
```

| Variable | Where to get it | Required? |
|---|---|---|
| `BOT_TOKEN` | Create a bot via [@BotFather](https://t.me/BotFather) | ✅ Yes |
| `API_ID` | [my.telegram.org](https://my.telegram.org) | ✅ Yes |
| `API_HASH` | [my.telegram.org](https://my.telegram.org) | ✅ Yes |
| `MONGODB_URL` | [MongoDB Atlas](https://www.mongodb.com/atlas) connection string | ✅ Yes |
| `ADMIN_ID` | Your numeric Telegram user ID (e.g. via [@userinfobot](https://t.me/userinfobot)) | ✅ Yes |
| `DB_CHANNEL_ID` | ID of the channel you store files in | ⭕ Optional (can also be set later via the `/admin` panel) |

**4. Run the bot**
```bash
python bot.py
```

You should see `KissuCloudBot is alive!` in the terminal once it's running.

### How to Use (Admin)

1. Make the bot an **admin** in your DB channel — otherwise it can't copy files from it.
2. Run `/start` with your admin account → you'll get a direct button to open the admin panel.
3. Open the panel anytime with `/admin`. From there you can:
   - **Set DB Channel** — the source channel files are copied from.
   - **Set FSUB** — the force-join channel ID + invite link.
   - **Generate Token** — format: `<ranges/files> <name> [|L:limit|] [|E:time|] [|T| or |F|] [?LINK]`
     - `01-19 21-25 Cutie` → generates just the token, unlimited uses, follows the global default timer and global auto-delete setting
     - `01-19 21-25 Cutie |L:99|` → limits the token to 99 redemptions
     - `01-19 21-25 Cutie |E:2h|` → gives this token its own custom expiry (2 hours), overriding the global default timer
     - `01-19 21-25 Cutie |T|` → forces auto-delete ON for this token's files (overrides the global setting)
     - `01-19 21-25 Cutie |F|` → forces auto-delete OFF for this token's files (overrides the global setting)
     - `01-19 21-25 Cutie ?LINK` → also returns a Telegram deep-link the user can click to redeem the token directly
     - All together, in any order: `01-19 21-25 Cutie |L:99| |E:2h| |T| ?LINK`
     - Omitting a marker uses its default: limit = unlimited, expiry = global default timer, auto-delete = follows global setting, link = not generated
   - **Set Timer** — how long before a token expires (`1h`, `30m`, `1d`).
   - **Auto-Delete** — how long after delivery files should be removed.
4. Share the generated token (`Kissu-CuteGirl`) with the user manually — the bot does not generate a shareable link itself.

### ⚠️ Known Limitations (Honest Disclosure)

- **FSUB check is now fail-closed** ✅ (fixed): if an error occurs while checking channel membership (e.g. the bot isn't an admin in that channel), the user is now treated as *not joined* rather than silently passed through. **Trade-off:** if the bot ever loses admin rights in the FSUB channel, every user will be blocked (the join-check will fail for everyone, joined or not). Check the FSUB status in `/admin` → **Debug** periodically — it now clearly flags whether the bot's permissions are broken.
- **Single admin only**: `ADMIN_ID` accepts a single ID — there's no built-in multi-admin support.
- **No rate-limiting on token guesses**: users can attempt as many token strings as they want with no cooldown or lockout.
- This is not real "cloud storage" — everything depends on the Telegram DB channel; if that channel is deleted or the bot loses access, delivery breaks.

### Folder Structure
```
KissuDrop/
├── bot.py            # Full bot logic (single file)
├── requirements.txt  # Python dependencies
└── .env.example      # Config template
```
