import os
import logging
import asyncio
from datetime import datetime
from typing import Optional, Dict, Any
from dotenv import load_dotenv
import aiosqlite
from cryptography.fernet import Fernet
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import ccxt.async_support as ccxt

# Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Обработка ключа шифрования ---
if not ENCRYPTION_KEY:
    # Ключ отсутствует – создаём новый
    ENCRYPTION_KEY = Fernet.generate_key().decode()
    # Записываем в .env
    with open(".env", "a") as f:
        f.write(f"\nENCRYPTION_KEY={ENCRYPTION_KEY}\n")
    logger.info("New ENCRYPTION_KEY generated and saved to .env")
else:
    try:
        # Проверяем валидность существующего ключа
        Fernet(ENCRYPTION_KEY.encode())
    except Exception as e:
        logger.warning(f"Invalid ENCRYPTION_KEY: {e}. Generating new one.")
        ENCRYPTION_KEY = Fernet.generate_key().decode()
        # Перезаписываем .env с новым ключом (сохраняя BOT_TOKEN)
        with open(".env", "w") as f:
            f.write(f"BOT_TOKEN={BOT_TOKEN}\nENCRYPTION_KEY={ENCRYPTION_KEY}\n")

cipher = Fernet(ENCRYPTION_KEY.encode())

# Состояния для ConversationHandler
ADD_EXCHANGE_NAME, ADD_EXCHANGE_KEY, ADD_EXCHANGE_SECRET, ADD_EXCHANGE_TESTNET = range(4)
TRADE_BASE, TRADE_QUOTE, TRADE_PAIR, TRADE_AMOUNT, TRADE_TP, TRADE_SL = range(6)

DB_FILE = "trading_bot.db"

# --- Работа с базой данных ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS exchanges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                exchange_name TEXT NOT NULL,
                api_key_encrypted TEXT NOT NULL,
                api_secret_encrypted TEXT NOT NULL,
                is_testnet BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        """)
        await db.commit()

async def add_user(user_id: int, username: str = None):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
            (user_id, username)
        )
        await db.commit()

async def add_exchange(user_id: int, exchange_name: str, api_key: str, api_secret: str, is_testnet: bool = False):
    encrypted_key = cipher.encrypt(api_key.encode()).decode()
    encrypted_secret = cipher.encrypt(api_secret.encode()).decode()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO exchanges (user_id, exchange_name, api_key_encrypted, api_secret_encrypted, is_testnet) VALUES (?, ?, ?, ?, ?)",
            (user_id, exchange_name, encrypted_key, encrypted_secret, is_testnet)
        )
        await db.commit()

async def get_user_exchanges(user_id: int) -> list:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            "SELECT exchange_name, api_key_encrypted, api_secret_encrypted, is_testnet FROM exchanges WHERE user_id = ?",
            (user_id,)
        )
        rows = await cursor.fetchall()
        exchanges = []
        for row in rows:
            exchanges.append({
                "name": row[0],
                "api_key": cipher.decrypt(row[1].encode()).decode(),
                "api_secret": cipher.decrypt(row[2].encode()).decode(),
                "is_testnet": bool(row[3])
            })
        return exchanges

async def delete_exchange(user_id: int, exchange_name: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "DELETE FROM exchanges WHERE user_id = ? AND exchange_name = ?",
            (user_id, exchange_name)
        )
        await db.commit()

# --- Вспомогательные функции для бирж ---
async def test_exchange_connection(exchange_name: str, api_key: str, api_secret: str, is_testnet: bool = False) -> bool:
    """Проверка подключения к бирже с детальным логированием."""
    try:
        exchange_class = getattr(ccxt, exchange_name)
        exchange = exchange_class({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}  # Для фьючерсов
        })
        if is_testnet:
            if exchange_name == 'binance':
                exchange.set_sandbox_mode(True)
            elif exchange_name == 'bybit':
                # Bybit testnet требует другой URL, но ccxt автоматически подставляет, если установлен флаг
                exchange.set_sandbox_mode(True)
        # Пробуем получить баланс – требует авторизации
        await exchange.fetch_balance()
        await exchange.close()
        return True
    except Exception as e:
        logger.error(f"Ошибка подключения к {exchange_name}: {e}")
        return False

async def create_exchange_instance(user_id: int, exchange_name: str):
    exchanges = await get_user_exchanges(user_id)
    for ex in exchanges:
        if ex['name'] == exchange_name:
            exchange_class = getattr(ccxt, exchange_name)
            exchange = exchange_class({
                'apiKey': ex['api_key'],
                'secret': ex['api_secret'],
                'enableRateLimit': True,
                'options': {'defaultType': 'future'}
            })
            if ex['is_testnet']:
                exchange.set_sandbox_mode(True)
            return exchange
    return None

# --- Команды ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await add_user(user.id, user.username)
    await update.message.reply_text(
        "👋 Добро пожаловать в бота для парной торговли!\n\n"
        "Команды:\n"
        "/add_exchange - добавить биржу\n"
        "/my_exchanges - список ваших бирж\n"
        "/balance - показать баланс на бирже\n"
        "/trade - начать настройку сделки\n"
        "/cancel - отменить текущее действие"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END

# --- Добавление биржи ---
async def add_exchange_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите название биржи (например, binance, bybit):")
    return ADD_EXCHANGE_NAME

async def add_exchange_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['exchange_name'] = update.message.text.strip().lower()
    await update.message.reply_text("Введите ваш API ключ:")
    return ADD_EXCHANGE_KEY

async def add_exchange_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Удаляем возможные пробелы
    context.user_data['api_key'] = update.message.text.strip().replace(" ", "")
    await update.message.reply_text("Введите ваш API Secret:")
    return ADD_EXCHANGE_SECRET

async def add_exchange_secret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['api_secret'] = update.message.text.strip().replace(" ", "")
    # Предлагаем выбрать режим тестнета
    keyboard = [
        [InlineKeyboardButton("Да", callback_data="testnet_yes")],
        [InlineKeyboardButton("Нет", callback_data="testnet_no")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Использовать тестовую сеть (sandbox)?", reply_markup=reply_markup)
    return ADD_EXCHANGE_TESTNET

async def add_exchange_testnet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    is_testnet = query.data == "testnet_yes"
    user_id = update.effective_user.id
    exchange_name = context.user_data['exchange_name']
    api_key = context.user_data['api_key']
    api_secret = context.user_data['api_secret']

    await query.edit_message_text("⏳ Проверка подключения...")
    success = await test_exchange_connection(exchange_name, api_key, api_secret, is_testnet)
    if success:
        await add_exchange(user_id, exchange_name, api_key, api_secret, is_testnet)
        await query.edit_message_text(f"✅ Биржа {exchange_name} успешно добавлена!")
    else:
        await query.edit_message_text(f"❌ Не удалось подключиться к {exchange_name}. Проверьте ключи и выберите режим (тестнет/основная сеть).")
    # Очистка данных
    context.user_data.pop('exchange_name', None)
    context.user_data.pop('api_key', None)
    context.user_data.pop('api_secret', None)
    return ConversationHandler.END

# --- Список бирж ---
async def my_exchanges(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    exchanges = await get_user_exchanges(user_id)
    if not exchanges:
        await update.message.reply_text("У вас пока нет добавленных бирж. Используйте /add_exchange.")
        return
    text = "Ваши биржи:\n"
    for ex in exchanges:
        text += f"🔹 {ex['name']} (тестнет: {'да' if ex['is_testnet'] else 'нет'})\n"
    keyboard = [[InlineKeyboardButton(f"Удалить {ex['name']}", callback_data=f"del_{ex['name']}")] for ex in exchanges]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(text, reply_markup=reply_markup)

async def delete_exchange_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    exchange_name = query.data.replace("del_", "")
    user_id = update.effective_user.id
    await delete_exchange(user_id, exchange_name)
    await query.edit_message_text(f"Биржа {exchange_name} удалена.")

# --- Баланс ---
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    exchanges = await get_user_exchanges(user_id)
    if not exchanges:
        await update.message.reply_text("Сначала добавьте биржу через /add_exchange.")
        return
    # Если несколько бирж – предложим выбрать
    if len(exchanges) == 1:
        ex = exchanges[0]
        await show_balance(update, user_id, ex['name'])
    else:
        keyboard = [[InlineKeyboardButton(ex['name'], callback_data=f"bal_{ex['name']}")] for ex in exchanges]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Выберите биржу для просмотра баланса:", reply_markup=reply_markup)

async def balance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    exchange_name = query.data.replace("bal_", "")
    user_id = update.effective_user.id
    await show_balance(query, user_id, exchange_name, edit=True)

async def show_balance(update_or_query, user_id: int, exchange_name: str, edit: bool = False):
    """Показывает баланс на указанной бирже."""
    exchange = await create_exchange_instance(user_id, exchange_name)
    if not exchange:
        msg = "❌ Не удалось подключиться к бирже."
        if edit:
            await update_or_query.edit_message_text(msg)
        else:
            await update_or_query.message.reply_text(msg)
        return
    try:
        balance = await exchange.fetch_balance()
        await exchange.close()
        # Формируем сообщение (только ненулевые балансы)
        total = balance.get('total', {})
        lines = [f"💰 Баланс на {exchange_name}:"]
        for asset, amount in total.items():
            if amount and amount > 0:
                lines.append(f"{asset}: {amount:.4f}")
        if len(lines) == 1:
            lines.append("Все балансы нулевые.")
        text = "\n".join(lines)
        if edit:
            await update_or_query.edit_message_text(text)
        else:
            await update_or_query.message.reply_text(text)
    except Exception as e:
        logger.error(f"Ошибка получения баланса: {e}")
        msg = f"❌ Ошибка получения баланса: {e}"
        if edit:
            await update_or_query.edit_message_text(msg)
        else:
            await update_or_query.message.reply_text(msg)

# --- Торговля (оставляем как есть, можно доработать) ---
async def trade_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    exchanges = await get_user_exchanges(user_id)
    if len(exchanges) < 2:
        await update.message.reply_text("Для парной торговли необходимо добавить минимум две биржи. Используйте /add_exchange.")
        return ConversationHandler.END
    context.user_data['exchanges_list'] = [ex['name'] for ex in exchanges]
    keyboard = [[InlineKeyboardButton(name, callback_data=f"base_{name}")] for name in context.user_data['exchanges_list']]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Выберите базовую биржу (лонг):", reply_markup=reply_markup)
    return TRADE_BASE

async def trade_base_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    base_ex = query.data.replace("base_", "")
    context.user_data['base_exchange'] = base_ex
    keyboard = [[InlineKeyboardButton(name, callback_data=f"quote_{name}")] for name in context.user_data['exchanges_list'] if name != base_ex]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("Выберите котируемую биржу (шорт):", reply_markup=reply_markup)
    return TRADE_QUOTE

async def trade_quote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    quote_ex = query.data.replace("quote_", "")
    context.user_data['quote_exchange'] = quote_ex
    await query.edit_message_text("Введите торговую пару (например, BTC/USDT):")
    return TRADE_PAIR

async def trade_pair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pair = update.message.text.strip().upper().replace(" ", "")
    if '/' not in pair:
        # Простая конвертация BTCUSDT -> BTC/USDT
        if len(pair) >= 6:
            pair = pair[:3] + '/' + pair[3:]
    context.user_data['pair'] = pair
    await update.message.reply_text("Введите объем сделки в $ (например, 100):")
    return TRADE_AMOUNT

async def trade_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
        context.user_data['amount'] = amount
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите число.")
        return TRADE_AMOUNT
    await update.message.reply_text(
        "Введите параметры тейк-профита в формате: процент объем_процента\n"
        "Например: 0.7 100  (0.7% от цены входа, закрыть 100% позиции)\n"
        "Или отправьте 0, если не нужен."
    )
    return TRADE_TP

async def trade_tp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text != '0':
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text("Неверный формат. Введите два числа через пробел (процент и процент объема).")
            return TRADE_TP
        try:
            tp_percent = float(parts[0])
            tp_volume_percent = float(parts[1])
            context.user_data['tp'] = (tp_percent, tp_volume_percent)
        except ValueError:
            await update.message.reply_text("Ошибка в числах. Попробуйте снова.")
            return TRADE_TP
    else:
        context.user_data['tp'] = None
    await update.message.reply_text(
        "Введите параметры стоп-лосса в процентах (например, 2.0).\n"
        "Если нужен трейлинг или перенос в безубыток, укажите дополнительные параметры через пробел:\n"
        "процент трейлинг перенос (1 - да, 0 - нет)\n"
        "Например: 2.0 1 0  (стоп 2%, трейлинг включен, перенос отключен)\n"
        "Или просто процент (2.0) для обычного стопа.\n"
        "Отправьте 0, если стоп-лосс не нужен."
    )
    return TRADE_SL

async def trade_sl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text != '0':
        parts = text.split()
        try:
            sl_percent = float(parts[0])
            trailing = int(parts[1]) if len(parts) > 1 else 0
            breakeven = int(parts[2]) if len(parts) > 2 else 0
            context.user_data['sl'] = {
                'percent': sl_percent,
                'trailing': bool(trailing),
                'breakeven': bool(breakeven)
            }
        except (ValueError, IndexError):
            await update.message.reply_text("Ошибка в параметрах. Попробуйте снова.")
            return TRADE_SL
    else:
        context.user_data['sl'] = None
    text = (
        f"📊 Параметры сделки:\n"
        f"База (лонг): {context.user_data['base_exchange']}\n"
        f"Котировка (шорт): {context.user_data['quote_exchange']}\n"
        f"Пара: {context.user_data['pair']}\n"
        f"Объем: {context.user_data['amount']} $\n"
    )
    if context.user_data.get('tp'):
        text += f"Тейк-профит: {context.user_data['tp'][0]}% ({context.user_data['tp'][1]}% объема)\n"
    if context.user_data.get('sl'):
        sl = context.user_data['sl']
        text += f"Стоп-лосс: {sl['percent']}% (трейлинг: {'да' if sl['trailing'] else 'нет'}, перенос в безубыток: {'да' if sl['breakeven'] else 'нет'})\n"
    keyboard = [
        [InlineKeyboardButton("🚀 Купить (лонг+шорт)", callback_data="execute_buy")],
        [InlineKeyboardButton("❌ Отмена", callback_data="execute_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(text, reply_markup=reply_markup)
    return ConversationHandler.END

async def execute_trade_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "execute_cancel":
        await query.edit_message_text("Сделка отменена.")
        return
    # Здесь должна быть логика исполнения сделки (аналогично предыдущей версии)
    await query.edit_message_text("⚠️ Исполнение сделки временно отключено в демо-версии.")
    # В реальном коде вставьте код из предыдущей версии с обработкой ордеров

# --- Основная функция ---
def main():
    asyncio.run(init_db())
    application = Application.builder().token(BOT_TOKEN).build()

    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("my_exchanges", my_exchanges))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("cancel", cancel))

    # Добавление биржи
    add_exchange_conv = ConversationHandler(
        entry_points=[CommandHandler("add_exchange", add_exchange_start)],
        states={
            ADD_EXCHANGE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_exchange_name)],
            ADD_EXCHANGE_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_exchange_key)],
            ADD_EXCHANGE_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_exchange_secret)],
            ADD_EXCHANGE_TESTNET: [CallbackQueryHandler(add_exchange_testnet_callback, pattern="^testnet_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(add_exchange_conv)

    # Удаление биржи
    application.add_handler(CallbackQueryHandler(delete_exchange_callback, pattern="^del_"))

    # Баланс (выбор биржи)
    application.add_handler(CallbackQueryHandler(balance_callback, pattern="^bal_"))

    # Торговля
    trade_conv = ConversationHandler(
        entry_points=[CommandHandler("trade", trade_start)],
        states={
            TRADE_BASE: [CallbackQueryHandler(trade_base_callback, pattern="^base_")],
            TRADE_QUOTE: [CallbackQueryHandler(trade_quote_callback, pattern="^quote_")],
            TRADE_PAIR: [MessageHandler(filters.TEXT & ~filters.COMMAND, trade_pair)],
            TRADE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, trade_amount)],
            TRADE_TP: [MessageHandler(filters.TEXT & ~filters.COMMAND, trade_tp)],
            TRADE_SL: [MessageHandler(filters.TEXT & ~filters.COMMAND, trade_sl)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(trade_conv)
    application.add_handler(CallbackQueryHandler(execute_trade_callback, pattern="^execute_"))

    application.run_polling()

if __name__ == "__main__":
    main()
