"""
Configuration du bot Telegram de prédiction Baccarat
"""
import os

def parse_channel_id(env_var: str, default: str) -> int:
    value = os.getenv(env_var) or default
    if value.startswith('-100'):
        return int(value)
    try:
        channel_id = int(value)
        if channel_id > 0 and len(str(channel_id)) >= 10:
            return int(f"-100{channel_id}") 
        return channel_id
    except ValueError:
        return 0

SOURCE_CHANNEL_ID = parse_channel_id('SOURCE_CHANNEL_ID', '-1002682552255')
PREDICTION_CHANNEL_ID = parse_channel_id('PREDICTION_CHANNEL_ID', '-1003343276131')
ADMIN_ID = int(os.getenv('ADMIN_ID') or '0')
API_ID = int(os.getenv('API_ID') or '0')
API_HASH = os.getenv('API_HASH') or ''
BOT_TOKEN = os.getenv('BOT_TOKEN') or ''
PORT = int(os.getenv('PORT') or '10000')

SUIT_MAPPING_EVEN = {'♠': '♣', '♣': '♠', '♦': '♥', '♥': '♦'}
SUIT_MAPPING_ODD = {'♠': '♥', '♣': '♦', '♦': '♣', '♥': '♠'}
ALL_SUITS = ['♥', '♠', '♦', '♣']
SUIT_DISPLAY = {'♠': '♠️', '♥': '❤️', '♦': '♦️', '♣': '♣️'}
SUIT_NORMALIZE = {'❤️': '♥', '❤': '♥', '♥️': '♥', '♠️': '♠', '♦️': '♦', '♣️': '♣'}
