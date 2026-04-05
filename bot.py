import os
import json
import time
import subprocess
from datetime import datetime
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message
import httpx
import logging

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

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
    "replied_to": set()
}


# Memory helpers
def save_memory() -> None:
    try:
        data = {
            "afk": {
                "is_afk": afk_status["is_afk"],
                "reason": afk_status["reason"],
                "start_time": afk_status["start_time"],
                "replied_to": list(afk_status["replied_to"]),
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
    except Exception:
        logger.exception("Failed to load memory")


async def get_ai_response(query: str) -> str:
    if not GROQ_API_KEY:
        return "⚠️ **AI Key not configured!**"

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    model_order = [
        "llama-3.3-70b-versatile",
        "grok-1-mini",
        "grok-1-small",
        "grok-1",
        "grok-2-mini",
        "grok-2",
    ]

    async with httpx.AsyncClient(timeout=30) as client:
        for model in model_order:
            try:
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": query}],
                }
                response = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    json=payload,
                    headers=headers,
                )
                if response.status_code != 200:
                    error_text = response.text.lower()
                    if "model_terms_required" in error_text or "terms acceptance" in error_text:
                        logger.warning("Skipping model %s due to terms acceptance requirement", model)
                        continue
                    if "does not support chat completions" in error_text or "unsupported chat" in error_text:
                        logger.warning("Skipping model %s because it does not support chat completions", model)
                        continue
                    logger.error(
                        "AI request failed for model %s: %s %s %s",
                        model,
                        response.status_code,
                        response.url,
                        response.text,
                    )
                    continue

                result = response.json()
                if "choices" in result and len(result["choices"]) > 0:
                    choice = result["choices"][0]
                    if "message" in choice and "content" in choice["message"]:
                        return choice["message"]["content"]
                    if "text" in choice:
                        return choice["text"]
                logger.error("AI response missing expected fields for model %s: %s", model, result)
            except Exception:
                logger.exception("AI request failed for model %s", model)

    return "❌ **AI request failed.**"


def format_code_block(text: str, lang: str = "") -> str:
    return f"```{lang}\n{text}\n```"


def get_reply_text(message: Message) -> str:
    if message.reply_to_message:
        if message.reply_to_message.text:
            return message.reply_to_message.text
        if message.reply_to_message.caption:
            return message.reply_to_message.caption
    return ""


@app.on_message(filters.command("ping", prefixes=COMMAND_PREFIX) & filters.me)
async def ping_handler(client: Client, message: Message) -> None:
    try:
        start_time = time.time()
        status = await message.reply_text("**🏓 Pinging...**")
        elapsed = (time.time() - start_time) * 1000
        await status.edit_text(f"**🏓 Pong!**\n⏱️ `{elapsed:.2f}ms`")
    except Exception:
        logger.exception("Ping handler failed")


@app.on_message(filters.command("id", prefixes=COMMAND_PREFIX) & filters.me)
async def id_handler(client: Client, message: Message) -> None:
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


@app.on_message(filters.command("userlink", prefixes=COMMAND_PREFIX) & filters.me)
async def userlink_handler(client: Client, message: Message) -> None:
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


@app.on_message(filters.command("ask", prefixes=COMMAND_PREFIX) & filters.me)
async def ask_handler(client: Client, message: Message) -> None:
    try:
        query = " ".join(message.command[1:]) if message.command else ""
        if not query:
            await message.reply_text(
                "❌ **Usage:** `.ask <your question>`\n\n"
                "**Example:** `.ask What is Python?`"
            )
            return
        reply_text = get_reply_text(message)
        if reply_text:
            query = f"{reply_text}\n\nQuestion: {query}"
        status = await message.reply_text("**🤔 Thinking...**")
        response = await get_ai_response(query)
        await status.edit_text(format_code_block(response))
    except Exception:
        logger.exception("Ask handler failed")
        await message.reply_text("❌ **Ask command failed.**")


@app.on_message(filters.command("afk", prefixes=COMMAND_PREFIX) & filters.me)
async def afk_handler(client: Client, message: Message) -> None:
    try:
        reason = " ".join(message.command[1:]) if message.command else "Away from keyboard"
        afk_status["is_afk"] = True
        afk_status["reason"] = reason
        afk_status["start_time"] = datetime.now().isoformat()
        afk_status["replied_to"] = set()
        save_memory()
        await message.reply_text(f"**🔴 AFK Activated**\n📝 **Reason:** {reason}")
    except Exception:
        logger.exception("AFK handler failed")


@app.on_message(filters.command("back", prefixes=COMMAND_PREFIX) & filters.me)
async def back_handler(client: Client, message: Message) -> None:
    try:
        if not afk_status["is_afk"]:
            await message.reply_text("❌ **Not AFK!**")
            return
        start_time = datetime.fromisoformat(afk_status["start_time"])
        duration = datetime.now() - start_time
        minutes = int(duration.total_seconds() / 60)
        afk_status["is_afk"] = False
        save_memory()
        await message.reply_text(
            f"**✅ Back Online**\n"
            f"⏱️ **Was AFK for:** `{minutes} minutes`"
        )
    except Exception:
        logger.exception("Back handler failed")


@app.on_message(filters.command("run", prefixes=COMMAND_PREFIX) & filters.me)
async def run_handler(client: Client, message: Message) -> None:
    try:
        if message.reply_to_message and message.reply_to_message.text:
            script = message.reply_to_message.text
        else:
            script = " ".join(message.command[1:]) if message.command else ""
        if not script:
            await message.reply_text(
                "❌ **Usage:** `.run <python code>` or reply to code with `.run`\n\n"
                "**Example:** `.run print(2+2)`"
            )
            return
        status = await message.reply_text("**⚙️ Executing...**")
        result = subprocess.run(
            ["python", "-c", script],
            capture_output=True,
            text=True,
            timeout=10
        )
        output = result.stdout.strip() if result.stdout else result.stderr.strip()
        output = output[:1000] if output else "No output"
        await status.edit_text(format_code_block(output, "python"))
    except subprocess.TimeoutExpired:
        await message.reply_text("❌ **Execution Timeout** (>10s)")
    except Exception:
        logger.exception("Run handler failed")
        await message.reply_text("❌ **Run command failed.**")


@app.on_message(filters.command("del", prefixes=COMMAND_PREFIX) & filters.me)
async def delete_handler(client: Client, message: Message) -> None:
    try:
        if not message.reply_to_message:
            await message.reply_text("❌ **Usage:** reply to a message with `.del` to delete it")
            return

        try:
            await message.reply_to_message.delete()
        except Exception:
            logger.exception("Failed to delete replied message")

        try:
            await message.delete()
        except Exception:
            logger.exception("Failed to delete command message")
    except Exception:
        logger.exception("Delete handler failed")
        await message.reply_text("❌ **Delete command failed.**")


@app.on_message(filters.command("purge", prefixes=COMMAND_PREFIX) & filters.me)
async def purge_handler(client: Client, message: Message) -> None:
    try:
        if message.command and len(message.command) > 1:
            try:
                count = int(message.command[1])
                if count <= 0:
                    raise ValueError
            except ValueError:
                await message.reply_text("❌ **Usage:** `.purge <count>` where count is a positive number")
                return

            chat_id = message.chat.id
            for offset in range(count + 1):
                msg_id = message.id - offset
                try:
                    await client.delete_messages(chat_id, msg_id)
                except Exception:
                    logger.exception("Failed to purge message %s", msg_id)
            return

        if not message.reply_to_message:
            await message.reply_text("❌ **Usage:** `.purge <count>` or reply to a message with `.purge` to delete range")
            return

        start_id = message.reply_to_message.id
        end_id = message.id
        if start_id > end_id:
            start_id, end_id = end_id, start_id

        ids_to_delete = list(range(start_id, end_id + 1))
        try:
            await client.delete_messages(message.chat.id, ids_to_delete)
        except Exception:
            logger.exception("Failed to purge messages %s", ids_to_delete)
    except Exception:
        logger.exception("Purge handler failed")
        await message.reply_text("❌ **Purge command failed.**")


@app.on_message(filters.command("help", prefixes=COMMAND_PREFIX) & filters.me)
async def help_handler(client: Client, message: Message) -> None:
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

**Utilities:**
• `.run <code>` - Execute Python script ⚙️
• `.run` (reply) - Execute replied code
• `.del` (reply) - Delete replied message and this command
• `.purge <count>` - Delete recent messages in chat
• `.purge` (reply) - Delete all messages from replied message to this command

**AFK System:**
• `.afk [reason]` - Set AFK status 🔴
• `.back` - Turn off AFK status ✅

**Example Usage:**
```
.ping
.id
.ask What is machine learning?
.run print('Hello World')
.afk Taking a break
```

❓ **Need help?** Reply to any command with questions!"""
        await message.reply_text(help_text)
    except Exception:
        logger.exception("Help handler failed")


@app.on_message(filters.incoming & filters.text)
async def afk_auto_reply(client: Client, message: Message) -> None:
    try:
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
