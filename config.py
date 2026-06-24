import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

STORAGE_CHANNEL_ID = int(os.getenv("STORAGE_CHANNEL_ID", "0"))
REQUEST_CHANNEL_ID = int(os.getenv("REQUEST_CHANNEL_ID", "0"))

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/telegram_index_bot")

def validate_config():
    """Validates that all necessary configurations are set."""
    errors = []
    if not API_ID:
        errors.append("API_ID is missing or not a valid integer")
    if not API_HASH:
        errors.append("API_HASH is missing")
    if not BOT_TOKEN:
        errors.append("BOT_TOKEN is missing")
    if not OWNER_ID:
        errors.append("OWNER_ID is missing or not a valid integer")
    if not STORAGE_CHANNEL_ID:
        errors.append("STORAGE_CHANNEL_ID is missing or not a valid integer")
    if not REQUEST_CHANNEL_ID:
        errors.append("REQUEST_CHANNEL_ID is missing or not a valid integer")
    
    if errors:
        raise ValueError("Configuration validation failed:\n" + "\n".join(errors))
