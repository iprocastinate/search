import datetime
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
import config

# Initialize MongoDB Client
client = AsyncIOMotorClient(config.MONGO_URI)
db = client.get_default_database(default="telegram_index_bot")

# Collections
users_col = db["users"]
admins_col = db["admins"]
posts_col = db["posts"]
settings_col = db["settings"]
search_sessions_col = db["search_sessions"]
deletions_col = db["scheduled_deletions"]

# --- USER MANAGEMENT ---
async def add_user(user_id: int, username: str = None, first_name: str = None):
    """Adds a new user to the database if they do not exist."""
    user = await users_col.find_one({"_id": user_id})
    if not user:
        await users_col.insert_one({
            "_id": user_id,
            "username": username,
            "first_name": first_name,
            "dm_autodelete_duration": 0, # 0 means disabled
            "joined_at": datetime.datetime.utcnow()
        })
    elif username or first_name:
        await users_col.update_one(
            {"_id": user_id},
            {"$set": {"username": username, "first_name": first_name}}
        )

async def get_user(user_id: int):
    """Retrieves user document."""
    return await users_col.find_one({"_id": user_id})

async def update_user_dm_autodelete(user_id: int, duration: int):
    """Updates user's custom DM auto-delete duration."""
    await users_col.update_one(
        {"_id": user_id},
        {"$set": {"dm_autodelete_duration": duration}}
    )

async def get_all_users():
    """Returns a list of all user IDs."""
    cursor = users_col.find({}, {"_id": 1})
    return [doc["_id"] async for doc in cursor]

# --- ADMIN MANAGEMENT ---
async def add_admin(admin_id: int):
    """Adds a user as admin."""
    await admins_col.update_one(
        {"_id": admin_id},
        {"$set": {"added_at": datetime.datetime.utcnow()}},
        upsert=True
    )

async def remove_admin(admin_id: int):
    """Removes a user from admins."""
    await admins_col.delete_one({"_id": admin_id})

async def is_admin(user_id: int) -> bool:
    """Checks if the user is the owner or an admin."""
    if user_id == config.OWNER_ID:
        return True
    admin = await admins_col.find_one({"_id": user_id})
    return admin is not None

async def get_all_admins():
    """Returns a list of all admin user IDs."""
    cursor = admins_col.find({}, {"_id": 1})
    return [doc["_id"] async for doc in cursor]

# --- POST MANAGEMENT ---
async def add_post(name: str, photo_file_id: str, caption_html: str, channel_link: str, storage_msg_id: int) -> ObjectId:
    """Creates a new post and returns the inserted ObjectId."""
    result = await posts_col.insert_one({
        "name": name,
        "photo_file_id": photo_file_id,
        "caption_html": caption_html,
        "channel_link": channel_link,
        "storage_msg_id": storage_msg_id,
        "created_at": datetime.datetime.utcnow()
    })
    return result.inserted_id

async def get_post_by_id(post_id: ObjectId):
    """Retrieves post details by its ID."""
    return await posts_col.find_one({"_id": post_id})

async def search_posts_by_name(name_query: str):
    """Searches for posts matching the name (case-insensitive substring)."""
    cursor = posts_col.find({"name": {"$regex": name_query, "$options": "i"}})
    return [doc async for doc in cursor]

async def search_posts_prefix(prefix_query: str, skip: int = 0, limit: int = 10):
    """Searches for posts where name starts with the prefix_query (case-insensitive)."""
    # Regex ^ ensures prefix matching
    cursor = posts_col.find({"name": {"$regex": f"^{prefix_query}", "$options": "i"}}).skip(skip).limit(limit)
    return [doc async for doc in cursor]

async def count_posts_prefix(prefix_query: str) -> int:
    """Counts posts matching the prefix_query."""
    return await posts_col.count_documents({"name": {"$regex": f"^{prefix_query}", "$options": "i"}})

# --- SYSTEM SETTINGS ---
async def set_setting(key: str, value):
    """Sets a system-wide setting."""
    await settings_col.update_one(
        {"_id": key},
        {"$set": {"value": value}},
        upsert=True
    )

async def get_setting(key: str, default=None):
    """Gets a system-wide setting."""
    setting = await settings_col.find_one({"_id": key})
    if setting:
        return setting["value"]
    return default

# --- DELETION SCHEDULER ---
async def add_scheduled_deletion(chat_id: int, message_id: int, delete_at: datetime.datetime, deletion_type: str):
    """Schedules a message to be deleted at a specific time."""
    await deletions_col.insert_one({
        "chat_id": chat_id,
        "message_id": message_id,
        "delete_at": delete_at,
        "type": deletion_type
    })

async def get_expired_deletions():
    """Retrieves all scheduled deletions that have passed their execution time."""
    now = datetime.datetime.utcnow()
    cursor = deletions_col.find({"delete_at": {"$lte": now}})
    return [doc async for doc in cursor]

async def delete_scheduled_deletion(deletion_id: ObjectId):
    """Removes a deletion record from the database."""
    await deletions_col.delete_one({"_id": deletion_id})

# --- SEARCH SESSIONS ---
async def create_search_session(query: str) -> ObjectId:
    """Caches a search query and returns the session ID."""
    result = await search_sessions_col.insert_one({
        "query": query,
        "created_at": datetime.datetime.utcnow()
    })
    return result.inserted_id

async def get_search_session(session_id: ObjectId):
    """Retrieves a search query by its session ID."""
    return await search_sessions_col.find_one({"_id": session_id})
