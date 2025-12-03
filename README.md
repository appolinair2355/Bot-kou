# Bot de Prédiction Baccarat

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
