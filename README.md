# 🧑‍💻 CloudBot

Ek Telegram bot jo **token-based file delivery** karta hai — ek private DB channel se files ko token ke through users tak bhejta hai, saath mein force-subscribe, auto-delete, per-token expiry, rate-limiting, aur ek fully button-based admin panel ke saath.

> 📦 Ye actual "cloud storage" nahi hai. Files kahin upload nahi hoti bot ke through — wo pehle se ek Telegram channel (DB Channel) mein padi hoti hain, bot sirf token ke basis pe unhe copy karke bhejta hai.

---

## Bot Kya Karta Hai

1. Admin apne ek private Telegram channel mein files daalta hai ("DB Channel").
2. Admin, admin panel se un files ke liye token generate karta hai (format: `Kissu-<naam>`).
3. Token user ko manually diya jaata hai — ya agar deep-link maanga ho, to seedha ek clickable link mil jaata hai jo Telegram khol ke token auto-submit kar deta hai.
4. User `/start` karta hai. Agar force-subscribe (FSUB) channel set hai, to pehle join karna zaroori hota hai.
5. User token bhejta hai → bot DB channel se files copy karke deliver kar deta hai.
6. Agar auto-delete on hai, to files ek set time baad khud delete ho jaati hain.

## Features

- **Token System** — single file, range (`4-8`), ya multiple ranges (`4-8 20-25`) ek hi token mein.
- **Usage Limit** — token ko fix number of redemptions tak limit kiya ja sakta hai, ya unlimited chhoda ja sakta hai. Revoke-list mein har token ka `used/limit` count dikhta hai.
- **Per-Token Expiry** — har token ki apni custom expiry ho sakti hai (global default se alag).
- **Auto-Delete Override** — har token ke liye alag se decide ho sakta hai ki uski files auto-delete hongi ya nahi (global setting se independent).
- **Deep-Link Generation** — chaho to token ke saath ek `t.me/BotUsername?start=token` wala link bhi mil jaata hai, jisse user seedha click karke redeem kar sake.
- **Force Subscribe (FSUB)** — file delivery channel-join se conditional hai. Membership check **fail-closed** hai: agar check ke dauraan koi error aaye (jaise bot ka us channel mein admin access na hona), to user ko *not-joined* treat kiya jaata hai — koi accidental bypass nahi hota. Trade-off: agar bot kabhi FSUB channel ka admin nahi raha, sab users block ho jaayenge — isliye `/admin` → Debug se status check karte rehna zaroori hai.
- **Rate-Limiting** 🤖 — agar koi user ek chhoti time-window ke andar bahut baar token redeem kare, to usko ek **fixed cooldown** ke liye rok diya jaata hai — jab tak wo exact time khatam na ho jaaye, sliding-window naya check nahi hota (spam karne se cooldown reset nahi hoga). Cooldown-message ab do alag messages mein aata hai: ek editable `🤖` (Edit Messages se) + ek exact-countdown text. Count, window, cooldown-time — sab `/admin` se customize ho sakte hain.
- **Multiple Admins** — `.env` wala `ADMIN_ID` **super-admin** hai; wahi doosre admins ko add/remove kar sakta hai (`/admin` → Manage Admins). Extra admins ko poora panel access milta hai, sirf Manage Admins unhe nahi dikhta.
- **Ban System** — kisi bhi user ko user-ID se ban kiya ja sakta hai (`/admin` → Ban/Unban User). Ban hote hi, agar wo FSUB channel ka member hai, use turant kick bhi kar diya jaata hai. Banned user ke liye bot silently non-responsive ho jaata hai (`/start`, token-redeem, sab). Poori banned-list JSON file ke roop mein export ho sakti hai.
- **Content Protection** — Telegram ke `protect_content` flag se forward/save optionally block ho sakta hai.
- **Customizable Messages** — welcome, verified, not-joined, restricted, admin-welcome, sending, invalid-token, rate-limit emoji, banned — sab default mein sirf ek emoji hain, lekin admin panel se chaho to inme extra text/messages add kiye ja sakte hain.
- **Broadcast** — sabhi users ko, ya specific channels ko ek saath message bhejne ki facility.
- **Export/Import Settings** — poora config JSON mein backup/restore ho sakta hai.
- **Debug Panel** — total users, active tokens, DB/FSUB status ek jagah.
- **Pagination** — bade token batches mein (10-10 files) deliver hote hain, "Next" button ke saath.
- **FloodWait Handling** — Telegram ka rate-limit lagne pe bot khud wait karke retry karta hai.
- **Config Caching** — settings ek chhote (5s) in-memory cache ke saath serve hoti hain, taaki har handler baar-baar DB round-trip na kare.

## Admin Panel — Poora Button-Based

Admin panel ka poora interaction ab inline buttons se hota hai — typing sirf wahin zaroori hai jahan koi genuinely free-form value chahiye (file ID, token ka naam, ek custom number/time). Kisi bhi settings-change ke baad naya message aane ki jagah, wahi purana panel-message edit ho jaata hai — taaki chat mein bar-bar naye bubbles na aayein.

**Set Timer** (⏱ Set Timer)
- Preset buttons: `30m`, `1h`, `6h`, `1d`.
- `✏️ Custom` — koi aur value chahiye to text-prompt khulta hai.

**Auto-Delete** (🗑 Auto-Delete)
- Preset buttons: `10m`, `1h`, `6h`.
- `🔴 Off` — band karne ke liye.
- `✏️ Custom` — koi aur value.

**Rate-Limit Settings** (🤖 Rate-Limit Settings) — naya section:
- **Count** — kitni baar redeem karne ke baad limit lagegi (default: 3).
- **Window** — kitne seconds ke andar wo count hona chahiye (default: 60s).
- **Cooldown** — limit lagne pe kitni der rukna padega — ye **fixed hard cooldown** hai, matlab exact itni der ke liye user block rahega chahe uske purane attempts window se bahar ho jaayein (default: 30s).
- **Message** — cooldown ka text, `{s}` likhne se wahan remaining seconds fill ho jaata hai (default: `Wait {s}s....`). Ye message alag se aata hai, `🤖` emoji (jo khud Edit Messages se editable hai) ke baad ek doosre message ke roop mein.

**Ban / Unban User** (🚫 Ban / Unban User) — naya section:
- User ID se ban/unban.
- Ban hote hi, agar user FSUB channel ka member hai, use turant kick kar diya jaata hai.
- Banned users ke liye bot puri tarah non-responsive ho jaata hai — `/start`, token, FSUB-verify, sab silently ignore.
- Poori list JSON file ke roop mein export ho sakti hai (📤 Export Ban List).

**Manage Admins** (👑 Manage Admins) — sirf super-admin (`.env` wala `ADMIN_ID`) ko dikhta hai:
- User ID se naye admins add/remove kar sakte ho.
- Extra admins ko poora panel-access milta hai, sirf ye Manage Admins section unhe nahi dikhta.

**Revoke Token** (❌ Revoke Token)
- Sabhi active tokens ki button-list, pagination ke saath (10 se zyada hone par). Har button mein token ka `used/limit` usage count bhi dikhta hai.
- Select karne pe confirmation step aata hai (poora detail: files count, usage), accidental revoke se bachne ke liye.

**Generate Token** (🔑 Generate Token) — 6-step guided wizard:

| Step | Kya poocha jaata hai | Input |
|---|---|---|
| 1 | File ID(s) / range | Typing |
| 2 | Token ka naam | Typing |
| 3 | Usage Limit | `Set Limit` ya `Skip` |
| 4 | Custom Expiry | `Set Expiry` ya `Skip` |
| 5 | Auto-Delete override | `Force ON` / `Force OFF` / `Skip` |
| 6 | Deep-link chahiye? | `Yes` / `No` |

Aakhri step ek summary dikhata hai, jahan se `✅ Generate Token` ya `🔙 Cancel` chuna jaata hai.

## Default Messages

Ye saare messages by default sirf ek emoji hain — agar chaho to `/admin` → ✏️ Edit Messages se inme text/extra-messages add kar sakte ho:

| Message | Kab trigger hota hai | Default |
|---|---|---|
| `welcome` | Naya user `/start` kare (FSUB pending) | 🧑‍💻 |
| `verified` | User join-check pass kar le (bina FSUB ke seedha) | 🪪 |
| `not_joined` | User FSUB channel join nahi kiya | 🗝️ |
| `restricted` | Token ki koi file DB channel mein nahi mili (deleted/service message) | ❗️ |
| `admin_welcome` | Admin khud `/start` kare | ✅ |
| `sending` | Token valid hai, files bhejna shuru | 📤 |
| `invalid_token` | Token galat/expired/revoked hai | ❌ |
| `ratelimit` | Rate-limit lag jaaye (exact-countdown text ke pehle) | 🤖 |
| `banned` | Banned user `/start` karne ki koshish kare | 🚫 |

## Tech Stack

| Cheez | Use |
|---|---|
| [Pyrogram](https://docs.pyrogram.org/) | Telegram MTProto client (bot framework) |
| [TgCrypto](https://github.com/pyrogram/tgcrypto) | Pyrogram ke liye fast encryption |
| [Motor](https://motor.readthedocs.io/) | Async MongoDB driver |
| MongoDB | Settings + tokens + users store karne ke liye |
| python-dotenv | `.env` se config load karne ke liye |

## Setup

**1. Repo clone karo**
```bash
git clone https://github.com/Kissu-TheDev/CloudBot.git
cd CloudBot
```

**2. Dependencies install karo**
```bash
pip install -r requirements.txt
```

**3. `.env` file banao**
```bash
cp .env.example .env
```

| Variable | Kahan se milega | Zaroori? |
|---|---|---|
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) se naya bot banake | Haan |
| `API_ID` | [my.telegram.org](https://my.telegram.org) | Haan |
| `API_HASH` | [my.telegram.org](https://my.telegram.org) | Haan |
| `MONGODB_URL` | [MongoDB Atlas](https://www.mongodb.com/atlas) connection string | Haan |
| `ADMIN_ID` | Apni Telegram numeric user ID (e.g. [@userinfobot](https://t.me/userinfobot) se) | Haan |
| `DB_CHANNEL_ID` | Jis channel mein files store karoge uski ID | Optional (`/admin` se bhi set ho sakti hai) |

**4. Bot run karo**
```bash
python bot.py
```
Terminal mein `KissuCloudBot is alive!` dikhega — bot chalu ho gaya.

## Use Kaise Karein (Admin)

1. Bot ko apne DB channel mein **admin** banao.
2. Apne account se `/start` karo → seedha admin panel ka button milega.
3. `/admin` se panel kabhi bhi khol sakte ho.

## Jaani-Maani Limitations (Honest Disclosure)

- **FSUB fail-closed hai** — agar bot kabhi FSUB channel ka admin nahi raha, sab users block ho jaayenge. `/admin` → Debug se check karte raho.
- **Rate-limit aur ban-check in-memory + DB mix hai** — cooldown timers in-memory hain (bot restart hote hi active cooldowns clear ho jaayenge, harmless hai), lekin bans aur admin-list MongoDB mein persist hote hain.
- **Wizard state in-memory hai** — agar bot Generate Token wizard ke beech restart ho jaaye, wo progress discard ho jaata hai.
- **Token `used_count` mein ek chhota race-condition window hai** — agar ek hi token ko do requests **exact same moment** pe redeem karein, usage-limit ka enforcement thoda loose ho sakta hai (atomic `$inc` hai, lekin limit-check aur increment ke beech gap hai). High-traffic single-token scenarios mein iska dhyan rakhna.
- Ye asli "cloud storage" nahi hai — sab kuch Telegram ke DB channel pe depend karta hai.

## Folder Structure
```
CloudBot/
├── bot.py            # Poora bot logic (single file)
├── requirements.txt  # Python dependencies
└── .env.example      # Config template
```

---

## English (Summary)

CloudBot is a token-gated Telegram file-delivery bot — files live in a private "DB Channel" and are copied to users only against a valid token. It supports per-token usage limits, custom expiry, auto-delete overrides, deep-links, force-subscribe gating (fail-closed), fixed-cooldown per-user rate-limiting, multi-admin support (with a super-admin able to add/remove other admins), a ban system with automatic FSUB-channel kick and JSON export, and a fully button-driven admin panel (`/admin`). See the Hinglish sections above for full setup and usage details — the two versions cover the same content.
