import os
import asyncio
import logging
import json
import time
import re
import importlib.util
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
import httpx
from pyrogram import Client, filters
from pyrogram.enums import ChatAction

try:
    from pymongo import MongoClient
    MONGO_AVAILABLE = True
except ImportError:
    MONGO_AVAILABLE = False

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────────────
API_ID      = int(os.getenv("API_ID", "0"))
API_HASH    = os.getenv("API_HASH", "")
SESSION_NAME = os.getenv("SESSION_NAME", "userbot")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
MONGO_URI   = os.getenv("MONGO_URI", "")
GROQ_API_URL = os.getenv("GROQ_API_URL", "https://api.groq.com/openai/v1/chat/completions")

# ── Model fallback chain (fastest → most capable) ─────────────────────────────
# [FIX] Was only 1 model. Now has 4 fallbacks so rate-limits don't kill the bot.
AVAILABLE_MODELS = [
    "llama-3.1-8b-instant",       # fast, cheap — default
    "llama-3.3-70b-versatile",    # smarter fallback
    "gemma2-9b-it",               # secondary fallback
    "mixtral-8x7b-32768",         # last resort
]
CURRENT_MODEL_INDEX = 0

# ── Memory / DB ────────────────────────────────────────────────────────────────
MEMORY: dict = {}
SCHEDULED_TASKS: dict = {}
DB_CLIENT = None
DB = None

# ── Constants ──────────────────────────────────────────────────────────────────
MEMORY_SHORT_TERM_LIMIT = 20    # [FIX] Was unbounded — now trimmed to last 20 entries
AGENT_MAX_STEPS = 3             # [FIX] Was 2 — increased to allow 2 retries

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════════════

def init_db() -> bool:
    global DB_CLIENT, DB
    if not (MONGO_AVAILABLE and MONGO_URI):
        return False
    try:
        logger.info("Connecting to MongoDB...")
        DB_CLIENT = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        DB_CLIENT.admin.command("ping")
        DB = DB_CLIENT["aura_bot"]
        logger.info("✓ MongoDB connected")
        return True
    except Exception as e:
        err = str(e).lower()
        if "authentication" in err or "bad auth" in err:
            logger.error("❌ MongoDB auth failed — check IP whitelist + credentials")
        else:
            logger.warning(f"MongoDB unavailable: {e} — using file fallback")
        return False


def load_memory():
    global MEMORY
    MEMORY = {}
    if DB is not None:
        try:
            for doc in DB.memory.find():
                cid = doc.get("chat_id")
                if cid is None:
                    continue
                MEMORY[cid] = {
                    "short_term":       doc.get("short_term", []),
                    "task":             doc.get("task", {"goal": "", "steps": []}),
                    "last_tool_result": doc.get("last_tool_result", ""),
                    "created_at":       doc.get("created_at"),
                    "updated_at":       doc.get("updated_at"),
                }
            logger.info(f"Loaded {len(MEMORY)} chats from MongoDB")
            return
        except Exception as e:
            logger.warning(f"MongoDB load failed: {e}")

    try:
        with open("memory.json", "r") as f:
            MEMORY = json.load(f)
        logger.info(f"Loaded {len(MEMORY)} chats from memory.json")
    except Exception:
        logger.info("Starting with empty memory")


def save_memory(chat_id: str):
    entry = MEMORY.setdefault(chat_id, {
        "short_term": [], "task": {"goal": "", "steps": []}, "last_tool_result": ""
    })
    now = datetime.now(tz=timezone.utc).isoformat()
    entry["updated_at"] = now
    entry.setdefault("created_at", now)

    # [FIX] Trim short_term so memory doesn't grow forever
    entry["short_term"] = entry["short_term"][-MEMORY_SHORT_TERM_LIMIT:]

    if DB is not None:
        try:
            DB.memory.update_one(
                {"chat_id": chat_id},
                {"$set": {**entry, "chat_id": chat_id}},
                upsert=True
            )
        except Exception as e:
            logger.warning(f"MongoDB save failed: {e}")

    try:
        with open("memory.json", "w") as f:
            json.dump(MEMORY, f)
    except Exception as e:
        logger.warning(f"memory.json save failed: {e}")


def clear_memory(chat_id: str):
    """Clear memory for a chat (used by .aura clear)."""
    MEMORY.pop(chat_id, None)
    if DB is not None:
        try:
            DB.memory.delete_one({"chat_id": chat_id})
        except Exception:
            pass
    save_memory(chat_id)  # write empty entry


# ═══════════════════════════════════════════════════════════════════════════════
# SYSTEM PROMPT
# ═══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are Aura, a Telegram AI assistant (userbot).

## AGENT MODE (action requested)
Return ONLY valid JSON — no extra text, no markdown:
{
  "thought": "brief reasoning (≤15 words)",
  "action": "tool_name",
  "parameters": { ... },
  "done": false
}
Set done=true when goal is complete.

## AVAILABLE TOOLS
| Tool            | Required params                        |
|-----------------|----------------------------------------|
| send_message    | text                                   |
| send_dm         | user_id, text                          |
| reply           | text                                   |
| edit_message    | message_id, text                       |
| delete_message  | message_id                             |
| forward_message | message_id, target_chat_id             |
| block_user      | user_id                                |
| pin_message     | message_id                             |
| search_message  | query, limit (optional, default 10)    |
| mute_chat       | duration_seconds (0 = unmute)          |

## RULES
- NEVER use tools for explanations — tools are for ACTIONS only
- send_dm: to send DM to another user by their user_id
- reply: send in current chat as reply
- If user_id or message_id is missing, extract from "Entities" in the prompt
- Set done=true on last action

## CHAT MODE (question/info requested)
Respond with clear, helpful text only. Be concise. No JSON.
"""


# ═══════════════════════════════════════════════════════════════════════════════
# DETECTION LOGIC
# ═══════════════════════════════════════════════════════════════════════════════

# [FIX] Expanded with common Hinglish action words
ACTION_VERBS = {
    # English
    "send", "block", "delete", "forward", "search", "edit", "schedule",
    "do", "make", "set", "create", "remove", "pin", "mute", "unmute",
    "reply", "message", "remind", "find", "get",
    # Hinglish
    "karo", "kar de", "kar dena", "bhej", "bhejde", "hatao", "chhupa",
    "dhundho", "laga", "lagao", "band karo", "khol", "likh", "likhde",
    "forward kar", "delete kar", "block kar", "send kar",
}

FAST_KEYWORDS = {
    "hi", "hey", "ok", "okay", "yes", "no", "thanks", "thank",
    "👍", "lol", "haha", "bye", "👋", "cool", "nice", "hm", "hmm",
    "yep", "nope", "sure", "alright", "k", "thik", "theek", "ha",
}

# [FIX] Schedule detection now requires EXPLICIT scheduling intent words.
# Old code ran parse_natural_schedule on EVERY message — "send message at 3pm"
# would be parsed as a schedule instead of an agent task.
SCHEDULE_INTENT_WORDS = {
    "remind", "reminder", "schedule", "schedule karo", "yaad dilana",
    "remind me", "set reminder", "alarm",
}

QUICK_REPLIES = {
    "hi": "Hey! 👋", "hey": "What's up! 👋", "ok": "Got it ✓",
    "okay": "Got it ✓", "yes": "Sure! ✓", "no": "Okay",
    "thanks": "You're welcome! 😊", "thank": "Happy to help! 😊",
    "bye": "See you! 👋", "cool": "Awesome! 😎", "nice": "👍",
    "hm": "Hmm?", "hmm": "Hmm?", "thik": "Theek hai ✓", "theek": "Theek hai ✓",
    "ha": "Haan! ✓",
}


def is_fast_path(text: str) -> bool:
    t = text.lower().strip()
    return any(kw in t for kw in FAST_KEYWORDS) and len(t) < 30


def has_schedule_intent(text: str) -> bool:
    """[FIX] Only return True if user EXPLICITLY wants to schedule something."""
    t = text.lower()
    return any(w in t for w in SCHEDULE_INTENT_WORDS)


def detect_mode(text: str, has_reply: bool) -> str:
    """
    Returns: "fast" | "direct" | "agent"
    [FIX] More precise action verb matching (word boundaries).
    """
    if is_fast_path(text):
        return "fast"

    t = text.lower().strip()

    # Check action verbs with word-boundary awareness
    words = set(re.findall(r"[\w\u0900-\u097f]+", t))
    multi_word = [v for v in ACTION_VERBS if " " in v and v in t]

    if words & ACTION_VERBS or multi_word:
        return "agent"

    if has_reply:
        return "agent"

    return "direct"


# ═══════════════════════════════════════════════════════════════════════════════
# ENTITY EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

def extract_entities(message) -> dict:
    entities = {"user_id": None, "message_id": None, "chat_id": message.chat.id}
    if message.reply_to_message:
        replied = message.reply_to_message
        entities["message_id"] = getattr(replied, "id", None)
        if replied.from_user:
            entities["user_id"] = replied.from_user.id
    elif message.from_user:
        entities["user_id"] = message.from_user.id
    return entities


def get_replied_text(message) -> str | None:
    if not message.reply_to_message:
        return None
    r = message.reply_to_message
    return r.text or r.caption or "<non-text message>"


# ═══════════════════════════════════════════════════════════════════════════════
# VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════

REQUIRED_PARAMS = {
    "send_message":    ["text"],
    "send_dm":         ["user_id", "text"],
    "reply":           ["text"],
    "edit_message":    ["message_id", "text"],
    "delete_message":  ["message_id"],
    "forward_message": ["message_id", "target_chat_id"],
    "block_user":      ["user_id"],
    "pin_message":     ["message_id"],
    "search_message":  ["query"],
    "mute_chat":       ["duration_seconds"],
}


def validate_action(action: str, params: dict) -> tuple[bool, str]:
    required = REQUIRED_PARAMS.get(action)
    if required is None:
        return False, f"unknown action: {action}"
    missing = [p for p in required if not params.get(p) and params.get(p) != 0]
    if missing:
        return False, f"{action}: missing {', '.join(missing)}"
    return True, "ok"


# ═══════════════════════════════════════════════════════════════════════════════
# SCHEDULE PARSING
# ═══════════════════════════════════════════════════════════════════════════════

def parse_natural_schedule(command: str) -> dict | None:
    """Parse scheduling commands. Returns {when: datetime, text: str} or None."""
    command = command.strip()
    if not command:
        return None

    when = None
    try:
        spec = importlib.util.find_spec("dateparser")
        if spec is not None:
            dateparser = importlib.import_module("dateparser")
            when = dateparser.parse(command, settings={
                "PREFER_DATES_FROM": "future",
                "RETURN_AS_TIMEZONE_AWARE": False,
            })
    except Exception:
        pass

    if not when:
        m = re.search(
            r"(?:on\s+)?(\d{1,2}\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*"
            r"(?:\s*\d{2,4})?)\s*(?:at\s*)?((?:\d{1,2}[:.]\d{2})\s*(?:am|pm)?)",
            command, flags=re.I
        )
        if m:
            for fmt in ("%d %b %Y %I:%M%p", "%d %B %Y %I:%M%p",
                        "%d %b %I:%M%p", "%d %B %I:%M%p"):
                try:
                    when = datetime.strptime(f"{m.group(1)} {m.group(2)}", fmt)
                    break
                except Exception:
                    continue

    if not isinstance(when, datetime):
        return None

    if when.tzinfo is not None:
        when = when.astimezone(tz=None).replace(tzinfo=None)
    if when < datetime.utcnow():
        when += timedelta(days=1)

    return {"when": when, "text": command}


def add_scheduled_task(chat_id, scheduled_at: float, task_text: str) -> str:
    task_id = f"{chat_id}_{int(scheduled_at)}_{len(SCHEDULED_TASKS)}"
    SCHEDULED_TASKS[task_id] = {
        "chat_id":      chat_id,
        "scheduled_at": scheduled_at,
        "task":         task_text,
        "created_at":   time.time(),
    }
    logger.info(f"Scheduled task {task_id} at epoch {scheduled_at}")
    return task_id


# ═══════════════════════════════════════════════════════════════════════════════
# GROQ API
# ═══════════════════════════════════════════════════════════════════════════════

async def call_groq(prompt: str, mode: str = "agent") -> str:
    """
    Call Groq with model fallback chain.
    mode: "agent" → expect JSON | "chat" → expect plain text
    """
    global CURRENT_MODEL_INDEX

    if not GROQ_API_KEY:
        if mode == "agent":
            return json.dumps({"thought": "no key", "action": "reply",
                               "parameters": {"text": "Groq API key missing."}, "done": True})
        return "Groq API key missing."

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}

    for attempt in range(CURRENT_MODEL_INDEX, len(AVAILABLE_MODELS)):
        model = AVAILABLE_MODELS[attempt]
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens":  800,
        }

        for retry in range(3):
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    r = await client.post(GROQ_API_URL, json=payload, headers=headers)

                if r.status_code == 429:
                    backoff = (2 ** retry) + 1
                    logger.warning(f"429 on {model}, retry {retry+1}/3 after {backoff}s")
                    await asyncio.sleep(backoff)
                    continue

                r.raise_for_status()
                data = r.json()

                if data.get("choices"):
                    CURRENT_MODEL_INDEX = attempt
                    return data["choices"][0].get("message", {}).get("content", "").strip()
                break

            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                logger.warning(f"{model} HTTP {code}")
                if code == 429 and retry < 2:
                    await asyncio.sleep((2 ** retry) + 1)
                    continue
                break
            except Exception as e:
                logger.warning(f"{model} error: {e}")
                if retry == 2:
                    break
                await asyncio.sleep(1)

        # Try next model
        logger.info(f"Trying next model after {model} failed")

    # All models failed
    if mode == "agent":
        return json.dumps({"thought": "all models failed", "action": "reply",
                           "parameters": {"text": "❌ All AI models unavailable."}, "done": True})
    return "❌ Failed to get response."


# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM TOOL WRAPPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def tg_send(app, chat_id, text: str, reply_to=None):
    try:
        return await app.send_message(chat_id, text, reply_to_message_id=reply_to)
    except Exception as e:
        logger.warning(f"send_message failed: {e}")
        return None


async def tg_send_dm(app, user_id: int, text: str):
    try:
        return await app.send_message(user_id, text)
    except Exception as e:
        logger.warning(f"send_dm to {user_id} failed: {e}")
        return None


async def tg_edit(app, chat_id, message_id: int, text: str):
    try:
        return await app.edit_message_text(chat_id, message_id, text)
    except Exception as e:
        logger.warning(f"edit_message failed: {e}")
        return None


async def tg_delete(app, chat_id, message_id: int):
    try:
        return await app.delete_messages(chat_id, message_id)
    except Exception as e:
        logger.warning(f"delete_message failed: {e}")
        return None


async def tg_forward(app, to_chat, from_chat, message_id: int):
    try:
        return await app.forward_messages(to_chat, from_chat, message_id)
    except Exception as e:
        logger.warning(f"forward_message failed: {e}")
        return None


async def tg_block(app, user_id: int) -> str:
    try:
        await app.block_user(user_id)
        return f"✓ Blocked {user_id}"
    except Exception as e:
        return f"error: {str(e)[:60]}"


async def tg_pin(app, chat_id, message_id: int) -> str:
    try:
        await app.pin_chat_message(chat_id, message_id)
        return f"✓ Pinned message {message_id}"
    except Exception as e:
        return f"error: {str(e)[:60]}"


async def tg_mute(app, chat_id, duration_seconds: int) -> str:
    """Mute current chat for duration_seconds (0 = unmute)."""
    try:
        from pyrogram.types import ChatPermissions
        if duration_seconds == 0:
            perms = ChatPermissions(can_send_messages=True)
            await app.set_chat_permissions(chat_id, perms)
            return "✓ Chat unmuted"
        until = datetime.now() + timedelta(seconds=duration_seconds)
        perms = ChatPermissions(can_send_messages=False)
        await app.set_chat_permissions(chat_id, perms, until_date=until)
        return f"✓ Muted for {duration_seconds}s"
    except Exception as e:
        return f"error: {str(e)[:60]}"


async def tg_search(app, chat_id, query: str, limit: int = 10) -> list[str]:
    results = []
    try:
        keywords = query.lower().split()
        async for m in app.search_messages(chat_id, query=query, limit=limit * 2):
            text = m.text or m.caption or ""
            score = sum(1 for kw in keywords if kw in text.lower())
            score += m.date.timestamp() / 1e10
            results.append((score, f"{m.id}: {text[:60]}"))
        results.sort(reverse=True, key=lambda x: x[0])
    except Exception as e:
        logger.warning(f"search_message failed: {e}")
    return [r[1] for r in results[:limit]]


async def tg_history(app, chat_id, limit: int = 20) -> list[str]:
    messages = []
    try:
        async for m in app.get_chat_history(chat_id, limit=limit):
            if m.text:
                messages.append(m.text)
            elif m.caption:
                messages.append(m.caption)
    except Exception as e:
        logger.warning(f"get_chat_history failed: {e}")
    return messages


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL EXECUTOR
# ═══════════════════════════════════════════════════════════════════════════════

async def execute_tool(app, message, action: str, params: dict) -> str:
    valid, err = validate_action(action, params)
    if not valid:
        return f"error: {err}"

    chat_id = message.chat.id

    try:
        if action == "send_message":
            await tg_send(app, chat_id, params["text"], reply_to=params.get("reply_to_message_id"))
            return "✓ sent"

        elif action == "send_dm":
            uid = int(params["user_id"])
            r = await tg_send_dm(app, uid, params["text"])
            return f"✓ DM sent to {uid}" if r else f"error: DM failed to {uid}"

        elif action == "reply":
            await tg_send(app, chat_id, params["text"], reply_to=message.id)
            return "✓ replied"

        elif action == "edit_message":
            r = await tg_edit(app, chat_id, params["message_id"], params["text"])
            return "✓ edited" if r else "error: cannot edit (not author or too old)"

        elif action == "delete_message":
            r = await tg_delete(app, chat_id, params["message_id"])
            return "✓ deleted" if r is not None else "error: cannot delete (not author or admin)"

        elif action == "forward_message":
            from_chat = params.get("from_chat_id", chat_id)
            await tg_forward(app, params["target_chat_id"], from_chat, params["message_id"])
            return "✓ forwarded"

        elif action == "block_user":
            return await tg_block(app, int(params["user_id"]))

        elif action == "pin_message":
            return await tg_pin(app, chat_id, params["message_id"])

        elif action == "mute_chat":
            return await tg_mute(app, chat_id, int(params["duration_seconds"]))

        elif action == "search_message":
            found = await tg_search(app, chat_id, params["query"],
                                    limit=int(params.get("limit", 10)))
            return "\n".join(found[:3]) if found else "no results"

        else:
            return f"error: unknown action '{action}'"

    except (ValueError, TypeError) as e:
        return f"error: invalid param — {e}"
    except Exception as e:
        return f"error: {str(e)[:60]}"


# ═══════════════════════════════════════════════════════════════════════════════
# DIRECT AI (single-shot, no tools)
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_direct_ai(app, message, goal: str, history: list, replied: str | None):
    """Single AI call for informational queries. No tool loop."""
    hist_str    = " | ".join(history[-5:]) if history else "<empty>"
    replied_str = (replied or "<none>")[:100]

    prompt = (
        f"User: {goal}\n"
        f"Chat history (recent): {hist_str}\n"
        f"Replied-to message: {replied_str}\n\n"
        "Respond helpfully and concisely:"
    )

    response = await call_groq(prompt, mode="chat")
    await tg_send(app, message.chat.id, response or "❌ No response", reply_to=message.id)


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT LOOP
# ═══════════════════════════════════════════════════════════════════════════════

async def agent_loop(app, message, goal: str, history: list, replied: str | None):
    """
    Multi-step agent with tool execution.
    [FIX] Removed "⏳ Processing..." intermediate message — in a userbot,
    sending that message makes it look like YOU sent "Processing..." to the chat,
    which is confusing. Instead we just type silently.
    [FIX] max_steps = 3 (was 2) for 2 retries on error.
    [FIX] Memory context is now injected into every step prompt.
    """
    chat_id  = str(message.chat.id)
    entities = extract_entities(message)

    memory = MEMORY.setdefault(chat_id, {
        "short_term": [], "task": {"goal": goal, "steps": []}, "last_tool_result": ""
    })
    memory["task"]["goal"] = goal

    hist_str    = " | ".join(history[-5:]) if history else "<empty>"
    replied_str = (replied or "<none>")[:100]
    entities_str = f"user_id={entities['user_id']}, msg_id={entities['message_id']}, chat_id={chat_id}"

    # [FIX] Include recent memory context so AI knows what happened before
    mem_context = " | ".join(memory["short_term"][-5:]) or "<none>"

    for step in range(1, AGENT_MAX_STEPS + 1):
        last_result = memory.get("last_tool_result", "")

        if step == 1:
            prompt = (
                f"TASK: {goal}\n"
                f"Entities: {entities_str}\n"
                f"Recent chat: {hist_str}\n"
                f"Replied to: {replied_str}\n"
                f"Memory context: {mem_context}\n\n"
                "Return JSON with ONE action. Set done=true when complete."
            )
        elif last_result.startswith("error:"):
            # Retry on error
            prompt = (
                f"Previous action result: {last_result}\n"
                f"Original task: {goal}\n"
                f"Entities: {entities_str}\n\n"
                "Fix the error and retry. Return JSON with done=true when fixed."
            )
        else:
            # Previous step succeeded and wasn't terminal — shouldn't happen
            # but handle gracefully
            return

        response = await call_groq(prompt, mode="agent")

        # Parse JSON
        try:
            # [FIX] Strip markdown code blocks if model wraps response in ```json
            clean = re.sub(r"```(?:json)?|```", "", response).strip()
            action_obj = json.loads(clean)
        except json.JSONDecodeError:
            logger.warning(f"JSON parse failed, falling back to chat mode. Raw: {response[:120]}")
            fallback = await call_groq(goal, mode="chat")
            await tg_send(app, message.chat.id, fallback or "❌ Failed to parse response",
                          reply_to=message.id)
            return

        action = action_obj.get("action", "reply")
        params = action_obj.get("parameters", {})
        done   = bool(action_obj.get("done", False))

        # Auto-inject entities if AI forgot to include them
        if entities.get("user_id") and action in ("block_user", "send_dm") and not params.get("user_id"):
            params["user_id"] = entities["user_id"]
        if entities.get("message_id") and action in ("delete_message", "forward_message", "pin_message") \
                and not params.get("message_id"):
            params["message_id"] = entities["message_id"]

        memory["task"]["steps"].append({"step": step, "action": action, "params": params})

        result = await execute_tool(app, message, action, params)
        memory["last_tool_result"] = result

        # [FIX] Trim and store in short_term
        memory["short_term"].append(f"step{step}.{action}=>{result[:40]}")

        save_memory(chat_id)
        logger.info(f"[agent] step={step} action={action} result={result[:60]}")

        # Terminal conditions
        if action in ("reply", "send_message", "send_dm") or done:
            return

        if not result.startswith("error:"):
            return  # success, no need to retry

        # result starts with "error:" → loop continues for retry
        if step == AGENT_MAX_STEPS:
            # Out of retries — report failure
            await tg_send(app, message.chat.id,
                          f"❌ Could not complete after {AGENT_MAX_STEPS} attempts.\nLast error: {result}",
                          reply_to=message.id)


# ═══════════════════════════════════════════════════════════════════════════════
# SCHEDULER LOOP (background)
# ═══════════════════════════════════════════════════════════════════════════════

async def scheduler_loop(app):
    while True:
        await asyncio.sleep(5)
        now = time.time()
        done_ids = []
        for tid, task in list(SCHEDULED_TASKS.items()):
            if now >= task.get("scheduled_at", 0):
                try:
                    await tg_send(app, task["chat_id"], f"⏰ Reminder: {task['task']}")
                except Exception as e:
                    logger.warning(f"Scheduled task {tid} failed: {e}")
                done_ids.append(tid)
        for tid in done_ids:
            del SCHEDULED_TASKS[tid]


# ═══════════════════════════════════════════════════════════════════════════════
# BOT STATUS
# ═══════════════════════════════════════════════════════════════════════════════

def build_status_text() -> str:
    model_names = ", ".join(AVAILABLE_MODELS)
    current     = AVAILABLE_MODELS[CURRENT_MODEL_INDEX]
    db_type     = "MongoDB" if (MONGO_AVAILABLE and DB is not None) else "File (memory.json)"
    chat_count  = len(MEMORY)
    task_count  = len(SCHEDULED_TASKS)
    return (
        f"**Aura Status** 🤖\n"
        f"• Model: `{current}` (index {CURRENT_MODEL_INDEX})\n"
        f"• Fallbacks: {len(AVAILABLE_MODELS)} models\n"
        f"• Memory: {db_type} ({chat_count} chats)\n"
        f"• Scheduled tasks: {task_count}\n"
        f"• Models available: `{model_names}`"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

async def main():
    init_db()
    load_memory()

    app = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH)

    @app.on_message(filters.text & filters.me)
    async def handler(_, message):
        raw = (message.text or "").strip()
        if not raw:
            return

        chat_id = str(message.chat.id)

        # ── Built-in commands (.aura ...) ──────────────────────────────────────
        if raw.lower().startswith(".aura"):
            subcmd = raw[5:].strip().lower()

            # [NEW] .aura clear — wipe memory for this chat
            if subcmd == "clear":
                clear_memory(chat_id)
                await tg_send(app, message.chat.id, "🗑️ Memory cleared for this chat.",
                              reply_to=message.id)
                return

            # [NEW] .aura status — show bot status
            if subcmd == "status":
                await tg_send(app, message.chat.id, build_status_text(),
                              reply_to=message.id)
                return

            # .aura <task> — force agent mode
            goal = raw[5:].strip()
            if not goal:
                await tg_send(app, message.chat.id,
                              "Usage: `.aura <task>` | `.aura clear` | `.aura status`",
                              reply_to=message.id)
                return
            force_agent = True
        else:
            goal        = raw
            force_agent = False

        await app.send_chat_action(message.chat.id, ChatAction.TYPING)

        try:
            history = await tg_history(app, message.chat.id, limit=20)
            replied = get_replied_text(message)

            # Mode detection
            if force_agent:
                mode = "agent"
            else:
                mode = detect_mode(goal, message.reply_to_message is not None)

            # [FIX] Schedule check ONLY if user explicitly says "remind/schedule/etc."
            if has_schedule_intent(goal) and mode in ("agent", "direct"):
                sched = parse_natural_schedule(goal)
                if sched:
                    tid = add_scheduled_task(chat_id, sched["when"].timestamp(), sched["text"])
                    when_str = sched["when"].strftime("%d %b %Y %H:%M")
                    await tg_send(app, message.chat.id,
                                  f"✅ Reminder set for {when_str}\n`{tid}`",
                                  reply_to=message.id)
                    return

            # Dispatch
            if mode == "fast":
                t = goal.lower()
                for kw, reply in QUICK_REPLIES.items():
                    if kw in t:
                        await tg_send(app, message.chat.id, reply, reply_to=message.id)
                        break

            elif mode == "direct":
                await handle_direct_ai(app, message, goal, history, replied)

            else:  # agent
                await agent_loop(app, message, goal, history, replied)

        except Exception as e:
            logger.exception("Handler error")
            await tg_send(app, message.chat.id,
                          f"❌ Error: {str(e)[:80]}", reply_to=message.id)

    async with app:
        asyncio.create_task(scheduler_loop(app))
        me = await app.get_me()
        db_label = "MongoDB" if (MONGO_AVAILABLE and DB is not None) else "File"
        print(f"✓ Aura running as: {me.first_name} (@{me.username})")
        print(f"✓ Memory backend: {db_label}")
        print(f"✓ Models: {' → '.join(AVAILABLE_MODELS)}")
        await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n✓ Aura stopped.")