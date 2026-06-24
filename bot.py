import asyncio
import datetime
import re
import base64
import logging
from bson import ObjectId
from pyrogram import Client, filters, enums, idle
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)
from pyrogram.errors import (
    RPCError,
    MessageDeleteForbidden,
    UserDeactivated,
    PeerIdInvalid,
    FloodWait
)

import config
import database

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Validate configuration on startup
config.validate_config()

# Initialize Pyrogram Bot Client
app = Client(
    "index_filter_bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    parse_mode=enums.ParseMode.HTML
)

# Admin states in-memory storage
# Structure: { user_id: { "state": STATE, "data": { ... } } }
admin_states = {}

# State constants
STATE_IDLE = "IDLE"
STATE_AWAITING_NAME = "AWAITING_NAME"
STATE_AWAITING_MEDIA = "AWAITING_MEDIA"
STATE_AWAITING_LINK = "AWAITING_LINK"
STATE_AWAITING_DM_DELETE = "AWAITING_DM_DELETE"
STATE_AWAITING_CHANNEL_DELETE = "AWAITING_CHANNEL_DELETE"

# --- STYLE HELPERS (MONOCAPS / STYLISH TEXT) ---
def to_monocaps(text: str) -> str:
    """Converts alphanumeric text to mathematical monospace characters for premium look."""
    out = []
    for char in text:
        o = ord(char)
        if 65 <= o <= 90:  # A-Z
            out.append(chr(o - 65 + 0x1D670))
        elif 97 <= o <= 122:  # a-z
            out.append(chr(o - 97 + 0x1D68A))
        elif 48 <= o <= 57:  # 0-9
            out.append(chr(o - 48 + 0x1D7F6))
        else:
            out.append(char)
    return "".join(out)

def style_header(title: str) -> str:
    return f"『 {to_monocaps(title)} 』"

# --- DEEP LINK ENCODING/DECODING ---
def encode_post_id(post_id: ObjectId) -> str:
    """Encodes MongoDB ObjectId (12 bytes) to URL-safe base64 string (16 chars)."""
    return base64.urlsafe_b64encode(post_id.binary).decode('utf-8').rstrip('=')

def decode_to_post_id(payload: str) -> ObjectId:
    """Decodes URL-safe base64 string back to MongoDB ObjectId."""
    padding = '=' * (4 - len(payload) % 4)
    payload_padded = payload + padding
    oid_bytes = base64.urlsafe_b64decode(payload_padded)
    return ObjectId(oid_bytes)

# --- CAPTION ENTITIES TO HTML CONVERTER ---
def entities_to_html(text: str, entities) -> str:
    """Converts plain text and message entities into a styled HTML string."""
    if not entities:
        return text
    
    # Telegram offsets and lengths are in UTF-16 code units.
    # Convert text to UTF-16-LE bytes to match offsets (2 bytes per unit).
    text_utf16 = text.encode("utf-16-le")
    
    insertions = {}
    
    def add_insertion(offset, tag, priority):
        if offset not in insertions:
            insertions[offset] = []
        insertions[offset].append((priority, tag))
        
    for i, entity in enumerate(entities):
        etype = entity.type.name if hasattr(entity.type, "name") else str(entity.type)
        etype = etype.lower().replace("messageentitytype.", "")
        
        start = entity.offset
        end = entity.offset + entity.length
        
        open_tag = ""
        close_tag = ""
        
        if etype == "bold":
            open_tag, close_tag = "<b>", "</b>"
        elif etype == "italic":
            open_tag, close_tag = "<i>", "</i>"
        elif etype == "underline":
            open_tag, close_tag = "<u>", "</u>"
        elif etype == "strikethrough":
            open_tag, close_tag = "<s>", "</s>"
        elif etype == "blockquote":
            open_tag, close_tag = "<blockquote>", "</blockquote>"
        elif etype == "code":
            open_tag, close_tag = "<code>", "</code>"
        elif etype == "pre":
            open_tag, close_tag = "<pre>", "</pre>"
        elif etype == "text_link":
            open_tag, close_tag = f'<a href="{entity.url}">', "</a>"
        else:
            continue
            
        # Priority: close tags (0) before open tags (1) at the same offset.
        # Nested tag ordering based on entity index.
        add_insertion(start, open_tag, (1, -i))
        add_insertion(end, close_tag, (0, i))
        
    chunks = []
    last_offset = 0
    
    for offset in sorted(insertions.keys()):
        chunk_bytes = text_utf16[last_offset * 2 : offset * 2]
        chunks.append(chunk_bytes.decode("utf-16-le"))
        
        sorted_tags = sorted(insertions[offset], key=lambda x: x[0])
        for _, tag in sorted_tags:
            chunks.append(tag)
            
        last_offset = offset
        
    chunk_bytes = text_utf16[last_offset * 2 :]
    chunks.append(chunk_bytes.decode("utf-16-le"))
    
    return "".join(chunks)

# --- AUTHORIZATION FILTER ---
async def is_admin_filter(_, __, message: Message) -> bool:
    return await database.is_admin(message.from_user.id)

admin_filter = filters.create(is_admin_filter)

# --- SCHEDULER: EXPIRY & DELETE HANDLER ---
async def start_auto_delete_scheduler():
    """Background task that runs every 10 seconds to delete expired messages."""
    while True:
        try:
            expired = await database.get_expired_deletions()
            for doc in expired:
                try:
                    await app.delete_messages(
                        chat_id=doc["chat_id"],
                        message_ids=doc["message_id"]
                    )
                    logger.info(f"Deleted expired message {doc['message_id']} in chat {doc['chat_id']}")
                except MessageDeleteForbidden:
                    logger.warning(f"Could not delete message {doc['message_id']} in chat {doc['chat_id']} (no permission)")
                except RPCError as e:
                    logger.error(f"Telegram RPC error deleting message: {e}")
                except Exception as e:
                    logger.error(f"Unexpected error deleting message: {e}")
                finally:
                    # Remove from DB regardless to avoid infinite retry loops
                    await database.delete_scheduled_deletion(doc["_id"])
        except Exception as e:
            logger.error(f"Error in auto delete scheduler loop: {e}")
        await asyncio.sleep(10)

# Helper to schedule a message deletion
async def schedule_message_deletion(chat_id: int, message_id: int, seconds: int, del_type: str):
    delete_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)
    await database.add_scheduled_deletion(chat_id, message_id, delete_at, del_type)

# --- BOT HANDLERS ---

@app.on_message(filters.command("start") & filters.private)
async def cmd_start(client: Client, message: Message):
    # Track the user in DB
    await database.add_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name
    )
    
    # Check if there is a start parameter (deep link)
    if len(message.command) > 1:
        payload = message.command[1]
        try:
            post_id = decode_to_post_id(payload)
            post = await database.get_post_by_id(post_id)
            if post:
                # Retrieve user settings to check DM auto-delete
                user_doc = await database.get_user(message.from_user.id)
                dm_del = user_doc.get("dm_autodelete_duration", 0) if user_doc else 0
                
                # Send the stored post to DM (with the actual channel link button)
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        text=to_monocaps("𝚓𝚘𝚒𝚗 𝚌𝚑𝚊𝚗𝚗𝚎𝚕"),
                        url=post["channel_link"]
                    )
                ]])
                
                sent_msg = await client.send_photo(
                    chat_id=message.from_user.id,
                    photo=post["photo_file_id"],
                    caption=post["caption_html"],
                    reply_markup=keyboard
                )
                
                if dm_del > 0:
                    await schedule_message_deletion(
                        chat_id=message.from_user.id,
                        message_id=sent_msg.id,
                        seconds=dm_del,
                        del_type="dm"
                    )
                    # Notify the user that this message will auto-delete
                    notif = await message.reply_text(
                        f"◆ {to_monocaps('𝚝𝚑𝚒𝚜 𝚙𝚘𝚜𝚝 𝚠𝚒𝚕𝚕 𝚋𝚎 𝚊𝚞𝚝𝚘-𝚍𝚎𝚕𝚎𝚝𝚎𝚍 𝚒𝚗')} <code>{dm_del}</code> {to_monocaps('𝚜𝚎𝚌𝚘𝚗𝚍𝚜')}."
                    )
                    await schedule_message_deletion(
                        chat_id=message.from_user.id,
                        message_id=notif.id,
                        seconds=dm_del,
                        del_type="dm"
                    )
                return
            else:
                await message.reply_text(
                    f"▼ {to_monocaps('𝚙𝚘𝚜𝚝 𝚗𝚘𝚝 𝚏𝚘𝚞𝚗𝚍 𝚘𝚛 𝚑𝚊𝚜 𝚋𝚎𝚎𝚗 𝚛𝚎𝚖𝚘𝚟𝚎𝚍')}."
                )
                return
        except Exception as e:
            logger.error(f"Error decoding deep link payload: {e}")
            await message.reply_text(
                f"▼ {to_monocaps('𝚒𝚗𝚟𝚊𝚕𝚒𝚍 𝚍𝚎𝚎𝚙 𝚕𝚒𝚗𝚔 𝚙𝚊𝚛𝚊𝚖𝚎𝚝𝚎𝚛')}."
            )
            return

    # Normal Welcome Message
    welcome_text = (
        f"{style_header('𝚠𝚎𝚕𝚌𝚘𝚖𝚎 𝚝𝚘 𝚒𝚗𝚍𝚎𝚡 𝚋𝚘𝚝')}\n\n"
        f"𝚑𝚎𝚕𝚕𝚘 {message.from_user.mention}! "
        f"𝚒 𝚊𝚖 𝚢𝚘𝚞𝚛 𝚏𝚛𝚒𝚎𝚗𝚍𝚕𝚢 𝚒𝚗𝚍𝚎𝚡 𝚊𝚗𝚍 𝚏𝚒𝚕𝚝𝚎𝚛 𝚌𝚘𝚖𝚙𝚊𝚗𝚒𝚘𝚗.\n\n"
        f"◆ 𝚠𝚛𝚒𝚝𝚎 𝚊 𝚙𝚘𝚜𝚝 𝚗𝚊𝚖𝚎 𝚍𝚒𝚛𝚎𝚌𝚝𝚕𝚢 𝚒𝚗 𝚌𝚑𝚊𝚝 𝚝𝚘 𝚏𝚒𝚗𝚍 𝚒𝚝.\n"
        f"◆ 𝚞𝚜𝚎 /𝚜𝚎𝚊𝚛𝚌𝚑 &lt;𝚚𝚞𝚎𝚛𝚢&gt; 𝚝𝚘 𝚜𝚎𝚊𝚛𝚌𝚑 𝚗𝚊𝚖𝚎𝚜 𝚠𝚒𝚝𝚑 𝚙𝚊𝚐𝚒𝚗𝚊𝚝𝚒𝚘𝚗.\n"
        f"◆ 𝚞𝚜𝚎 /𝚊𝚞𝚝𝚘𝚍𝚎𝚕𝚎𝚝𝚎 𝚝𝚘 𝚜𝚎𝚝 𝚞𝚙 𝚢𝚘𝚞𝚛 𝚌𝚞𝚜𝚝𝚘𝚖 𝚖𝚎𝚜𝚜𝚊𝚐𝚎 𝚕𝚒𝚏𝚎𝚝𝚒𝚖𝚎."
    )
    await message.reply_text(welcome_text)

# --- ADMIN MANAGEMENT COMMANDS ---

@app.on_message(filters.command("addadmin") & filters.user(config.OWNER_ID))
async def cmd_add_admin(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text(f"▼ {to_monocaps('𝚞𝚜𝚊𝚐𝚎')}: `/addadmin <user_id>`")
        return
    try:
        admin_id = int(message.command[1])
        await database.add_admin(admin_id)
        await message.reply_text(f"[+] {to_monocaps('𝚞𝚜𝚎𝚛')} <code>{admin_id}</code> {to_monocaps('𝚒𝚜 𝚗𝚘𝚠 𝚊𝚗 𝚊𝚍𝚖𝚒𝚗')}.")
    except ValueError:
        await message.reply_text(f"▼ {to_monocaps('𝚙𝚕𝚎𝚊𝚜𝚎 𝚙𝚛𝚘𝚟𝚒𝚍𝚎 𝚊 𝚟𝚊𝚕𝚒𝚍 𝚗𝚞𝚖𝚎𝚛𝚒𝚌 𝚞𝚜𝚎𝚛 𝚒𝚍')}.")

@app.on_message(filters.command("removeadmin") & filters.user(config.OWNER_ID))
async def cmd_remove_admin(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text(f"▼ {to_monocaps('𝚞𝚜𝚊𝚐𝚎')}: `/removeadmin <user_id>`")
        return
    try:
        admin_id = int(message.command[1])
        await database.remove_admin(admin_id)
        await message.reply_text(f"[x] {to_monocaps('𝚞𝚜𝚎𝚛')} <code>{admin_id}</code> {to_monocaps('𝚛𝚎𝚖𝚘𝚟𝚎𝚍 𝚏𝚛𝚘𝚖 𝚊𝚍𝚖𝚒𝚗𝚜')}.")
    except ValueError:
        await message.reply_text(f"▼ {to_monocaps('𝚙𝚕𝚎𝚊𝚜𝚎 𝚙𝚛𝚘𝚟𝚒𝚍𝚎 𝚊 𝚟𝚊𝚕𝚒𝚍 𝚗𝚞𝚖𝚎𝚛𝚒𝚌 𝚞𝚜𝚎𝚛 𝚒𝚍')}.")

@app.on_message(filters.command("listadmins") & admin_filter)
async def cmd_list_admins(client: Client, message: Message):
    admins = await database.get_all_admins()
    out = [
        style_header("𝚊𝚍𝚖𝚒𝚗𝚒𝚜𝚝𝚛𝚊𝚝𝚘𝚛𝚜"),
        f"◆ {to_monocaps('𝚘𝚠𝚗𝚎𝚛')}: <code>{config.OWNER_ID}</code>"
    ]
    for idx, admin_id in enumerate(admins, 1):
        out.append(f"  {idx}. <code>{admin_id}</code>")
    await message.reply_text("\n".join(out))

@app.on_message(filters.command("setmainchannel") & admin_filter)
async def cmd_set_main_channel(client: Client, message: Message):
    # Can be set via /setmainchannel <id> in DM
    # Or /setmainchannel in the channel itself
    if message.chat.type in [enums.ChatType.CHANNEL, enums.ChatType.SUPERGROUP]:
        channel_id = message.chat.id
        await database.set_setting("main_channel_id", channel_id)
        await message.reply_text(f"[+] {to_monocaps('𝚝𝚑𝚒𝚜 𝚌𝚑𝚊𝚗𝚗𝚎𝚕 𝚑𝚊𝚜 𝚋𝚎𝚎𝚗 𝚜𝚎𝚝 𝚊𝚜 𝚝𝚑𝚎 𝚖𝚊𝚒𝚗 𝚌𝚑𝚊𝚗𝚗𝚎𝚕')}.")
        return

    if len(message.command) < 2:
        await message.reply_text(f"▼ {to_monocaps('𝚞𝚜𝚊𝚐𝚎')} (𝚒𝚗 𝚍𝚖): `/setmainchannel <channel_id>`")
        return
    
    try:
        channel_id = int(message.command[1])
        await database.set_setting("main_channel_id", channel_id)
        await message.reply_text(f"[+] {to_monocaps('𝚖𝚊𝚒𝚗 𝚌𝚑𝚊𝚗𝚗𝚎𝚕 𝚒𝚍 𝚜𝚎𝚝 𝚝𝚘')} <code>{channel_id}</code>.")
    except ValueError:
        await message.reply_text(f"▼ {to_monocaps('𝚙𝚕𝚎𝚊𝚜𝚎 𝚙𝚛𝚘𝚟𝚒𝚍𝚎 𝚊 𝚟𝚊𝚕𝚒𝚍 𝚗𝚞𝚖𝚎𝚛𝚒𝚌 𝚌𝚑𝚊𝚗𝚗𝚎𝚕 𝚒𝚍')}.")

# --- CANCEL COMMAND ---
@app.on_message(filters.command("cancel") & admin_filter)
async def cmd_cancel(client: Client, message: Message):
    uid = message.from_user.id
    if uid in admin_states:
        del admin_states[uid]
        await message.reply_text(f"[x] {to_monocaps('𝚘𝚙𝚎𝚛𝚊𝚝𝚒𝚘𝚗 𝚌𝚊𝚗𝚌𝚎𝚕𝚕𝚎𝚍')}.")
    else:
        await message.reply_text(f"◆ {to_monocaps('𝚗𝚘 𝚊𝚌𝚝𝚒𝚟𝚎 𝚘𝚙𝚎𝚛𝚊𝚝𝚒𝚘𝚗 𝚝𝚘 𝚌𝚊𝚗𝚌𝚎𝚕')}.")

# --- POST CREATION FLOW (/addpost) ---

@app.on_message(filters.command("addpost") & admin_filter & filters.private)
async def cmd_add_post(client: Client, message: Message):
    uid = message.from_user.id
    admin_states[uid] = {"state": STATE_AWAITING_NAME, "data": {}}
    await message.reply_text(
        f"{style_header('𝚌𝚛𝚎𝚊𝚝𝚎 𝚙𝚘𝚜𝚝')}\n\n"
        f"◆ {to_monocaps('𝚙𝚕𝚎𝚊𝚜𝚎 𝚜𝚎𝚗𝚍 𝚝𝚑𝚎 𝚗𝚊𝚖𝚎 𝚏𝚘𝚛 𝚝𝚑𝚎 𝚙𝚘𝚜𝚝')}.\n\n"
        f"  {to_monocaps('𝚜𝚎𝚗𝚍 /cancel 𝚝𝚘 𝚊𝚋𝚘𝚛𝚝')}."
    )

# --- AUTO DELETE SETTING COMMAND (/autodelete) ---

@app.on_message(filters.command("autodelete") & filters.private)
async def cmd_autodelete(client: Client, message: Message):
    uid = message.from_user.id
    is_usr_admin = await database.is_admin(uid)
    
    keyboard = [
        [InlineKeyboardButton(to_monocaps("𝚍𝚖 𝚊𝚞𝚝𝚘-𝚍𝚎𝚕𝚎𝚝𝚎"), callback_data="autodel:dm")]
    ]
    if is_usr_admin:
        keyboard.append([
            InlineKeyboardButton(to_monocaps("𝚖𝚊𝚒𝚗 𝚌𝚑𝚊𝚗𝚗𝚎𝚕 𝚊𝚞𝚝𝚘-𝚍𝚎𝚕𝚎𝚝𝚎"), callback_data="autodel:channel")
        ])
        
    await message.reply_text(
        f"{style_header('𝚊𝚞𝚝𝚘-𝚍𝚎𝚕𝚎𝚝𝚎 𝚜𝚎𝚝𝚝𝚒𝚗𝚐𝚜')}\n\n"
        f"◆ {to_monocaps('𝚙𝚕𝚎𝚊𝚜𝚎 𝚜𝚎𝚕𝚎𝚌𝚝 𝚠𝚑𝚒𝚌𝚑 𝚍𝚎𝚕𝚎𝚝𝚒𝚘𝚗 𝚝𝚒𝚖𝚎𝚛 𝚝𝚘 𝚌𝚘𝚗𝚏𝚒𝚐𝚞𝚛𝚎')}:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

@app.on_callback_query(filters.regex(r"^autodel:(dm|channel)$"))
async def cb_autodel_choice(client: Client, callback_query: CallbackQuery):
    choice = callback_query.data.split(":")[1]
    uid = callback_query.from_user.id
    
    if choice == "channel":
        is_usr_admin = await database.is_admin(uid)
        if not is_usr_admin:
            await callback_query.answer(to_monocaps("𝚞𝚗𝚊𝚞𝚝𝚑𝚘𝚛𝚒𝚣𝚎𝚍"), show_alert=True)
            return
        admin_states[uid] = {"state": STATE_AWAITING_CHANNEL_DELETE, "data": {}}
    else:
        admin_states[uid] = {"state": STATE_AWAITING_DM_DELETE, "data": {}}
        
    await callback_query.message.edit_text(
        f"{style_header('𝚜𝚎𝚝 𝚍𝚎𝚕𝚎𝚝𝚒𝚘𝚗 𝚝𝚒𝚖𝚎')}\n\n"
        f"◆ {to_monocaps('𝚙𝚕𝚎𝚊𝚜𝚎 𝚜𝚎𝚗𝚍 𝚝𝚑𝚎 𝚍𝚞𝚛𝚊𝚝𝚒𝚘𝚗 𝚒𝚗 𝚜𝚎𝚌𝚘𝚗𝚍𝚜')} (𝚎.𝚐., 𝟹𝟶 𝚏𝚘𝚛 𝟹𝟶𝚜, 𝟹𝟶𝟶 𝚏𝚘𝚛 𝟻𝚖𝚒𝚗).\n"
        f"◆ {to_monocaps('𝚜𝚎𝚗𝚍 𝟶 𝚝𝚘 𝚍𝚒𝚜𝚊𝚋𝚕𝚎 𝚊𝚞𝚝𝚘-𝚍𝚎𝚕𝚎𝚝𝚒𝚘𝚗')}.\n\n"
        f"  {to_monocaps('𝚜𝚎𝚗𝚍 /cancel 𝚝𝚘 𝚊𝚋𝚘𝚛𝚝')}."
    )
    await callback_query.answer()

# --- BROADCAST COMMAND ---

@app.on_message(filters.command("broadcast") & admin_filter)
async def cmd_broadcast(client: Client, message: Message):
    if not message.reply_to_message:
        await message.reply_text(f"▼ {to_monocaps('𝚙𝚕𝚎𝚊𝚜𝚎 𝚛𝚎𝚙𝚕𝚢 𝚝𝚘 𝚊 𝚖𝚎𝚜𝚜𝚊𝚐𝚎 𝚝𝚘 𝚋𝚛𝚘𝚊𝚍𝚌𝚊𝚜𝚝 𝚒𝚝')}.")
        return
    
    reply_msg = message.reply_to_message
    users = await database.get_all_users()
    
    status_msg = await message.reply_text(
        f"◆ {to_monocaps('𝚋𝚛𝚘𝚊𝚍𝚌𝚊𝚜𝚝 𝚜𝚝𝚊𝚛𝚝𝚎𝚍')}...\n"
        f"◆ {to_monocaps('𝚝𝚘𝚝𝚊𝚕 𝚞𝚜𝚎𝚛𝚜')}: <code>{len(users)}</code>"
    )
    
    success = 0
    failed = 0
    
    for user_id in users:
        try:
            await reply_msg.copy(chat_id=user_id)
            success += 1
        except (UserDeactivated, PeerIdInvalid):
            # Clean up inactive/blocked users
            failed += 1
        except FloodWait as e:
            await asyncio.sleep(e.value)
            try:
                await reply_msg.copy(chat_id=user_id)
                success += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1
            
        # Update progress occasionally
        if (success + failed) % 20 == 0:
            await status_msg.edit_text(
                f"◆ {to_monocaps('𝚋𝚛𝚘𝚊𝚍𝚌𝚊𝚜𝚝𝚒𝚗𝚐')}...\n"
                f"◆ {to_monocaps('𝚜𝚞𝚌𝚌𝚎𝚜𝚜')}: <code>{success}</code>\n"
                f"◆ {to_monocaps('𝚏𝚊𝚒𝚕𝚎𝚍')}: <code>{failed}</code>"
            )
            
    await status_msg.edit_text(
        f"{style_header('𝚋𝚛𝚘𝚊𝚍𝚌𝚊𝚜𝚝 𝚌𝚘𝚖𝚙𝚕𝚎𝚝𝚎𝚍')}\n\n"
        f"◆ {to_monocaps('𝚜𝚞𝚌𝚌𝚎𝚜𝚜𝚏𝚞𝚕')}: <code>{success}</code>\n"
        f"◆ {to_monocaps('𝚏𝚊𝚒𝚕𝚎𝚍')}: <code>{failed}</code>"
    )

# --- PAGINATED SEARCH (/search) ---

@app.on_message(filters.command("search"))
async def cmd_search(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text(f"▼ {to_monocaps('𝚞𝚜𝚊𝚐𝚎')}: `/search <query>`")
        return
        
    query = " ".join(message.command[1:]).strip()
    
    # Perform search count
    total_count = await database.count_posts_prefix(query)
    if total_count == 0:
        # Record unfound search request in Request Channel
        user_info = f"@{message.from_user.username}" if message.from_user.username else f"𝙸𝙳: {message.from_user.id}"
        request_text = (
            f"{style_header('𝚗𝚎𝚠 𝚙𝚘𝚜𝚝 𝚛𝚎𝚚𝚞𝚎𝚜𝚝')}\n\n"
            f"◆ {to_monocaps('𝚚𝚞𝚎𝚛𝚢')}: <code>{query}</code>\n"
            f"◆ {to_monocaps('𝚞𝚜𝚎𝚛')}: {user_info}"
        )
        try:
            await client.send_message(
                chat_id=config.REQUEST_CHANNEL_ID,
                text=request_text
            )
        except Exception as e:
            logger.error(f"Failed to send request to Request Channel: {e}")
            
        await message.reply_text(
            f"▼ {to_monocaps('𝚗𝚘 𝚙𝚘𝚜𝚝𝚜 𝚏𝚘𝚞𝚗𝚍 𝚜𝚝𝚊𝚛𝚝𝚒𝚗𝚐 𝚠𝚒𝚝𝚑')} <code>{query}</code>.\n"
            f"◆ {to_monocaps('𝚢𝚘𝚞𝚛 𝚛𝚎𝚚𝚞𝚎𝚜𝚝 𝚑𝚊𝚜 𝚋𝚎𝚎𝚗 𝚕𝚘𝚐𝚐𝚎𝚍')}."
        )
        return
        
    # Create search session in DB
    session_id = await database.create_search_session(query)
    await send_search_page(client, message.chat.id, session_id, page=1, target_message=None)

async def send_search_page(client: Client, chat_id: int, session_id: ObjectId, page: int, target_message: Message = None):
    session = await database.get_search_session(session_id)
    if not session:
        return
        
    query = session["query"]
    limit = 10
    skip = (page - 1) * limit
    
    posts = await database.search_posts_prefix(query, skip=skip, limit=limit)
    total_count = await database.count_posts_prefix(query)
    total_pages = (total_count + limit - 1) // limit
    
    text_lines = [
        f"{style_header('𝚜𝚎𝚊𝚛𝚌𝚑 𝚛𝚎𝚜𝚞𝚕𝚝𝚜')}",
        f"◆ {to_monocaps('𝚚𝚞𝚎𝚛𝚢')}: <code>{query}</code>",
        f"◆ {to_monocaps('𝚙𝚊𝚐𝚎')}: <code>{page}/{total_pages}</code>\n"
    ]
    
    # Generate list & buttons
    keyboard = []
    current_row = []
    
    for idx, post in enumerate(posts, 1):
        global_idx = skip + idx
        text_lines.append(f"{global_idx}. <code>{post['name'].upper()}</code>")
        
        # Base64 post ID for callback
        b64_id = encode_post_id(post["_id"])
        btn = InlineKeyboardButton(text=str(global_idx), callback_data=f"view:{b64_id}")
        current_row.append(btn)
        
        # 5 buttons per row
        if len(current_row) == 5:
            keyboard.append(current_row)
            current_row = []
            
    if current_row:
        keyboard.append(current_row)
        
    # Add pagination buttons
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(to_monocaps("« 𝚙𝚛𝚎𝚟"), callback_data=f"page:{session_id}:{page-1}"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton(to_monocaps("𝚗𝚎𝚡𝚝 »"), callback_data=f"page:{session_id}:{page+1}"))
        
    if nav_row:
        keyboard.append(nav_row)
        
    full_text = "\n".join(text_lines)
    
    if target_message:
        await target_message.edit_text(full_text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await client.send_message(chat_id, full_text, reply_markup=InlineKeyboardMarkup(keyboard))

@app.on_callback_query(filters.regex(r"^page:"))
async def cb_pagination(client: Client, callback_query: CallbackQuery):
    parts = callback_query.data.split(":")
    session_id = ObjectId(parts[1])
    page = int(parts[2])
    
    try:
        await send_search_page(client, callback_query.message.chat.id, session_id, page, callback_query.message)
    except Exception as e:
        logger.error(f"Error handling page callback: {e}")
    await callback_query.answer()

# Handling selection from search page or name match list
@app.on_callback_query(filters.regex(r"^view:"))
async def cb_view_post(client: Client, callback_query: CallbackQuery):
    b64_id = callback_query.data.split(":")[1]
    post_id = decode_to_post_id(b64_id)
    post = await database.get_post_by_id(post_id)
    
    if not post:
        await callback_query.answer(to_monocaps("𝚙𝚘𝚜𝚝 𝚗𝚘𝚝 𝚏𝚘𝚞𝚗𝚍"), show_alert=True)
        return
        
    # Send the post with the BASE64 DEEP LINK button to the user's DM
    # Wait, the search list item clicks should behave exactly like search results:
    # "user writes name if there is post related to the name bot sends the post with base 64 link in the dm..."
    bot_info = await client.get_me()
    deep_link = f"https://t.me/{bot_info.username}?start={b64_id}"
    
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            text=to_monocaps("𝚐𝚎𝚝 𝚙𝚘𝚜𝚝"),
            url=deep_link
        )
    ]])
    
    # Retrieve user setting to check DM auto-delete
    user_doc = await database.get_user(callback_query.from_user.id)
    dm_del = user_doc.get("dm_autodelete_duration", 0) if user_doc else 0
    
    sent_msg = await client.send_photo(
        chat_id=callback_query.from_user.id,
        photo=post["photo_file_id"],
        caption=post["caption_html"],
        reply_markup=keyboard
    )
    
    if dm_del > 0:
        await schedule_message_deletion(
            chat_id=callback_query.from_user.id,
            message_id=sent_msg.id,
            seconds=dm_del,
            del_type="dm"
        )
        notif = await client.send_message(
            chat_id=callback_query.from_user.id,
            text=f"◆ {to_monocaps('𝚝𝚑𝚒𝚜 𝚙𝚘𝚜𝚝 𝚠𝚒𝚕𝚕 𝚋𝚎 𝚊𝚞𝚝𝚘-𝚍𝚎𝚕𝚎𝚝𝚎𝚍 𝚒𝚗')} <code>{dm_del}</code> {to_monocaps('𝚜𝚎𝚌𝚘𝚗𝚍𝚜')}."
        )
        await schedule_message_deletion(
            chat_id=callback_query.from_user.id,
            message_id=notif.id,
            seconds=dm_del,
            del_type="dm"
        )
        
    await callback_query.answer()

# --- CONVERSATIONAL INPUT & CHAT ROUTING HANDLERS ---

@app.on_message(filters.private)
async def handle_private_messages(client: Client, message: Message):
    uid = message.from_user.id
    text = message.text.strip() if message.text else ""
    
    # 1. Track user in database
    await database.add_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name
    )
    
    # 2. Check if admin has an active state
    if uid in admin_states:
        state_data = admin_states[uid]
        current_state = state_data["state"]
        
        # Cancel command check is already handled by command filter, but just in case:
        if text.startswith("/"):
            # If they enter a command, allow it to pass through and clear state
            del admin_states[uid]
            return
            
        if current_state == STATE_AWAITING_NAME:
            if not text:
                await message.reply_text(f"▼ {to_monocaps('𝚙𝚕𝚎𝚊𝚜𝚎 𝚜𝚎𝚗𝚍 𝚊 𝚟𝚊𝚕𝚒𝚍 𝚝𝚎𝚡𝚝 𝚗𝚊𝚖𝚎')}.")
                return
            state_data["data"]["name"] = text
            state_data["state"] = STATE_AWAITING_MEDIA
            await message.reply_text(
                f"◆ {to_monocaps('𝚗𝚊𝚖𝚎 𝚜𝚎𝚝 𝚝𝚘')}: <code>{text.upper()}</code>\n\n"
                f"◆ {to_monocaps('𝚗𝚘𝚠 𝚙𝚕𝚎𝚊𝚜𝚎 𝚜𝚎𝚗𝚍 𝚝𝚑𝚎 𝙸𝙼𝙰𝙶𝙴 𝚠𝚒𝚝𝚑 𝚌𝚊𝚙𝚝𝚒𝚘𝚗')}."
            )
            return
            
        elif current_state == STATE_AWAITING_MEDIA:
            if not message.photo:
                await message.reply_text(f"▼ {to_monocaps('𝚙𝚕𝚎𝚊𝚜𝚎 𝚜𝚎𝚗𝚍 𝚊𝚗 𝙸𝙼𝙰𝙶𝙴 (𝚙𝚑𝚘𝚝𝚘) 𝚠𝚒𝚝𝚑 𝚢𝚘𝚞𝚛 𝚌𝚊𝚙𝚝𝚒𝚘𝚗')}.")
                return
                
            state_data["data"]["photo_file_id"] = message.photo.file_id
            
            # Format and store caption preserving entities
            caption_text = message.caption or ""
            entities = message.caption_entities
            caption_html = entities_to_html(caption_text, entities)
            
            state_data["data"]["caption_html"] = caption_html
            state_data["state"] = STATE_AWAITING_LINK
            
            await message.reply_text(
                f"[+] {to_monocaps('𝚖𝚎𝚍𝚒𝚊 & 𝚌𝚊𝚙𝚝𝚒𝚘𝚗 𝚛𝚎𝚌𝚎𝚒𝚟𝚎𝚍')}.\n\n"
                f"◆ {to_monocaps('𝚗𝚘𝚠 𝚙𝚕𝚎𝚊𝚜𝚎 𝚜𝚎𝚗𝚍 𝚝𝚑𝚎 𝚊𝚜𝚜𝚘𝚌𝚒𝚊𝚝𝚎𝚍 𝚌𝚑𝚊𝚗𝚗𝚎𝚕 𝚕𝚒𝚗𝚔')}."
            )
            return
            
        elif current_state == STATE_AWAITING_LINK:
            if not text or not (text.startswith("http://") or text.startswith("https://") or text.startswith("t.me/")):
                await message.reply_text(f"▼ {to_monocaps('𝚙𝚕𝚎𝚊𝚜𝚎 𝚜𝚎𝚗𝚍 𝚊 𝚟𝚊𝚕𝚒𝚍 𝚌𝚑𝚊𝚗𝚗𝚎𝚕 𝚕𝚒𝚗𝚔')} (𝚎.𝚐., https://t.me/...).")
                return
                
            channel_link = text
            name = state_data["data"]["name"]
            photo_file_id = state_data["data"]["photo_file_id"]
            caption_html = state_data["data"]["caption_html"]
            
            # Post to storage channel
            # Storage channel gets post with the actual channel link button
            storage_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    text=to_monocaps("𝚓𝚘𝚒𝚗 𝚌𝚑𝚊𝚗𝚗𝚎𝚕"),
                    url=channel_link
                )
            ]])
            
            try:
                storage_msg = await client.send_photo(
                    chat_id=config.STORAGE_CHANNEL_ID,
                    photo=photo_file_id,
                    caption=caption_html,
                    reply_markup=storage_kb
                )
                storage_msg_id = storage_msg.id
            except Exception as e:
                logger.error(f"Failed to post to Storage Channel: {e}")
                await message.reply_text(
                    f"▼ {to_monocaps('𝚎𝚛𝚛𝚘𝚛 𝚙𝚘𝚜𝚝𝚒𝚗𝚐 𝚝𝚘 𝚜𝚝𝚘𝚛𝚊𝚐𝚎 𝚌𝚑𝚊𝚗𝚗𝚎𝚕')}.\n"
                    f"{to_monocaps('𝚖𝚊𝚔𝚎 𝚜𝚞𝚛𝚎 𝚝𝚑𝚎 𝚋𝚘𝚝 𝚒𝚜 𝚊𝚗 𝚊𝚍𝚖𝚒𝚗 𝚝𝚑𝚎𝚛𝚎')}."
                )
                del admin_states[uid]
                return
                
            # Insert post into database to generate ObjectId
            post_id = await database.add_post(
                name=name,
                photo_file_id=photo_file_id,
                caption_html=caption_html,
                channel_link=channel_link,
                storage_msg_id=storage_msg_id
            )
            
            # Generate base64 deep link
            b64_id = encode_post_id(post_id)
            bot_info = await client.get_me()
            deep_link = f"https://t.me/{bot_info.username}?start={b64_id}"
            
            # Post to main channel
            # Main channel gets post with base64 deep link button
            main_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    text=to_monocaps("𝚐𝚎𝚝 𝚙𝚘𝚜𝚝"),
                    url=deep_link
                )
            ]])
            
            main_channel_id = await database.get_setting("main_channel_id")
            if main_channel_id:
                try:
                    main_msg = await client.send_photo(
                        chat_id=main_channel_id,
                        photo=photo_file_id,
                        caption=caption_html,
                        reply_markup=main_kb
                    )
                    
                    # Schedule main channel deletion if configured
                    chan_del = await database.get_setting("main_channel_autodelete", 0)
                    if chan_del > 0:
                        await schedule_message_deletion(
                            chat_id=main_channel_id,
                            message_id=main_msg.id,
                            seconds=chan_del,
                            del_type="channel"
                        )
                except Exception as e:
                    logger.error(f"Failed to post to Main Channel ({main_channel_id}): {e}")
                    await message.reply_text(
                        f"▼ {to_monocaps('𝚙𝚘𝚜𝚝𝚎𝚍 𝚝𝚘 𝚜𝚝𝚘𝚛𝚊𝚐𝚎 𝚋𝚞𝚝 𝚏𝚊𝚒𝚕𝚎𝚍 𝚝𝚘 𝚜𝚎𝚗𝚍 𝚝𝚘 𝚖𝚊𝚒𝚗 𝚌𝚑𝚊𝚗𝚗𝚎𝚕')}."
                    )
            else:
                await message.reply_text(
                    f"▲ {to_monocaps('𝚖𝚊𝚒𝚗 𝚌𝚑𝚊𝚗𝚗𝚎𝚕 𝚒𝚜 𝚗𝚘𝚝 𝚜𝚎𝚝')}! "
                    f"{to_monocaps('𝚙𝚘𝚜𝚝 𝚠𝚊𝚜 𝚜𝚊𝚟𝚎𝚍 𝚝𝚘 𝚍𝚊𝚝𝚊𝚋𝚊𝚜𝚎 𝚊𝚗𝚍 𝚜𝚝𝚘𝚛𝚊𝚐𝚎 𝚋𝚞𝚝 𝚗𝚘𝚝 𝚋𝚛𝚘𝚊𝚍𝚌𝚊𝚜𝚝')}.\n"
                    f"◆ {to_monocaps('𝚞𝚜𝚎 /setmainchannel 𝚝𝚘 𝚌𝚘𝚗𝚏𝚒𝚐𝚞𝚛𝚎 𝚒𝚝')}."
                )
                
            # Clear admin state
            del admin_states[uid]
            
            await message.reply_text(
                f"[+] {to_monocaps('𝚙𝚘𝚜𝚝 𝚌𝚛𝚎𝚊𝚝𝚎𝚍 𝚜𝚞𝚌𝚌𝚎𝚜𝚜𝚏𝚞𝚕𝚕𝚢')}!\n\n"
                f"◆ {to_monocaps('𝚗𝚊𝚖𝚎')}: <code>{name.upper()}</code>\n"
                f"◆ {to_monocaps('𝚍𝚎𝚎𝚙 𝚕𝚒𝚗𝚔')}: {deep_link}"
            )
            return
            
        elif current_state == STATE_AWAITING_DM_DELETE:
            try:
                seconds = int(text)
                if seconds < 0:
                    raise ValueError
                await database.update_user_dm_autodelete(uid, seconds)
                del admin_states[uid]
                
                if seconds == 0:
                    await message.reply_text(f"[+] {to_monocaps('𝚍𝚖 𝚊𝚞𝚝𝚘-𝚍𝚎𝚕𝚎𝚝𝚒𝚘𝚗 𝚑𝚊𝚜 𝚋𝚎𝚎𝚗 𝚍𝚒𝚜𝚊𝚋𝚕𝚎𝚍')}.")
                else:
                    await message.reply_text(
                        f"[+] {to_monocaps('𝚍𝚖 𝚊𝚞𝚝𝚘-𝚍𝚎𝚕𝚎𝚝𝚒𝚘𝚗 𝚜𝚎𝚝 𝚝𝚘')} <code>{seconds}</code> {to_monocaps('𝚜𝚎𝚌𝚘𝚗𝚍𝚜')}."
                    )
            except ValueError:
                await message.reply_text(f"▼ {to_monocaps('𝚙𝚕𝚎𝚊𝚜𝚎 𝚜𝚎𝚗𝚍 𝚊 𝚟𝚊𝚕𝚒𝚍 𝚗𝚘𝚗-𝚗𝚎𝚐𝚊𝚝𝚒𝚟𝚎 𝚒𝚗𝚝𝚎𝚐𝚎𝚛')}.")
            return
            
        elif current_state == STATE_AWAITING_CHANNEL_DELETE:
            try:
                seconds = int(text)
                if seconds < 0:
                    raise ValueError
                
                is_usr_admin = await database.is_admin(uid)
                if not is_usr_admin:
                    del admin_states[uid]
                    return
                    
                await database.set_setting("main_channel_autodelete", seconds)
                del admin_states[uid]
                
                if seconds == 0:
                    await message.reply_text(f"[+] {to_monocaps('𝚖𝚊𝚒𝚗 𝚌𝚑𝚊𝚗𝚗𝚎𝚕 𝚊𝚞𝚝𝚘-𝚍𝚎𝚕𝚎𝚝𝚒𝚘𝚗 𝚑𝚊𝚜 𝚋𝚎𝚎𝚗 𝚍𝚒𝚜𝚊𝚋𝚕𝚎𝚍')}.")
                else:
                    await message.reply_text(
                        f"[+] {to_monocaps('𝚖𝚊𝚒𝚗 𝚌𝚑𝚊𝚗𝚗𝚎𝚕 𝚊𝚞𝚝𝚘-𝚍𝚎𝚕𝚎𝚝𝚒𝚘𝚗 𝚜𝚎𝚝 𝚝𝚘')} <code>{seconds}</code> {to_monocaps('𝚜𝚎𝚌𝚘𝚗𝚍𝚜')}."
                    )
            except ValueError:
                await message.reply_text(f"▼ {to_monocaps('𝚙𝚕𝚎𝚊𝚜𝚎 𝚜𝚎𝚗𝚍 𝚊 𝚟𝚊𝚕𝚒𝚍 𝚗𝚘𝚗-𝚗𝚎𝚐𝚊𝚝𝚒𝚟𝚎 𝚒𝚗𝚝𝚎𝚐𝚎𝚛')}.")
            return

    # 3. Direct Search-by-name Flow (If not in any configuration state)
    if text:
        posts = await database.search_posts_by_name(text)
        if posts:
            bot_info = await client.get_me()
            # Get user DM delete settings
            user_doc = await database.get_user(uid)
            dm_del = user_doc.get("dm_autodelete_duration", 0) if user_doc else 0
            
            for post in posts:
                b64_id = encode_post_id(post["_id"])
                deep_link = f"https://t.me/{bot_info.username}?start={b64_id}"
                
                # Send the post with BASE64 DEEP LINK button
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        text=to_monocaps("𝚐𝚎𝚝 𝚙𝚘𝚜𝚝"),
                        url=deep_link
                    )
                ]])
                
                sent_msg = await message.reply_photo(
                    photo=post["photo_file_id"],
                    caption=post["caption_html"],
                    reply_markup=keyboard
                )
                
                if dm_del > 0:
                    await schedule_message_deletion(
                        chat_id=uid,
                        message_id=sent_msg.id,
                        seconds=dm_del,
                        del_type="dm"
                    )
                    
            if dm_del > 0:
                notif = await message.reply_text(
                    f"◆ {to_monocaps('𝚜𝚎𝚗𝚝 𝚙𝚘𝚜𝚝𝚜 𝚠𝚒𝚕𝚕 𝚋𝚎 𝚊𝚞𝚝𝚘-𝚍𝚎𝚕𝚎𝚝𝚎𝚍 𝚒𝚗')} <code>{dm_del}</code> {to_monocaps('𝚜𝚎𝚌𝚘𝚗𝚍𝚜')}."
                )
                await schedule_message_deletion(
                    chat_id=uid,
                    message_id=notif.id,
                    seconds=dm_del,
                    del_type="dm"
                )
        else:
            # Record unfound search request in Request Channel
            user_info = f"@{message.from_user.username}" if message.from_user.username else f"𝙸𝙳: {message.from_user.id}"
            request_text = (
                f"{style_header('𝚗𝚎𝚠 𝚙𝚘𝚜𝚝 𝚛𝚎𝚚𝚞𝚎𝚜𝚝')}\n\n"
                f"◆ {to_monocaps('𝚚𝚞𝚎𝚛𝚢')}: <code>{text}</code>\n"
                f"◆ {to_monocaps('𝚞𝚜𝚎𝚛')}: {user_info}"
            )
            try:
                await client.send_message(
                    chat_id=config.REQUEST_CHANNEL_ID,
                    text=request_text
                )
            except Exception as e:
                logger.error(f"Failed to send request to Request Channel: {e}")
                
            await message.reply_text(
                f"▼ {to_monocaps('𝚗𝚘 𝚙𝚘𝚜𝚝𝚜 𝚏𝚘𝚞𝚗𝚍 𝚖𝚊𝚝𝚌𝚑𝚒𝚗𝚐')} <code>{text}</code>.\n"
                f"◆ {to_monocaps('𝚢𝚘𝚞𝚛 𝚛𝚎𝚚𝚞𝚎𝚜𝚝 𝚑𝚊𝚜 𝚋𝚎𝚎𝚗 𝚕𝚘𝚐𝚐𝚎𝚍')}."
            )

# --- HEALTH CHECK SERVER FOR RENDER ---
def start_health_check_server():
    """Starts a simple HTTP server to satisfy Render's health check requirements for web services."""
    import http.server
    import socketserver
    import threading
    import os

    port = int(os.getenv("PORT", "8080"))

    class HealthCheckHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/health"):
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "healthy"}')
            else:
                self.send_response(404)
                self.end_headers()
                
        def log_message(self, format, *args):
            # Suppress default request logging to avoid cluttering bot stdout
            pass

    def run_server():
        socketserver.TCPServer.allow_reuse_address = True
        try:
            with socketserver.TCPServer(("0.0.0.0", port), HealthCheckHandler) as httpd:
                logger.info(f"Health check server listening on port {port}")
                httpd.serve_forever()
        except Exception as e:
            logger.error(f"Failed to start health check server: {e}")

    threading.Thread(target=run_server, daemon=True).start()

# --- APP STARTUP HANDLER ---
async def main():
    logger.info("Starting Telegram bot...")
    
    # Start health check server for Render (if running as Web Service)
    start_health_check_server()
    
    await app.start()
    logger.info("Telegram bot started successfully!")
    
    # Start the background task scheduler
    asyncio.create_task(start_auto_delete_scheduler())
    
    # Keep the bot running
    await idle()
    
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())

