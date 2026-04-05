# 🤖 Userbot-Ryuk - Telegram Userbot

A powerful Telegram userbot with AI integration, featuring ping, ID lookup, user links, AFK system, and Python script execution.

---

## ✨ Features

### **General Commands**
- **`.ping`** - Check bot responsiveness with latency ⏱️
- **`.id`** - Get user ID, chat ID, message ID, and timestamp 🆔
- **`.userlink`** - Get user link (your link or reply to someone's link) 👤
- **`.help`** - Show all available commands 📖

### **AI Features** 🤖
- **`.ask <question>`** - Ask AI questions and get intelligent responses
- **`.ask` (reply)** - Ask AI about replied messages with context
  - Automatically includes message context in the query
  - Responses formatted in code blocks for clarity
  - AI uses Groq (free tier, very fast)

### **Python Script Execution** ⚙️
- **`.run <code>`** - Execute Python code directly
- **`.run` (reply)** - Execute Python code from replied message
  - Output limited to 1000 characters
  - Automatic code block formatting
  - 10-second timeout for safety

### **AFK System** 🔴
- **`.afk [reason]`** - Set AFK status with optional reason
- **`.back`** - Disable AFK and see how long you were away
  - Auto-replies to people who mention you
  - Tracks time away
  - Remembers and doesn't spam same person

---

## 🚀 Setup Instructions

### **1. Clone/Download Repository**
```bash
git clone <your-repo>
cd Userbot-Ryuk
```

### **2. Install Dependencies**
```bash
pip install -r requirements.txt
```

### **3. Get Telegram API Credentials**
- Go to [my.telegram.org](https://my.telegram.org)
- Log in with your phone number
- Go to API Development Tools
- Create a new app
- Copy your **API ID** and **API Hash**

### **4. Get AI API Key (Optional but Recommended)**
- Visit [Groq Console](https://console.groq.com/)
- Sign up for free
- Create an API key in dashboard

### **5. Configure Environment**
Create `.env` file:
```bash
cp .env.example .env
```

Edit `.env` with your credentials:
```env
API_ID=123456789
API_HASH=your_api_hash_here
YOUR_USER_ID=987654321
GROQ_API_KEY=your_groq_key_here
SESSION_NAME=userbot
```

**To get YOUR_USER_ID:**
1. Run the bot with placeholder ID
2. Send `.id` command to any chat
3. Copy your User ID and update `.env`

### **6. Run the Userbot**
```bash
python bot.py
```

First run will create `userbot.session` file after scanning QR code.

---

## 📖 Usage Examples

### **Ping Check**
```
.ping
```
**Output:** Shows latency in milliseconds

### **Get IDs**
```
.id
```
**Output:**
```
📋 ID Information:
👤 User ID: 123456789
💬 Chat ID: -100123456789
📨 Message ID: 456
⏰ Timestamp: 1712250000
```

### **User Links**
```
.userlink
```
**Output:** Clickable link to your profile

### **Ask AI**
```
.ask What is artificial intelligence?
```
or reply to a message and send `.ask explain this`

**Output:**
```
Artificial intelligence refers to...
```

### **Run Python**
```
.run print(2**10)
.run import math; print(math.pi)
.run sum([1,2,3,4,5])
```

### **AFK Mode**
```
.afk In a meeting, will reply soon
```
- Bot will auto-reply to people mentioning you
- Shows reason and time away

```
.back
```
- Disables AFK mode
- Shows total time away

---

## 📋 Command Prefix

All commands use **`.`** (dot) as prefix:
- `.ping`
- `.ask`
- `.run`
- etc.

---

## ⚙️ Configuration

### **Environment Variables**
| Variable | Description | Required |
|----------|-------------|----------|
| `API_ID` | Telegram API ID | ✅ Yes |
| `API_HASH` | Telegram API Hash | ✅ Yes |
| `YOUR_USER_ID` | Your Telegram User ID | ✅ Yes |
| `GROQ_API_KEY` | Groq AI API Key | ❌ Optional |
| `SESSION_NAME` | Session file name | ❌ Optional |

---

## 🔒 Security Notes

- **Never share** your API credentials
- **Never share** your session files
- Only you can use this userbot (filtered by YOUR_USER_ID)
- Keep `.env` and `.session` files private
- Use strong passwords for Telegram account

---

## 📦 Dependencies

- `pyrogram` - Telegram Client
- `tgcrypto` - Encryption
- `httpx` - Async HTTP requests
- `python-dotenv` - Environment variables

---

## 🐛 Troubleshooting

### **"Invalid API ID" Error**
- Check API_ID and API_HASH from my.telegram.org
- Make sure they're in `.env` file

### **"Connection refused"**
- Check internet connection
- Telegram might be blocked in your region

### **AI not working**
- Verify GROQ_API_KEY is valid
- Check API quota at console.groq.com

### **Session file issues**
- Delete `.session` and `.session-journal` files
- Re-run bot to generate new session

---

## 📝 Notes

- Command prefix is **`.`** (required for all commands)
- Commands only work for YOUR_USER_ID
- Responses are formatted with **bold**, `code blocks`, and emojis for clarity
- AI responses auto-wrapped in code blocks
- Python script output limited to 1000 chars for safety

---

## 🤝 Contributing

Feel free to modify and add features!

---

## ⚖️ Disclaimer

This is for personal use only. Comply with Telegram's Terms of Service. Use responsibly!

---

**Made with ❤️ for Telegram users**