# sf.py
# Вебхук-бот для Render/Heroku: меню, корзина, удаление, комментарий, доставка 99 ₽, статусы для админа.
# FIX: убран лишний asyncio.run/await для run_webhook (исправляет "event loop is already running").
# Требования: python-telegram-bot[webhooks] (рекомендуем 21.6), python-dotenv (опц.).

import os, json, sqlite3, re, logging
from datetime import datetime
from typing import Dict, Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ---------------- .env ----------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
DB_PATH = os.getenv("DB_PATH", "orders.db")

def _auto_base_url() -> str:
    base = os.getenv("BASE_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if base:
        return base.rstrip("/")
    host = os.getenv("RENDER_EXTERNAL_HOSTNAME")
    if host:
        return f"https://{host}".rstrip("/")
    return ""

BASE_URL = _auto_base_url()
WEBHOOK_SECRET_PATH = os.getenv("WEBHOOK_SECRET_PATH", "tgwebhook")
PORT = int(os.environ.get("PORT", "10000"))

DELIVERY_FEE = 99
ROOM_RE = re.compile(r'^\d+[A-Za-zА-Яа-я]$')

MENU: Dict[str, tuple] = {
    "energy": ("ЭНЕРГЕТИК", 65),
    "cola": ("КОЛА (ориг)", 110),
    "chips": ("ЧИПСЫ", 70),
    "pepsi": ("ПЕПСИ (ориг)", 105),
    "water": ("ВОДА", 44),
    "chocopie": ("ЧОКОПАЙ", 25),
    "7up": ("СЕВЭНАП (ориг)", 105),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("snackbot")

STATE: Dict[int, Dict[str, Any]] = {}

# ---------------- DB ----------------
def db_init():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            room TEXT,
            items_json TEXT,
            note TEXT,
            total INTEGER,
            status TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def db_insert_order(user_id:int, username:str, room:str, items:Dict[str,int], note:str, total:int)->int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now = datetime.now().isoformat(timespec="seconds")
    cur.execute("""
        INSERT INTO orders (user_id, username, room, items_json, note, total, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'NEW', ?, ?)
    """, (user_id, username or "", room, json.dumps(items, ensure_ascii=False), note or "", total, now, now))
    conn.commit()
    oid = cur.lastrowid
    conn.close()
    return oid

def db_update_status(order_id:int, status:str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now = datetime.now().isoformat(timespec="seconds")
    cur.execute("UPDATE orders SET status=?, updated_at=? WHERE id=?", (status, now, order_id))
    conn.commit()
    conn.close()

def db_get_order(order_id:int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE id=?", (order_id,))
    row = cur.fetchone()
    conn.close()
    if not row: return None
    keys = ["id","user_id","username","room","items_json","note","total","status","created_at","updated_at"]
    rec = dict(zip(keys,row))
    rec["items"] = json.loads(rec["items_json"]) if rec["items_json"] else {}
    return rec

# ---------------- Helpers/UI ----------------
def fmt_items(cart:Dict[str,int])->str:
    if not cart: return "—"
    return "\n".join(f"• {MENU[k][0]} ×{q} = {MENU[k][1]*q}₽" for k,q in cart.items())

def get_cart_subtotal(cart:Dict[str,int])->int:
    return sum(MENU[i][1]*q for i,q in cart.items())

def menu_keyboard()->InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"{v[0]} — {v[1]}₽", callback_data=f"add:{k}")] for k,v in MENU.items()]
    rows.append([InlineKeyboardButton("🏫 Сменить аудиторию", callback_data="change_room")])
    rows.append([InlineKeyboardButton("🧺 Корзина", callback_data="cart"),
                 InlineKeyboardButton("✅ Оформить", callback_data="checkout")])
    return InlineKeyboardMarkup(rows)

def admin_order_kb(order_id:int)->InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Принять", callback_data=f"adm:{order_id}:ACCEPTED"),
         InlineKeyboardButton("🛵 В пути", callback_data=f"adm:{order_id}:ON_THE_WAY")],
        [InlineKeyboardButton("📦 Доставлен", callback_data=f"adm:{order_id}:DELIVERED"),
         InlineKeyboardButton("🚫 Отмена", callback_data=f"adm:{order_id}:CANCELED")]
    ])

def cart_keyboard(cart:Dict[str,int])->InlineKeyboardMarkup:
    kb = []
    for k,q in cart.items():
        kb.append([InlineKeyboardButton(f"➖ Убрать {MENU[k][0]}", callback_data=f"del:{k}")])
    kb.append([InlineKeyboardButton("➕ Добавить ещё", callback_data="back2menu"),
               InlineKeyboardButton("✅ Оформить", callback_data="checkout")])
    return InlineKeyboardMarkup(kb)

# ---------------- Bot Logic ----------------
async def ensure_state(update: Update)->Dict[str,Any]:
    chat_id = update.effective_chat.id
    if chat_id not in STATE:
        STATE[chat_id] = {"room": None, "cart": {}, "note": None, "awaiting": "room"}
    return STATE[chat_id]

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = await ensure_state(update)
    st["awaiting"] = "room"
    await update.message.reply_text("Привет! 🍫 Введи номер аудитории (цифры + буква, например 429Г):")

async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    user = update.effective_user
    st = STATE.setdefault(chat_id, {"room": None, "cart": {}, "note": None, "awaiting": None})
    data = query.data

    if data == "change_room":
        st["awaiting"] = "room"
        await query.edit_message_text("Введи новую аудиторию (например, 429г):")
        return

    if data.startswith("add:"):
        item = data.split(":")[1]
        st["cart"][item] = st["cart"].get(item, 0) + 1
        subtotal = get_cart_subtotal(st["cart"])
        await query.edit_message_text(
            f"Добавил: {MENU[item][0]} — {MENU[item][1]}₽\n"
            f"Текущая сумма товаров: {subtotal}₽",
            reply_markup=menu_keyboard()
        )
        return

    if data == "cart":
        if not st["cart"]:
            await query.edit_message_text("Корзина пуста.", reply_markup=menu_keyboard())
            return
        subtotal = get_cart_subtotal(st["cart"])
        grand = subtotal + DELIVERY_FEE
        lines = [
            "🧺 Твоя корзина:",
            fmt_items(st["cart"]),
            f"\n💰 Товары: {subtotal}₽",
            f"🚚 Доставка: {DELIVERY_FEE}₽",
            f"Итого: {grand}₽",
        ]
        await query.edit_message_text("\n".join(lines), reply_markup=cart_keyboard(st["cart"]))
        return

    if data.startswith("del:"):
        item = data.split(":")[1]
        if st["cart"].get(item, 0) > 1:
            st["cart"][item] -= 1
        else:
            st["cart"].pop(item, None)

        if not st["cart"]:
            await query.edit_message_text("Корзина пуста.", reply_markup=menu_keyboard())
            return

        subtotal = get_cart_subtotal(st["cart"])
        grand = subtotal + DELIVERY_FEE
        lines = [
            "🧺 Твоя корзина (обновлено):",
            fmt_items(st["cart"]),
            f"\n💰 Товары: {subtotal}₽",
            f"🚚 Доставка: {DELIVERY_FEE}₽",
            f"Итого: {grand}₽",
        ]
        await query.edit_message_text("\n".join(lines), reply_markup=cart_keyboard(st["cart"]))
        return

    if data == "back2menu":
        await query.edit_message_text("Продолжай выбирать:", reply_markup=menu_keyboard())
        return

    if data == "checkout":
        if not st["cart"]:
            await query.edit_message_text("Корзина пуста.", reply_markup=menu_keyboard())
            return
        if not st["room"]:
            st["awaiting"] = "room"
            await query.edit_message_text("Введи номер аудитории (например, 429Г):")
            return
        subtotal = get_cart_subtotal(st["cart"])
        grand = subtotal + DELIVERY_FEE
        lines = [
            f"📍 Аудитория {st['room']}",
            fmt_items(st["cart"]),
            f"\n💰 Товары: {subtotal}₽",
            f"🚚 Доставка: {DELIVERY_FEE}₽",
            f"Итого к оплате: {grand}₽"
        ]
        kb = [[InlineKeyboardButton("✍️ Добавить комментарий", callback_data="add_comment")],
              [InlineKeyboardButton("💳 Подтвердить без комментария", callback_data="confirm")]]
        await query.edit_message_text("Проверь заказ:\n" + "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))
        return

    if data == "add_comment":
        st["awaiting"] = "comment"
        await query.edit_message_text("Напиши комментарий (или /skip чтобы пропустить):")
        return

    if data == "confirm":
        subtotal = get_cart_subtotal(st["cart"])
        grand = subtotal + DELIVERY_FEE
        note = st.get("note") or "—"
        order_id = db_insert_order(user.id, user.username or "", st["room"], st["cart"], note, grand)

        admin_text = (
            f"🆕 Заказ #{order_id}\n"
            f"От @{user.username or '—'} (id {user.id})\n"
            f"Аудитория: {st['room']}\n"
            f"{fmt_items(st['cart'])}\n\n"
            f"💰 Товары: {subtotal}₽\n"
            f"🚚 Доставка: {DELIVERY_FЕЕ}₽\n"
            f"Итого: {grand}₽\n"
            f"Комментарий: {note}"
        )
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(aid, admin_text, reply_markup=admin_order_kb(order_id))
            except Exception as e:
                log.warning(f"Admin notify fail: {e}")

        await query.edit_message_text(
            f"✅ Заказ #{order_id} принят!\n\n"
            f"💰 Товары: {subtotal}₽\n"
            f"🚚 Доставка: {DELIVERY_FЕЕ}₽\n"
            f"Итого к оплате: {grand}₽\n"
            f"Комментарий: {note}"
        )
        st["cart"].clear()
        st["note"] = None
        return

    if data.startswith("adm:"):
        _, oid_str, status = data.split(":")
        order_id = int(oid_str)
        rec = db_get_order(order_id)
        if not rec:
            await query.answer("Не найден", show_alert=True)
            return
        db_update_status(order_id, status)
        text_map = {"ACCEPTED": "✅ принят", "ON_THE_WAY": "🛵 в пути", "DELIVERED": "📦 доставлен", "CANCELED": "🚫 отменён"}
        msg = f"Статус твоего заказа #{order_id}: {text_map.get(status, status)}"
        try:
            await context.bot.send_message(rec["user_id"], msg)
        except Exception:
            pass
        await context.bot.send_message(chat_id, text=f"Заказ #{order_id} обновлён → {text_map.get(status, status)}")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    st = await ensure_state(update)
    text = (update.message.text or "").strip()

    if st.get("awaiting") == "room":
        if not ROOM_RE.fullmatch(text):
            await update.message.reply_text("Формат аудитории: цифры + буква (например, 429Г).")
            return
        st["room"] = text.upper()
        st["awaiting"] = None
        await update.message.reply_text(f"✅ Аудитория установлена: {st['room']}", reply_markup=menu_keyboard())
        return

    if st.get("awaiting") == "comment":
        if text == "/skip":
            st["note"] = None
        else:
            st["note"] = text
        st["awaiting"] = None
        subtotal = get_cart_subtotal(st["cart"])
        grand = subtotal + DELIVERY_FEE
        await update.message.reply_text(
            "Комментарий сохранён ✅\n"
            "Проверь сумму и подтверди заказ:\n"
            f"💰 Товары: {subtotal}₽\n"
            f"🚚 Доставка: {DELIVERY_FEE}₽\n"
            f"Итого к оплате: {grand}₽",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Подтвердить заказ", callback_data="confirm")]])
        )
        return

    await update.message.reply_text("Добавляй позиции из меню:", reply_markup=menu_keyboard())

# ---------------- Main (blocking run_webhook) ----------------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не указан BOT_TOKEN")
    db_init()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    base = BASE_URL
    if not base:
        raise RuntimeError("BASE_URL не задан и не удалось определить автоматически. Укажи BASE_URL в Environment или положись на RENDER_EXTERNAL_URL.")
    webhook_url = f"{base.rstrip('/')}/{WEBHOOK_SECRET_PATH}"

    log.info(f"Starting webhook on 0.0.0.0:{PORT} → {webhook_url}")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_SECRET_PATH,
        webhook_url=webhook_url,
    )

if __name__ == "__main__":
    main()
