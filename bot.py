import os
import sys
import json
import time
import subprocess
import asyncio
import ast
import re
import tempfile
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.errors import ChatWriteForbidden
from pyrogram.types import Message
import httpx
import logging
from typing import Optional
import hashlib

# Load environment variables
load_dotenv()

# Setup logging
LOG_FILE = os.getenv("LOG_FILE", "userbot.log")
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Configuration
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_NAME = os.getenv("SESSION_NAME", "userbot")
COMMAND_PREFIX = "."
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# Initialize Pyrogram Client
app = Client(
    SESSION_NAME,
    api_id=API_ID,
    api_hash=API_HASH
)

# AFK status storage
afk_status = {
    "is_afk": False,
    "reason": "",
    "start_time": None,
    "replied_to": set(),
    "last_cleanup": datetime.now()
}

# Sudo users storage
sudo_users = set()
SUDO_USERS = os.getenv("SUDO_USERS", "")
if SUDO_USERS:
    for user_id in re.split(r"[\s,;]+", SUDO_USERS.strip()):
        if user_id.isdigit():
            sudo_users.add(int(user_id))

# Conversation history with better structure
conversation_history: dict[int, list[dict[str, str]]] = {}
conversation_summaries: dict[int, dict] = {}  # NEW: Summaries for old conversations
response_cache: dict[str, dict] = {}  # NEW: Cache responses

# Process tracking
running_process: subprocess.Popen | None = None
process_lock = asyncio.Lock()

# Constants
MAX_AFK_REPLIED_TO = 1000
AFK_CLEANUP_INTERVAL = 3600
MAX_CONVERSATION_HISTORY = 8
RESPONSE_CACHE_EXPIRY = 3600  # 1 hour
MAX_CACHE_SIZE = 100
SENTIMENT_KEYWORDS = {  # NEW: Sentiment analysis
    "positive": ["great", "amazing", "love", "excellent", "thanks", "grateful"],
    "negative": ["hate", "terrible", "bad", "awful", "angry", "frustrated"],
}

# IMPROVED: Dynamic system prompts
def get_dynamic_system_prompt(chat_type: str, sentiment: str = "neutral") -> str:
    """Generate context-aware system prompt"""
    base_prompt = """You are Ryuk, a sophisticated AI assistant embedded in Telegram.

Core Traits:
- Speak naturally but intelligently
- Keep responses concise (max 2000 chars) unless detailed answer needed
- Provide code examples when technical questions asked
- Be accurate with facts, admit uncertainty when unsure
- Avoid robotic replies; maintain personality consistency"""
    
    if chat_type == "private":
        base_prompt += "\n- Format: Direct, personal tone suitable for 1-on-1 chat"
    elif chat_type == "group":
        base_prompt += "\n- Format: Clear and inclusive for group discussion"
    
    if sentiment == "positive":
        base_prompt += "\n- Tone: Enthusiastic, encouraging, optimistic"
    elif sentiment == "negative":
        base_prompt += "\n- Tone: Empathetic, understanding, supportive"
    else:
        base_prompt += "\n- Tone: Balanced, informative, objective"
    
    base_prompt += "\n\nRemember: Be helpful, harmless, and honest."
    return base_prompt

# NEW: Sentiment analysis
def analyze_sentiment(text: str) -> str:
    """Simple sentiment analysis"""
    text_lower = text.lower()
    pos_count = sum(1 for word in SENTIMENT_KEYWORDS["positive"] if word in text_lower)
    neg_count = sum(1 for word in SENTIMENT_KEYWORDS["negative"] if word in text_lower)
    
    if pos_count > neg_count:
        return "positive"
    elif neg_count > pos_count:
        return "negative"
    return "neutral"

# NEW: Response caching
def get_cache_key(query: str, chat_id: int) -> str:
    """Generate cache key"""
    return hashlib.md5(f"{query}_{chat_id}".encode()).hexdigest()

def get_cached_response(query: str, chat_id: int) -> Optional[str]:
    """Retrieve cached response if valid"""
    cache_key = get_cache_key(query, chat_id)
    if cache_key in response_cache:
        cached = response_cache[cache_key]
        if time.time() - cached["timestamp"] < RESPONSE_CACHE_EXPIRY:
            logger.info(f"Cache hit for: {query[:30]}...")
            return cached["response"]
        else:
            del response_cache[cache_key]
    return None

def cache_response(query: str, chat_id: int, response: str) -> None:
    """Cache response with timestamp"""
    if len(response_cache) >= MAX_CACHE_SIZE:
        # Remove oldest entry
        oldest_key = min(response_cache.keys(), key=lambda k: response_cache[k]["timestamp"])
        del response_cache[oldest_key]
    
    cache_key = get_cache_key(query, chat_id)
    response_cache[cache_key] = {
        "response": response,
        "timestamp": time.time(),
        "query": query
    }

# NEW: Conversation summary for memory efficiency
def create_conversation_summary(chat_id: int) -> dict:
    """Summarize conversation to free memory"""
    history = conversation_history.get(chat_id, [])
    if not history:
        return {}
    
    # Extract key info
    user_messages = [msg["content"] for msg in history if msg["role"] == "user"]
    topics = []
    for msg in user_messages[-3:]:
        if len(msg) > 5:
            topics.append(msg[:50] + "..." if len(msg) > 50 else msg)
    
    return {
        "last_topics": topics,
        "message_count": len(history),
        "last_updated": datetime.now().isoformat()
    }

# Memory helpers (enhanced)
def save_memory() -> None:
    try:
        # Create conversation summaries for old conversations
        for chat_id in conversation_history:
            if len(conversation_history[chat_id]) > MAX_CONVERSATION_HISTORY:
                if chat_id not in conversation_summaries:
                    conversation_summaries[chat_id] = create_conversation_summary(chat_id)
        
        data = {
            "afk": {
                "is_afk": afk_status["is_afk"],
                "reason": afk_status["reason"],
                "start_time": afk_status["start_time"],
                "replied_to": list(afk_status["replied_to"]),
            },
            "sudo_users": list(sudo_users),
            "conversation_history": {
                str(chat_id): history
                for chat_id, history in conversation_history.items()
            },
            "conversation_summaries": conversation_summaries,
            "cache_stats": {
                "size": len(response_cache),
                "timestamp": time.time()
            }
        }
        with open("memory.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        logger.exception("Failed to save memory")


def load_memory() -> None:
    if not os.path.exists("memory.json"):
        return
    try:
        with open("memory.json", "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return
            data = json.loads(content)
            loaded = data.get("afk", {})
            afk_status["is_afk"] = loaded.get("is_afk", False)
            afk_status["reason"] = loaded.get("reason", "")
            afk_status["start_time"] = loaded.get("start_time", None)
            afk_status["replied_to"] = set(loaded.get("replied_to", []))

            global conversation_history, conversation_summaries
            conversation_history = {
                int(chat_id): messages
                for chat_id, messages in data.get("conversation_history", {}).items()
                if isinstance(messages, list)
            }
            
            conversation_summaries = data.get("conversation_summaries", {})

            global sudo_users
            loaded_sudo = set()
            for user_id in data.get("sudo_users", []):
                if isinstance(user_id, int):
                    loaded_sudo.add(user_id)
                elif isinstance(user_id, str) and user_id.isdigit():
                    loaded_sudo.add(int(user_id))
            sudo_users.update(loaded_sudo)
    except Exception:
        logger.exception("Failed to load memory")


def is_sudo_or_owner(message: Message) -> bool:
    if not message.from_user:
        return False
    if message.from_user.is_self:
        return True
    return message.from_user.id in sudo_users

def authorized_sender(_, __, message: Message) -> bool:
    if not message.from_user:
        return False
    if message.from_user.is_self:
        return True
    return message.from_user.id in sudo_users

AUTHORIZED_SENDER = filters.create(authorized_sender)


def append_conversation_history(chat_id: int, role: str, content: str) -> None:
    history = conversation_history.setdefault(chat_id, [])
    history.append({"role": role, "content": content})
    if len(history) > MAX_CONVERSATION_HISTORY:
        if chat_id not in conversation_summaries:
            conversation_summaries[chat_id] = create_conversation_summary(chat_id)
        del history[:-MAX_CONVERSATION_HISTORY]
    save_memory()


def cleanup_afk_memory() -> None:
    """Cleanup AFK memory to prevent unbounded growth."""
    now = datetime.now()
    
    if (now - afk_status["last_cleanup"]).total_seconds() < AFK_CLEANUP_INTERVAL:
        return
    
    if len(afk_status["replied_to"]) > MAX_AFK_REPLIED_TO:
        replied_list = list(afk_status["replied_to"])
        afk_status["replied_to"] = set(replied_list[len(replied_list)//2:])
        save_memory()
        logger.info("Cleaned up AFK replied_to set (new size: %d)", len(afk_status["replied_to"]))
    
    afk_status["last_cleanup"] = now


# IMPROVED: AI response with better temperature control
async def get_ai_response(
    query: str,
    search_context: str | None = None,
    history: list[dict[str, str]] | None = None,
    temperature: float = 0.6,  # NEW: Configurable temperature
    chat_type: str = "private",  # NEW: Context awareness
) -> str:
    """Use Groq free tier models for generating answers with improved prompt engineering"""
    if not GROQ_API_KEY:
        return "⚠️ **AI Key not configured!**"

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    prompt = query
    if search_context:
        prompt = (
            "Answer based on this search data. Be concise and accurate.\n\n"
            f"Data:\n{search_context}\n\nQuestion: {query}"
        )

    free_models = [
        "llama-3.1-8b-instant",
        "llama-3.3-70b-versatile",
        "mixtral-8x7b-32768",
        "gemma-7b-it",
    ]

    # IMPROVED: Dynamic system prompt based on sentiment and context
    sentiment = analyze_sentiment(query)
    system_prompt = get_dynamic_system_prompt(chat_type, sentiment)

    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    async with httpx.AsyncClient(timeout=30) as client:
        for model in free_models:
            try:
                payload = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,  # NEW: Use configurable temperature
                    "max_tokens": 1024,  # NEW: Limit response length
                }
                response = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    json=payload,
                    headers=headers,
                )
                if response.status_code == 200:
                    result = response.json()
                    if "choices" in result and len(result["choices"]) > 0:
                        choice = result["choices"][0]
                        if "message" in choice and "content" in choice["message"]:
                            return choice["message"]["content"]
                logger.warning("Model %s failed: %s", model, response.status_code)
            except Exception:
                logger.exception("Request failed for model %s", model)

    return "❌ **AI request failed.**"


async def search_web(query: str) -> str:
    """Enhanced web search using DuckDuckGo API"""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            params = {
                "q": query,
                "format": "json",
                "no_html": "1",
                "skip_disambig": "1",
            }
            response = await client.get("https://api.duckduckgo.com/", params=params)
            
            if response.status_code not in {200, 202}:
                logger.error("DuckDuckGo search failed: %s", response.status_code)
                return "❌ **Search failed.**"

            data = response.json()
            results = []
            
            if data.get("Answer"):
                results.append(f"**Answer:** {data['Answer']}")
            
            if data.get("AbstractText"):
                abstract = data["AbstractText"]
                if abstract and abstract not in results:
                    results.append(f"**Summary:** {abstract}")
            
            related = data.get("RelatedTopics", [])
            topic_results = []
            for item in related:
                if isinstance(item, dict):
                    if item.get("Text"):
                        text = item["Text"]
                        if " -- " in text:
                            title, desc = text.split(" -- ", 1)
                            topic_results.append(f"• **{title}:** {desc[:150]}")
                        else:
                            topic_results.append(f"• {text[:150]}")
                    if len(topic_results) >= 5:
                        break
            
            if topic_results:
                results.append("**Related Results:**\n" + "\n".join(topic_results))
            
            if not results:
                results.append("❌ **No results found.**")
            
            return "\n\n".join(results)
    except Exception:
        logger.exception("Web search failed")
        return "❌ **Search failed.**"


def format_code_block(code: str, language: str = "text") -> str:
    """Format code in markdown code block"""
    return f"```{language}\n{code}\n```"


def get_reply_text(message: Message) -> str:
    """Extract text from replied message"""
    if not message.reply_to_message:
        return ""
    
    reply_msg = message.reply_to_message
    text = ""
    
    if reply_msg.text:
        text = reply_msg.text
    elif reply_msg.caption:
        text = reply_msg.caption
    
    return text[:500]


def normalize_python_code(code: str) -> str:
    """Clean and normalize Python code"""
    lines = code.split("\n")
    cleaned = []
    
    for line in lines:
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        cleaned.append(line)
    
    return "\n".join(cleaned)


SUPPORTED_LANGUAGES = {
    "python": {"cmd": [sys.executable], "ext": ".py"},
    "py": {"cmd": [sys.executable], "ext": ".py"},
    "js": {"cmd": ["node"], "ext": ".js"},
    "node": {"cmd": ["node"], "ext": ".js"},
    "bash": {"cmd": ["bash"], "ext": ".sh"},
    "sh": {"cmd": ["bash"], "ext": ".sh"},
    "php": {"cmd": ["php"], "ext": ".php"},
    "ruby": {"cmd": ["ruby"], "ext": ".rb"},
    "rb": {"cmd": ["ruby"], "ext": ".rb"},
    "perl": {"cmd": ["perl"], "ext": ".pl"},
    "pl": {"cmd": ["perl"], "ext": ".pl"},
    "lua": {"cmd": ["lua"], "ext": ".lua"},
}
EXTENSION_LANGUAGE = {
    ".py": "python",
    ".js": "js",
    ".sh": "bash",
    ".bash": "bash",
    ".php": "php",
    ".rb": "ruby",
    ".pl": "perl",
    ".lua": "lua",
}


def detect_language_from_extension(filename: str) -> str | None:
    ext = os.path.splitext(filename)[1].lower()
    return EXTENSION_LANGUAGE.get(ext)


def get_runner(language: str) -> list[str] | None:
    if not language:
        return None
    return SUPPORTED_LANGUAGES.get(language.lower(), {}).get("cmd")


def write_code_to_temp_file(code: str, language: str) -> str:
    extension = SUPPORTED_LANGUAGES.get(language, {}).get("ext", ".txt")
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=extension)
    temp_file.write(code.encode("utf-8"))
    temp_file.close()
    return temp_file.name


async def run_code_async(command: list[str], cwd: str | None = None, timeout: int = 30) -> tuple[str, str]:
    """Execute any supported code asynchronously"""
    global running_process
    async with process_lock:
        if running_process and running_process.returncode is None:
            raise RuntimeError("Another task is already running")
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        running_process = process

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        return stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return "", "Timeout: Execution took too long"
    finally:
        async with process_lock:
            running_process = None


async def run_python_async(code: str) -> tuple[str, str]:
    """Execute Python code asynchronously"""
    return await run_code_async([sys.executable, "-c", code])


def split_message(text: str, max_len: int = 4096) -> list[str]:
    """Split long messages into chunks"""
    if len(text) <= max_len:
        return [text]
    
    chunks = []
    current = ""
    
    for line in text.split("\n"):
        if len(current) + len(line) + 1 <= max_len:
            current += line + "\n"
        else:
            if current:
                chunks.append(current)
            current = line + "\n"
    
    if current:
        chunks.append(current)
    
    return chunks


def get_latest_log_lines(count: int) -> str:
    """Get latest log lines"""
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
            return "".join(lines[-count:])
    except Exception:
        return "No logs available"


# Command handlers with improved AI
@app.on_message(filters.command("ping", prefixes=COMMAND_PREFIX) & AUTHORIZED_SENDER)
async def ping_handler(client: Client, message: Message) -> None:
    if not is_sudo_or_owner(message):
        return
    try:
        start_time = time.time()
        pong_msg = await message.reply_text("🏓 **Pong!**")
        latency = (time.time() - start_time) * 1000
        await pong_msg.edit_text(f"🏓 **Pong!** `{latency:.2f}ms`")
    except Exception:
        logger.exception("Ping handler failed")


@app.on_message(filters.command("id", prefixes=COMMAND_PREFIX) & AUTHORIZED_SENDER)
async def id_handler(client: Client, message: Message) -> None:
    if not is_sudo_or_owner(message):
        return
    try:
        user_id = message.from_user.id if message.from_user else "N/A"
        chat_id = message.chat.id
        message_id = message.id
        
        reply_user_id = "N/A"
        if message.reply_to_message and message.reply_to_message.from_user:
            reply_user_id = message.reply_to_message.from_user.id
        
        info_text = f"""**📋 ID Information:**

👤 **Your ID:** `{user_id}`
💬 **Chat ID:** `{chat_id}`
📝 **Message ID:** `{message_id}`
👥 **Reply to User ID:** `{reply_user_id}`
"""
        await message.reply_text(info_text)
    except Exception:
        logger.exception("ID handler failed")


@app.on_message(filters.command("userlink", prefixes=COMMAND_PREFIX) & AUTHORIZED_SENDER)
async def userlink_handler(client: Client, message: Message) -> None:
    if not is_sudo_or_owner(message):
        return
    try:
        user = message.from_user
        if not user:
            await message.reply_text("❌ **No user information available**")
            return
        
        user_link = f"tg://user?id={user.id}"
        link_text = f"[{user.first_name or 'User'}](tg://user?id={user.id})"
        
        info_text = f"""**👤 User Information:**

**Username:** @{user.username if user.username else 'N/A'}
**First Name:** {user.first_name or 'N/A'}
**Last Name:** {user.last_name or 'N/A'}
**ID:** `{user.id}`
**Link:** {link_text}
**Bot:** {"Yes" if user.is_bot else "No"}
**Verified:** {"Yes" if user.is_verified else "No"}
"""
        await message.reply_text(info_text)
    except Exception:
        logger.exception("Userlink handler failed")


@app.on_message(filters.command(["ai", "ask"], prefixes=COMMAND_PREFIX) & AUTHORIZED_SENDER)
async def ask_handler(client: Client, message: Message) -> None:
    """IMPROVED: Better AI with caching, sentiment analysis, and dynamic prompts"""
    if not is_sudo_or_owner(message):
        return
    try:
        query = " ".join(message.command[1:]) if len(message.command) > 1 else ""
        if not query:
            await message.reply_text(
                "❌ **Usage:** `.ask <your question>`\n\n"
                "**Example:** `.ask What is Python?`"
            )
            return

        reply_text = get_reply_text(message)
        if reply_text:
            query = f"{reply_text}\n\nQuestion: {query}"

        # NEW: Check cache first
        cached = get_cached_response(query, message.chat.id)
        if cached:
            chunks = split_message(cached, 4096)
            try:
                await message.reply_text(chunks[0])
                for chunk in chunks[1:]:
                    await message.reply_text(chunk)
            except ChatWriteForbidden:
                logger.warning("Cache retrieval blocked: write forbidden in chat %s", message.chat.id)
            return

        try:
            status = await message.reply_text("**🔎 Thinking**")
        except ChatWriteForbidden:
            logger.warning("Ask handler blocked in chat %s: write forbidden", message.chat.id)
            return
        
        # Search web
        search_results = await search_web(query)
        search_failed = search_results.startswith("❌") or search_results.startswith("⚠️")
        
        try:
            await status.edit_text("**🤖 Typing...**")
        except ChatWriteForbidden:
            logger.warning("Ask handler blocked: write forbidden editing status")
            return

        # IMPROVED: Context-aware temperature
        temperature = 0.3 if "code" in query.lower() or "how to" in query.lower() else 0.6
        chat_type = "private" if message.chat.type == "private" else "group"
        
        history = conversation_history.get(message.chat.id, [])
        response = await get_ai_response(
            query,
            search_results if not search_failed else None,
            history,
            temperature=temperature,  # NEW
            chat_type=chat_type,  # NEW
        )
        
        if response.startswith("❌") or response.startswith("⚠️"):
            if not search_failed:
                response = search_results
            else:
                response = "❌ **Unable to process request.**"

        if not response.startswith("❌") and not response.startswith("⚠️"):
            append_conversation_history(message.chat.id, "user", query)
            append_conversation_history(message.chat.id, "assistant", response)
            cache_response(query, message.chat.id, response)  # NEW: Cache response
        
        clean_response = response.strip()
        chunks = split_message(clean_response, 4096)
        
        try:
            await status.edit_text(chunks[0])
        except ChatWriteForbidden:
            logger.warning("Ask handler blocked: write forbidden sending response")
            return

        for chunk in chunks[1:]:
            try:
                await message.reply_text(chunk)
            except ChatWriteForbidden:
                logger.warning("Ask handler blocked: write forbidden sending chunk")
                return
    except ChatWriteForbidden:
        logger.warning("Ask handler blocked in chat %s: write forbidden", message.chat.id)
    except Exception:
        logger.exception("Ask handler failed")
        try:
            await message.reply_text("❌ **Ask command failed.**")
        except ChatWriteForbidden:
            logger.warning("Failed to send error: write forbidden")


@app.on_message(filters.command("run", prefixes=COMMAND_PREFIX) & AUTHORIZED_SENDER)
async def run_handler(client: Client, message: Message) -> None:
    if not is_sudo_or_owner(message):
        return
    temp_file = None
    try:
        args = message.command[1:]
        language = None
        code = ""
        file_path = None

        if message.reply_to_message and message.reply_to_message.document:
            with tempfile.TemporaryDirectory() as tmp_dir:
                file_name = message.reply_to_message.document.file_name or "code"
                file_path = os.path.join(tmp_dir, file_name)
                await message.reply_to_message.download(file_name=file_path)
                language = detect_language_from_extension(file_name)
                if not language and args:
                    language = args[0].lower()
                if not language:
                    await message.reply_text(
                        "❌ **Unsupported file type. Use `.run <language>` or send a supported file extension.**"
                    )
                    return
                runner = get_runner(language)
                if not runner:
                    await message.reply_text("❌ **Unsupported language.**")
                    return

                status = await message.reply_text("⏳ **Running file...**")
                stdout, stderr = await run_code_async(runner + [file_path])
        else:
            if message.reply_to_message and (message.reply_to_message.text or message.reply_to_message.caption):
                code = message.reply_to_message.text or message.reply_to_message.caption
                if args and args[0].lower() in SUPPORTED_LANGUAGES:
                    language = args[0].lower()
                    if len(args) > 1:
                        code = " ".join(args[1:])
                else:
                    language = args[0].lower() if args else "python"
            else:
                if not args:
                    await message.reply_text(
                        "❌ **Usage:** `.run <language> <code>` or reply to code/file with `.run`"
                    )
                    return
                if args[0].lower() in SUPPORTED_LANGUAGES:
                    language = args[0].lower()
                    code = " ".join(args[1:])
                elif os.path.exists(args[0]):
                    file_path = args[0]
                    language = detect_language_from_extension(file_path)
                else:
                    language = "python"
                    code = " ".join(args)

            if not file_path:
                if not code.strip():
                    await message.reply_text(
                        "❌ **Usage:** `.run <language> <code>` or reply to code/file with `.run`"
                    )
                    return
                if language not in SUPPORTED_LANGUAGES:
                    await message.reply_text(
                        "❌ **Unsupported language. Supported:** python, js, bash, php, ruby, perl, lua"
                    )
                    return
                temp_file = write_code_to_temp_file(code, language)
                file_path = temp_file

            runner = get_runner(language)
            if not runner:
                await message.reply_text("❌ **Unsupported language.**")
                return

            status = await message.reply_text("⏳ **Running code...**")
            stdout, stderr = await run_code_async(runner + [file_path])

        output = stdout if stdout else stderr
        if not output:
            output = "No output"
        if len(output) > 1500:
            output = output[:1500] + "\n...output truncated"

        await status.edit_text(format_code_block(output, "text"))
    except RuntimeError as e:
        await message.reply_text(f"⚠️ {e}")
    except Exception:
        logger.exception("Run handler failed")
        await message.reply_text("❌ **Run command failed.**")
    finally:
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass


@app.on_message(filters.command("end", prefixes=COMMAND_PREFIX) & AUTHORIZED_SENDER)
async def end_handler(client: Client, message: Message) -> None:
    if not is_sudo_or_owner(message):
        return
    try:
        async with process_lock:
            if running_process and running_process.returncode is None:
                running_process.terminate()
                try:
                    await asyncio.wait_for(running_process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    running_process.kill()
                
                await message.reply_text("✅ **Running task terminated successfully!**")
                return

        await message.reply_text("⚠️ **No running task to terminate.**")
    except Exception:
        logger.exception("End handler failed")
        await message.reply_text("❌ **End command failed.**")


@app.on_message(filters.command("del", prefixes=COMMAND_PREFIX) & AUTHORIZED_SENDER)
async def delete_handler(client: Client, message: Message) -> None:
    if not is_sudo_or_owner(message):
        return
    try:
        if not message.reply_to_message:
            await message.reply_text("❌ **Reply to a message with `.del` to delete it.**")
            return

        await message.reply_to_message.delete()
        await message.delete()
    except Exception:
        logger.exception("Delete handler failed")
        await message.reply_text("❌ **Delete command failed.**")


@app.on_message(filters.command("purge", prefixes=COMMAND_PREFIX) & AUTHORIZED_SENDER)
async def purge_handler(client: Client, message: Message) -> None:
    if not is_sudo_or_owner(message):
        return
    try:
        if not message.reply_to_message:
            await message.reply_text("❌ **Reply to the first message to purge from with `.purge`.**")
            return

        start_id = message.reply_to_message.id
        end_id = message.id
        if start_id > end_id:
            start_id, end_id = end_id, start_id

        ids_to_delete = list(range(start_id, end_id + 1))
        status = await message.reply_text(f"⏳ **Purging {len(ids_to_delete)} messages...**")

        try:
            await client.delete_messages(message.chat.id, ids_to_delete)
            await status.edit_text(f"✅ **Purged {len(ids_to_delete)} messages**")
        except Exception:
            logger.exception("Failed to purge messages %s", ids_to_delete)
            await status.edit_text("❌ **Failed to purge messages.**")
    except Exception:
        logger.exception("Purge handler failed")
        await message.reply_text("❌ **Purge command failed.**")


@app.on_message(filters.command("help", prefixes=COMMAND_PREFIX) & AUTHORIZED_SENDER)
async def help_handler(client: Client, message: Message) -> None:
    if not is_sudo_or_owner(message):
        return
    try:
        help_text = """**📚 Userbot Commands Help (ENHANCED AI)**

**General:**
• `.ping` - Check bot responsiveness ⏱️
• `.id` - Get user/chat/message IDs 🆔
• `.userlink` - Get user link 👤
• `.help` - Show this help message 📖

**AI Features**
• `.ask <question>` - Ask AI with smart caching ⚡
• `.ask` (reply) - Ask AI about replied message 💬

**Utilities:**
• `.run <code>` - Execute replied code or inline code in supported languages
• `.run <language> <code>` - Choose language explicitly (python, js, bash, php, ruby, perl, lua)
• `.run` (reply to file) - Execute supported file attachment
• `.end` - Stop the currently running task
• `.del` (reply) - Delete replied message and command message
• `.purge` (reply) - Delete messages from replied message to this command

**Sudo Management:**
• `.addsudo <user_id>` - Add user to sudo list 🔑
• `.addsudo` (reply) - Add replied user to sudo list
• `.rmsudo <user_id>` - Remove user from sudo list ❌
• `.rmsudo` (reply) - Remove replied user from sudo list
• `.listsudo` - List all sudo users 📋
• `.afk [reason]` - Set AFK status 🔴

"""
        await message.reply_text(help_text)
    except Exception:
        logger.exception("Help handler failed")


@app.on_message(filters.command("addsudo", prefixes=COMMAND_PREFIX) & filters.me)
async def addsudo_handler(client: Client, message: Message) -> None:
    try:
        if message.reply_to_message and message.reply_to_message.from_user:
            target_user = message.reply_to_message.from_user
            user_id = target_user.id
            username = target_user.first_name or f"User({user_id})"
        else:
            if len(message.command) < 2:
                await message.reply_text("❌ **Usage:** `.addsudo <user_id>` or reply to a user with `.addsudo`")
                return
            try:
                user_id = int(message.command[1])
                username = f"User({user_id})"
            except ValueError:
                await message.reply_text("❌ **Invalid user ID**")
                return
        
        if user_id in sudo_users:
            await message.reply_text(f"⚠️ **User {username} is already a sudo user**")
            return
        
        sudo_users.add(user_id)
        save_memory()
        await message.reply_text(f"✅ **Added {username} to sudo users**\n👤 **ID:** `{user_id}`")
    except Exception:
        logger.exception("Addsudo handler failed")


@app.on_message(filters.command("rmsudo", prefixes=COMMAND_PREFIX) & filters.me)
async def rmsudo_handler(client: Client, message: Message) -> None:
    try:
        if message.reply_to_message and message.reply_to_message.from_user:
            target_user = message.reply_to_message.from_user
            user_id = target_user.id
            username = target_user.first_name or f"User({user_id})"
        else:
            if len(message.command) < 2:
                await message.reply_text("❌ **Usage:** `.rmsudo <user_id>` or reply to a user with `.rmsudo`")
                return
            try:
                user_id = int(message.command[1])
                username = f"User({user_id})"
            except ValueError:
                await message.reply_text("❌ **Invalid user ID**")
                return
        
        if user_id not in sudo_users:
            await message.reply_text(f"⚠️ **User {username} is not a sudo user**")
            return
        
        sudo_users.discard(user_id)
        save_memory()
        await message.reply_text(f"✅ **Removed {username} from sudo users**\n👤 **ID:** `{user_id}`")
    except Exception:
        logger.exception("Rmsudo handler failed")


@app.on_message(filters.command("listsudo", prefixes=COMMAND_PREFIX) & filters.me)
async def listsudo_handler(client: Client, message: Message) -> None:
    try:
        if not sudo_users:
            await message.reply_text("📋 **Sudo Users List:**\n\n❌ No sudo users added yet")
            return
        
        sudo_list = sorted(list(sudo_users))
        response = "📋 **Sudo Users List:**\n\n"
        
        for idx, user_id in enumerate(sudo_list, 1):
            response += f"{idx}. `{user_id}`\n"
        
        response += f"\n**Total:** {len(sudo_users)} sudo user(s)"
        
        chunks = split_message(response, 4096)
        status = await message.reply_text(chunks[0])
        for chunk in chunks[1:]:
            await message.reply_text(chunk)
    except Exception:
        logger.exception("Listsudo handler failed")



@app.on_message(filters.command("afk", prefixes=COMMAND_PREFIX))
async def afk_handler(client: Client, message: Message) -> None:
    if not is_sudo_or_owner(message):
        return
    try:
        reason = " ".join(message.command[1:]) if len(message.command) > 1 else "Away from keyboard"
        afk_status["is_afk"] = True
        afk_status["reason"] = reason
        afk_status["start_time"] = datetime.now().isoformat()
        afk_status["replied_to"] = set()
        save_memory()
        await message.reply_text(f"**🔴 AFK Activated**\n📝 **Reason:** {reason}")
    except Exception:
        logger.exception("AFK handler failed")
        await message.reply_text("❌ **AFK command failed.**")


@app.on_message(filters.me & ~filters.command("afk", prefixes=COMMAND_PREFIX))
async def disable_afk_on_outgoing(client: Client, message: Message) -> None:
    try:
        if not afk_status["is_afk"]:
            return

        afk_status["is_afk"] = False
        afk_status["reason"] = ""
        afk_status["start_time"] = None
        afk_status["replied_to"] = set()
        save_memory()
    except Exception:
        logger.exception("Failed to disable AFK on outgoing message")


@app.on_message(filters.incoming & filters.text & ~filters.command(["ping", "id", "userlink", "ask", "ai", "afk", "run", "del", "purge", "help", "addsudo", "rmsudo", "listsudo", "logs", "redeploy", "end"], prefixes=COMMAND_PREFIX) & ~filters.me)
async def afk_auto_reply(client: Client, message: Message) -> None:
    try:
        cleanup_afk_memory()
        
        if not afk_status["is_afk"]:
            return
        if not message.from_user or message.from_user.is_self:
            return
        
        is_private = message.chat.type in ("private", "bot")
        is_mentioned = message.mentioned or (message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.is_self)
        
        if not (is_private or is_mentioned):
            return
        
        sender_id = message.from_user.id
        if sender_id in afk_status["replied_to"]:
            return
        if afk_status["start_time"]:
            start_time = datetime.fromisoformat(afk_status["start_time"])
        else:
            start_time = datetime.now()
        duration = datetime.now() - start_time
        minutes = int(duration.total_seconds() / 60)
        response = (
            f"**🔴 I'm Currently AFK**\n\n"
            f"📝 **Reason:** {afk_status['reason']}\n"
            f"⏱️ **Away for:** {minutes} minutes\n\n"
            f"💬 I'll reply when I'm back!"
        )
        try:
            await message.reply_text(response)
        except ChatWriteForbidden:
            logger.warning("AFK reply blocked: write forbidden")
            return
        afk_status["replied_to"].add(sender_id)
        save_memory()
    except Exception:
        logger.exception("AFK auto-reply failed")


def main() -> None:
    load_memory()
    logger.info("🚀 Userbot Starting (ENHANCED AI VERSION)...")
    logger.info(f"📱 Session: {SESSION_NAME}")
    logger.info(f"⚙️ Commands available with prefix: {COMMAND_PREFIX}")
    logger.info(f"✨ Features: Sentiment Analysis, Response Caching, Dynamic Prompts, Context Awareness")
    app.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("⛔ Userbot Stopped")
        save_memory()
    except Exception:
        logger.exception("Userbot failed")
        save_memory()
