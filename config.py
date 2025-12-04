"""
Configuration du bot Telegram de prédiction Baccarat
"""
import os
import json # NOUVEAU

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

# --- NOUVELLES CONFIGURATIONS ---

# Offsets par défaut
A_OFFSET_DEFAULT = 1 # Décalage de prédiction (N -> N + A_OFFSET)
R_OFFSET_DEFAULT = 0 # Nombre d'essais de vérification (N+0 à N+R_OFFSET)

# Emojis de vérification selon l'offset (N+0, N+1, N+2, etc.)
VERIFICATION_EMOJIS = {
    0: "✅0️⃣",  # 1er essai (N+0)
    1: "✅1️⃣",  # 2ème essai (N+1)
    2: "✅2️⃣",  # 3ème essai (N+2)
    3: "✅3️⃣",  # 4ème essai (N+3)
    4: "✅4️⃣",  # 5ème essai (N+4)
    5: "✅5️⃣",  # 6ème essai (N+5)
    6: "✅6️⃣",  # 7ème essai (N+6)
    7: "✅7️⃣",  # 8ème essai (N+7)
    8: "✅8️⃣",  # 9ème essai (N+8)
    9: "✅9️⃣",  # 10ème essai (N+9)
    10: "✅🔟"  # 11ème essai (N+10)
}
