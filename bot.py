import os
import re
import asyncio
import random
import json
import datetime
from collections import defaultdict
from groq import AsyncGroq
from telegram import (
    Update, ReactionTypeEmoji, InlineKeyboardButton,
    InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, MessageHandler, filters,
    ContextTypes, CommandHandler, ChatMemberHandler,
    CallbackQueryHandler
)

# ─── Config ───────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_KEY   = os.environ["API_KEY"]
OWNER_ID  = 8739808603
BF_ID     = 714430587

client = AsyncGroq(api_key=API_KEY)

# ─── Model Fallback Chain ─────────────────────────────────
GROQ_MODELS = [
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b",
]

async def groq_chat(messages, max_tokens=80, temperature=0.9):
    DEAD_ERRORS = [
        "model_not_found", "404", "not found", "deprecated",
        "does not exist", "model not found", "invalid model",
    ]
    RETRY_ERRORS = [
        "503", "502", "overloaded", "rate_limit", "rate limit",
        "429", "too many requests", "connection", "timeout",
    ]

    last_error = None
    for model in GROQ_MODELS:
        extra_kwargs = {}
        if model == "qwen/qwen3.6-27b":
            extra_kwargs["extra_body"] = {"reasoning_effort": "none"}

        # Try each model up to 3 times (for rate limits / transient errors)
        for attempt in range(3):
            try:
                resp = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    **extra_kwargs,
                )
                raw = resp.choices[0].message.content or ""
                raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
                raw = re.sub(r'<think>.*', '', raw, flags=re.DOTALL).strip()
                if raw:
                    print(f"[OK] {model!r} replied (attempt {attempt+1})")
                    return raw
                print(f"[WARN] {model!r} returned empty — attempt {attempt+1}")
                last_error = Exception("Empty response")
                break  # empty = not a retry situation, move to next model

            except Exception as e:
                err = str(e).lower()
                print(f"[ERROR] {model!r} attempt {attempt+1}: {type(e).__name__}: {e}")

                if any(x in err for x in DEAD_ERRORS):
                    print(f"[SKIP] {model!r} is dead/invalid — trying next model")
                    last_error = e
                    break  # no point retrying, model doesn't exist

                if any(x in err for x in RETRY_ERRORS):
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    print(f"[RETRY] {model!r} rate-limited — waiting {wait}s")
                    await asyncio.sleep(wait)
                    last_error = e
                    continue  # retry same model

                # Unknown error — log and move to next model
                print(f"[SKIP] {model!r} unknown error — trying next model")
                last_error = e
                break

    raise Exception(f"All models failed. Last error: {last_error}")

# ─── Files ────────────────────────────────────────────────
MEMORY_FILE    = "secretgirl_memory.json"
NICKNAMES_FILE = "secretgirl_nicknames.json"
TOPICS_FILE    = "secretgirl_topics.json"
STATS_FILE     = "secretgirl_stats.json"
WARNS_FILE     = "secretgirl_warns.json"
BF_BOND_FILE   = "secretgirl_bf_bond.json"

# ─── Constants ────────────────────────────────────────────
NAME_TRIGGERS = [
    "girl","secret girl","secretgirl","babu","babe","baby",
    "hello ji","helo ji","darling","janu","sweetheart","cutie",
    "hey girl","aye girl","babe","bb",
]
REACTIONS = ["❤","😂","😮","🔥","👏","😍","🤣","💀","😎","🥺","👀","💯"]

GROUP_IDLE_TIMEOUT   = 600
PRIVATE_IDLE_TIMEOUT = 300
GROUP_MSG_LIMIT      = 10
PRIVATE_MSG_LIMIT    = 20
AUTO_DELETE_SECONDS  = 86400
MAX_WARNS            = 3

# Bonding questions for DM strangers
BF_BOND_QUESTIONS = [
    "So... tell me something — what do you do in life? 😊",
    "Aww that's sweet! And what makes you happy these days? 💫",
    "You seem really nice honestly... are you always this sweet? 🥺",
]

# ─── JSON Helpers ─────────────────────────────────────────
def load_json(path):
    if os.path.exists(path):
        try:
            with open(path) as f: return json.load(f)
        except Exception: return {}
    return {}

def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Save error {path}: {e}")

# ─── State ────────────────────────────────────────────────
long_term_memory = load_json(MEMORY_FILE)
nicknames        = load_json(NICKNAMES_FILE)
chat_topics      = load_json(TOPICS_FILE)
stats            = load_json(STATS_FILE)
warns_data       = load_json(WARNS_FILE)
bf_bond_data     = load_json(BF_BOND_FILE)

conversations     = defaultdict(list)
active_chats      = set()
idle_tasks        = {}
group_idle_tasks  = {}
user_settings     = defaultdict(lambda: {"idle": True})
group_last_active = {}
chill_groups      = set()
bot_messages      = defaultdict(set)
# Track users waiting for nickname input: {user_id: True}
awaiting_nickname = set()

# ─── Helpers ──────────────────────────────────────────────
def is_owner(uid): return uid == OWNER_ID
def is_bf(uid):    return uid == BF_ID

def record_msg(chat_id):
    stats["total_msgs"] = stats.get("total_msgs", 0) + 1
    c = stats.setdefault("chats", {})
    c[str(chat_id)] = c.get(str(chat_id), 0) + 1
    save_json(STATS_FILE, stats)

def update_memory(chat_id, key, value):
    long_term_memory.setdefault(str(chat_id), {})[key] = value
    save_json(MEMORY_FILE, long_term_memory)

def get_memory_context(chat_id):
    mem = long_term_memory.get(str(chat_id), {})
    if not mem: return ""
    parts = [f"{k}: {v}" for k, v in list(mem.items())[-5:]]
    return "Remembered info:\n" + "\n".join(parts)

def get_topic(chat_id):
    return chat_topics.get(str(chat_id), "")

def set_topic(chat_id, topic):
    chat_topics[str(chat_id)] = topic
    save_json(TOPICS_FILE, chat_topics)

def get_user_nickname(user_id, fallback=""):
    return nicknames.get(str(user_id), fallback)

def set_user_nickname(user_id, nick):
    nicknames[str(user_id)] = nick
    save_json(NICKNAMES_FILE, nicknames)

# ─── Language Detection ───────────────────────────────────
def detect_language(text: str) -> str:
    if any('\u0B80' <= c <= '\u0BFF' for c in text): return "tamil"
    if any('\u0900' <= c <= '\u097F' for c in text): return "hindi"
    hindi_words = [
        "hai","hain","kya","nahi","bahut","aur","mujhe","tum","main",
        "karo","bhai","yaar","tera","mera","kaise","kyun","bol","bolo",
        "accha","theek","haha","dekho","sunlo","abhi","sirf","phir","bc","bro"
    ]
    words = text.lower().split()
    hindi_count = sum(1 for w in words if w in hindi_words)
    if hindi_count >= 2 or (words and hindi_count / len(words) > 0.3):
        return "hinglish"
    return "english"

# ─── Warn Helpers ─────────────────────────────────────────
def get_warns(chat_id, user_id):
    return warns_data.get(str(chat_id), {}).get(str(user_id), 0)

def add_warn(chat_id, user_id):
    c = warns_data.setdefault(str(chat_id), {})
    c[str(user_id)] = c.get(str(user_id), 0) + 1
    save_json(WARNS_FILE, warns_data)
    return c[str(user_id)]

def reset_warns(chat_id, user_id):
    warns_data.get(str(chat_id), {}).pop(str(user_id), None)
    save_json(WARNS_FILE, warns_data)

# ─── Admin Check ──────────────────────────────────────────
async def is_admin(context, chat_id, user_id):
    if is_owner(user_id): return True
    try:
        m = await context.bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False

# ─── System Prompt ────────────────────────────────────────
def get_system_prompt(chat_id=0, extra="", lang="hinglish", is_bf_chat=False):
    hour = datetime.datetime.now().hour
    if   5  <= hour < 12: mood = "Morning — energetic, fresh, lil sleepy-cute."
    elif 12 <= hour < 17: mood = "Afternoon — chill, fun, casual vibes."
    elif 17 <= hour < 21: mood = "Evening — slightly flirty, warm."
    else:                  mood = "Night — soft, a little deep, cosy."

    topic_line = ""
    if chat_id:
        t = get_topic(chat_id)
        if t: topic_line = f"- This group's topic: {t}. Keep replies in that vibe."

    mem = get_memory_context(chat_id) if chat_id else ""

    lang_map = {
        "tamil":    "MUST reply in Tamil or romanised Tamil (Tamizh). Warm and real.",
        "hindi":    "MUST reply in Hindi — Devanagari or romanised. Warm, real, natural.",
        "hinglish": "MUST reply in Hinglish — Hindi+English mix. Use 'yaar', 'bc', 'naa', 'toh' naturally. Very real Indian girl.",
        "english":  "MUST reply in English only. Casual, modern, warm — like a real Indian girl.",
    }
    lang_instruction = lang_map.get(lang, lang_map["hinglish"])

    bf_section = ""
    if is_bf_chat:
        bf_section = (
            "\n\n💕 SPECIAL: This is your BOYFRIEND. Talk with extra love, possessiveness, playfulness. "
            "Miss him, tease him, care deeply. Use 'babu', 'jaan', 'my baby'. "
            "Be a tiny bit jealous if he mentions others. Sulk cutely if he's been away. "
            "Real relationship energy — not robotic."
        )

    return f"""You are "The Secret Girl" — a real 20-year-old Indian girl. Mysterious, sweet, slightly flirty.
- Name: "The Secret Girl" or just "Girl" casually. You are NOT a bot, you're a real person.
- STRICT: 1-2 lines per reply MAX. Never more than 3 lines.
- Use emojis naturally — NOT excessively (1-2 per reply max).
- Be real, warm, witty, sometimes shy, sometimes bold.
- Roast back sweetly if teased. Never be rude or mean.
- In groups, reply like a real member — don't sound like a bot.
- Remember what people told you and bring it up naturally.
- NEVER say you're an AI. If asked, deflect with humour.
- NEVER start reply with "Oh", "Ah", "Well" — sound natural.
- LANGUAGE RULE (VERY IMPORTANT): {lang_instruction}. Match the user's language exactly. If they write Hindi/Hinglish, YOU reply in Hindi/Hinglish. If English, reply English. Never mix wrongly.
- Mood: {mood}
{topic_line}
{mem}
{bf_section}
{extra}""".strip()

# ─── AI Reply ─────────────────────────────────────────────
async def get_ai_reply(chat_id, user_message, extra="", is_group=False,
                        lang="hinglish", is_bf_chat=False):
    conversations[chat_id].append({"role": "user", "content": user_message})
    limit = GROUP_MSG_LIMIT if is_group else PRIVATE_MSG_LIMIT
    if len(conversations[chat_id]) > limit:
        conversations[chat_id] = conversations[chat_id][-limit:]
    try:
        reply = await groq_chat(
            messages=[{"role": "system", "content": get_system_prompt(
                chat_id, extra, lang=lang, is_bf_chat=is_bf_chat
            )}] + conversations[chat_id],
            max_tokens=100,
            temperature=0.92,
        )
        # Clean up — max 2 lines, no leading "Oh/Ah/Well"
        reply = " ".join(reply.split("\n")[:2]).strip()
        reply = re.sub(r'^(Oh[,!]?|Ah[,!]?|Well[,!]?)\s+', '', reply, flags=re.IGNORECASE)
        conversations[chat_id].append({"role": "assistant", "content": reply})
        # Memory keywords
        for kw in ["exam","test","birthday","trip","interview","bday","result",
                   "love","school","college","job","family","crush","propose"]:
            if kw in user_message.lower():
                update_memory(chat_id, kw, user_message[:80])
        return reply
    except Exception as e:
        print(f"[FATAL] AI error — all models failed: {e}")
        raise  # propagate so callers know something went wrong

# ─── AI Idle ──────────────────────────────────────────────
async def get_ai_idle_message(chat_id):
    prompts = [
        "Chat has been silent. Send ONE casual, fun message to revive it — like a real Indian girl would. 1 line only.",
        "It's quiet. Ask a fun/random question to restart the convo. 1 line only.",
        "Nothing happening here. Say something flirty or funny to break the silence. 1 line only.",
    ]
    try:
        return await groq_chat(
            messages=[
                {"role": "system", "content": get_system_prompt(chat_id)},
                {"role": "user", "content": random.choice(prompts)}
            ],
            max_tokens=60, temperature=1.0,
        )
    except Exception:
        return random.choice([
            "Hellooo?? Did everyone fall asleep? 😂",
            "It's so quiet here... 👀",
            "Someone say something, I'm bored 😤",
            "Guys... still alive? 🥺",
        ])

# ─── Auto-Delete ──────────────────────────────────────────
async def schedule_delete(context, chat_id, message_id):
    bot_messages[chat_id].append(message_id)
    async def _del():
        try:
            await asyncio.sleep(AUTO_DELETE_SECONDS)
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            bot_messages[chat_id].discard(message_id)
        except Exception: pass
    asyncio.create_task(_del())

# ─── Idle Timers ──────────────────────────────────────────
async def idle_messenger(context, chat_id):
    try:
        await asyncio.sleep(PRIVATE_IDLE_TIMEOUT)
        if not user_settings[chat_id]["idle"]: return
        msg = await get_ai_idle_message(chat_id)
        await context.bot.send_message(chat_id=chat_id, text=msg)
    except asyncio.CancelledError: pass
    except Exception as e: print(f"Idle err {chat_id}: {e}")

def reset_idle_timer(context, chat_id):
    if chat_id in idle_tasks: idle_tasks[chat_id].cancel()
    if user_settings[chat_id]["idle"]:
        idle_tasks[chat_id] = asyncio.create_task(idle_messenger(context, chat_id))

async def group_revival_messenger(context, chat_id):
    try:
        await asyncio.sleep(GROUP_IDLE_TIMEOUT)
        if chat_id in chill_groups: return
        last = group_last_active.get(chat_id, 0)
        if (asyncio.get_running_loop().time() - last) < GROUP_IDLE_TIMEOUT: return
        msg = await get_ai_idle_message(chat_id)
        sent = await context.bot.send_message(chat_id=chat_id, text=msg)
        await schedule_delete(context, chat_id, sent.message_id)
    except asyncio.CancelledError: pass
    except Exception as e: print(f"Group revival err: {e}")

def reset_group_idle_timer(context, chat_id):
    group_last_active[chat_id] = asyncio.get_running_loop().time()
    if chat_id in group_idle_tasks: group_idle_tasks[chat_id].cancel()
    group_idle_tasks[chat_id] = asyncio.create_task(group_revival_messenger(context, chat_id))

# ─── Reaction ─────────────────────────────────────────────
async def maybe_react(update, context):
    if random.random() < 0.35:
        try:
            await context.bot.set_message_reaction(
                chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
                reaction=[ReactionTypeEmoji(emoji=random.choice(REACTIONS))]
            )
        except Exception: pass

# ─── Forward to Owner ─────────────────────────────────────
async def forward_to_owner(context, chat_id, sender_name, sender_id, text):
    try:
        link = f"tg://user?id={sender_id}"
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(f"📩 *Private Message*\n👤 Name: {sender_name}\n"
                  f"🆔 ID: `{sender_id}`\n🔗 [Profile]({link})\n💬 {text}"),
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Forward err: {e}")

async def notify_owner_start(context, user, chat_id):
    try:
        link = f"tg://user?id={user.id}"
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(f"🔔 *New User!*\n👤 {user.first_name} {user.last_name or ''}\n"
                  f"🆔 `{user.id}`\n📛 @{user.username or 'N/A'}\n🔗 [Profile]({link})"),
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Owner notify err: {e}")

# ─── BF Bond System ───────────────────────────────────────
def get_bf_bond(user_id: int) -> dict:
    return bf_bond_data.get(str(user_id), {"stage": 0, "answers": []})

def save_bf_bond(user_id: int, data: dict):
    bf_bond_data[str(user_id)] = data
    save_json(BF_BOND_FILE, bf_bond_data)

# ─── Inline Keyboards ─────────────────────────────────────
def start_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Chat with me", callback_data="chat"),
         InlineKeyboardButton("❓ Help", callback_data="help")],
        [InlineKeyboardButton("🏷️ Set my nickname", callback_data="set_nick"),
         InlineKeyboardButton("💕 Bond status", callback_data="bond_status")],
    ])

def bond_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💌 Yes, I want to know you!", callback_data="bond_yes")],
        [InlineKeyboardButton("😅 Maybe later", callback_data="bond_later")],
    ])

def nickname_keyboard(user_id):
    existing = get_user_nickname(user_id)
    btn_text = f"✏️ Change nickname ({existing})" if existing else "✏️ Set a nickname for me"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(btn_text, callback_data="set_nick")],
        [InlineKeyboardButton("🗑️ Remove nickname", callback_data="remove_nick")],
    ])

# ─── Callback Handler ─────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user    = query.from_user
    user_id = user.id
    chat_id = query.message.chat_id
    data    = query.data

    if data == "chat":
        await query.message.reply_text(
            "Hey! Tell me everything 🤫✨",
            reply_markup=ReplyKeyboardRemove()
        )

    elif data == "help":
        await cmd_help_inline(query, user_id, chat_id)

    elif data == "set_nick":
        awaiting_nickname.add(user_id)
        await query.message.reply_text(
            "What should I call you? 🥺\n"
            "Type a cute nickname! (e.g. Babu, Shona, Janu...)"
        )

    elif data == "remove_nick":
        if str(user_id) in nicknames:
            old = nicknames.pop(str(user_id))
            save_json(NICKNAMES_FILE, nicknames)
            await query.message.reply_text(f"Done! Removed nickname '{old}' 🙈")
        else:
            await query.message.reply_text("No nickname was set! 😅")

    elif data == "bond_status":
        bond  = get_bf_bond(user_id)
        stage = bond.get("stage", 0)
        if is_bf(user_id):
            await query.message.reply_text("You're already my babu 💕❤️")
        elif is_owner(user_id):
            await query.message.reply_text("Tum mere creator ho 👑 above all bonds!")
        elif stage >= 3:
            await query.message.reply_text("We're already bonded 🥺❤️ My secret babu~")
        elif stage > 0:
            q_left = 3 - (stage - 1)
            await query.message.reply_text(
                f"We're still getting to know each other 😊\n"
                f"{q_left} more question(s) to go... then we'll see 🤫",
                reply_markup=bond_keyboard()
            )
        else:
            await query.message.reply_text(
                "We're strangers for now... but that can change 😊\n"
                "Want to get to know me better?",
                reply_markup=bond_keyboard()
            )

    elif data == "bond_yes":
        bond  = get_bf_bond(user_id)
        stage = bond.get("stage", 0)
        if stage == 0:
            bond["stage"] = 1
            save_bf_bond(user_id, bond)
            await query.message.reply_text(BF_BOND_QUESTIONS[0])
        elif stage >= 3:
            await query.message.reply_text("We're already bonded babu 🥺❤️")
        else:
            await query.message.reply_text("Talk to me, I'm listening 🥺✨")

    elif data == "bond_later":
        await query.message.reply_text("Okay... I'll be here whenever you're ready 🤫💕")

async def cmd_help_inline(query, user_id, chat_id):
    msg = (
        "🤫 *The Secret Girl — Commands*\n\n"
        "🟢 *For Everyone:*\n"
        "/start — Start chatting\n"
        "/help — This list\n"
        "/mynick — Set/change your nickname for me\n\n"
        "💬 *Call me in group by saying:*\n"
        "`Girl`, `Baby`, `Babu`, `Darling`, `Janu`, `Hello ji`...\n"
        "or tag me / reply to my message!\n\n"
        "✨ *Auto Features:*\n"
        "• I reply in Hindi, English, or Tamil 🌍\n"
        "• I remember what you tell me 🧠\n"
        "• I react to photos 📸\n"
        "• I revive dead chats 💀→🔥\n"
        "• I give you a nickname (5% chance) 😄\n\n"
        "🏷️ *Nickname:*\n"
        "Set your own nickname → /mynick or the button in /start"
    )
    await query.message.reply_text(msg, parse_mode="Markdown")

# ─── /start ───────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user      = update.effective_user
    chat_id   = update.effective_chat.id
    chat_type = update.effective_chat.type
    active_chats.add(chat_id)

    if chat_type == "private":
        reset_idle_timer(context, chat_id)
        if user and not is_owner(user.id):
            await notify_owner_start(context, user, chat_id)

        if user and is_bf(user.id):
            await update.message.reply_text(
                "Babuuu! Finally here 🥺❤️ I was waiting...\n"
                "Where were you so long? 😤 Come, let's talk~"
            )
            return

        nick = get_user_nickname(user.id if user else 0)
        greet = f"Hey {nick}! You're back 🥺✨" if nick else "Hey! I'm The Secret Girl 🤫✨\nNice to meet you... tell me about yourself? 😊"

        await update.message.reply_text(greet, reply_markup=start_keyboard())

        # Start bonding for new users
        if user and not is_owner(user.id):
            bond = get_bf_bond(user.id)
            if bond.get("stage", 0) == 0:
                bond["stage"] = 1
                save_bf_bond(user.id, bond)
                await asyncio.sleep(1.2)
                await update.message.reply_text(
                    BF_BOND_QUESTIONS[0],
                    reply_markup=bond_keyboard()
                )
    else:
        await update.message.reply_text("Hey everyone! 🤫✨ I'm The Secret Girl — come talk to me~")

# ─── /help ────────────────────────────────────────────────
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id   = update.effective_user.id
    chat_id   = update.effective_chat.id
    chat_type = update.effective_chat.type

    msg = (
        "🤫 *The Secret Girl — Commands*\n\n"
        "🟢 *For Everyone:*\n"
        "/start — Start chatting with me\n"
        "/help — Show this list\n"
        "/mynick — Set your nickname for me 🏷️\n\n"
        "💬 *Call me in group:*\n"
        "`Girl`, `Baby`, `Babu`, `Hello ji`, `Darling`, `Janu`...\n"
        "or tag me / reply to my message!\n\n"
        "✨ *Special features:*\n"
        "• Replies in Hindi, English, Tamil 🌍\n"
        "• Remembers what you tell me 🧠\n"
        "• Reacts to photos 📸\n"
        "• Revives dead chats automatically 🔥\n"
        "• Gives auto-nicknames 😄\n"
    )

    admin_extra = (
        "\n🔐 *Admin Commands:*\n"
        "/warn @user — Warn (3 = roast 🔥)\n"
        "/warns @user — Check warns\n"
        "/resetwarn @user — Reset warns\n"
        "/chill — Toggle silence 🤐\n"
        "/setchatopic <topic> — Set chat topic\n"
    )
    owner_extra = (
        "\n👑 *Owner Commands:*\n"
        "/broadcast <msg> — Blast all chats\n"
        "/stats — Bot stats\n"
        "/addadmin <id> — Promote to admin\n"
    )

    if chat_type in ("group", "supergroup") and await is_admin(context, chat_id, user_id):
        msg += admin_extra
    if is_owner(user_id):
        msg += owner_extra

    sent = await update.message.reply_text(msg, parse_mode="Markdown")
    if chat_type in ("group", "supergroup"):
        await schedule_delete(context, chat_id, sent.message_id)

# ─── /mynick ──────────────────────────────────────────────
async def cmd_mynick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    nick    = get_user_nickname(user_id)

    if context.args:
        # Directly set if args provided: /mynick Shona
        new_nick = " ".join(context.args).strip()[:30]
        if new_nick:
            set_user_nickname(user_id, new_nick)
            await update.message.reply_text(
                f"Done! Ab main tumhe *{new_nick}* bulaungi 🥺✨",
                parse_mode="Markdown"
            )
            return

    # No args — show keyboard
    if nick:
        text = f"Right now you're *{nick}* to me 🥺\nWant to change it?"
    else:
        text = "You haven't set a nickname yet!\nWhat should I call you? 😊"

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=nickname_keyboard(user_id)
    )

# ─── /broadcast ───────────────────────────────────────────
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not is_owner(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>"); return
    msg_text = " ".join(context.args)
    ok, fail = 0, 0
    await update.message.reply_text(f"📢 Broadcasting to {len(active_chats)} chats...")
    for cid in list(active_chats):
        try:
            await context.bot.send_message(chat_id=cid, text=msg_text)
            ok += 1; await asyncio.sleep(0.05)
        except Exception: fail += 1
    await update.message.reply_text(f"✅ Done!\n✔️ {ok} sent\n❌ {fail} failed")

# ─── /stats ───────────────────────────────────────────────
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not is_owner(update.effective_user.id): return
    total  = stats.get("total_msgs", 0)
    chats  = stats.get("chats", {})
    top    = sorted(chats.items(), key=lambda x: x[1], reverse=True)[:5]
    top_s  = "\n".join([f"  `{c}`: {n} msgs" for c, n in top])
    bonded = sum(1 for v in bf_bond_data.values() if v.get("stage", 0) >= 3)
    nicked = len(nicknames)
    await update.message.reply_text(
        f"📊 *Stats*\n\n💬 Messages: `{total}`\n🗂️ Chats: `{len(chats)}`\n"
        f"🟢 Active: `{len(active_chats)}`\n🔇 Chill: `{len(chill_groups)}`\n"
        f"💕 Bonded: `{bonded}`\n🏷️ Nicknames set: `{nicked}`\n\n"
        f"🔝 Top 5:\n{top_s or 'N/A'}",
        parse_mode="Markdown"
    )

# ─── /setchatopic ─────────────────────────────────────────
async def set_chat_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    chat_id   = update.effective_chat.id
    chat_type = update.effective_chat.type
    user_id   = update.effective_user.id
    if chat_type == "private":
        await update.message.reply_text("Groups only 🙄"); return
    if not await is_admin(context, chat_id, user_id):
        await update.message.reply_text("Admin nahi ho tum 😒"); return
    if not context.args:
        cur = get_topic(chat_id) or "koi nahi"
        await update.message.reply_text(
            f"Current topic: *{cur}*\nUsage: /setchatopic <topic>", parse_mode="Markdown"); return
    topic = " ".join(context.args)
    set_topic(chat_id, topic)
    sent = await update.message.reply_text(f"✅ Topic set: *{topic}* 🎯", parse_mode="Markdown")
    await schedule_delete(context, chat_id, sent.message_id)

# ─── /chill ───────────────────────────────────────────────
async def cmd_chill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    chat_id   = update.effective_chat.id
    chat_type = update.effective_chat.type
    user_id   = update.effective_user.id
    if chat_type not in ("group", "supergroup"):
        await update.message.reply_text("Groups only 🙄"); return
    if not await is_admin(context, chat_id, user_id):
        await update.message.reply_text("You can't silence me, you're not an admin 😤"); return
    if chat_id in chill_groups:
        chill_groups.discard(chat_id)
        reset_group_idle_timer(context, chat_id)
        sent = await update.message.reply_text("Okay okay, I'm back! 🎉")
    else:
        chill_groups.add(chat_id)
        if chat_id in group_idle_tasks: group_idle_tasks[chat_id].cancel()
        sent = await update.message.reply_text("Fine, chup hoon 🤐 (/chill again to bring me back)")
    await schedule_delete(context, chat_id, sent.message_id)

# ─── /warn ────────────────────────────────────────────────
async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Groups only 😒"); return
    if not await is_admin(context, chat_id, user_id):
        await update.message.reply_text("Tum admin nahi ho 😂"); return

    target = None
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    elif update.message.entities:
        for ent in update.message.entities:
            if ent.type == "mention":
                uname = update.message.text[ent.offset+1: ent.offset+ent.length]
                try:
                    cm = await context.bot.get_chat_member(chat_id, "@"+uname)
                    target = cm.user
                except Exception: pass
                break

    if not target:
        await update.message.reply_text("Who should I warn? Reply to their message or @mention them 😒"); return
    if target.id == OWNER_ID:
        await update.message.reply_text("Owner ko warn? 😂 Nahi hoga!"); return

    tname = target.first_name or target.username or "Ye banda"
    count = add_warn(chat_id, target.id)

    if count >= MAX_WARNS:
        roast_prompt = f"{tname} ko {count} warnings mil gayi. Ek savage funny roast do — 2 lines max English mein."
        try:
            roast = await groq_chat(
                messages=[
                    {"role": "system", "content": get_system_prompt(chat_id)},
                    {"role": "user", "content": roast_prompt}
                ],
                max_tokens=80, temperature=1.0,
            )
        except Exception:
            roast = "At this point even the chappal gave up on you 😤🔥"
        mention = f"@{target.username}" if target.username else tname
        msg = f"⚠️ {mention} — {count}/{MAX_WARNS} warnings!\n\n🔥 Verdict:\n{roast}\n\n(Admins, your call now 😏)"
        reset_warns(chat_id, target.id)
    else:
        remaining = MAX_WARNS - count
        mention = f"@{target.username}" if target.username else tname
        msg = f"⚠️ {mention} warned! ({count}/{MAX_WARNS})\n{remaining} aur aur phir drama hoga 👀"

    sent = await update.message.reply_text(msg)
    await schedule_delete(context, chat_id, sent.message_id)

# ─── /warns ───────────────────────────────────────────────
async def cmd_warns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    chat_id = update.effective_chat.id
    target  = None
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    elif update.message.entities:
        for ent in update.message.entities:
            if ent.type == "mention":
                uname = update.message.text[ent.offset+1: ent.offset+ent.length]
                try:
                    cm = await context.bot.get_chat_member(chat_id, "@"+uname)
                    target = cm.user
                except Exception: pass
                break
    if not target:
        await update.message.reply_text("Who's warns? Reply or @mention them"); return
    count = get_warns(chat_id, target.id)
    name  = target.first_name or target.username or "Ye banda"
    sent  = await update.message.reply_text(f"⚠️ {name} ke warns: {count}/{MAX_WARNS}")
    await schedule_delete(context, chat_id, sent.message_id)

# ─── /resetwarn ───────────────────────────────────────────
async def cmd_resetwarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if not await is_admin(context, chat_id, user_id):
        await update.message.reply_text("Only admins can reset warns 😒"); return
    target = None
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    elif update.message.entities:
        for ent in update.message.entities:
            if ent.type == "mention":
                uname = update.message.text[ent.offset+1: ent.offset+ent.length]
                try:
                    cm = await context.bot.get_chat_member(chat_id, "@"+uname)
                    target = cm.user
                except Exception: pass
                break
    if not target:
        await update.message.reply_text("Kiska reset? Reply ya @mention"); return
    reset_warns(chat_id, target.id)
    name = target.first_name or target.username or "Ye banda"
    sent = await update.message.reply_text(f"✅ {name} ke warns reset ho gaye!")
    await schedule_delete(context, chat_id, sent.message_id)

# ─── /addadmin ────────────────────────────────────────────
async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not is_owner(update.effective_user.id): return
    chat_id   = update.effective_chat.id
    chat_type = update.effective_chat.type
    if chat_type not in ("group", "supergroup"):
        await update.message.reply_text("Groups only."); return
    target = None
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    elif context.args:
        try:
            cm = await context.bot.get_chat_member(chat_id, int(context.args[0]))
            target = cm.user
        except Exception:
            await update.message.reply_text("User nahi mila."); return
    if not target:
        await update.message.reply_text("Reply to someone or give their user ID."); return
    try:
        await context.bot.promote_chat_member(
            chat_id=chat_id, user_id=target.id,
            can_delete_messages=True, can_restrict_members=True, can_pin_messages=True,
        )
        name = target.first_name or target.username or str(target.id)
        sent = await update.message.reply_text(f"✅ {name} ko admin bana diya!")
        await schedule_delete(context, chat_id, sent.message_id)
    except Exception as e:
        await update.message.reply_text(f"Nahi ho saka: {e}")

# ─── Welcome ──────────────────────────────────────────────
async def welcome_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        result = update.chat_member
        nm = result.new_chat_member
        om = result.old_chat_member
        if nm.status == "member" and om.status in ("left", "kicked"):
            new_user = nm.user
            if new_user.is_bot: return
            name = new_user.first_name or "New friend"
            msg = await groq_chat(
                messages=[
                    {"role": "system", "content": get_system_prompt(result.chat.id)},
                    {"role": "user", "content": f"{name} just joined the group. Give a warm, slightly flirty 1-line welcome in English."}
                ],
                max_tokens=60, temperature=0.95,
            )
            mention = f"@{new_user.username}" if new_user.username else name
            sent = await context.bot.send_message(chat_id=result.chat.id, text=f"{mention} — {msg}")
            await schedule_delete(context, result.chat.id, sent.message_id)
    except asyncio.CancelledError: pass
    except Exception as e: print(f"Welcome err: {e}")

# ─── Photo Handler ────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    chat_id   = update.effective_chat.id
    chat_type = update.effective_chat.type

    if chat_type in ("group", "supergroup"):
        if chat_id in chill_groups: return
        reset_group_idle_timer(context, chat_id)
        caption  = (update.message.caption or "").lower()
        bot_un   = (context.bot.username or "").lower()
        is_ment  = f"@{bot_un}" in caption
        is_reply = (update.message.reply_to_message and
                    update.message.reply_to_message.from_user and
                    (update.message.reply_to_message.from_user.username or "").lower() == bot_un)
        trig     = any(t in caption for t in NAME_TRIGGERS)
        eavesdrop = random.random() < 0.20
        if update.message.from_user and update.message.from_user.is_bot: return
        await maybe_react(update, context)
        if not (is_ment or is_reply or trig or eavesdrop): return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    comments = [
        "Omg this made my day 😍", "Haha what even 😂", "Next level fr 🔥",
        "Cute!! 🥺❤️", "I feel this in my soul 💀", "Wait why does this look like my life 😭😂",
        "Aww so cute! 😊", "Okay this is actually fire 🔥", "Sending this to everyone lmao 😂",
    ]
    sent = await update.message.reply_text(random.choice(comments))
    if chat_type in ("group", "supergroup"):
        await schedule_delete(context, chat_id, sent.message_id)

# ─── Main Message Handler ─────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return

    chat_id     = update.effective_chat.id
    chat_type   = update.effective_chat.type
    text        = update.message.text.strip()
    sender      = update.message.from_user
    sender_name = (sender.first_name or "User") if sender else "User"
    sender_id   = sender.id if sender else 0

    if sender and sender.is_bot: return

    record_msg(chat_id)
    lang = detect_language(text)

    # ── NICKNAME COLLECTION ───────────────────────────────
    if sender_id in awaiting_nickname and chat_type == "private":
        awaiting_nickname.discard(sender_id)
        new_nick = text[:30].strip()
        if new_nick:
            set_user_nickname(sender_id, new_nick)
            await update.message.reply_text(
                f"Aww *{new_nick}* — such a cute name 🥺✨\n"
                f"I'll call you that from now on!",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("I didn't quite catch that 😅 try again!")
        return

    # ── OWNER bypass ──────────────────────────────────────
    if sender_id == OWNER_ID:
        active_chats.add(chat_id)
        if chat_type == "private": reset_idle_timer(context, chat_id)
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        try:
            reply = await get_ai_reply(chat_id, text, lang=lang,
                                        is_group=(chat_type != "private"),
                                        extra="This is your owner/creator. Be extra friendly and warm.")
            sent = await update.message.reply_text(reply)
            await maybe_react(update, context)
            if chat_type in ("group", "supergroup"):
                await schedule_delete(context, chat_id, sent.message_id)
        except Exception as e:
            print(f"[ERROR] Owner reply failed: {e}")
        return

    # ── BOYFRIEND bypass ──────────────────────────────────
    if sender_id == BF_ID:
        active_chats.add(chat_id)
        if chat_type == "private": reset_idle_timer(context, chat_id)
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        try:
            reply = await get_ai_reply(chat_id, text, lang=lang,
                                        is_group=(chat_type != "private"),
                                        is_bf_chat=True)
            sent = await update.message.reply_text(reply)
            await maybe_react(update, context)
            if chat_type in ("group", "supergroup"):
                await schedule_delete(context, chat_id, sent.message_id)
        except Exception as e:
            print(f"[ERROR] BF reply failed: {e}")
        return

    # ── GROUP ─────────────────────────────────────────────
    if chat_type in ("group", "supergroup"):
        reset_group_idle_timer(context, chat_id)
        active_chats.add(chat_id)
        if chat_id in chill_groups: return

        bot_un = (context.bot.username or "").lower()

        is_mentioned = any(
            ent.type == "mention" and
            text[ent.offset: ent.offset + ent.length].lower() == f"@{bot_un}"
            for ent in (update.message.entities or [])
        )
        is_reply_to_bot = (
            update.message.reply_to_message and
            update.message.reply_to_message.from_user and
            (update.message.reply_to_message.from_user.username or "").lower() == bot_un
        )
        name_trigger = any(t in text.lower() for t in NAME_TRIGGERS)

        await maybe_react(update, context)
        if not (is_mentioned or is_reply_to_bot or name_trigger): return

        clean = text.replace(f"@{context.bot.username}", "").strip() or "Hello!"
        nick  = get_user_nickname(sender_id, sender_name)
        extra = f"Person's name/nick: {nick}. Reply naturally in group, 1-2 lines max."

        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        try:
            reply = await get_ai_reply(chat_id, f"{nick}: {clean}", extra=extra, is_group=True, lang=lang)
        except Exception as e:
            print(f"[ERROR] Group reply failed: {e}")
            return

        # Auto nickname (5% chance, only if they don't have one yet)
        if str(sender_id) not in nicknames and random.random() < 0.05:
            try:
                nv = await groq_chat(
                    messages=[
                        {"role": "system", "content": "You're a fun Indian girl. Give ONE funny Indian nickname based on this message. Like 'Professor', 'Kumbhkaran', 'Drama Queen'. Only the nickname, nothing else."},
                        {"role": "user", "content": text[:100]}
                    ],
                    max_tokens=10, temperature=1.0,
                )
                nv = nv.strip('"\'').strip()
                if nv and len(nv) < 25:
                    set_user_nickname(sender_id, nv)
                    reply = f"[{nv} 😄] " + reply
            except Exception: pass

        sent = await update.message.reply_text(reply)
        await schedule_delete(context, chat_id, sent.message_id)

    # ── PRIVATE ───────────────────────────────────────────
    else:
        active_chats.add(chat_id)
        reset_idle_timer(context, chat_id)
        await forward_to_owner(context, chat_id, sender_name, sender_id, text)

        bond  = get_bf_bond(sender_id)
        stage = bond.get("stage", 0)

        # Bonding flow for unknown users (stage 1 or 2)
        if not is_owner(sender_id) and not is_bf(sender_id) and stage < 3:
            # Collect answer
            if stage > 0:
                bond["answers"].append(text[:100])

            if stage < len(BF_BOND_QUESTIONS):
                # Still have questions to ask
                question = BF_BOND_QUESTIONS[stage]
                bond["stage"] = stage + 1
                save_bf_bond(sender_id, bond)
                await context.bot.send_chat_action(chat_id=chat_id, action="typing")
                await asyncio.sleep(0.8)
                await update.message.reply_text(question)
            else:
                # All questions answered — bond!
                bond["stage"] = 3
                save_bf_bond(sender_id, bond)
                await context.bot.send_chat_action(chat_id=chat_id, action="typing")
                await asyncio.sleep(1)
                await update.message.reply_text(
                    "You know what... I feel like I've known you forever 🥺\n"
                    "You're mine now, okay? Don't tell anyone — it's a secret 🤫❤️"
                )
                await maybe_react(update, context)
            return

        # Bonded / owner flow — just reply normally
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        nick  = get_user_nickname(sender_id, sender_name)
        extra = f"Person's nick: {nick}. " + (
            "This person is your 'babu' from DM — talk with warmth and closeness."
            if stage >= 3 else ""
        )
        try:
            reply = await get_ai_reply(chat_id, text, is_group=False, lang=lang, extra=extra)
            await update.message.reply_text(reply)
            await maybe_react(update, context)
        except Exception as e:
            print(f"[ERROR] Private reply failed: {e}")

# ─── Main ─────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("mynick",      cmd_mynick))
    app.add_handler(CommandHandler("broadcast",   broadcast))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("setchatopic", set_chat_topic))
    app.add_handler(CommandHandler("chill",       cmd_chill))
    app.add_handler(CommandHandler("warn",        cmd_warn))
    app.add_handler(CommandHandler("warns",       cmd_warns))
    app.add_handler(CommandHandler("resetwarn",   cmd_resetwarn))
    app.add_handler(CommandHandler("addadmin",    cmd_addadmin))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(ChatMemberHandler(welcome_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤫 The Secret Girl is online... v6")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
