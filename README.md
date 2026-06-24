# Telegram Index & Filter Bot

A premium, highly-styled Telegram Index/Filter bot written in Python using Pyrogram and MongoDB. It allows admins to publish posts, generates deep links, forwards content to a main channel, paginates search results, and schedules message auto-deletes in both private chats and channels.

## Features

- **Double-Layer Delivery**:
  - Searches yield posts containing a **base64 deep link** button (`Get Post`).
  - Clicking this button opens the bot and sends the original stored post containing the **actual channel link** button (`Join Channel`).
- **Paginated Search**:
  - `/search <query>` lists names starting with the query, featuring next/prev inline pagination buttons.
- **Friendly, Emoji-free Elegant Styling**:
  - Uses native text symbols (e.g. `◆`, `『』`, `[+]`, `[x]`) instead of emojis.
  - Formats all system responses in a mathematical monospace (monocaps) font.
- **Background Deletion Scheduler**:
  - Robust auto-delete functionality for both DM posts and Main Channel posts. Deletion timers persist inside MongoDB to prevent losing state on restarts.
- **Request Channel Logging**:
  - When users search for a post that doesn't exist, the search query and requester details are logged automatically to a Request Channel.
- **Admin Management**:
  - Dynamically add/remove admins and configure the Main Channel ID on the fly using commands.

---

## Configuration

Copy `.env` to `.env` and fill out the following settings:

```env
# TELEGRAM BOT CONFIGURATION
API_ID=123456
API_HASH=your_api_hash
BOT_TOKEN=your_bot_token
OWNER_ID=123456789

# CHANNEL CONFIGURATIONS
STORAGE_CHANNEL_ID=-100xxxxxxxxxx
REQUEST_CHANNEL_ID=-100yyyyyyyyyy

# DATABASE CONFIGURATION
MONGO_URI=mongodb://localhost:27017/telegram_index_bot
```

### Channel Administrator Roles
For the bot to work, ensure the bot is added as an administrator with posting permissions in:
1. The **Storage Channel** (`STORAGE_CHANNEL_ID`)
2. The **Request Channel** (`REQUEST_CHANNEL_ID`)
3. The **Main Channel** (set dynamically via command)

---

## Installation & Running

1. **Install Requirements**:
   ```bash
   pip install -r requirements.txt
   ```
2. **Start the Bot**:
   ```bash
   python bot.py
   ```

---

## Commands Reference

### Owner Commands
- `/addadmin <user_id>`: Grant admin privileges to a user.
- `/removeadmin <user_id>`: Revoke admin privileges.

### Admin Commands
- `/listadmins`: List the owner and active administrators.
- `/setmainchannel <channel_id>`: Sets the target Main Channel for post broadcasts. (Can also be executed inside the channel itself).
- `/addpost`: Start the conversational multi-step post creation process.
- `/cancel`: Cancel the current post-creation or timer setup flow.
- `/broadcast`: (Reply to a message) Forward the target message to all users in the database.

### Public Commands
- `/start`: Starts the bot and welcomes the user. Deep-links trigger retrieval of stored posts.
- `/autodelete`: Interactive menu to configure DM auto-delete durations.
- `/search <query>`: Prefix search posts with paginated navigation buttons.
- Typing any text in the bot's private chat acts as a search query.
