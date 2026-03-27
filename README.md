# AuraDoc Telegram Bot

AuraDoc is a production-ready asynchronous Telegram bot written in Python using the `python-telegram-bot` v20+ library. It provides a comprehensive suite of document conversion utilities, PDF manipulation tools, and OCR capabilities, all accessible via an intuitive inline keyboard interface.

> **Bot name:** AuraDoc

---

## 🚀 Features

### 📄 Document conversions
- Image → PDF (send multiple images or files; use `/list` to view queue, `/rotate`, `/swap`, `/remove`, `/rename` commands or the inline buttons to adjust order/rotation/names, and optionally set the output PDF filename before conversion; at review stage you can tap 📋 to preview each file)
- Image(s) → Word (queue review screen also offers **To DOC**; runs OCR on all images and produces a `.docx`)
- OCR (requires tesseract binary; may not be provided on some hosts)
- Word/Excel/PowerPoint → PDF (depends on LibreOffice; this tool may be unavailable on restricted hosts)
- PDF → Word (.docx)
- Word → PDF
- Excel → PDF
- PowerPoint → PDF

**Image management commands**: while adding images you may send text commands to control the list:

```
/rotate N D    # rotate image #N by D degrees
/swap i j      # swap positions of image i and j
/remove N      # remove image N from the list
/list          # show current images and order
```

### 🧩 PDF tools
- Merge multiple PDFs
- Split PDF into pages
- Remove a specific page
- Rotate PDF pages
- Compress PDF
- Add a watermark
- Protect PDF with password

### 🔍 OCR
- Extract text from images using Tesseract
- Returns extracted text as a Word document

### 🛠 Other highlights
- Async handlers & modern architecture
- Inline keyboard UI for tool selection
- Auto-detects file types sent by users
- "Processing..." status messages
- Unique file names for concurrent users
- Temporary files stored in `downloads/` and cleaned hourly
- Basic user tracking with SQLite (`users.db`)
- Private-chat only operation with error handling

---

## 🧩 Prerequisites

- Python 3.10 or higher
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) installed and on PATH (optional; required for OCR feature, not available on all hosting platforms)
- [LibreOffice](https://www.libreoffice.org/) installed and accessible (`soffice` command, used for Word/Excel/PowerPoint conversions). This binary is **not provided on certain hosts (e.g. StackHost free tier)**, so those conversions may fail.

Install Python dependencies:

```bash
pip install -r requirements.txt
```

**requirements.txt** should contain:
```
python-telegram-bot>=20.0a6
Pillow
img2pdf
PyPDF2
pdf2docx
docx2pdf
pytesseract
python-docx
```

---

## 🛠 Setup & Running

1. Clone or download this repository.
2. Set your bot token as an environment variable:
   ```bash
   export BOT_TOKEN="<your_token_here>"  # Linux/macOS
   setx BOT_TOKEN "<your_token_here>"    # Windows (restart terminal)
   ```
3. Run the bot:
   ```bash
   python bot.py
   ```
4. Chat with the bot on Telegram and choose tools via the inline keyboard.

---

## 🗄 Data & Storage

- Temporary files are placed in `downloads/` and auto-purged hourly.
- Basic user info (chat_id, username, first/last seen) is stored persistently in `users.db` (SQLite).

---

## 📦 Deployment Tips

- Run inside a virtual environment or Docker container.
- Use a process manager (systemd, pm2, supervisord) to keep the bot running.
- Configure periodic database backups if needed.
- To extend functionality, add new callbacks and handlers.

---

## 📝 License

This project is provided as-is under the MIT license. Feel free to modify and distribute.

---

For questions or contributions, open an issue or pull request in the repository.

---

## Userbot + Gemini AI integration (new)

This workspace now includes `bot.py` — a simple Telegram *userbot* (runs on your user account) that integrates with a Gemini-compatible HTTP API. It supports:
- `.ask <question>` as a reply to any message: sends the replied message as context plus your question to the AI and replies with the AI's answer.
- `.cmd` actions to control Telegram from your account (send, reply, delete, forward, block, unblock).

Setup (layman steps):

1. Create a file named `.env` in the project root with these values filled:

```
API_ID=123456     # from https://my.telegram.org
API_HASH=your_api_hash_here
SESSION_NAME=userbot
GEMINI_API_URL=https://your.gemini.endpoint/here
GEMINI_API_KEY=your_gemini_key_here
# Optional: comma-separated allowed user ids (if set, only these users can use commands)
AUTH_USER_IDS=123456789
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Run the userbot:

```bash
python bot.py
```

4. Use it from your Telegram account (the one you logged in with when the Pyrogram session was created): reply to a message and send `.ask What is this?` — the bot will reply with the Gemini answer. Send `.cmd help` for available `.cmd` actions.

Notes:
- The `GEMINI_API_URL` should point to an HTTP endpoint compatible with your Gemini/free provider; adjust the request shape if your provider needs a different JSON format.
- This userbot operates as *you* — it uses your Telegram account. Be careful when running it and only run on trusted machines.
- For production use, restrict `AUTH_USER_IDS` to a small set of admin ids or keep default (only your own account can control it).