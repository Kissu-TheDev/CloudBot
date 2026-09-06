# KissuDrop

A Telegram bot for **token-based file delivery**. Files are stored in a private Telegram channel (the "DB Channel"); the bot copies them to a user's chat only when the user presents a valid token.

> KissuDrop is not a file-storage service. No file is ever uploaded to or held by the bot itself — files reside in an existing Telegram channel, and the bot's role is limited to verifying a token and copying the associated messages to the requesting user.

---

## 🇮🇳 Hinglish

### Bot Kya Karta Hai

1. Admin apne ek private Telegram channel mein files rakhta hai ("DB Channel").
2. Admin, admin panel ke through un files ke liye ek token generate karta hai (format: `Kissu-<naam>`).
3. Token, user ko manually share kiya jaata hai — bot khud koi shareable link nahi banata (jab tak deep-link explicitly na maanga jaaye).
4. User `/start` karta hai. Agar force-subscribe (FSUB) channel set hai, to pehle join karna zaroori hota hai.
5. User token bhejta hai → bot DB channel se corresponding files copy karke deliver karta hai.
6. Agar auto-delete on hai, to delivered files ek set time ke baad user ke chat se apne aap hat jaati hain.

### Features

- **Token System** — single file, range (`4-8`), ya multiple ranges (`4-8 20-25`) ek hi token mein.
- **Usage Limit** — token ko fix number of redemptions tak limit kiya ja sakta hai.
- **Force Subscribe (FSUB)** — file delivery ek channel join karne se conditional hoti hai. Membership-check fail-closed hai: agar check ke dauraan koi error aaye (jaise bot ka us channel mein admin access na hona), to user ko *not-joined* treat kiya jaata hai, bypass allow nahi hota.
- **Auto-Delete** — delivered files ek configurable delay ke baad khud delete ho jaati hain.
- **Content Protection** — Telegram ke `protect_content` flag se forward/save optionally block kiya ja sakta hai.
- **Custom Messages** — welcome, verified, sending, invalid-token, aur not-joined states admin panel se editable hain, extra follow-up messages ke saath.
- **Broadcast** — sabhi users ko, ya specific channels ko message bhejne ki facility.
- **Export/Import Settings** — poora config JSON ke roop mein backup/restore ho sakta hai.
- **Debug Panel** — total users, active tokens, aur DB/FSUB status ek jagah dikhta hai.
- **Pagination** — bade token batches mein (10-10 files) deliver hote hain, "Next" button ke saath.
- **FloodWait Handling** — Telegram ke rate-limit lagne par bot khud wait karke retry karta hai.

### Admin Panel — Button-Based Workflow

Admin panel ka poora interaction ab primarily inline buttons se hota hai; typing sirf un jagah zaroori hai jahan koi free-form value (file ID, token ka naam, ek custom number ya time) di jaani ho.

**Set Timer** (`/admin` → ⏱ Set Timer)
- Preset buttons: `30m`, `1h`, `6h`, `1d` — tap karte hi seedha default expiry set ho jaati hai.
- `✏️ Custom` — agar in presets se alag koi value chahiye, ye button ek text-prompt kholta hai (format: `1h`, `30m`, `1d`).

**Auto-Delete** (`/admin` → 🗑 Auto-Delete)
- Preset buttons: `10m`, `1h`, `6h`.
- `🔴 Off` — auto-delete band karne ke liye.
- `✏️ Custom` — kisi bhi doosri value ke liye text-prompt.

**Revoke Token** (`/admin` → ❌ Revoke Token)
- Sabhi active (non-revoked, non-expired) tokens ki ek button-list dikhti hai; 10 se zyada hone par pagination (`⏮ Prev` / `Next ⏭`) available hai.
- Token select karne par ek confirmation step aata hai (`✅ Confirm Revoke` / `🔙 Cancel`) taaki accidental tap se koi token galti se revoke na ho.

**Generate Token** (`/admin` → 🔑 Generate Token) — ek 6-step guided wizard:

| Step | Kya poocha jaata hai | Input type |
|---|---|---|
| 1 | File ID(s) / range | Typing (unavoidable) |
| 2 | Token ka naam | Typing (unavoidable) |
| 3 | Usage Limit | `Set Limit` (typing follow karta hai) ya `Skip` |
| 4 | Custom Expiry | `Set Expiry` (typing follow karta hai) ya `Skip` |
| 5 | Auto-Delete override | `Force ON` / `Force OFF` / `Skip` (sab button se) |
| 6 | Deep-link chahiye? | `Yes` / `No` (button se) |

Aakhri step ek summary screen dikhata hai (sab selected values ke saath) jahan se `✅ Generate Token` ya `🔙 Cancel` chuna jaata hai. Kisi bhi step par `🔙 Cancel` poore wizard ko discard kar deta hai.

### Button-Based Panel: Fayde aur Trade-offs

**Fayde:**
- Format yaad rakhne ki zaroorat nahi — pehle marker-syntax (`|L:99|`, `|E:2h|`, `|T|`, `?LINK`) type karna padta tha, ab guided steps se sab set hota hai.
- Typo/format-error ki gunjaish kaafi kam ho jaati hai kyunki zyadatar values button-tap se aati hain, free text se nahi.
- Revoke ke liye token ka naam yaad rakh ke type karna nahi padta — list mein se select kiya ja sakta hai.

**Trade-offs:**
- Ek token generate karne mein pehle ek single-line command se kaam ho jaata tha; ab 6 sequential steps hain, isliye ek token banane mein zyada taps/messages lagte hain.
- Wizard ki state (`pending_action`) in-memory hai (database mein persist nahi hoti). Agar bot process kisi wizard ke beech mein restart ho jaaye, to wo in-progress token generation discard ho jaata hai aur admin ko dobara shuru karna padta hai.
- Power-users jo purana marker-format yaad rakh ke fast bulk-generate karte the, unke liye ye flow dheema lag sakta hai.

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
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) se naya bot banake | Haan |
| `API_ID` | [my.telegram.org](https://my.telegram.org) | Haan |
| `API_HASH` | [my.telegram.org](https://my.telegram.org) | Haan |
| `MONGODB_URL` | [MongoDB Atlas](https://www.mongodb.com/atlas) connection string | Haan |
| `ADMIN_ID` | Apni Telegram numeric user ID (e.g. via [@userinfobot](https://t.me/userinfobot)) | Haan |
| `DB_CHANNEL_ID` | Jis channel mein files store karoge uski ID | Optional (baad mein `/admin` panel se bhi set ho sakti hai) |

**4. Bot ko run karo**
```bash
python bot.py
```

Terminal mein `KissuCloudBot is alive!` dikhega — matlab bot chalu ho gaya.

### Use Kaise Karein (Admin)

1. Bot ko apne DB channel mein **admin** banao (warna wo files copy nahi kar payega).
2. Bot ko `/start` karo apne (admin) account se → seedha admin panel ka button milega.
3. `/admin` se panel kabhi bhi khol sakte ho.

### Jaani-Maani Limitations (Honest Disclosure)

- **FSUB check fail-closed hai**: agar member-check ke dauraan koi error aata hai (jaise bot us channel ka admin nahi hai), to user ko *not-joined* treat kiya jaata hai, bypass nahi hone diya jaata. Trade-off: agar bot galti se FSUB channel ka admin nahi raha, to sab users block ho jayenge. `/admin` → **Debug** panel se FSUB status samay-samay par check karte rehna zaroori hai.
- **Single admin only**: `ADMIN_ID` ek hi ID leta hai — built-in multiple-admin support nahi hai.
- **No rate-limiting on token guesses**: users jitni baar chahein galat token try kar sakte hain, koi cooldown/lockout nahi hai.
- **In-memory wizard state**: Generate Token wizard ki progress bot restart hone par lost ho jaati hai (upar "Trade-offs" section mein detail hai).
- Ye actual "cloud storage" nahi hai — sab kuch Telegram ke DB channel par depend karta hai; wo channel delete/leave hui to delivery break ho jaayegi.

### Folder Structure
```
KissuDrop/
├── bot.py            # Poora bot logic (single file)
├── requirements.txt  # Python dependencies
└── .env.example      # Config template
```

---

## 🇬🇧 English

### What This Bot Does

1. The admin stores files in a private Telegram channel (the "DB Channel").
2. The admin generates a token for those files through the admin panel (format: `Kissu-<name>`).
3. The token is shared with a user manually — the bot does not generate a shareable link on its own unless a deep-link is explicitly requested.
4. The user runs `/start`. If a force-subscribe (FSUB) channel is configured, joining it is required first.
5. The user sends the token, and the bot copies the corresponding files from the DB channel to deliver them.
6. If auto-delete is enabled, delivered files are automatically removed from the user's chat after a set delay.

### Features

- **Token system** — supports single files, ranges (`4-8`), or multiple ranges (`4-8 20-25`) in one token.
- **Usage limits** — caps the number of times a token can be redeemed.
- **Force Subscribe (FSUB)** — gates delivery behind joining a channel. The membership check is fail-closed: if an error occurs during the check (e.g. the bot lacks admin rights in that channel), the user is treated as not joined rather than allowed through.
- **Auto-delete** — delivered files are removed after a configurable delay.
- **Content protection** — Telegram's `protect_content` flag can optionally block forwarding/saving.
- **Custom messages** — welcome, verified, sending, invalid-token, and not-joined states are editable from the admin panel, including extra follow-up messages.
- **Broadcast** — send a message to all users or to specific channels.
- **Export/import settings** — back up or restore the entire configuration as JSON.
- **Debug panel** — shows total users, active tokens, and DB/FSUB status.
- **Pagination** — large token batches are delivered in pages of 10, with a "Next" button.
- **FloodWait handling** — the bot waits and retries automatically when Telegram rate-limits it.

### Admin Panel — Button-Based Workflow

Interaction with the admin panel is now primarily through inline buttons. Typing is required only where a genuinely free-form value is needed: a file ID, a token name, or a custom number/time.

**Set Timer** (`/admin` → Set Timer)
- Preset buttons: `30m`, `1h`, `6h`, `1d` — tapping one sets the default expiry immediately.
- `Custom` — opens a text prompt for any other value (format: `1h`, `30m`, `1d`).

**Auto-Delete** (`/admin` → Auto-Delete)
- Preset buttons: `10m`, `1h`, `6h`.
- `Off` — disables auto-delete.
- `Custom` — text prompt for any other value.

**Revoke Token** (`/admin` → Revoke Token)
- Displays a button list of all active (non-revoked, non-expired) tokens, with pagination (`Prev` / `Next`) when there are more than 10.
- Selecting a token requires confirmation (`Confirm Revoke` / `Cancel`) to reduce the chance of an accidental revocation.

**Generate Token** (`/admin` → Generate Token) — a 6-step guided wizard:

| Step | Prompt | Input type |
|---|---|---|
| 1 | File ID(s) / range | Typed (unavoidable) |
| 2 | Token name | Typed (unavoidable) |
| 3 | Usage limit | `Set Limit` (followed by typed input) or `Skip` |
| 4 | Custom expiry | `Set Expiry` (followed by typed input) or `Skip` |
| 5 | Auto-delete override | `Force ON` / `Force OFF` / `Skip` (all buttons) |
| 6 | Deep-link | `Yes` / `No` (buttons) |

The final step shows a summary of all selected values with `Generate Token` / `Cancel` options. `Cancel` at any step discards the wizard entirely.

### Button-Based Panel: Benefits and Trade-offs

**Benefits:**
- No need to memorize a format — the previous marker syntax (`|L:99|`, `|E:2h|`, `|T|`, `?LINK`) has been replaced with guided, sequential steps.
- Lower chance of typos or malformed input, since most values now come from button taps rather than free text.
- Revoking a token no longer requires remembering and typing its name — it can be selected from a list.

**Trade-offs:**
- Generating a token previously took one line of text; it now takes 6 sequential steps, meaning more taps/messages per token.
- Wizard state (`pending_action`) is held in memory, not persisted to the database. If the bot process restarts mid-wizard, the in-progress token generation is lost and must be restarted.
- Admins who relied on the old marker format for fast bulk generation may find this flow slower.

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
| `BOT_TOKEN` | Create a bot via [@BotFather](https://t.me/BotFather) | Yes |
| `API_ID` | [my.telegram.org](https://my.telegram.org) | Yes |
| `API_HASH` | [my.telegram.org](https://my.telegram.org) | Yes |
| `MONGODB_URL` | [MongoDB Atlas](https://www.mongodb.com/atlas) connection string | Yes |
| `ADMIN_ID` | Your numeric Telegram user ID (e.g. via [@userinfobot](https://t.me/userinfobot)) | Yes |
| `DB_CHANNEL_ID` | ID of the channel you store files in | Optional (can also be set later via the `/admin` panel) |

**4. Run the bot**
```bash
python bot.py
```

The terminal will display `KissuCloudBot is alive!` once the bot is running.

### How to Use (Admin)

1. Make the bot an **admin** in your DB channel — otherwise it cannot copy files from it.
2. Run `/start` with your admin account to get a direct button to open the admin panel.
3. Open the panel at any time with `/admin`.

### Known Limitations (Honest Disclosure)

- **FSUB check is fail-closed**: if an error occurs while checking channel membership (e.g. the bot isn't an admin in that channel), the user is treated as not joined rather than passed through. Trade-off: if the bot loses admin rights in the FSUB channel, every user will be blocked. Check FSUB status periodically via `/admin` → **Debug**.
- **Single admin only**: `ADMIN_ID` accepts a single ID — there is no built-in multi-admin support.
- **No rate-limiting on token guesses**: users can attempt any number of token strings with no cooldown or lockout.
- **In-memory wizard state**: Generate Token wizard progress is lost if the bot restarts mid-flow (see "Trade-offs" above for detail).
- This is not real "cloud storage" — everything depends on the Telegram DB channel; if that channel is deleted or access is lost, delivery breaks.

### Folder Structure
```
KissuDrop/
├── bot.py            # Full bot logic (single file)
├── requirements.txt  # Python dependencies
└── .env.example      # Config template
```
