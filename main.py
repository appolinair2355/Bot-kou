import os
import asyncio
import re
import logging
import sys
import zipfile
import shutil
import json
from datetime import datetime, timedelta, timezone, time
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from aiohttp import web
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID, PORT,
    SUIT_DISPLAY, SUIT_NORMALIZE,
    A_OFFSET_DEFAULT, R_OFFSET_DEFAULT, VERIFICATION_EMOJIS
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
A_OFFSET = A_OFFSET_DEFAULT
R_OFFSET = R_OFFSET_DEFAULT
CONFIG_FILE = 'bot_config.json'
prediction_block_until = None 

# Variables pour la commande /ec (Écart Personnalisé)
ec_active = False
ec_gaps = []  # Liste des écarts [3, 4, 5, ...]
ec_gap_index = 0
ec_last_source_game = 0 # Le numéro de jeu source (N) qui a déclenché la dernière prédiction
ec_first_trigger_done = False # Vrai après la première prédiction P1

# --- Fonctions de Persistance ---

def load_config():
    """Charge la configuration depuis le fichier JSON."""
    global A_OFFSET, R_OFFSET, ec_active, ec_gaps, ec_gap_index, ec_last_source_game, ec_first_trigger_done
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                A_OFFSET = config.get('a_offset', A_OFFSET_DEFAULT)
                R_OFFSET = config.get('r_offset', R_OFFSET_DEFAULT)
                # Chargement EC
                ec_active = config.get('ec_active', False)
                ec_gaps = config.get('ec_gaps', [])
                ec_gap_index = config.get('ec_gap_index', 0)
                ec_last_source_game = config.get('ec_last_source_game', 0)
                ec_first_trigger_done = config.get('ec_first_trigger_done', False)
                
            logger.info(f"⚙️ Configuration chargée: A_OFFSET={A_OFFSET}, R_OFFSET={R_OFFSET}, EC_ACTIVE={ec_active}")
        except Exception as e:
            logger.error(f"Erreur chargement config: {e}")
            A_OFFSET = A_OFFSET_DEFAULT
            R_OFFSET = R_OFFSET_DEFAULT
            # En cas d'erreur de chargement, on s'assure que EC est désactivé
            ec_active = False
            ec_gaps = []
            ec_gap_index = 0
            ec_last_source_game = 0
            ec_first_trigger_done = False
    else:
        logger.info("⚙️ Fichier config.json non trouvé. Utilisation des valeurs par défaut.")
        save_config() # Sauvegarde les valeurs par défaut si le fichier n'existe pas

def save_config():
    """Sauvegarde la configuration dans le fichier JSON."""
    try:
        config = {
            'a_offset': A_OFFSET,
            'r_offset': R_OFFSET,
            # Sauvegarde EC
            'ec_active': ec_active,
            'ec_gaps': ec_gaps,
            'ec_gap_index': ec_gap_index,
            'ec_last_source_game': ec_last_source_game,
            'ec_first_trigger_done': ec_first_trigger_done
        }
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4)
        logger.info("⚙️ Configuration sauvegardée.")
    except Exception as e:
        logger.error(f"Erreur sauvegarde config: {e}")

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

def is_odd(number: int) -> bool:
    """Vérifie si un numéro est impair."""
    return number % 2 != 0

def is_message_finalized(message: str) -> bool:
    """Vérifie si le message est un résultat final."""
    if '⏰' in message:
        return False
    return '✅' in message or '🔰' in message

def suit_in_group(group_str: str, target_suit: str) -> bool:
    """Vérifie si une couleur est présente dans un groupe."""
    normalized_target = normalize_suit(target_suit)
    suit_pattern = r'[♠♥♦♣]|♠️|♥️|♦️|♣️|❤️|❤'
    matches = re.findall(suit_pattern, group_str)
    for match in matches:
        if normalize_suit(match) == normalized_target:
            return True
    return False

# --- Fonctions d'Extraction Avancée et de Logique de Carte (RÈGLES COMPLEXES) ---

CARD_VALUES_ODD = {'A', '3', '5', '7', '9', 'J', 'K'}
CARD_VALUES_EVEN = {'2', '4', '6', '8', 'T', '10', 'Q'} 

def is_card_value_odd(card_value: str) -> bool:
    """Détermine si la valeur de la carte est impaire (A, 3, 5, 7, 9, J, K)."""
    normalized_value = card_value.upper().replace('10', 'T') 
    return normalized_value in CARD_VALUES_ODD

def extract_first_card_details(group_str: str):
    """Extrait la valeur et la couleur de la première carte d'un groupe."""
    value_pattern = r'([A2-9JQKT]|10)?'
    suit_pattern = r'([♠♥♦♣]|♠️|♥️|♦️|♣️|❤️|❤)'
    
    match = re.search(value_pattern + suit_pattern, group_str, re.IGNORECASE)
    
    if match:
        value = match.group(1) if match.group(1) else ''
        suit = normalize_suit(match.group(2))
        return value, suit
    return None, None

def get_predicted_suit(base_suit: str, card_value: str, game_number: int) -> str:
    """
    Applique la transformation selon la nouvelle règle complexe (N, Couleur de base, Parité de la carte).
    """
    normalized_suit = normalize_suit(base_suit)
    is_odd_game = is_odd(game_number)
    
    # Vérification de la valeur de la carte
    if not card_value:
        # S'il n'y a pas de valeur, on suppose IMPAIRE par défaut
        is_value_odd = True
        logger.warning(f"Jeu #{game_number}: Valeur de carte manquante pour la base {base_suit}. Défaut: IMPAIRE.")
    else:
        is_value_odd = is_card_value_odd(card_value) 
    
    H = '♥' # Coeur (❤️)
    S = '♠' # Pique (♠️)
    D = '♦' # Carreau (♦️)
    C = '♣' # Trèfle (♣️)
    
    # --- Jeux PAIRS (is_odd_game est False) ---
    if not is_odd_game:
        # 1. Enseigne : H ou S (❤️ ou ♠️)
        if normalized_suit in [H, S]:
            if not is_value_odd: # Valeur PAIRE (2, 4, 6, 8, 10, Q)
                # ♠️ → ♣️ et ❤️ → ♦️
                return {'♠': C, '♥': D}.get(normalized_suit, normalized_suit)
            else: # Valeur IMPAIRE (A, 3, 5, 7, 9, J, K)
                # ♠️ → ♠️ et ❤️ → ❤️ (Aucune transformation)
                return normalized_suit
        
        # 2. Enseigne : D ou C (♦️ ou ♣️)
        elif normalized_suit in [D, C]:
            if not is_value_odd: # Valeur PAIRE (2, 4, 6, 8, 10, Q)
                # ♦️ → ♠️ et ♣️ → ❤️
                return {'♦': S, '♣': H}.get(normalized_suit, normalized_suit)
            else: # Valeur IMPAIRE (A, 3, 5, 7, 9, J, K)
                # ♦️ → ♣️ et ♣️ → ♦️
                return {'♦': C, '♣': D}.get(normalized_suit, normalized_suit)

    # --- Jeux IMPAIRS (is_odd_game est True) ---
    else:
        # 1. Enseigne : H ou S (❤️ ou ♠️)
        if normalized_suit in [H, S]:
            if not is_value_odd: # Valeur PAIRE (2, 4, 6, 8, 10, Q)
                # ♠️ → ❤️ et ❤️ → ♣️
                return {'♠': H, '♥': C}.get(normalized_suit, normalized_suit)
            else: # Valeur IMPAIRE (A, 3, 5, 7, 9, J, K)
                # ♠️ → ♦️ et ❤️ → ♠️
                return {'♠': D, '♥': S}.get(normalized_suit, normalized_suit)
        
        # 2. Enseigne : D ou C (♦️ ou ♣️)
        elif normalized_suit in [D, C]:
            if not is_value_odd: # Valeur PAIRE (2, 4, 6, 8, 10, Q)
                # ♦️ → ❤️ et ♣️ → ♠️
                return {'♦': H, '♣': S}.get(normalized_suit, normalized_suit)
            else: # Valeur IMPAIRE (A, 3, 5, 7, 9, J, K)
                # ♦️ → ♦️ et ♣️ → ♣️ (Aucune transformation)
                return normalized_suit
    
    return normalized_suit

# --- Logique de Prédiction (Immédiate) ---

async def send_prediction_to_channel(target_game: int, predicted_suit: str, base_game: int, base_suit: str):
    """Envoie la prédiction au canal de prédiction."""
    global R_OFFSET
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
            'r_offset': R_OFFSET, 
            'verification_attempt': 0, 
            'created_at': datetime.now().isoformat()
        }

        logger.info(f"Prédiction active: Jeu #{target_game} - {display_suit} (basé sur #{base_game})")
        return msg_id

    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        return None

async def update_prediction_status(game_number: int, new_status: str, verification_game_number: int = None):
    """Met à jour le message de prédiction dans le canal."""
    try:
        if game_number not in pending_predictions:
            return False

        pred = pending_predictions[game_number]
        suit = pred['suit']
        display_suit = SUIT_DISPLAY.get(suit, suit)
        
        # Calcul de l'index de vérification (N+0, N+1, N+2, ...)
        verification_index = 0
        if verification_game_number is not None:
             verification_index = verification_game_number - game_number

        if new_status == '✅':
            # Utilise l'emoji basé sur l'index de vérification
            status_emoji = VERIFICATION_EMOJIS.get(verification_index, '✅')
            
            # Correction: Simplification du message de succès comme demandé
            updated_msg = f"📲Game:{game_number}:{display_suit} statut :{status_emoji}"
            
        elif new_status == '❌':
            # Message de statut SIMPLE pour l'échec
            updated_msg = f"📲Game:{game_number}:{display_suit} statut :{new_status}"
        else:
            updated_msg = f"📲Game:{game_number}:{display_suit} statut :{new_status}"


        if PREDICTION_CHANNEL_ID and pred['message_id'] > 0 and prediction_channel_ok:
            try:
                await client.edit_message(PREDICTION_CHANNEL_ID, pred['message_id'], updated_msg)
                logger.info(f"✅ Prédiction #{game_number} mise à jour: {new_status} (Essai N+{verification_index})")
            except Exception as e:
                logger.error(f"❌ Erreur mise à jour dans le canal: {e}")

        pred['status'] = new_status

        if new_status in ['✅', '❌']:
            # La prédiction est terminée
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
    Gère la logique de blocage /time et la logique de séquence /ec.
    """
    global current_game_number, A_OFFSET, prediction_block_until, ec_active, ec_gaps, ec_gap_index, ec_last_source_game, ec_first_trigger_done
    try:
        current_time = datetime.now()
        should_trigger = False
        log_mode = ""
        
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
        
        # Extraction de la valeur ET de la couleur
        card_value, base_suit = extract_first_card_details(second_group)

        if not base_suit:
            logger.info(f"Jeu #{game_number}: Pas de couleur trouvée dans le 2nd groupe.")
            return
            
        predicted_suit = get_predicted_suit(base_suit, card_value, game_number)
        
        # --- LOGIQUE DE DÉCLENCHEMENT DE LA PRÉDICTION ---

        if ec_active and ec_gaps:
            # Mode EC activé: Priorité, ignore le blocage /time
            
            if not ec_first_trigger_done:
                # P1: Première prédiction après /ec activation. Déclenchement immédiat.
                should_trigger = True 
                log_mode = "EC (P1 Initial) N + A_OFFSET"

                # Mise à jour de l'état pour P2 après succès
                ec_last_source_game = game_number # N=100 est l'ancre
                # ec_gap_index reste 0 (P2 utilisera G1=3)
                ec_first_trigger_done = True
                
            else:
                # Subsequent predictions (P2, P3, P4, ...)
                
                # Le gap à utiliser (G1, G2, G3, ...)
                current_gap = ec_gaps[ec_gap_index]
                
                # Le numéro de jeu source requis pour déclencher (e.g., 100 + 3 = 103)
                required_source_game = ec_last_source_game + current_gap
                
                if game_number >= required_source_game:
                    # Déclenchement! N_current a atteint ou dépassé le requis.
                    should_trigger = True
                    
                    # --- Mise à jour de l'état pour la *prochaine* prédiction ---
                    
                    # Avance l'index pour la prochaine rotation (P3 utilisera G2=4)
                    ec_gap_index = (ec_gap_index + 1) % len(ec_gaps)
                    
                    # L'actuel game_number (e.g., 103, 107, 112) devient la nouvelle ancre
                    ec_last_source_game = game_number 
                    
                    log_mode = f"EC (Next P) N + A_OFFSET, Gap {current_gap} satisfied by N={game_number}"
                    
                else:
                    # Sauter: N_current est trop bas, attendre.
                    logger.info(f"EC: Skip prediction for #{game_number}. Waiting for source game #{required_source_game} (Gap {current_gap}). Last anchor: #{ec_last_source_game}")
                    return # Sauter la prédiction
                    
            if should_trigger:
                # Sauvegarde l'état EC avant l'envoi, juste au cas où l'envoi échoue
                save_config()

        else:
            # Mode A_OFFSET standard (et vérification du blocage /time)
            
            if prediction_block_until and prediction_block_until > current_time:
                remaining_seconds = (prediction_block_until - current_time).total_seconds()
                logger.info(f"⏳ PRÉDICTION BLOQUÉE par /time: Reste {remaining_seconds:.1f} secondes. Ignoré pour Jeu #{game_number}")
                return
            
            # Si le temps de blocage est passé, on réinitialise la variable
            if prediction_block_until and prediction_block_until <= current_time:
                prediction_block_until = None
                logger.warning("Blocage des prédictions /time levé automatiquement.")

            should_trigger = True
            log_mode = f"A_OFFSET (N+{A_OFFSET})"


        # --- Déclenchement de la Prédiction ---
        if should_trigger:
            target_game = game_number + A_OFFSET
            
            if target_game not in pending_predictions and target_game > current_game_number:
                
                parity = "impair" if is_odd(game_number) else "pair"
                card_info = f"{card_value or ''}{SUIT_DISPLAY.get(base_suit, base_suit)}"
                
                logger.info(f"🎯 Jeu #{game_number} ({parity}): Carte {card_info} -> Prédiction #{target_game}: {predicted_suit} ({log_mode})")
                
                await send_prediction_to_channel(target_game, predicted_suit, game_number, base_suit)
                
            else:
                logger.info(f"Prédiction #{target_game} déjà active ou cible trop proche de l'actuel ({current_game_number})")

    except Exception as e:
        logger.error(f"Erreur traitement prédiction: {e}")
        import traceback
        logger.error(traceback.format_exc())

async def process_verification(message_text: str):
    """
    VÉRIFICATION: Attend que le message soit finalisé.
    Vérifie si le costume prédit est dans le PREMIER groupe.
    Gère la vérification sur N+0 à N+R_OFFSET.
    """
    try:
        if not is_message_finalized(message_text):
            return

        current_game_number = extract_game_number(message_text)
        if current_game_number is None:
            return

        # Éviter les doublons de vérification
        message_hash = f"{current_game_number}_{message_text[:80]}"
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
        
        # --- LOGIQUE DE VÉRIFICATION SUR R_OFFSET ESSAIS ---
        
        # Parcourir les prédictions en attente (pending_predictions)
        for pred_game_number, pred in list(pending_predictions.items()):
            target_suit = pred['suit']
            r_offset = pred['r_offset']
            
            # Si le jeu actuel est dans la fenêtre de vérification (de N+0 à N+r_offset)
            # La fenêtre va de pred_game_number (N+0) à pred_game_number + r_offset
            if pred_game_number <= current_game_number <= pred_game_number + r_offset:
                
                # Vérifier si la couleur prédite est dans le PREMIER groupe
                if suit_in_group(first_group, target_suit):
                    # SUCCÈS
                    logger.info(f"✅ Jeu #{current_game_number}: {SUIT_DISPLAY.get(target_suit, target_suit)} trouvé dans le 1er groupe! (Prédiction #{pred_game_number})")
                    await update_prediction_status(pred_game_number, '✅', current_game_number)
                
                elif current_game_number == pred_game_number + r_offset:
                    # ÉCHEC (Dernier essai atteint)
                    logger.info(f"❌ Jeu #{current_game_number}: {SUIT_DISPLAY.get(target_suit, target_suit)} NON trouvé après {r_offset} essais. (Prédiction #{pred_game_number})")
                    await update_prediction_status(pred_game_number, '❌')
                
                else:
                    # ÉCHEC (Essai non final), on incrémente le compteur pour le prochain jeu
                    pred['verification_attempt'] += 1
                    # Note: On ne met pas à jour le statut du message ici, on attend soit le succès, soit l'échec final.
                    logger.info(f"⏳ Jeu #{current_game_number}: {SUIT_DISPLAY.get(target_suit, target_suit)} non trouvé. Continue vérification pour #{pred_game_number} (Essai: {pred['verification_attempt']})")

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
    await event.respond("🤖 **Bot de Prédiction Baccarat**\n\nCommandes: `/status`, `/help`, `/debug`, `/deploy`, `/reset`, `/a`, `/r`, `/time`, `/ec`")

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
            status_msg += f"• Jeu #{game_num}: {display_suit} - Statut: {pred['status']} (Base #{pred['base_game']}, R={pred['r_offset']}, Essai {pred['verification_attempt']})\n"
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
    
    emojis = ", ".join([f"{VERIFICATION_EMOJIS[i]}" for i in range(R_OFFSET + 1)])

    # Statut /time
    time_status = "Inactif"
    if prediction_block_until and prediction_block_until > datetime.now():
        remaining_seconds = (prediction_block_until - datetime.now()).total_seconds()
        time_status = f"Bloqué ({remaining_seconds:.1f}s restantes)"
    
    # Statut /ec
    ec_status = "Inactif"
    ec_info = ""
    if ec_active and ec_gaps:
        gaps_str_display = ", ".join(map(str, ec_gaps))
        current_gap = ec_gaps[ec_gap_index] if ec_gaps else 'N/A'
        
        ec_status = f"ACTIF (Écarts: {gaps_str_display})"
        
        if ec_last_source_game == 0:
             ec_next_anchor = "En attente de P1..."
        elif not ec_first_trigger_done:
            ec_next_anchor = f"Prochaine ancre pour P2: #{ec_last_source_game} + Gap {current_gap} = #{ec_last_source_game + current_gap}"
        else:
             ec_next_anchor = f"Prochaine ancre: #{ec_last_source_game} + Gap {current_gap} = #{ec_last_source_game + current_gap}"


        ec_info = f"• Ancre Source Précédente: #{ec_last_source_game}\n• Écart/Index Actuel: {current_gap}/{ec_gap_index}\n• {ec_next_anchor}"


    debug_msg = f"""🔍 **Informations de débogage:**

**Configuration:**
• Source Channel: {SOURCE_CHANNEL_ID}
• Prediction Channel: {PREDICTION_CHANNEL_ID}
• Admin ID: {ADMIN_ID}

**Accès aux canaux:**
• Canal source: {'✅ OK' if source_channel_ok else '❌ Non accessible'}
• Canal prédiction: {'✅ OK' if prediction_channel_ok else '❌ Non accessible'}

**Offsets (Persistants):**
• A_OFFSET (/a): N + {A_OFFSET} (Utilisé par défaut ou si /ec actif)
• R_OFFSET (/r): {R_OFFSET}

**Modes Spéciaux:**
• Blocage /time: {time_status} (Ignoré si /ec actif)
• Mode /ec: {ec_status}
{ec_info}

**État:**
• Jeu actuel: #{current_game_number}
• Prédictions actives: {len(pending_predictions)}
"""
    await event.respond(debug_msg)

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel:
        return

    await event.respond("""📖 **Aide - Bot de Prédiction Baccarat**

**Règles de prédiction:**
La transformation dépend de la **parité du jeu (N)** et de la **parité de la carte (Paire/Impaire)**. La prédiction est TOUJOURS pour le jeu **N + A_OFFSET** (où N est le jeu source).

**Vérification:**
Vérifie si le costume prédit est dans le PREMIER groupe pour les jeux **N+0 à N+R_OFFSET**.

**Commandes Administrateur:**
• `/a [valeur]` - Offset de prédiction standard (défaut: 1)
• `/r [valeur]` - Nombre d'essais de vérification (0 à 10, défaut: 0)
• `/time [secondes]` - **BLOQUE** temporairement l'envoi de nouvelles prédictions (mode standard uniquement). (`/time 0` pour débloquer).
• `/ec [e1,e2,...]` - **MODE ÉCART PERSONNALISÉ**. Prend le contrôle du déclenchement des prédictions. **Ignore** `/time`. (Ex: `/ec 3,4,5`). Utilisez `/ec 0` pour désactiver.
• `/status` - Voir les prédictions actives
• `/debug` - Informations système
• `/reset` - Reset manuel des prédictions
• `/deploy` - Télécharger le bot pour Render.com
""")

@client.on(events.NewMessage(pattern='/a(?: (\d+))?'))
async def cmd_a_offset(event):
    if event.is_group or event.is_channel:
        return
    if not is_admin(event.sender_id):
        await event.respond("Commande réservée à l'administrateur")
        return
    
    global A_OFFSET
    match = re.match(r'/a (\d+)', event.message.message)
    
    if match:
        new_a = int(match.group(1))
        A_OFFSET = new_a
        save_config()
        await event.respond(f"✅ **Offset de prédiction (/a)** mis à jour.\n\nLa prédiction sera lancée pour le jeu **N + {A_OFFSET}**.")
    else:
        await event.respond(f"ℹ️ **Offset de prédiction actuel (/a): N + {A_OFFSET}**\n\nUtilisation: `/a [valeur]` (ex: `/a 3`)")


@client.on(events.NewMessage(pattern='/r(?: (\d+))?'))
async def cmd_r_offset(event):
    if event.is_group or event.is_channel:
        return
    if not is_admin(event.sender_id):
        await event.respond("Commande réservée à l'administrateur")
        return
    
    global R_OFFSET
    match = re.match(r'/r (\d+)', event.message.message)
    
    if match:
        new_r = int(match.group(1))
        if 0 <= new_r <= 10:
            R_OFFSET = new_r
            save_config()
            emojis = ", ".join([f"{VERIFICATION_EMOJIS[i]}" for i in range(new_r + 1)])
            await event.respond(f"""✅ **Offset de vérification (/r)** mis à jour: **{R_OFFSET}** essais supplémentaires.
La vérification se fera de N+0 à N+{R_OFFSET}.
\n**Émojis de succès:** {emojis}""")
        else:
            await event.respond("❌ La valeur de /r doit être comprise entre **0** et **10**.")
    else:
        emojis = ", ".join([f"{VERIFICATION_EMOJIS[i]}" for i in range(R_OFFSET + 1)])
        await event.respond(f"""ℹ️ **Offset de vérification actuel (/r): {R_OFFSET}**
La vérification se fait sur **{R_OFFSET + 1}** jeux (N+0 à N+{R_OFFSET}).
\n**Émojis de succès:** {emojis}
\nUtilisation: `/r [valeur]` (ex: `/r 2`)""")
        
@client.on(events.NewMessage(pattern='/time(?: (\d+))?'))
async def cmd_time(event):
    """
    Bloque la génération de nouvelles prédictions pendant une durée spécifiée.
    """
    if event.is_group or event.is_channel:
        return
    if not is_admin(event.sender_id):
        await event.respond("Commande réservée à l'administrateur")
        return
    
    global prediction_block_until, ec_active
    
    match = re.match(r'/time (\d+)', event.message.message)
    current_time = datetime.now()
    wat_tz = timezone(timedelta(hours=1)) # Pour l'affichage à l'utilisateur

    if ec_active:
        await event.respond("❌ **Le mode `/ec` est actif et a la priorité.** Le blocage `/time` est ignoré.")
        return

    if match:
        duration_seconds = int(match.group(1))
        
        if duration_seconds == 0:
            prediction_block_until = None
            await event.respond("✅ **Blocage des prédictions levé.**\n\nLe bot reprendra les prédictions au prochain jeu.")
            logger.warning("Blocage des prédictions levé manuellement.")
            return

        if duration_seconds > 7200: # Limite à 2 heures (7200 secondes)
            await event.respond("❌ La durée maximale autorisée pour le blocage est de 7200 secondes (2 heures).")
            return

        block_end_time = current_time + timedelta(seconds=duration_seconds)
        prediction_block_until = block_end_time
        
        end_time_wat = block_end_time.astimezone(wat_tz).strftime("%H:%M:%S WAT")
        
        await event.respond(f"⛔ **Blocage des prédictions activé.**\n\nDurée: **{duration_seconds} secondes** ({duration_seconds/60:.2f} minutes).\nReprise des prédictions à **{end_time_wat}**.")
        logger.warning(f"Prédictions bloquées pendant {duration_seconds} secondes. Reprise à {prediction_block_until.isoformat()}")
        
    else:
        # Vérifier le statut actuel si aucun argument n'est fourni
        if prediction_block_until and prediction_block_until > current_time:
            remaining_seconds = (prediction_block_until - current_time).total_seconds()
            end_time_wat = prediction_block_until.astimezone(wat_tz).strftime("%H:%M:%S WAT")
            
            await event.respond(f"ℹ️ **Statut actuel: BLOQUÉ**\n\nFin du blocage à **{end_time_wat}** (Reste {remaining_seconds:.1f} secondes).\n\nPour débloquer: `/time 0`. Pour bloquer: `/time [secondes]`.")
        else:
            prediction_block_until = None
            await event.respond("ℹ️ **Statut actuel: ACTIF**\n\nUtilisation: `/time [secondes]` (ex: `/time 120` pour bloquer 2 minutes). Utilisez `/time 0` pour débloquer immédiatement.")

@client.on(events.NewMessage(pattern='/ec(?: (.+))?'))
async def cmd_ec(event):
    """
    Active le mode Écart Personnalisé (ec) et désactive le blocage /time.
    """
    if event.is_group or event.is_channel:
        return
    if not is_admin(event.sender_id):
        await event.respond("Commande réservée à l'administrateur")
        return
    
    global ec_active, ec_gaps, ec_gap_index, ec_last_source_game, ec_first_trigger_done, prediction_block_until, current_game_number
    
    match = re.match(r'/ec (.+)', event.message.message)
    
    if match:
        gap_str = match.group(1).strip()
        
        # Commande /ec 0 ou /ec OFF pour désactiver
        if gap_str.upper() in ['0', 'OFF', 'STOP']:
            ec_active = False
            ec_gaps = []
            ec_gap_index = 0
            ec_last_source_game = 0
            ec_first_trigger_done = False
            save_config()
            await event.respond("✅ **Mode Écart Personnalisé (/ec) désactivé.**\n\nLe bot revient à l'offset de prédiction standard (`/a`).")
            return

        # Parse les écarts (doivent être des entiers positifs)
        try:
            gaps = [int(g.strip()) for g in gap_str.split(',') if g.strip()]
            if not gaps or any(g <= 0 for g in gaps):
                raise ValueError("Les écarts doivent être des entiers positifs (séparés par des virgules).")
        except ValueError as e:
            await event.respond(f"❌ Erreur de format: {e}. Format attendu: `/ec 3,4,5` (entiers positifs).")
            return

        ec_active = True
        ec_gaps = gaps
        ec_gap_index = 0
        ec_last_source_game = 0 # Reset l'ancre pour forcer le P1 initial
        ec_first_trigger_done = False # Doit lancer P1 d'abord
        
        # Le blocage /time n'est pas nécessaire, car la logique /ec l'ignore, mais on le clear pour la clarté.
        if prediction_block_until:
            prediction_block_until = None
            await event.respond("⚠️ Le blocage `/time` a été levé automatiquement (priorité à `/ec`).")

        save_config()
        
        gaps_str_display = ", ".join(map(str, ec_gaps))
        await event.respond(f"""✅ **Mode Écart Personnalisé (/ec) activé!**
\n**Écarts définis ({len(ec_gaps)}):** {gaps_str_display}
\n**Prochaine prédiction (P1):** Se déclenchera sur le prochain jeu source reçu (N) et prédira pour **N + A_OFFSET** (`/a {A_OFFSET}`).
\n**P2 et suivants:** Se déclencheront lorsque le numéro source sera le **dernier N + le prochain écart** (Ex: 100 + {gaps[0]}).
\nPour désactiver: `/ec 0` ou `/ec off`""")

    else:
        # Afficher le statut actuel
        if ec_active and ec_gaps:
            gaps_str_display = ", ".join(map(str, ec_gaps))
            current_gap = ec_gaps[ec_gap_index] if ec_gaps else 'N/A'
            
            status_msg = f"ℹ️ **Mode Écart Personnalisé (/ec) ACTIF**\n"
            status_msg += f"**Écarts définis:** {gaps_str_display}\n"

            if not ec_first_trigger_done:
                status_msg += "**Statut:** En attente de la première prédiction (P1) sur le prochain jeu source (N)."
            else:
                next_required = ec_last_source_game + current_gap
                status_msg += f"**Prochain écart utilisé:** {current_gap} (Index {ec_gap_index} / {len(ec_gaps)})\n"
                status_msg += f"**Ancre du dernier N prédit:** #{ec_last_source_game}\n"
                status_msg += f"**Jeu source minimum requis pour la prochaine prédiction:** **#{next_required}**"
            
            status_msg += "\n\nUtilisation: `/ec 3,4,5` ou `/ec 0` pour désactiver."
        else:
            status_msg = "ℹ️ **Mode Écart Personnalisé (/ec) INACTIF**\n\nUtilisation: `/ec 3,4,5` pour définir la séquence d'écarts. Le bot se base sur le dernier numéro source (N) pour calculer le numéro source minimum pour la prédiction suivante (N + écart)."
            
        await event.respond(status_msg)

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

        # Création de config.py
        config_content = '''"""
Configuration du bot Telegram de prédiction Baccarat
"""
import os
import json

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

# Mappings simplifiés pour le code qui utilise la logique complexe
# Ces mappings ne sont plus utilisés dans get_predicted_suit, mais peuvent l'être ailleurs ou pour la compatibilité
SUIT_MAPPING_EVEN = {'♠': '♣', '♣': '♠', '♦': '♥', '♥': '♦'}
SUIT_MAPPING_ODD = {'♠': '♥', '♣': '♦', '♦': '♣', '♥': '♠'}
ALL_SUITS = ['♥', '♠', '♦', '♣']
SUIT_DISPLAY = {'♠': '♠️', '♥': '❤️', '♦': '♦️', '♣': '♣️'}
SUIT_NORMALIZE = {'❤️': '♥', '❤': '♥', '♥️': '♥', '♠️': '♠', '♦️': '♦', '♣️': '♣'}

# --- NOUVELLES CONFIGURATIONS ---

A_OFFSET_DEFAULT = 1
R_OFFSET_DEFAULT = 0

VERIFICATION_EMOJIS = {
    0: "✅0️⃣",
    1: "✅1️⃣",
    2: "✅2️⃣",
    3: "✅3️⃣",
    4: "✅4️⃣",
    5: "✅5️⃣",
    6: "✅6️⃣",
    7: "✅7️⃣",
    8: "✅8️⃣",
    9: "✅9️⃣",
    10: "✅🔟"
}
'''
        with open(os.path.join(deploy_dir, 'config.py'), 'w', encoding='utf-8') as f:
            f.write(config_content)

        # Copie de main.py
        with open('main.py', 'r', encoding='utf-8') as f:
            main_content = f.read()
        with open(os.path.join(deploy_dir, 'main.py'), 'w', encoding='utf-8') as f:
            f.write(main_content)

        # Création de requirements.txt
        requirements_content = '''telethon==1.35.0
aiohttp==3.9.5
python-dotenv==1.0.1
pyyaml==6.0.1
openpyxl==3.1.2
'''
        with open(os.path.join(deploy_dir, 'requirements.txt'), 'w', encoding='utf-8') as f:
            f.write(requirements_content)

        # Création de render.yaml
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

        # Création de README.md
        readme_content = f'''# Bot de Prédiction Baccarat

## Déploiement sur Render.com

1. Créez un compte sur https://render.com
2. Uploadez ce projet sur GitHub
3. Sur Render, créez un nouveau "Web Service" depuis votre repo GitHub
4. Configurez les variables d'environnement:
   - API_ID: Votre API ID Telegram
   - API_HASH: Votre API Hash Telegram
   - BOT_TOKEN: Token de votre bot (@BotFather)
   - ADMIN_ID: Votre ID Telegram

## Règles de Prédiction (Mise à Jour)

**Configuration par commandes:**
- `/a [valeur]`: Offset de prédiction standard (N -> N + A_OFFSET)
- `/r [valeur]`: Nombre d'essais de vérification (0 à 10, défaut: 0)
- `/time [secondes]`: Bloque temporairement les prédictions (mode standard).
- `/ec [e1,e2,...]`: **Mode Écart Personnalisé** (Désactive/Ignore `/time`).

**Nouvelle Logique /ec (Écart sur le Numéro Source):**
- La première prédiction (P1) se fait sur le prochain jeu source reçu (N -> N + A_OFFSET).
- Les prédictions suivantes (P2, P3...) se font seulement lorsque le numéro source atteint **[Ancre N précédente + Écart actuel]**.
- La prédiction cible reste toujours **N_source + A_OFFSET**.

**Reset automatique:**
- Toutes les 2 heures
- Quotidien à 00h59 WAT
'''
        with open(os.path.join(deploy_dir, 'README.md'), 'w', encoding='utf-8') as f:
            f.write(readme_content)

        zip_path = '/tmp/ren.zip'
        if os.path.exists(zip_path):
            os.remove(zip_path)

        # Inclusion d'un fichier bot_config.json vide pour le déploiement initial
        initial_config = {
            'a_offset': A_OFFSET_DEFAULT, 
            'r_offset': R_OFFSET_DEFAULT,
            'ec_active': False,
            'ec_gaps': [],
            'ec_gap_index': 0,
            'ec_last_source_game': 0,
            'ec_first_trigger_done': False
        }
        with open(os.path.join(deploy_dir, CONFIG_FILE), 'w', encoding='utf-8') as f:
            json.dump(initial_config, f, indent=4)

        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(deploy_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, deploy_dir)
                    zipf.write(file_path, arcname)

        await client.send_file(
            event.chat_id,
            zip_path,
            caption=f"📦 **ren.zip**\n\nFichier prêt pour déploiement sur Render.com (port 10000)\n\n**Mise à jour majeure:**\n• **Réintégration de la règle de prédiction complexe** (Parité Jeu + Parité Carte).\n• **Format du message de succès simplifié** (`📲Game:N:S statut :✅0️⃣`).\n• Réintégration des commandes `/time` et `/ec` avec persistance et logique de rotation."
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
<p><strong>Config:</strong> A={A_OFFSET}, R={R_OFFSET}</p>
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
        load_config() # Chargement de la config A, R et EC au démarrage
        
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
