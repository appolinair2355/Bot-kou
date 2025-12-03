import os
import asyncio
import re
import logging
import sys
import zipfile
import shutil
from datetime import datetime, timedelta, timezone, time
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from aiohttp import web
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID, PORT,
    SUIT_MAPPING_EVEN, SUIT_MAPPING_ODD, ALL_SUITS, SUIT_DISPLAY, SUIT_NORMALIZE
)

# --- Configuration et Initialisation ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Vérifications de la configuration
if not API_ID or API_ID == 0:
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH:
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN:
    logger.error("BOT_TOKEN manquant")
    exit(1)

logger.info(f"Configuration: SOURCE_CHANNEL={SOURCE_CHANNEL_ID}, PREDICTION_CHANNEL={PREDICTION_CHANNEL_ID}")

# Initialisation du client Telegram
session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

# --- Variables Globales d'État ---
pending_predictions = {}
processed_predictions = set()
processed_verifications = set()
current_game_number = 0
source_channel_ok = False
prediction_channel_ok = False
transfer_enabled = True

# --- Fonctions d'Analyse ---

def normalize_suit(suit: str) -> str:
    """Normalise un symbole de couleur."""
    return SUIT_NORMALIZE.get(suit, suit)

def extract_game_number(message: str):
    """Extrait le numéro de jeu du message."""
    match = re.search(r"#N\s*(\d+)\.?", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def extract_parentheses_groups(message: str):
    """Extrait le contenu entre parenthèses."""
    return re.findall(r"\(([^)]*)\)", message)

def get_first_suit_in_group(group_str: str) -> str:
    """Trouve la première couleur (suit) dans un groupe."""
    suit_pattern = r'[♠♥♦♣]|♠️|♥️|♦️|♣️|❤️|❤'
    match = re.search(suit_pattern, group_str)
    if match:
        return normalize_suit(match.group())
    return None

def suit_in_group(group_str: str, target_suit: str) -> bool:
    """Vérifie si une couleur est présente dans un groupe."""
    normalized_target = normalize_suit(target_suit)
    suit_pattern = r'[♠♥♦♣]|♠️|♥️|♦️|♣️|❤️|❤'
    matches = re.findall(suit_pattern, group_str)
    for match in matches:
        if normalize_suit(match) == normalized_target:
            return True
    return False

def is_odd(number: int) -> bool:
    """Vérifie si un numéro est impair."""
    return number % 2 != 0

def get_predicted_suit(base_suit: str, game_number: int) -> str:
    """
    Applique la transformation selon le numéro de jeu:
    - Jeux PAIRS: ♠️→♣️, ♣️→♠️, ♦️→♥️, ♥️→♦️
    - Jeux IMPAIRS: ♠️→♥️, ♣️→♦️, ♦️→♣️, ♥️→♠️
    """
    normalized = normalize_suit(base_suit)
    if is_odd(game_number):
        return SUIT_MAPPING_ODD.get(normalized, normalized)
    else:
        return SUIT_MAPPING_EVEN.get(normalized, normalized)

def is_message_finalized(message: str) -> bool:
    """Vérifie si le message est un résultat final."""
    if '⏰' in message:
        return False
    return '✅' in message or '🔰' in message

# --- Logique de Prédiction (Immédiate) ---

async def send_prediction_to_channel(target_game: int, predicted_suit: str, base_game: int, base_suit: str):
    """Envoie la prédiction au canal de prédiction."""
    try:
        display_suit = SUIT_DISPLAY.get(predicted_suit, predicted_suit)
        prediction_msg = f"📲Game:{target_game}:{display_suit} statut :⏳"

        msg_id = 0

        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and prediction_channel_ok:
            try:
                pred_msg = await client.send_message(PREDICTION_CHANNEL_ID, prediction_msg)
                msg_id = pred_msg.id
                logger.info(f"✅ Prédiction envoyée au canal: Jeu #{target_game} -> {display_suit}")
            except Exception as e:
                logger.error(f"❌ Erreur envoi prédiction au canal: {e}")
        else:
            logger.warning(f"⚠️ Canal de prédiction non accessible")

        pending_predictions[target_game] = {
            'message_id': msg_id,
            'suit': predicted_suit,
            'base_game': base_game,
            'base_suit': base_suit,
            'status': '⏳',
            'created_at': datetime.now().isoformat()
        }

        logger.info(f"Prédiction active: Jeu #{target_game} - {display_suit} (basé sur #{base_game})")
        return msg_id

    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        return None

async def update_prediction_status(game_number: int, new_status: str):
    """Met à jour le message de prédiction dans le canal."""
    try:
        if game_number not in pending_predictions:
            return False

        pred = pending_predictions[game_number]
        suit = pred['suit']
        display_suit = SUIT_DISPLAY.get(suit, suit)
        base_game = pred['base_game']
        base_suit = pred['base_suit']
        base_display = SUIT_DISPLAY.get(base_suit, base_suit)

        if new_status == '✅':
            parity = "Impaire" if is_odd(base_game) else "Paire"
            updated_msg = f"""📲Game:{game_number}:{display_suit} statut :{new_status}
⚜🟩validé   premier enseigne du Banquier : {base_display} numero du jeu precedent {parity}
{base_display}={display_suit}"""
        else:
            updated_msg = f"📲Game:{game_number}:{display_suit} statut :{new_status}"

        if PREDICTION_CHANNEL_ID and pred['message_id'] > 0 and prediction_channel_ok:
            try:
                await client.edit_message(PREDICTION_CHANNEL_ID, pred['message_id'], updated_msg)
                logger.info(f"✅ Prédiction #{game_number} mise à jour: {new_status}")
            except Exception as e:
                logger.error(f"❌ Erreur mise à jour dans le canal: {e}")

        pred['status'] = new_status

        if new_status in ['✅', '❌']:
            del pending_predictions[game_number]
            logger.info(f"Prédiction #{game_number} terminée: {new_status}")

        return True

    except Exception as e:
        logger.error(f"Erreur mise à jour prédiction: {e}")
        return False

# --- Traitement des Messages ---

async def process_prediction(message_text: str):
    """
    PRÉDICTION: Se fait immédiatement dès qu'un numéro est détecté.
    N'attend PAS que le message soit finalisé.
    """
    global current_game_number
    try:
        game_number = extract_game_number(message_text)
        if game_number is None:
            return

        current_game_number = game_number

        # Éviter les doublons de prédiction
        if game_number in processed_predictions:
            return
        processed_predictions.add(game_number)

        # Nettoyer l'historique
        if len(processed_predictions) > 500:
            old_predictions = sorted(processed_predictions)[:250]
            for p in old_predictions:
                processed_predictions.discard(p)

        groups = extract_parentheses_groups(message_text)
        if len(groups) < 2:
            logger.info(f"Jeu #{game_number}: Pas assez de groupes pour prédiction")
            return

        second_group = groups[1]
        first_suit_second_group = get_first_suit_in_group(second_group)

        if first_suit_second_group:
            predicted_suit = get_predicted_suit(first_suit_second_group, game_number)
            target_game = game_number + 1

            if target_game not in pending_predictions:
                parity = "impair" if is_odd(game_number) else "pair"
                logger.info(f"🎯 Jeu #{game_number} ({parity}): {first_suit_second_group} -> Prédiction #{target_game}: {predicted_suit}")
                await send_prediction_to_channel(target_game, predicted_suit, game_number, first_suit_second_group)
            else:
                logger.info(f"Prédiction #{target_game} déjà active")

    except Exception as e:
        logger.error(f"Erreur traitement prédiction: {e}")
        import traceback
        logger.error(traceback.format_exc())

async def process_verification(message_text: str):
    """
    VÉRIFICATION: Attend que le message soit finalisé.
    Vérifie si le costume prédit est dans le PREMIER groupe.
    """
    try:
        if not is_message_finalized(message_text):
            return

        game_number = extract_game_number(message_text)
        if game_number is None:
            return

        # Éviter les doublons de vérification
        message_hash = f"{game_number}_{message_text[:80]}"
        if message_hash in processed_verifications:
            return
        processed_verifications.add(message_hash)

        # Nettoyer l'historique
        if len(processed_verifications) > 500:
            processed_verifications.clear()

        groups = extract_parentheses_groups(message_text)
        if len(groups) < 1:
            return

        first_group = groups[0]

        # Vérifier si on a une prédiction en attente pour ce jeu
        if game_number in pending_predictions:
            pred = pending_predictions[game_number]
            target_suit = pred['suit']

            # Vérifier si le costume prédit est dans le PREMIER groupe
            if suit_in_group(first_group, target_suit):
                logger.info(f"✅ Jeu #{game_number}: {SUIT_DISPLAY.get(target_suit, target_suit)} trouvé dans le 1er groupe!")
                await update_prediction_status(game_number, '✅')
            else:
                logger.info(f"❌ Jeu #{game_number}: {SUIT_DISPLAY.get(target_suit, target_suit)} NON trouvé dans le 1er groupe")
                await update_prediction_status(game_number, '❌')

    except Exception as e:
        logger.error(f"Erreur traitement vérification: {e}")
        import traceback
        logger.error(traceback.format_exc())

async def transfer_to_admin(message_text: str):
    """Transfère le message à l'admin si activé."""
    if transfer_enabled and ADMIN_ID and ADMIN_ID != 0:
        try:
            await client.send_message(ADMIN_ID, f"📨 Message:\n\n{message_text}")
        except Exception as e:
            logger.error(f"❌ Erreur transfert admin: {e}")

# --- Gestion des Messages Telegram ---

@client.on(events.NewMessage())
async def handle_message(event):
    """Gère les nouveaux messages dans le canal source."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id

        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id

        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            
            # Prédiction immédiate (n'attend pas la finalisation)
            await process_prediction(message_text)
            
            # Vérification (attend la finalisation)
            await process_verification(message_text)

    except Exception as e:
        logger.error(f"Erreur handle_message: {e}")

@client.on(events.MessageEdited())
async def handle_edited_message(event):
    """Gère les messages édités dans le canal source."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id

        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id

        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            
            # Vérification sur messages édités (attend la finalisation)
            await process_verification(message_text)

    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")

# --- Reset Automatique ---

async def reset_all_data():
    """Efface toutes les données stockées."""
    global pending_predictions, processed_predictions, processed_verifications, current_game_number
    
    count = len(pending_predictions)
    pending_predictions.clear()
    processed_predictions.clear()
    processed_verifications.clear()
    current_game_number = 0
    
    logger.info(f"🔄 Reset effectué - {count} prédictions effacées")
    
    if ADMIN_ID and ADMIN_ID != 0:
        try:
            await client.send_message(ADMIN_ID, f"🔄 **Reset automatique effectué**\n\n{count} prédictions effacées.")
        except:
            pass

async def schedule_periodic_reset():
    """Reset automatique toutes les 2 heures."""
    while True:
        await asyncio.sleep(2 * 60 * 60)  # 2 heures
        logger.info("⏰ Reset périodique (2h)...")
        await reset_all_data()

async def schedule_daily_reset():
    """Reset quotidien à 00h59 WAT (UTC+1)."""
    wat_tz = timezone(timedelta(hours=1))
    
    while True:
        now = datetime.now(wat_tz)
        reset_time = now.replace(hour=0, minute=59, second=0, microsecond=0)
        
        if now >= reset_time:
            reset_time += timedelta(days=1)
        
        wait_seconds = (reset_time - now).total_seconds()
        logger.info(f"⏰ Prochain reset quotidien dans {wait_seconds/3600:.1f} heures")
        
        await asyncio.sleep(wait_seconds)
        
        logger.info("🌙 Reset quotidien à 00h59 WAT...")
        await reset_all_data()
        
        # Petite pause pour éviter les doubles déclenchements
        await asyncio.sleep(60)

# --- Commandes Administrateur ---

def is_admin(sender_id):
    return ADMIN_ID and ADMIN_ID != 0 and sender_id == ADMIN_ID

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel:
        return
    await event.respond("🤖 **Bot de Prédiction Baccarat**\n\nCommandes: `/status`, `/help`, `/debug`, `/deploy`, `/reset`")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel:
        return
    if not is_admin(event.sender_id):
        await event.respond("Commande réservée à l'administrateur")
        return

    status_msg = f"📊 **État des prédictions:**\n\n🎮 Jeu actuel: #{current_game_number}\n\n"
    
    if pending_predictions:
        status_msg += f"**🔮 Actives ({len(pending_predictions)}):**\n"
        for game_num, pred in sorted(pending_predictions.items()):
            display_suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
            status_msg += f"• Jeu #{game_num}: {display_suit} - Statut: {pred['status']}\n"
    else:
        status_msg += "**🔮 Aucune prédiction active**\n"

    await event.respond(status_msg)

@client.on(events.NewMessage(pattern='/reset'))
async def cmd_reset(event):
    if event.is_group or event.is_channel:
        return
    if not is_admin(event.sender_id):
        await event.respond("Commande réservée à l'administrateur")
        return
    
    await reset_all_data()
    await event.respond("🔄 **Reset manuel effectué!**\n\nToutes les prédictions ont été effacées.")

@client.on(events.NewMessage(pattern='/debug'))
async def cmd_debug(event):
    if event.is_group or event.is_channel:
        return
    if not is_admin(event.sender_id):
        await event.respond("Commande réservée à l'administrateur")
        return

    debug_msg = f"""🔍 **Informations de débogage:**

**Configuration:**
• Source Channel: {SOURCE_CHANNEL_ID}
• Prediction Channel: {PREDICTION_CHANNEL_ID}
• Admin ID: {ADMIN_ID}

**Accès aux canaux:**
• Canal source: {'✅ OK' if source_channel_ok else '❌ Non accessible'}
• Canal prédiction: {'✅ OK' if prediction_channel_ok else '❌ Non accessible'}

**État:**
• Jeu actuel: #{current_game_number}
• Prédictions actives: {len(pending_predictions)}

**Règles de transformation:**
• Jeux PAIRS: ♠️→♣️, ♣️→♠️, ♦️→♥️, ♥️→♦️
• Jeux IMPAIRS: ♠️→♥️, ♣️→♦️, ♦️→♣️, ♥️→♠️

**Reset automatique:**
• Toutes les 2 heures
• Quotidien à 00h59 WAT
"""
    await event.respond(debug_msg)

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel:
        return

    await event.respond("""📖 **Aide - Bot de Prédiction Baccarat**

**Règles de prédiction:**
Le bot lit le 2ème groupe du message source et prend la 1ère carte (couleur).
La prédiction est envoyée IMMÉDIATEMENT (n'attend pas la finalisation).

**Vérification:**
Attend que le message soit finalisé (✅ ou 🔰).
Vérifie si le costume prédit est dans le PREMIER groupe.

**Transformation selon parité du jeu:**
• Jeux PAIRS (ex: #1220):
  ♠️→♣️, ♣️→♠️, ♦️→♥️, ♥️→♦️
  
• Jeux IMPAIRS (ex: #1219):
  ♠️→♥️, ♣️→♦️, ♦️→♣️, ♥️→♠️

**Prédiction:** Toujours pour le jeu N+1

**Reset automatique:**
• Toutes les 2 heures
• Quotidien à 00h59 WAT

**Commandes:**
• `/start` - Démarrer le bot
• `/status` - Voir les prédictions actives
• `/debug` - Informations système
• `/reset` - Reset manuel des prédictions
• `/deploy` - Télécharger le bot pour Render.com
• `/transfert` - Activer le transfert des messages
• `/stoptransfert` - Désactiver le transfert
• `/help` - Cette aide
""")

@client.on(events.NewMessage(pattern='/transfert|/activetransfert'))
async def cmd_active_transfert(event):
    if event.is_group or event.is_channel:
        return
    if not is_admin(event.sender_id):
        await event.respond("Commande réservée à l'administrateur")
        return
    global transfer_enabled
    transfer_enabled = True
    await event.respond("✅ Transfert des messages activé!")

@client.on(events.NewMessage(pattern='/stoptransfert'))
async def cmd_stop_transfert(event):
    if event.is_group or event.is_channel:
        return
    if not is_admin(event.sender_id):
        await event.respond("Commande réservée à l'administrateur")
        return
    global transfer_enabled
    transfer_enabled = False
    await event.respond("⛔ Transfert des messages désactivé.")

@client.on(events.NewMessage(pattern='/deploy'))
async def cmd_deploy(event):
    """Génère un fichier ZIP deployable sur Render.com"""
    if event.is_group or event.is_channel:
        return
    if not is_admin(event.sender_id):
        await event.respond("Commande réservée à l'administrateur")
        return

    await event.respond("📦 Préparation du fichier de déploiement...")

    try:
        deploy_dir = '/tmp/deploy_package'
        if os.path.exists(deploy_dir):
            shutil.rmtree(deploy_dir)
        os.makedirs(deploy_dir)

        config_content = '''"""
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
'''
        with open(os.path.join(deploy_dir, 'config.py'), 'w', encoding='utf-8') as f:
            f.write(config_content)

        with open('main.py', 'r', encoding='utf-8') as f:
            main_content = f.read()
        with open(os.path.join(deploy_dir, 'main.py'), 'w', encoding='utf-8') as f:
            f.write(main_content)

        requirements_content = '''telethon==1.35.0
aiohttp==3.9.5
python-dotenv==1.0.1
pyyaml==6.0.1
openpyxl==3.1.2
'''
        with open(os.path.join(deploy_dir, 'requirements.txt'), 'w', encoding='utf-8') as f:
            f.write(requirements_content)

        render_content = '''services:
  - type: web
    name: telegram-prediction-bot
    env: python
    region: oregon
    plan: free
    buildCommand: pip install -r requirements.txt
    startCommand: python main.py
    envVars:
      - key: PORT
        value: 10000
      - key: API_ID
        sync: false
      - key: API_HASH
        sync: false
      - key: BOT_TOKEN
        sync: false
      - key: ADMIN_ID
        sync: false
      - key: SOURCE_CHANNEL_ID
        value: -1002682552255
      - key: PREDICTION_CHANNEL_ID
        value: -1003343276131
'''
        with open(os.path.join(deploy_dir, 'render.yaml'), 'w', encoding='utf-8') as f:
            f.write(render_content)

        readme_content = '''# Bot de Prédiction Baccarat

## Déploiement sur Render.com

1. Créez un compte sur https://render.com
2. Uploadez ce projet sur GitHub
3. Sur Render, créez un nouveau "Web Service" depuis votre repo GitHub
4. Configurez les variables d'environnement:
   - API_ID: Votre API ID Telegram
   - API_HASH: Votre API Hash Telegram
   - BOT_TOKEN: Token de votre bot (@BotFather)
   - ADMIN_ID: Votre ID Telegram

## Règles de Prédiction

**Prédiction (immédiate):**
- Lit la première carte du 2ème groupe
- Applique la transformation selon parité du jeu
- Prédit pour le jeu N+1

**Vérification (après finalisation):**
- Vérifie si le costume prédit est dans le 1er groupe

**Transformations:**
- Jeux PAIRS: ♠️→♣️, ♣️→♠️, ♦️→♥️, ♥️→♦️
- Jeux IMPAIRS: ♠️→♥️, ♣️→♦️, ♦️→♣️, ♥️→♠️

**Reset automatique:**
- Toutes les 2 heures
- Quotidien à 00h59 WAT
'''
        with open(os.path.join(deploy_dir, 'README.md'), 'w', encoding='utf-8') as f:
            f.write(readme_content)

        zip_path = '/tmp/ren.zip'
        if os.path.exists(zip_path):
            os.remove(zip_path)

        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(deploy_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, deploy_dir)
                    zipf.write(file_path, arcname)

        await client.send_file(
            event.chat_id,
            zip_path,
            caption="📦 **ren.zip**\n\nFichier prêt pour déploiement sur Render.com (port 10000)\n\nContenu:\n• main.py\n• config.py\n• requirements.txt\n• render.yaml\n• README.md\n\n**Nouveautés:**\n• Prédiction immédiate\n• Vérification sur 1er groupe\n• Reset auto 2h + 00h59 WAT"
        )

        shutil.rmtree(deploy_dir)
        os.remove(zip_path)

        logger.info("✅ Fichier ren.zip envoyé")

    except Exception as e:
        logger.error(f"Erreur création deploy: {e}")
        await event.respond(f"❌ Erreur: {e}")

# --- Serveur Web ---

async def index(request):
    html = f"""<!DOCTYPE html>
<html>
<head><title>Bot Prédiction Baccarat</title></head>
<body>
<h1>🎯 Bot de Prédiction Baccarat</h1>
<p>Le bot est en ligne et surveille les canaux.</p>
<p><strong>Jeu actuel:</strong> #{current_game_number}</p>
<p><strong>Prédictions actives:</strong> {len(pending_predictions)}</p>
</body>
</html>"""
    return web.Response(text=html, content_type='text/html', status=200)

async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"🌐 Serveur web démarré sur le port {PORT}")

# --- Démarrage Principal ---

async def verify_channels():
    """Vérifie l'accès aux canaux."""
    global source_channel_ok, prediction_channel_ok

    try:
        if SOURCE_CHANNEL_ID and SOURCE_CHANNEL_ID != 0:
            try:
                entity = await client.get_entity(SOURCE_CHANNEL_ID)
                source_channel_ok = True
                logger.info(f"✅ Accès au canal source: {getattr(entity, 'title', SOURCE_CHANNEL_ID)}")
            except Exception as e:
                logger.error(f"❌ Impossible d'accéder au canal source: {e}")

        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0:
            try:
                entity = await client.get_entity(PREDICTION_CHANNEL_ID)
                prediction_channel_ok = True
                logger.info(f"✅ Accès au canal de prédiction: {getattr(entity, 'title', PREDICTION_CHANNEL_ID)}")
            except Exception as e:
                logger.error(f"❌ Impossible d'accéder au canal de prédiction: {e}")

    except Exception as e:
        logger.error(f"Erreur vérification canaux: {e}")

async def main():
    """Fonction principale."""
    try:
        await client.start(bot_token=BOT_TOKEN)
        me = await client.get_me()
        logger.info(f"✅ Bot connecté: @{me.username}")

        await verify_channels()
        await start_web_server()

        # Lancer les tâches de reset automatique
        asyncio.create_task(schedule_periodic_reset())
        asyncio.create_task(schedule_daily_reset())

        logger.info("🚀 Bot opérationnel - En attente de messages...")
        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"Erreur principale: {e}")
        import traceback
        logger.error(traceback.format_exc())

if __name__ == "__main__":
    asyncio.run(main())
