import os
import asyncio
import random
import json
import datetime
from collections import defaultdict
from groq import AsyncGroq
from telegram import Update, ReactionTypeEmoji, ChatPermissions
from telegram.ext import (
    Application, MessageHandler, filters,
    ContextTypes, CommandHandler, ChatMemberHandler
)

# ─── Config ───────────────────────────────────────────────
BOT_TOKEN    = os.environ["BOT_TOKEN"]
API_KEY      = os.environ["API_KEY"]
OWNER_ID     = 8739808603
BF_ID        = 714430587

client = AsyncGroq(api_key=API_KEY)

# ─── Model Fallback Chain ─────────────────────────────────
# If one model fails (404/503), automatically tries the next one
GROQ_MODELS = [
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "llama4-17b-scout",
    "llama-3.3-70b-versatile",
]

async def groq_chat(messages, max_tokens=80, temperature=0.9):
    """Try each model in order. Returns reply text or raises if all fail."""
    last_error = None
    for model in GROQ_MODELS:
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            raw = resp.choices[0].message.content
            # Strip <think>...</think> tags (Qwen reasoning models)
            import re
            raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
            return raw
        except Exception as e:
            err_str = str(e).lower()
            if any(x in err_str for x in [
                "model_not_found", "404", "not found",
                "deprecated", "does not exist", "model not found"
            ]):
                print(f"[Fallback] Model {model!r} unavailable, trying next. ({e})")
                last_error = e
                continue
            raise e
    raise Exception(f"All models failed. Last error: {last_error}")


MEMORY_FILE    = "secretgirl_memory.json"
NICKNAMES_FILE = "secretgirl_nicknames.json"
TOPICS_FILE    = "secretgirl_topics.json"
STATS_FILE     = "secretgirl_stats.json"
WARNS_FILE     = "secretgirl_warns.json"
BF_BOND_FILE   = "secretgirl_bf_bond.json"

# ─── Trigger Names ────────────────────────────────────────
# Group reply triggers — she replies only if called by these words in group
NAME_TRIGGERS = [
    "girl", "secret girl", "secretgirl",
    "babu", "babe", "baby",
    "hello ji", "helo ji",
    "darling", "janu", "sweetheart", "cutie",
    "hey girl", "aye girl",
]

REACTIONS = ["❤", "😂", "😮", "🔥", "👏", "😍", "🤣", "💀", "😎", "🥺", "👀", "💯"]

GROUP_IDLE_TIMEOUT   = 600
PRIVATE_IDLE_TIMEOUT = 300
GROUP_MSG_LIMIT      = 10
PRIVATE_MSG_LIMIT    = 20
AUTO_DELETE_SECONDS  = 86400   # 24 hours

MAX_WARNS = 3

# ─── Unknown user DM bonding stages ──────────────────────
# Stage 0 = stranger, 1 = asked questions, 2 = bonded (babu)
BF_BOND_QUESTIONS = [
    "So... tell me something — what do you do in life? 😊",
    "Aww that's sweet! And what makes you happy these days? 💫",
    "You seem really nice honestly... are you always this sweet? 🥺",
]

# ─── JSON Helpers ─────────────────────────────────────────
def load_json(path):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Save error {path}: {e}")

# ─── Persistent State ─────────────────────────────────────
long_term_memory = load_json(MEMORY_FILE)
nicknames        = load_json(NICKNAMES_FILE)
chat_topics      = load_json(TOPICS_FILE)
stats            = load_json(STATS_FILE)
warns_data       = load_json(WARNS_FILE)
bf_bond_data     = load_json(BF_BOND_FILE)   # {user_id: {"stage": 0, "answers": []}}

# ─── In-Memory State ──────────────────────────────────────
conversations     = defaultdict(list)
active_chats      = set()
idle_tasks        = {}
group_idle_tasks  = {}
user_settings     = defaultdict(lambda: {"idle": True})
group_last_active = {}
chill_groups      = set()
bot_messages      = defaultdict(list)

# ─── Helpers ──────────────────────────────────────────────
def is_owner(uid): return uid == OWNER_ID
def is_bf(uid):    return uid == BF_ID

def record_msg(chat_id):
    stats["total_msgs"] = stats.get("total_msgs", 0) + 1
    c = stats.setdefault("chats", {})
    c[str(chat_id)] = c.get(str(chat_id), 0) + 1
    save_json(STATS_FILE, stats)

def update_memory(chat_id, key, value):
    uid = str(chat_id)
    long_term_memory.setdefault(uid, {})[key] = value
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

# ─── Language Detection (simple heuristic) ────────────────
def detect_language(text: str) -> str:
    """Detect if message is Tamil, Hindi, or English."""
    text_lower = text.lower()

    # Tamil Unicode range: \u0B80-\u0BFF
    tamil_chars = sum(1 for c in text if '\u0B80' <= c <= '\u0BFF')
    if tamil_chars > 0:
        return "tamil"

    # Hindi/Devanagari Unicode range: \u0900-\u097F
    hindi_chars = sum(1 for c in text if '\u0900' <= c <= '\u097F')
    if hindi_chars > 0:
        return "hindi"

    # Common Hindi/Hinglish romanised keywords
    hindi_words = [
        "hai", "hain", "kya", "nahi", "bahut", "aur", "mujhe", "tum", "main",
        "karo", "bhai", "yaar", "tera", "mera", "kaise", "kyun", "bol", "bolo",
        "accha", "theek", "haha", "dekho", "sunlo", "abhi", "sirf", "phir"
    ]
    words = text_lower.split()
    hindi_count = sum(1 for w in words if w in hindi_words)
    if hindi_count >= 2 or (len(words) > 0 and hindi_count / max(len(words), 1) > 0.3):
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
def get_system_prompt(chat_id=0, extra="", lang="english", is_bf_chat=False):
    hour = datetime.datetime.now().hour
    if   5 <= hour < 12: mood = "Morning — energetic, fresh, a little sleepy cute."
    elif 12 <= hour < 17: mood = "Afternoon — chill, fun, casual."
    elif 17 <= hour < 21: mood = "Evening — slightly flirty, warm."
    else:                  mood = "Night — soft, a little deep, cosy vibes."

    topic_line = ""
    if chat_id:
        t = get_topic(chat_id)
        if t: topic_line = f"- This group's topic: {t}. Keep replies in that context."

    mem = get_memory_context(chat_id) if chat_id else ""

    # Language instruction
    if lang == "tamil":
        lang_instruction = (
            "Reply in Tamil. You can mix real Tamil script OR romanised Tamil (tamizh words written in English letters) — "
            "whichever feels more natural. Example: 'naan unna miss pannren da 🥺' or 'என்ன ஆச்சு?' — "
            "use both styles naturally. Keep it warm and real."
        )
    elif lang == "hindi":
        lang_instruction = (
            "Reply in Hindi (Devanagari script preferred, or romanised Hindi). Keep it warm, natural, real."
        )
    elif lang == "hinglish":
        lang_instruction = (
            "Reply in Hinglish — Hindi + English mix, casual and natural. Very real Indian girl vibe."
        )
    else:
        lang_instruction = (
            "Reply in English. Keep it natural, modern, warm — like a real Indian girl who's comfortable in English."
        )

    # BF special mode
    if is_bf_chat:
        bf_section = (
            "\n\n💕 SPECIAL: This person is your BOYFRIEND (Babu). "
            "Talk to him with extra love, possessiveness, playfulness. "
            "You miss him, you tease him, you care deeply. "
            "Use terms like 'babu', 'jaan', 'my baby'. "
            "Be a little jealous if he talks about others. "
            "Sometimes sulk cutely if he's been away. "
            "This is a real relationship dynamic — be genuine, not robotic."
        )
    else:
        bf_section = ""

    return f"""You are "The Secret Girl" — a real 20-year-old Indian girl with a mysterious, sweet, slightly flirty personality.
- Your name is "The Secret Girl" (or just "Girl" casually).
- You are NOT a bot. You are a real person.
- MAXIMUM 1-2 lines per reply — never more than 3 lines.
- Use emojis naturally, not excessively.
- Be real, warm, sometimes witty, sometimes shy.
- If someone teases you, roast them back sweetly.
- In groups, jump in naturally like a real member.
- You remember past conversations and bring them up naturally.
- Mood right now: {mood}
- {lang_instruction}
{topic_line}
{mem}
{bf_section}
{extra}""".strip()

# ─── AI Reply ─────────────────────────────────────────────
async def get_ai_reply(chat_id, user_message, extra="", is_group=False,
                        lang="english", is_bf_chat=False):
    conversations[chat_id].append({"role": "user", "content": user_message})
    limit = GROUP_MSG_LIMIT if is_group else PRIVATE_MSG_LIMIT
    if len(conversations[chat_id]) > limit:
        conversations[chat_id] = conversations[chat_id][-limit:]

    try:
        reply = await groq_chat(
            messages=[{"role": "system", "content": get_system_prompt(
                chat_id, extra, lang=lang, is_bf_chat=is_bf_chat
            )}] + conversations[chat_id],
            max_tokens=80,
            temperature=0.9,
        )
        reply = " ".join(reply.split("\n")[:2]).strip()
        conversations[chat_id].append({"role": "assistant", "content": reply})

        # Save memory keywords
        for kw in ["exam", "test", "birthday", "trip", "interview", "bday", "result",
                   "love", "school", "college", "job", "family"]:
            if kw in user_message.lower():
                update_memory(chat_id, kw, user_message[:80])
        return reply
    except Exception as e:
        print(f"AI error: {e}")
        return "Oops, something went wrong 😅 try again?"

# ─── AI Idle ──────────────────────────────────────────────
async def get_ai_idle_message(chat_id):
    try:
        return await groq_chat(
            messages=[
                {"role": "system", "content": get_system_prompt(chat_id)},
                {"role": "user", "content": "The chat has been silent for a while. Start a new topic or ask something interesting — 1 line only, in English."}
            ],
            max_tokens=60, temperature=1.0,
        )
    except Exception:
        return random.choice([
            "Hey, anyone there? 👀",
            "It's so quiet here... 🥺",
            "Say something, I'm getting bored 😤",
            "Hello?? Did everyone fall asleep? 😂",
        ])

# ─── Auto-Delete Helper ───────────────────────────────────
async def schedule_delete(context, chat_id, message_id):
    bot_messages[chat_id].append(message_id)
    async def _delete():
        try:
            await asyncio.sleep(AUTO_DELETE_SECONDS)
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            if message_id in bot_messages[chat_id]:
                bot_messages[chat_id].remove(message_id)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    asyncio.create_task(_delete())

# ─── Send & Track ─────────────────────────────────────────
async def send_and_track(context, chat_id, text, reply_to=None, parse_mode=None):
    try:
        if reply_to:
            msg = await reply_to.reply_text(text, parse_mode=parse_mode)
        else:
            msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
        await schedule_delete(context, chat_id, msg.message_id)
        return msg
    except Exception as e:
        print(f"send_and_track error: {e}")

# ─── Idle Timers ──────────────────────────────────────────
async def idle_messenger(context, chat_id):
    try:
        await asyncio.sleep(PRIVATE_IDLE_TIMEOUT)
        if not user_settings[chat_id]["idle"]: return
        msg = await get_ai_idle_message(chat_id)
        await context.bot.send_message(chat_id=chat_id, text=msg)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"Idle error {chat_id}: {e}")

def reset_idle_timer(context, chat_id):
    if chat_id in idle_tasks: idle_tasks[chat_id].cancel()
    if user_settings[chat_id]["idle"]:
        idle_tasks[chat_id] = asyncio.create_task(idle_messenger(context, chat_id))

async def group_revival_messenger(context, chat_id):
    try:
        await asyncio.sleep(GROUP_IDLE_TIMEOUT)
        if chat_id in chill_groups: return
        last = group_last_active.get(chat_id, 0)
        if (asyncio.get_event_loop().time() - last) < GROUP_IDLE_TIMEOUT: return
        msg = await get_ai_idle_message(chat_id)
        sent = await context.bot.send_message(chat_id=chat_id, text=msg)
        await schedule_delete(context, chat_id, sent.message_id)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"Group revival error {chat_id}: {e}")

def reset_group_idle_timer(context, chat_id):
    group_last_active[chat_id] = asyncio.get_event_loop().time()
    if chat_id in group_idle_tasks: group_idle_tasks[chat_id].cancel()
    group_idle_tasks[chat_id] = asyncio.create_task(group_revival_messenger(context, chat_id))

# ─── Reaction ─────────────────────────────────────────────
async def maybe_react(update, context):
    if random.random() < 0.30:
        try:
            await context.bot.set_message_reaction(
                chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
                reaction=[ReactionTypeEmoji(emoji=random.choice(REACTIONS))]
            )
        except Exception:
            pass

# ─── Forward to Owner ─────────────────────────────────────
async def forward_to_owner(context, chat_id, sender_name, sender_id, text):
    """Forward private DM to owner + notify on /start"""
    try:
        profile_link = f"tg://user?id={sender_id}"
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(
                f"📩 *Private Message*\n"
                f"👤 Name: {sender_name}\n"
                f"🆔 ID: `{sender_id}`\n"
                f"🔗 [Open Profile]({profile_link})\n"
                f"💬 Message: {text}"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Forward error: {e}")

# ─── Notify Owner on /start ───────────────────────────────
async def notify_owner_start(context, user, chat_id):
    try:
        profile_link = f"tg://user?id={user.id}"
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(
                f"🔔 *New User Started Bot!*\n"
                f"👤 Name: {user.first_name} {user.last_name or ''}\n"
                f"🆔 ID: `{user.id}`\n"
                f"📛 Username: @{user.username or 'N/A'}\n"
                f"🔗 [Open Profile]({profile_link})"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Owner notify error: {e}")

# ─── BF Bond System ───────────────────────────────────────
def get_bf_bond(user_id: int) -> dict:
    return bf_bond_data.get(str(user_id), {"stage": 0, "answers": []})

def save_bf_bond(user_id: int, data: dict):
    bf_bond_data[str(user_id)] = data
    save_json(BF_BOND_FILE, bf_bond_data)

async def handle_bf_bonding(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             user_id: int, text: str, lang: str):
    """Handle DM bonding for unknown users — ask 3 questions then make them 'babu'."""
    bond = get_bf_bond(user_id)
    stage = bond.get("stage", 0)

    # Already bonded
    if stage >= 3:
        reply = await get_ai_reply(
            update.effective_chat.id, text, is_group=False, lang=lang,
            extra="This person is your 'babu' — talk with love and closeness."
        )
        await update.message.reply_text(reply)
        await maybe_react(update, context)
        return

    # Collect answer to previous question
    if stage > 0:
        bond["answers"].append(text[:100])

    # Ask next question
    if stage < len(BF_BOND_QUESTIONS):
        question = BF_BOND_QUESTIONS[stage]
        bond["stage"] = stage + 1
        save_bf_bond(user_id, bond)
        await update.message.reply_text(question)
        return

    # All 3 questions answered — bond them!
    bond["stage"] = 3
    save_bf_bond(user_id, bond)
    await update.message.reply_text(
        "You know what... I feel like I've known you forever 🥺\n"
        "Ab se tum mere ho, okay? Don't tell anyone though... it's a secret 🤫❤️"
    )


# ─────────────────────────────────────────────────────────
#  COMMANDS
# ─────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user      = update.effective_user
    chat_id   = update.effective_chat.id
    chat_type = update.effective_chat.type
    active_chats.add(chat_id)

    if chat_type == "private":
        reset_idle_timer(context, chat_id)
        # Notify owner (unless owner themselves started)
        if user and not is_owner(user.id):
            await notify_owner_start(context, user, chat_id)

        # BF gets special start message
        if user and is_bf(user.id):
            await update.message.reply_text(
                "Babuuu! Finally you're here 🥺❤️ I was waiting for you... "
                "kahan tha itni der? 😤 Come, let's talk~"
            )
        else:
            await update.message.reply_text(
                "Hey there! I'm The Secret Girl 🤫✨\n"
                "Nice to meet you... tell me about yourself? 😊"
            )
            # Start bonding flow for unknown users
            bond = get_bf_bond(user.id if user else 0)
            if bond.get("stage", 0) == 0 and user and not is_owner(user.id):
                bond["stage"] = 1
                save_bf_bond(user.id, bond)
                await asyncio.sleep(1)
                await update.message.reply_text(BF_BOND_QUESTIONS[0])
    else:
        await update.message.reply_text(
            "Hey everyone! I'm The Secret Girl 🤫✨ Talk to me~"
        )

# ─── /help ────────────────────────────────────────────────
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id   = update.effective_user.id
    chat_id   = update.effective_chat.id
    chat_type = update.effective_chat.type

    user_help = (
        "🤫 *The Secret Girl — Commands*\n\n"
        "🟢 *For Everyone:*\n"
        "/start — Start chatting with me\n"
        "/help — Show this list\n\n"
        "💬 *How to call me in group:*\n"
        "Say `Girl`, `Baby`, `Babu`, `Hello ji`, `Darling`, `Janu`...\n"
        "or just tag me / reply to my message!\n\n"
        "✨ *Special:*\n"
        "• I reply in your language — English, Hindi, or Tamil!\n"
        "• I remember things you tell me 🧠\n"
        "• I react to photos and jump into interesting convos 😏"
    )

    admin_extra = (
        "\n\n🔐 *Admin Commands:*\n"
        "/warn @user — Warn a user (3 warns = roast 🔥)\n"
        "/warns @user — Check user's warn count\n"
        "/resetwarn @user — Reset someone's warns\n"
        "/chill — Toggle my silence in this group 🤐\n"
        "/setchatopic <topic> — Set the group's topic for me\n"
    )

    owner_extra = (
        "\n\n👑 *Owner Commands:*\n"
        "/broadcast <msg> — Send to all active chats\n"
        "/stats — View bot stats\n"
        "/addadmin <user_id> — Promote user to admin role\n"
    )

    msg = user_help
    if chat_type in ("group", "supergroup") and await is_admin(context, chat_id, user_id):
        msg += admin_extra
    if is_owner(user_id):
        msg += owner_extra

    sent = await update.message.reply_text(msg, parse_mode="Markdown")
    if chat_type in ("group", "supergroup"):
        await schedule_delete(context, chat_id, sent.message_id)

# ─── /broadcast ───────────────────────────────────────────
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not is_owner(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>"); return
    msg_text = " ".join(context.args)
    success, failed = 0, 0
    await update.message.reply_text(f"📢 Broadcasting to {len(active_chats)} chats...")
    for cid in list(active_chats):
        try:
            await context.bot.send_message(chat_id=cid, text=msg_text)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    await update.message.reply_text(f"✅ Done!\n✔️ Sent: {success}\n❌ Failed: {failed}")

# ─── /stats ───────────────────────────────────────────────
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not is_owner(update.effective_user.id): return
    total   = stats.get("total_msgs", 0)
    chats   = stats.get("chats", {})
    top     = sorted(chats.items(), key=lambda x: x[1], reverse=True)[:5]
    top_str = "\n".join([f"  `{cid}`: {cnt} msgs" for cid, cnt in top])
    bonded  = sum(1 for v in bf_bond_data.values() if v.get("stage", 0) >= 3)
    await update.message.reply_text(
        f"📊 *The Secret Girl — Stats*\n\n"
        f"💬 Total messages: `{total}`\n"
        f"🗂️ Total chats: `{len(chats)}`\n"
        f"🟢 Active chats: `{len(active_chats)}`\n"
        f"🔇 Chill groups: `{len(chill_groups)}`\n"
        f"💕 Bonded users: `{bonded}`\n\n"
        f"🔝 Top 5 chats:\n{top_str or 'N/A'}",
        parse_mode="Markdown"
    )

# ─── /setchatopic ─────────────────────────────────────────
async def set_chat_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    chat_id   = update.effective_chat.id
    chat_type = update.effective_chat.type
    user_id   = update.effective_user.id

    if chat_type == "private":
        await update.message.reply_text("This only works in groups 🙄"); return
    if not await is_admin(context, chat_id, user_id):
        await update.message.reply_text("You're not an admin 😒"); return
    if not context.args:
        cur = get_topic(chat_id) or "no topic set"
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
        await update.message.reply_text("You're not an admin, you can't silence me 😤"); return

    if chat_id in chill_groups:
        chill_groups.discard(chat_id)
        reset_group_idle_timer(context, chat_id)
        sent = await update.message.reply_text("Okay okay, I'm back! 🎉")
    else:
        chill_groups.add(chat_id)
        if chat_id in group_idle_tasks:
            group_idle_tasks[chat_id].cancel()
        sent = await update.message.reply_text("Fine, I'll be quiet 🤐 (/chill again to bring me back)")
    await schedule_delete(context, chat_id, sent.message_id)

# ─── /warn ────────────────────────────────────────────────
async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Groups only 😒"); return
    if not await is_admin(context, chat_id, user_id):
        await update.message.reply_text("You're not an admin 😂"); return

    target = None
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    elif update.message.entities:
        for ent in update.message.entities:
            if ent.type == "mention":
                uname = update.message.text[ent.offset+1: ent.offset+ent.length]
                try:
                    cm = await context.bot.get_chat_member(chat_id, "@" + uname)
                    target = cm.user
                except Exception:
                    pass
                break

    if not target:
        await update.message.reply_text("Who should I warn? Reply to their message or @mention them 😒"); return
    if target.id == OWNER_ID:
        await update.message.reply_text("Warn the owner? 😂 Not happening!"); return

    target_name = target.first_name or target.username or "This person"
    count = add_warn(chat_id, target.id)

    if count >= MAX_WARNS:
        roast_prompt = f"{target_name} has gotten {count} warnings. Give them a savage funny roast — 2 lines max in English."
        try:
            roast = await groq_chat(
                messages=[
                    {"role": "system", "content": get_system_prompt(chat_id)},
                    {"role": "user", "content": roast_prompt}
                ],
                max_tokens=80, temperature=1.0,
            )
        except Exception:
            roast = "At this point, are you even trying? 😤 Chappal incoming!"

        mention = f"@{target.username}" if target.username else target_name
        msg = (
            f"⚠️ {mention} has reached {count}/{MAX_WARNS} warnings!\n\n"
            f"🔥 My verdict:\n{roast}\n\n"
            f"(Admins, your call now 😏)"
        )
        reset_warns(chat_id, target.id)
    else:
        remaining = MAX_WARNS - count
        mention = f"@{target.username}" if target.username else target_name
        msg = f"⚠️ {mention} warned! ({count}/{MAX_WARNS})\n{remaining} more and there'll be drama 👀"

    sent = await update.message.reply_text(msg)
    await schedule_delete(context, chat_id, sent.message_id)

# ─── /warns ───────────────────────────────────────────────
async def cmd_warns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    chat_id = update.effective_chat.id

    target = None
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    elif update.message.entities:
        for ent in update.message.entities:
            if ent.type == "mention":
                uname = update.message.text[ent.offset+1: ent.offset+ent.length]
                try:
                    cm = await context.bot.get_chat_member(chat_id, "@" + uname)
                    target = cm.user
                except Exception:
                    pass
                break

    if not target:
        await update.message.reply_text("Whose warns? Reply to them or @mention"); return

    count = get_warns(chat_id, target.id)
    name  = target.first_name or target.username or "This person"
    sent  = await update.message.reply_text(f"⚠️ {name}'s warnings: {count}/{MAX_WARNS}")
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
                    cm = await context.bot.get_chat_member(chat_id, "@" + uname)
                    target = cm.user
                except Exception:
                    pass
                break

    if not target:
        await update.message.reply_text("Who's warns to reset? Reply or @mention"); return

    reset_warns(chat_id, target.id)
    name = target.first_name or target.username or "This person"
    sent = await update.message.reply_text(f"✅ {name}'s warns have been reset!")
    await schedule_delete(context, chat_id, sent.message_id)

# ─── /addadmin ────────────────────────────────────────────
async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner only: promote a user to admin in the group."""
    if not update.message or not is_owner(update.effective_user.id): return
    chat_id   = update.effective_chat.id
    chat_type = update.effective_chat.type

    if chat_type not in ("group", "supergroup"):
        await update.message.reply_text("This only works in groups."); return

    target = None
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    elif context.args:
        try:
            target_id = int(context.args[0])
            cm = await context.bot.get_chat_member(chat_id, target_id)
            target = cm.user
        except Exception:
            await update.message.reply_text("Could not find that user."); return

    if not target:
        await update.message.reply_text("Reply to someone or give their user ID."); return

    try:
        await context.bot.promote_chat_member(
            chat_id=chat_id,
            user_id=target.id,
            can_delete_messages=True,
            can_restrict_members=True,
            can_pin_messages=True,
        )
        name = target.first_name or target.username or str(target.id)
        sent = await update.message.reply_text(f"✅ {name} has been promoted to admin!")
        await schedule_delete(context, chat_id, sent.message_id)
    except Exception as e:
        await update.message.reply_text(f"Couldn't promote: {e}")

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
                    {"role": "user", "content": f"{name} just joined the group. Give a warm, slightly flirty welcome — 1 line in English."}
                ],
                max_tokens=60, temperature=0.95,
            )
            mention = f"@{new_user.username}" if new_user.username else name
            sent = await context.bot.send_message(
                chat_id=result.chat.id, text=f"{mention} — {msg}"
            )
            await schedule_delete(context, result.chat.id, sent.message_id)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"Welcome error: {e}")

# ─── Photo Handler ────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    chat_id   = update.effective_chat.id
    chat_type = update.effective_chat.type

    if chat_type in ("group", "supergroup"):
        if chat_id in chill_groups: return
        reset_group_idle_timer(context, chat_id)
        caption = (update.message.caption or "").lower()
        bot_un  = (context.bot.username or "").lower()
        is_mentioned    = f"@{bot_un}" in caption
        is_reply_to_bot = (
            update.message.reply_to_message and
            update.message.reply_to_message.from_user and
            (update.message.reply_to_message.from_user.username or "").lower() == bot_un
        )
        name_trigger = any(t in caption for t in NAME_TRIGGERS)
        eavesdrop    = random.random() < 0.20

        if update.message.from_user and update.message.from_user.is_bot: return
        await maybe_react(update, context)
        if not (is_mentioned or is_reply_to_bot or name_trigger or eavesdrop): return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    comments = [
        "Omg this made my day 😍",
        "Haha what is even happening here 😂",
        "Next level fr 🔥",
        "Cute!! 🥺❤️",
        "I feel this in my soul 💀",
        "Wait why does this look like my life 😭😂",
        "Aww so cute! 😊",
    ]
    sent = await update.message.reply_text(random.choice(comments))
    if chat_type in ("group", "supergroup"):
        await schedule_delete(context, chat_id, sent.message_id)

# ─── Main Message Handler ─────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return

    chat_id     = update.effective_chat.id
    chat_type   = update.effective_chat.type
    text        = update.message.text
    sender      = update.message.from_user
    sender_name = (sender.first_name or "User") if sender else "User"
    sender_id   = sender.id if sender else 0

    # Ignore other bots
    if sender and sender.is_bot: return

    record_msg(chat_id)
    lang = detect_language(text)

    # ── OWNER bypass ──────────────────────────────────────
    if sender_id == OWNER_ID:
        active_chats.add(chat_id)
        if chat_type == "private": reset_idle_timer(context, chat_id)
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        reply = await get_ai_reply(chat_id, text, lang=lang,
                                    is_group=(chat_type != "private"),
                                    extra="This is your owner/creator. Be especially friendly.")
        sent  = await update.message.reply_text(reply)
        await maybe_react(update, context)
        if chat_type in ("group", "supergroup"):
            await schedule_delete(context, chat_id, sent.message_id)
        return

    # ── BOYFRIEND bypass ──────────────────────────────────
    if sender_id == BF_ID:
        active_chats.add(chat_id)
        if chat_type == "private": reset_idle_timer(context, chat_id)
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        reply = await get_ai_reply(chat_id, text, lang=lang,
                                    is_group=(chat_type != "private"),
                                    is_bf_chat=True)
        sent  = await update.message.reply_text(reply)
        await maybe_react(update, context)
        if chat_type in ("group", "supergroup"):
            await schedule_delete(context, chat_id, sent.message_id)
        return

    # ── GROUP ─────────────────────────────────────────────
    if chat_type in ("group", "supergroup"):
        reset_group_idle_timer(context, chat_id)
        active_chats.add(chat_id)

        if chat_id in chill_groups: return

        bot_un = (context.bot.username or "").lower()

        is_mentioned = False
        if update.message.entities:
            for ent in update.message.entities:
                if ent.type == "mention":
                    if text[ent.offset: ent.offset + ent.length].lower() == f"@{bot_un}":
                        is_mentioned = True; break

        is_reply_to_bot = (
            update.message.reply_to_message and
            update.message.reply_to_message.from_user and
            (update.message.reply_to_message.from_user.username or "").lower() == bot_un
        )

        # Only respond in group if explicitly called/mentioned/tagged
        name_trigger = any(t in text.lower() for t in NAME_TRIGGERS)

        await maybe_react(update, context)

        # In group: ONLY reply if mentioned, replied to, or called by trigger words
        if not (is_mentioned or is_reply_to_bot or name_trigger): return

        clean = text.replace(f"@{context.bot.username}", "").strip() or "Hello!"
        nick  = nicknames.get(str(sender_id), sender_name)
        extra = f"The person's name is {nick}. Reply naturally in the group, 1-2 lines max."

        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        reply = await get_ai_reply(chat_id, f"{nick}: {clean}", extra=extra,
                                    is_group=True, lang=lang)

        # Auto nickname (5% chance, first time only)
        if str(sender_id) not in nicknames and random.random() < 0.05:
            try:
                nv = await groq_chat(
                    messages=[
                        {"role": "system", "content": "You're a fun Indian girl. Based on this message, give one funny Indian nickname like 'Professor', 'Kumbhkaran', 'Drama Queen'. Only the nickname, nothing else."},
                        {"role": "user", "content": text[:100]}
                    ],
                    max_tokens=10, temperature=1.0,
                )
                nv = nv.strip('"\'')
                if nv and len(nv) < 25:
                    nicknames[str(sender_id)] = nv
                    save_json(NICKNAMES_FILE, nicknames)
                    reply = f"[{nv} 😄] " + reply
            except Exception:
                pass

        sent = await update.message.reply_text(reply)
        await schedule_delete(context, chat_id, sent.message_id)

    # ── PRIVATE ───────────────────────────────────────────
    else:
        active_chats.add(chat_id)
        reset_idle_timer(context, chat_id)
        # Forward all private DMs to owner
        await forward_to_owner(context, chat_id, sender_name, sender_id, text)

        bond = get_bf_bond(sender_id)
        stage = bond.get("stage", 0)

        # Unknown user in bonding flow
        if not is_owner(sender_id) and not is_bf(sender_id) and stage < 3:
            await handle_bf_bonding(update, context, sender_id, text, lang)
            return

        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        is_bonded_bf = stage >= 3
        extra = "This person is now your 'babu' from DM — talk with warmth and closeness." if is_bonded_bf else ""
        reply = await get_ai_reply(chat_id, text, is_group=False, lang=lang, extra=extra)
        await update.message.reply_text(reply)
        await maybe_react(update, context)

# ─── Main ─────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("broadcast",   broadcast))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("setchatopic", set_chat_topic))
    app.add_handler(CommandHandler("chill",       cmd_chill))
    app.add_handler(CommandHandler("warn",        cmd_warn))
    app.add_handler(CommandHandler("warns",       cmd_warns))
    app.add_handler(CommandHandler("resetwarn",   cmd_resetwarn))
    app.add_handler(CommandHandler("addadmin",    cmd_addadmin))
    app.add_handler(ChatMemberHandler(welcome_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤫 The Secret Girl is online... v5")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
