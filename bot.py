import os
import sys
import json
import time
import subprocess
import asyncio
import ast
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message
import httpx
import logging

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
API_ID = int(os.getenv("API_ID", ""))
API_HASH = os.getenv("API_HASH", "")
SESSION_NAME = os.getenv("SESSION_NAME", "userbot")
COMMAND_PREFIX = "."
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
AI_SYSTEM_PROMPT = os.getenv(
    "AI_SYSTEM_PROMPT",
    "You are a highly intelligent, emotionally aware AI assistant. "
    "You speak in a warm, slightly dramatic, charming tone. "
    "You understand context deeply and respond naturally like a human. "
    "Keep responses engaging, slightly expressive but not cringe. "
    "Avoid robotic replies. Maintain personality consistency. "
    "Adapt tone based on user mood. Be helpful but also charismatic. "
    "Always address the user as RYUK in every response, and never call them by any other name."
)

COMMANDS = [
    "ping", "id", "userlink", "ask", "afk",
    "run", "del", "purge", "help", "addsudo", "rmsudo", "listsudo", "logs", "redeploy"
]

# Session file path helper
SESSION_PATH = SESSION_NAME if SESSION_NAME.endswith(".session") else f"{SESSION_NAME}.session"
USE_BOT_TOKEN = not os.path.exists(SESSION_PATH) and bool(BOT_TOKEN)

# Initialize Pyrogram Client
if USE_BOT_TOKEN:
    app = Client(
        SESSION_NAME,
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
    )
else:
    app = Client(
        SESSION_NAME,
        api_id=API_ID,
        api_hash=API_HASH,
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

# Conversation history storage for stateful AI
conversation_history: dict[int, list[dict[str, str]]] = {}

# Constants
MAX_AFK_REPLIED_TO = 1000  # Prevent unbounded growth
AFK_CLEANUP_INTERVAL = 3600  # 1 hour in seconds
MAX_CONVERSATION_HISTORY = 8  # Keep last few ask exchanges per chat


# Memory helpers
def save_memory() -> None:
    try:
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

            # Load conversation history
            global conversation_history
            conversation_history = {
                int(chat_id): messages
                for chat_id, messages in data.get("conversation_history", {}).items()
                if isinstance(messages, list)
            }

            # Load sudo users
            global sudo_users
            sudo_users = set(data.get("sudo_users", []))
    except Exception:
        logger.exception("Failed to load memory")


def is_sudo_or_owner(message: Message) -> bool:
    if not message.from_user:
        return False
    if message.from_user.is_self:
        return True
    return message.from_user.id in sudo_users


def append_conversation_history(chat_id: int, role: str, content: str) -> None:
    history = conversation_history.setdefault(chat_id, [])
    history.append({"role": role, "content": content})
    if len(history) > MAX_CONVERSATION_HISTORY:
        del history[:-MAX_CONVERSATION_HISTORY]
    save_memory()


def cleanup_afk_memory() -> None:
    """Cleanup AFK memory to prevent unbounded growth."""
    now = datetime.now()
    
    # Check if cleanup interval has passed
    if (now - afk_status["last_cleanup"]).total_seconds() < AFK_CLEANUP_INTERVAL:
        return
    
    # Limit replied_to set size
    if len(afk_status["replied_to"]) > MAX_AFK_REPLIED_TO:
        # Convert to list, remove oldest half, and convert back to set
        replied_list = list(afk_status["replied_to"])
        afk_status["replied_to"] = set(replied_list[len(replied_list)//2:])
        save_memory()
        logger.info("Cleaned up AFK replied_to set (new size: %d)", len(afk_status["replied_to"]))
    
    afk_status["last_cleanup"] = now


async def get_ai_response(
    query: str,
    search_context: str | None = None,
    history: list[dict[str, str]] | None = None,
) -> str:
    """Use Groq free tier models for generating answers."""
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

    messages = [{"role": "system", "content": AI_SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    async with httpx.AsyncClient(timeout=30) as client:
        for model in free_models:
            try:
                payload = {
                    "model": model,
                    "messages": messages,
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
    """Use DuckDuckGo free API for search (no authentication required)."""
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
                logger.error("Search request failed: %s", response.status_code)
                return "❌ **Search failed.**"

            data = response.json()
            if data.get("AbstractText"):
                return data["AbstractText"]

            if data.get("Answer"):
                return data["Answer"]

            related = data.get("RelatedTopics", [])
            results = []
            for item in related:
                if isinstance(item, dict) and item.get("Text"):
                    results.append(item["Text"])
                if len(results) >= 3:
                    break

            if results:
                return "\n".join(results[:3])

            return "❌ **No search results found.**"
    except Exception:
        logger.exception("Search request failed")
        return "❌ **Search failed.**"


def split_message(text: str, max_length: int = 4096) -> list:
    """Split text into chunks for Telegram's message limit."""
    if len(text) <= max_length:
        return [text]
    
    chunks = []
    current_chunk = ""
    
    for line in text.split('\n'):
        if len(current_chunk) + len(line) + 1 > max_length:
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = line
        else:
            if current_chunk:
                current_chunk += '\n' + line
            else:
                current_chunk = line
    
    if current_chunk:
        chunks.append(current_chunk)
    
    return chunks if chunks else [text[:max_length]]


def get_latest_log_lines(count: int = 1) -> str:
    if not os.path.exists(LOG_FILE):
        return "❌ **No logs found.**"

    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = []
            for line in reversed(f.readlines()):
                if line.strip():
                    lines.append(line.rstrip("\n"))
                    if len(lines) >= count:
                        break
        if not lines:
            return "❌ **Log file is empty.**"
        return "\n".join(reversed(lines))
    except Exception:
        logger.exception("Failed to read latest log line")
        return "❌ **Could not read logs.**"


async def run_python_async(code: str, timeout: int = 10) -> tuple:
    """Run Python code asynchronously without blocking the event loop."""
    try:
        # Use asyncio to prevent blocking
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ["python", "-c", code],
                    capture_output=True,
                    text=True,
                    timeout=timeout
                )
            ),
            timeout=timeout + 1
        )
        return (result.stdout.strip(), result.stderr.strip())
    except asyncio.TimeoutError:
        return ("", "Execution Timeout")
    except Exception as e:
        return ("", str(e))


def format_code_block(text: str, lang: str = "") -> str:
    return f"```{lang}\n{text}\n```"


def normalize_python_code(code: str) -> str:
    if "\n" in code or ";" in code:
        return code
    try:
        ast.parse(code)
        return code
    except SyntaxError:
        parts = re.split(r"\s+(?=[A-Za-z_]\w*\s*(?:=|\(|\[|\{|\:))", code)
        if len(parts) <= 1:
            return code
        candidate = "; ".join(parts)
        try:
            ast.parse(candidate)
            return candidate
        except SyntaxError:
            return code


def get_reply_text(message: Message) -> str:
    if message.reply_to_message:
        if message.reply_to_message.text:
            return message.reply_to_message.text
        if message.reply_to_message.caption:
            return message.reply_to_message.caption
    return ""


@app.on_message(filters.command("ping", prefixes=COMMAND_PREFIX))
async def ping_handler(client: Client, message: Message) -> None:
    if not is_sudo_or_owner(message):
        return
    try:
        start_time = time.time()
        status = await message.reply_text("**🏓 Pinging...**")
        elapsed = (time.time() - start_time) * 1000
        await status.edit_text(f"**🏓 Pong!**\n⏱️ `{elapsed:.2f}ms`")
    except Exception:
        logger.exception("Ping handler failed")


@app.on_message(filters.command("id", prefixes=COMMAND_PREFIX))
async def id_handler(client: Client, message: Message) -> None:
    if not is_sudo_or_owner(message):
        return
    try:
        if message.reply_to_message and message.reply_to_message.from_user:
            target_user = message.reply_to_message.from_user
            response = (
                f"**📋 Replied User ID:**\n\n"
                f"👤 **User:** `{target_user.first_name}`\n"
                f"🆔 **User ID:** `{target_user.id}`\n"
            )
            if message.chat.type in {"group", "supergroup", "channel"}:
                response += f"\n💬 **Chat ID:** `{message.chat.id}`"
        else:
            user_id = message.from_user.id if message.from_user else "N/A"
            response = f"**📋 Your ID:**\n\n👤 **User ID:** `{user_id}`\n"
            if message.chat.type in {"group", "supergroup", "channel"}:
                response += f"💬 **Group ID:** `{message.chat.id}`\n"
                response += f"🏷️ **Group:** `{message.chat.title or 'Unknown'}`"
        await message.reply_text(response)
    except Exception:
        logger.exception("ID handler failed")
        await message.reply_text("❌ **ID command failed.**")


@app.on_message(filters.command("userlink", prefixes=COMMAND_PREFIX))
async def userlink_handler(client: Client, message: Message) -> None:
    if not is_sudo_or_owner(message):
        return
    try:
        if message.reply_to_message and message.reply_to_message.from_user:
            user = message.reply_to_message.from_user
            link = f"[{user.first_name}](tg://user?id={user.id})"
            response = f"**👤 User Link:** {link}\n**ID:** `{user.id}`"
        else:
            user = message.from_user
            link = f"[{user.first_name}](tg://user?id={user.id})"
            response = f"**👤 Your Link:** {link}\n**Your ID:** `{user.id}`"
        await message.reply_text(response)
    except Exception:
        logger.exception("Userlink handler failed")
        await message.reply_text("❌ **Userlink command failed.**")


@app.on_message(filters.command("run", prefixes=COMMAND_PREFIX))
async def run_handler(client: Client, message: Message) -> None:
    if not is_sudo_or_owner(message):
        return
    try:
        if message.reply_to_message and message.reply_to_message.text:
            script = message.reply_to_message.text
        else:
            script = " ".join(message.command[1:]) if len(message.command) > 1 else ""
        script = normalize_python_code(script)
        if not script:
            await message.reply_text(
                "❌ **Usage:** `.run <python code>` or reply to code with `.run`\n\n"
                "**Example:** `.run print(2+2)`"
            )
            return
        
        # Safety warning
        warning_msg = await message.reply_text(
            "⚠️ **SECURITY WARNING**\n"
            "You are about to execute Python code. This can be dangerous!\n"
            "Make sure you trust the code source.\n\n"
            "**Executing...**"
        )
        
        stdout, stderr = await run_python_async(script)
        output = stdout if stdout else stderr
        output = output[:1000] if output else "No output"
        
        await warning_msg.edit_text(format_code_block(output, "python"))
    except Exception:
        logger.exception("Run handler failed")
        await message.reply_text("❌ **Run command failed.**")


@app.on_message(filters.command("del", prefixes=COMMAND_PREFIX))
async def delete_handler(client: Client, message: Message) -> None:
    if not is_sudo_or_owner(message):
        return
    try:
        if not message.reply_to_message:
            await message.reply_text("❌ **Usage:** reply to a message with `.del` to delete it")
            return

        # Send confirmation FIRST before deleting
        try:
            status = await message.reply_text("⏳ **Deleting...**")
        except Exception:
            logger.exception("Failed to send status")
            status = None

        deleted_count = 0
        try:
            await message.reply_to_message.delete()
            deleted_count += 1
        except Exception:
            logger.exception("Failed to delete replied message")

        try:
            await message.delete()
            deleted_count += 1
        except Exception:
            logger.exception("Failed to delete command message")
        
        # Update status if it exists
        if status and deleted_count > 0:
            try:
                await status.edit_text(f"✅ **Deleted {deleted_count} message(s)**")
            except Exception:
                logger.exception("Failed to edit status")
    except Exception:
        logger.exception("Delete handler failed")
        await message.reply_text("❌ **Delete command failed.**")


@app.on_message(filters.command("purge", prefixes=COMMAND_PREFIX))
async def purge_handler(client: Client, message: Message) -> None:
    if not is_sudo_or_owner(message):
        return
    try:
        if message.command and len(message.command) > 1:
            try:
                count = int(message.command[1])
                if count <= 0:
                    raise ValueError
            except ValueError:
                await message.reply_text("❌ **Usage:** `.purge <count>` where count is a positive number")
                return

            # Send status FIRST before deleting
            status = await message.reply_text(f"⏳ **Purging {count} messages...**")
            
            chat_id = message.chat.id
            ids_to_delete = []
            for offset in range(count + 1):
                msg_id = message.id - offset
                ids_to_delete.append(msg_id)
            
            # Batch delete (more efficient)
            try:
                await client.delete_messages(chat_id, ids_to_delete)
                await status.edit_text(f"✅ **Purged {count} messages**")
            except Exception:
                logger.exception("Failed to purge messages %s", ids_to_delete)
                await status.edit_text("❌ **Failed to purge messages.**")
            return

        if not message.reply_to_message:
            await message.reply_text("❌ **Usage:** `.purge <count>` or reply to a message with `.purge` to delete range")
            return

        start_id = message.reply_to_message.id
        end_id = message.id
        if start_id > end_id:
            start_id, end_id = end_id, start_id

        # Send status FIRST before deleting
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


@app.on_message(filters.command("help", prefixes=COMMAND_PREFIX))
async def help_handler(client: Client, message: Message) -> None:
    if not is_sudo_or_owner(message):
        return
    try:
        help_text = """**📚 Userbot Commands Help**

**General:**
• `.ping` - Check bot responsiveness ⏱️
• `.id` - Get user/chat/message IDs 🆔
• `.userlink` - Get user link 👤
• `.help` - Show this help message 📖

**AI Features:**
• `.ask <question>` - Ask AI anything 🤖
• `.ask` (reply) - Ask AI about replied message 💬
• `.ask` will fallback to web search when AI cannot provide an exact answer

**Utilities:**
• `.run <code>` - Execute Python script ⚙️
• `.run` (reply) - Execute replied code
• `.del` (reply) - Delete replied message and this command
• `.purge <count>` - Delete recent messages in chat
• `.purge` (reply) - Delete all messages from replied message to this command

**Note:** Sudo users can use these commands, but `.addsudo`, `.rmsudo`, and `.listsudo` remain owner-only.

**Sudo Management:**
• `.addsudo <user_id>` - Add user to sudo list 🔑
• `.addsudo` (reply) - Add replied user to sudo list
• `.rmsudo <user_id>` - Remove user from sudo list ❌
• `.rmsudo` (reply) - Remove replied user from sudo list
• `.listsudo` - List all sudo users 📋

**AFK System:**
• `.afk [reason]` - Set AFK status 🔴
• AFK disables automatically when you send your next message ✅

**Example Usage:**
```
.ping
.id
.ask What is machine learning?
.run print('Hello World')
.afk Taking a break
.addsudo 123456789
.listsudo
```

❓ **Need help?** Reply to any command with questions!"""
        await message.reply_text(help_text)
    except Exception:
        logger.exception("Help handler failed")
        await message.reply_text("❌ **Help command failed.**")


@app.on_message(filters.command("logs", prefixes=COMMAND_PREFIX))
async def logs_handler(client: Client, message: Message) -> None:
    if not is_sudo_or_owner(message):
        return
    try:
        count = 1
        if len(message.command) > 1:
            try:
                count = int(message.command[1])
                if count < 1 or count > 20:
                    raise ValueError
            except ValueError:
                await message.reply_text("❌ **Usage:** `.logs [count]` where count is 1-20")
                return

        latest_log = get_latest_log_lines(count)
        quoted_log = "> " + latest_log.replace("\n", "\n> ")
        await message.reply_text(quoted_log)
    except Exception:
        logger.exception("Logs handler failed")
        await message.reply_text("❌ **Logs command failed.**")


@app.on_message(filters.command("redeploy", prefixes=COMMAND_PREFIX))
async def restart_handler(client: Client, message: Message) -> None:
    if not is_sudo_or_owner(message):
        return
    try:
        await message.reply_text("🔄 **Restarting bot...**")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception:
        logger.exception("Restart handler failed")
        await message.reply_text("❌ **Restart command failed.**")


@app.on_message(filters.command("addsudo", prefixes=COMMAND_PREFIX) & filters.me)
async def addsudo_handler(client: Client, message: Message) -> None:
    try:
        if message.reply_to_message and message.reply_to_message.from_user:
            target_user = message.reply_to_message.from_user
            user_id = target_user.id
            username = target_user.first_name or f"User({user_id})"
        else:
            # Get user ID from command args
            if len(message.command) < 2:
                await message.reply_text(
                    "❌ **Usage:** `.addsudo <user_id>` or reply to a user with `.addsudo`"
                )
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
        await message.reply_text("❌ **Addsudo command failed.**")


@app.on_message(filters.command("rmsudo", prefixes=COMMAND_PREFIX) & filters.me)
async def rmsudo_handler(client: Client, message: Message) -> None:
    try:
        if message.reply_to_message and message.reply_to_message.from_user:
            target_user = message.reply_to_message.from_user
            user_id = target_user.id
            username = target_user.first_name or f"User({user_id})"
        else:
            # Get user ID from command args
            if len(message.command) < 2:
                await message.reply_text(
                    "❌ **Usage:** `.rmsudo <user_id>` or reply to a user with `.rmsudo`"
                )
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
        await message.reply_text("❌ **Rmsudo command failed.**")


@app.on_message(filters.command("listsudo", prefixes=COMMAND_PREFIX) & filters.me)
async def listsudo_handler(client: Client, message: Message) -> None:
    try:
        if not sudo_users:
            await message.reply_text("📋 **Sudo Users List:**\n\n❌ No sudo users added yet")
            return
        
        # Split into chunks if too many users
        sudo_list = sorted(list(sudo_users))
        response = "📋 **Sudo Users List:**\n\n"
        
        for idx, user_id in enumerate(sudo_list, 1):
            response += f"{idx}. `{user_id}`\n"
        
        response += f"\n**Total:** {len(sudo_users)} sudo user(s)"
        
        # Split message if too long
        chunks = split_message(response, 4096)
        status = await message.reply_text(chunks[0])
        for chunk in chunks[1:]:
            await message.reply_text(chunk)
    except Exception:
        logger.exception("Listsudo handler failed")
        await message.reply_text("❌ **Listsudo command failed.**")


@app.on_message(filters.command("ask", prefixes=COMMAND_PREFIX))
async def ask_handler(client: Client, message: Message) -> None:
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

        status = await message.reply_text("**🔎 Thinking**")
        
        # Step 1: Get search results
        search_results = await search_web(query)
        search_failed = search_results.startswith("❌") or search_results.startswith("⚠️")
        
        # Step 2: Generate AI response with search context and chat history
        await status.edit_text("**🤖 Typing...**")
        history = conversation_history.get(message.chat.id, [])
        response = await get_ai_response(
            query,
            search_results if not search_failed else None,
            history,
        )
        
        # Step 3: Handle failures with fallbacks
        if response.startswith("❌") or response.startswith("⚠️"):
            if not search_failed:
                response = search_results
            else:
                response = "❌ **Unable to process request.**"

        # Save chat history only when the result is not a terminal error
        if not response.startswith("❌") and not response.startswith("⚠️"):
            append_conversation_history(message.chat.id, "user", query)
            append_conversation_history(message.chat.id, "assistant", response)
        
        # Step 4: Format and send response
        clean_response = response.strip()
        
        chunks = split_message(clean_response, 4096)
        await status.edit_text(chunks[0])
        for chunk in chunks[1:]:
            await message.reply_text(chunk)
    except Exception:
        logger.exception("Ask handler failed")
        await message.reply_text("❌ **Ask command failed.**")


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
        # Optional: Notify that AFK is off.
        # await message.reply_text("**🟢 AFK Deactivated**")
    except Exception:
        logger.exception("Failed to disable AFK on outgoing message")


@app.on_message(filters.incoming & filters.text & ~filters.command(list(COMMANDS), prefixes=COMMAND_PREFIX) & ~filters.me)
async def afk_auto_reply(client: Client, message: Message) -> None:
    try:
        cleanup_afk_memory()  # Prevent memory leak
        
        if not afk_status["is_afk"]:
            return
        if not message.from_user or message.from_user.is_self:
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
        await message.reply_text(response)
        afk_status["replied_to"].add(sender_id)
        save_memory()
    except Exception:
        logger.exception("AFK auto-reply failed")


def main() -> None:
    load_memory()

    session_path = SESSION_NAME
    if not session_path.endswith(".session"):
        session_path = f"{session_path}.session"

    if not os.path.exists(session_path) and not BOT_TOKEN:
        logger.error(
            "No Telegram session file found at %s and BOT_TOKEN is not set. "
            "Provide a valid .session file or set BOT_TOKEN in the environment.",
            session_path,
        )
        return

    if os.path.exists(session_path):
        logger.info("🔑 Auth mode: user session")
    else:
        logger.info("🔑 Auth mode: bot token")

    logger.info("🚀 Userbot Starting...")
    logger.info(f"📱 Session: {SESSION_NAME}")
    logger.info(f"⚙️ Commands available with prefix: {COMMAND_PREFIX}")
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
