import os
import logging
import httpx
from pathlib import Path
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    WebAppInfo, InlineQueryResultsButton,
    InlineQueryResultArticle, InputTextMessageContent
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    InlineQueryHandler, PreCheckoutQueryHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ===================== НАСТРОЙКИ =====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / "config" / ".env", override=True)

BOT_TOKEN    = os.getenv("BOT_TOKEN")
BOT_USERNAME     = os.getenv("BOT_USERNAME", "chingamebot")
API_BASE         = os.getenv("API_BASE", "https://chin-games-bot.onrender.com")
BASE_STATIC      = f"{API_BASE}/static"
WEBAPP_URL       = f"{BASE_STATIC}/index.html?v=2205243"
INLINE_IMAGE_URL = f"{BASE_STATIC}/icons/inline.png"  # используется для inline превью

# ID владельца бота для уведомлений о подозрительной активности
ADMIN_ID          = int(os.getenv("ADMIN_ID", "0"))
INTERNAL_SECRET   = os.getenv("INTERNAL_SECRET", "")
# Тот же фоллбэк, что и в api/tg_auth.py — выводим секрет из BOT_TOKEN,
# чтобы bot↔API совпадали без отдельной env-переменной.
if not INTERNAL_SECRET and BOT_TOKEN:
    import hashlib
    INTERNAL_SECRET = hashlib.sha256(("chin-internal:" + BOT_TOKEN).encode()).hexdigest()


def _internal_headers() -> dict:
    """Заголовок для bot→API вызовов, защищённых require_internal."""
    if not INTERNAL_SECRET:
        logger.warning("[BOT] INTERNAL_SECRET не задан — confirm-эндпоинты вернут 401")
    return {"X-Internal-Secret": INTERNAL_SECRET}

# ===================== КЛАВИАТУРЫ =====================
def get_webapp_keyboard(user_id: int | None = None):
    ref_url = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}" if user_id else None
    keyboard = [
        [InlineKeyboardButton("🎮 Запустить Chin Games", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton("📖 Как играть", callback_data="show_rules")],
    ]
    if ref_url:
        keyboard.append([InlineKeyboardButton("👥 Пригласить друга", url=ref_url)])
    return InlineKeyboardMarkup(keyboard)


RULES_TEXT = (
    "📖 <b>Как играть в Chin Games</b>\n\n"
    "🎮 <b>Игры.</b> Нажми «Запустить Chin Games» и выбирай: Snake, 2048, "
    "Math Master, Flappy Chin и Дурак онлайн.\n\n"
    "⚡ <b>Энергия.</b> Каждая партия тратит энергию. Она восстанавливается со "
    "временем — а если не хочешь ждать, пополни в Магазине за ⭐ Stars.\n\n"
    "🏆 <b>Рекорды.</b> Доводи партию до конца — лучший результат попадает в "
    "таблицу лидеров каждой игры.\n\n"
    "🃏 <b>Дурак онлайн.</b> Создавай лобби или заходи к друзьям, играй на ⭐ — "
    "победитель забирает банк.\n\n"
    "👤 <b>Профиль и Магазин</b> — внизу приложения: статистика, скины, пополнение."
)


async def bind_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/bind — привязать текущий чат для автопоста соревнований (только админ)."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or user.id != ADMIN_ID:
        return
    title = (chat.title or (user.first_name or "") or str(chat.id))
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{API_BASE}/api/announce/bind",
                                  headers=_internal_headers(),
                                  json={"chat_id": chat.id, "title": title})
        ok = r.status_code == 200
        await update.message.reply_text("✅ Чат привязан — сюда будут приходить анонсы соревнований." if ok
                                        else "⚠️ Не удалось привязать чат.")
    except Exception as e:
        logger.error(f"[BIND] {e}")
        await update.message.reply_text("⚠️ Ошибка привязки чата.")


async def unbind_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unbind — отвязать текущий чат."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or user.id != ADMIN_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{API_BASE}/api/announce/unbind",
                              headers=_internal_headers(), json={"chat_id": chat.id})
        await update.message.reply_text("✅ Чат отвязан от анонсов.")
    except Exception as e:
        logger.error(f"[UNBIND] {e}")
        await update.message.reply_text("⚠️ Ошибка.")


async def rules_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка «Как играть» в /start — показывает краткие правила."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    await query.message.reply_text(
        text=RULES_TEXT,
        parse_mode="HTML",
        reply_markup=get_webapp_keyboard(),
    )

# ─── Дедупликация /start ────────────────────────────────────────────
_last_start: dict[int, float] = {}

# ===================== УВЕДОМЛЕНИЯ АДМИНИСТРАТОРУ =====================

async def notify_admin(bot, text: str):
    """Отправляет сообщение администратору бота."""
    if not ADMIN_ID:
        logger.warning("[ADMIN] ADMIN_ID не задан, уведомление не отправлено")
        return
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
        logger.info(f"[ADMIN] Уведомление отправлено: {text[:80]}...")
    except Exception as e:
        logger.error(f"[ADMIN] Не удалось отправить уведомление: {e}")


async def run_fraud_checks(bot, inviter_id: int, inviter_name: str):
    """
    Запускает проверки на подозрительную активность рефераловой системы.
    Отправляет уведомление администратору при обнаружении нарушений.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{API_BASE}/api/referral/fraud_check?inviter_id={inviter_id}")
            if resp.status_code != 200:
                return
            data = resp.json()

        # Проверка 1: слишком много рефералов за сутки
        daily_count = data.get("daily_count")
        if daily_count:
            await notify_admin(
                bot,
                f"⚠️ <b>Подозрительная активность — Флуд рефералов</b>\n\n"
                f"👤 Пользователь: <b>{inviter_name}</b> (ID: <code>{inviter_id}</code>)\n"
                f"📊 Привлёк <b>{daily_count}</b> рефералов за последние 24 часа\n"
                f"🔍 Рекомендуется ручная проверка аккаунта."
            )

        # Проверка 2: высокий процент «мёртвых» рефералов
        inactive = data.get("inactive_ratio")
        if inactive:
            pct = int(inactive["ratio"] * 100)
            await notify_admin(
                bot,
                f"⚠️ <b>Подозрительная активность — Мёртвые рефералы</b>\n\n"
                f"👤 Пользователь: <b>{inviter_name}</b> (ID: <code>{inviter_id}</code>)\n"
                f"📊 {inactive['inactive']} из {inactive['total']} рефералов ({pct}%) "
                f"не доиграли до 3 игр\n"
                f"🔍 Возможна накрутка фиктивными аккаунтами."
            )
    except Exception as e:
        logger.warning(f"[FRAUD] Ошибка при проверке fraud: {e}")


# ===================== ХЕНДЛЕРЫ =====================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import time
    user = update.effective_user
    args = context.args or []
    now  = time.monotonic()
    last = _last_start.get(user.id, 0)

    if args:
        if now - last < 3:
            return  # дубль — игнорируем

    _last_start[user.id] = now

    # ── Реферал: /start ref_123456 ──────────────────────────────────
    ref_param = args[0] if args else ""
    if ref_param.startswith("ref_"):
        try:
            inviter_id = int(ref_param[4:])
            invitee_id = user.id

            if inviter_id != invitee_id:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        f"{API_BASE}/api/referral/register",
                        json={
                            "inviter_id":   inviter_id,
                            "invitee_id":   invitee_id,
                            "invitee_name": user.first_name or "Игрок",
                        }
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        # Если регистрация новая — уведомляем инвайтера
                        # (награда НЕ начисляется здесь — только после 3 игр)
                        if data.get("new"):
                            try:
                                inviter_name = data.get("inviter_name", "Игрок")
                                await context.bot.send_message(
                                    chat_id=inviter_id,
                                    text=(
                                        f"🔗 По твоей ссылке зарегистрировался "
                                        f"<b>{user.first_name or 'новый игрок'}</b>!\n\n"
                                        f"⏳ Награда будет начислена, когда друг "
                                        f"<b>отыграет 3 игры</b>.\n"
                                        f"Следи за прогрессом в профиле!"
                                    ),
                                    parse_mode="HTML"
                                )
                            except Exception as e:
                                logger.warning(f"[REF] Не удалось уведомить inviter {inviter_id}: {e}")

                            # Отправляем инвайти ссылку на страницу соглашения + капчи
                            policy_url = f"{BASE_STATIC}/ref_policy.html?user_id={invitee_id}"
                            try:
                                policy_keyboard = InlineKeyboardMarkup([[
                                    InlineKeyboardButton(
                                        "📋 Принять условия и получить бонус",
                                        web_app=WebAppInfo(url=policy_url)
                                    )
                                ]])
                                await update.message.reply_text(
                                    text=(
                                        f"🎁 <b>Тебя пригласил друг!</b>\n\n"
                                        f"Чтобы активировать бонус и начать зарабатывать вместе — "
                                        f"прими условия реферальной программы.\n\n"
                                        f"👇 Нажми кнопку ниже:"
                                    ),
                                    parse_mode="HTML",
                                    reply_markup=policy_keyboard
                                )
                            except Exception as e:
                                logger.warning(f"[REF] Не удалось отправить policy ссылку invitee {invitee_id}: {e}")

                            # Запускаем проверку на фрод после регистрации
                            inviter_name = data.get("inviter_name", str(inviter_id))
                            context.application.create_task(
                                run_fraud_checks(context.bot, inviter_id, inviter_name)
                            )
                    else:
                        logger.warning(f"[REF] register failed: {resp.status_code} {resp.text}")
        except (ValueError, Exception) as e:
            logger.warning(f"[REF] bad ref param '{ref_param}': {e}")

    # ── Приветственное сообщение ────────────────────────────────────
    text = f"👋 Привет, <b>{user.first_name or 'Игрок'}</b>!\n\nДобро пожаловать в <b>Chin Games</b> 🎮"
    await update.message.reply_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_webapp_keyboard(user.id)
    )


async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик inline-запросов (@chingamebot в любом чате).

    Показывает превью с картинкой inline.png + большую кнопку "Играть".
    Реализован максимально надёжно: при любой ошибке с фото — падает обратно на простую кнопку.
    """
    try:
        results = []

        # Пытаемся показать красивое превью с картинкой
        if INLINE_IMAGE_URL:
            try:
                results.append(
                    InlineQueryResultArticle(
                        id="chin_games_preview",
                        title="Chin Games",
                        description="Мини-игры • Призы в Stars • Турниры",
                        thumb_url=INLINE_IMAGE_URL,
                        thumb_width=128,
                        thumb_height=128,
                        input_message_content=InputTextMessageContent(
                            message_text="🎮 Открыть Chin Games"
                        ),
                    )
                )
            except Exception as photo_err:
                logger.warning(f"[INLINE] Не удалось добавить превью с фото: {photo_err}")

        # Всегда показываем большую кнопку "Играть" внизу
        await update.inline_query.answer(
            results=results,
            cache_time=5,
            is_personal=True,
            button=InlineQueryResultsButton(
                text="🎮 Играть в Chin Games",
                start_parameter="play"
            )
        )

    except Exception as e:
        logger.error(f"Inline error: {e}")
        # Последний фоллбэк — просто кнопка без результатов
        try:
            await update.inline_query.answer(
                results=[],
                cache_time=1,
                is_personal=True,
                button=InlineQueryResultsButton(
                    text="🎮 Играть в Chin Games",
                    start_parameter="play"
                )
            )
        except Exception:
            pass


async def pre_checkout_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Telegram требует ответ в течение 10 секунд.
    Поддерживаемые payload:
      energy:{user_id}:{amount}   — покупка энергии
      wallet:{user_id}:{amount}   — пополнение кошелька
      case:{user_id}:{price}      — случайный кейс (price = 1000)
    """
    query   = update.pre_checkout_query
    payload = query.invoice_payload

    try:
        parts = payload.split(":")
        if len(parts) != 3:
            raise ValueError(f"Неверный формат payload: {payload!r}")

        ptype, user_id_str, amount_str = parts
        user_id = int(user_id_str)
        amount  = int(amount_str)

        if ptype not in ("energy", "wallet", "case", "regen"):
            raise ValueError(f"Неизвестный тип платежа: {ptype}")
        if user_id != query.from_user.id:
            raise ValueError(f"user_id mismatch: payload={user_id} sender={query.from_user.id}")
        if amount <= 0:
            raise ValueError(f"Некорректный amount: {amount}")
        if ptype == "case" and amount != 600:
            raise ValueError(f"Некорректная цена кейса: {amount}")

        logger.info(f"✅ PreCheckout OK | type={ptype} user={user_id} amount={amount}")
        await query.answer(ok=True)

    except Exception as e:
        logger.warning(f"❌ PreCheckout rejected | {e}")
        await query.answer(ok=False, error_message="Платёж отклонён. Попробуй ещё раз.")


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Срабатывает после успешной оплаты Stars.
    """
    payment = update.message.successful_payment
    payload = payment.invoice_payload
    user    = update.effective_user

    logger.info(f"💳 SuccessfulPayment | user={user.id} payload={payload} stars={payment.total_amount}")

    try:
        parts = payload.split(":")
        if len(parts) != 3:
            raise ValueError(f"Bad payload: {payload!r}")

        ptype, user_id_str, amount_str = parts
        user_id = int(user_id_str)
        amount  = int(amount_str)

        async with httpx.AsyncClient(timeout=10) as client:

            if ptype == "wallet":
                resp = await client.post(
                    f"{API_BASE}/api/wallet/confirm_topup",
                    headers=_internal_headers(),
                    json={
                        "user_id":    user_id,
                        "amount":     amount,
                        "first_name": user.first_name or "",
                    }
                )
                if resp.status_code == 200:
                    new_balance = resp.json().get("balance", "?")
                    await update.message.reply_text(
                        f"✅ Кошелёк пополнен!\n"
                        f"⭐ +{amount} Stars\n"
                        f"Баланс: {new_balance} ⭐",
                    )
                    logger.info(f"[WALLET] topup confirmed: user={user_id} +{amount} balance={new_balance}")
                else:
                    logger.error(f"[WALLET] confirm_topup failed: {resp.status_code} {resp.text}")

            elif ptype == "energy":
                resp = await client.post(
                    f"{API_BASE}/api/shop/confirm",
                    headers=_internal_headers(),
                    json={"user_id": user_id, "amount": amount}
                )
                await update.message.reply_text(
                    f"✅ Куплено {amount} ⚡ энергии!\n"
                    f"Открой игру — заряд уже зачислен.",
                )
                logger.info(f"[SHOP] energy confirmed: user={user_id} +{amount}⚡")

            elif ptype == "case":
                resp = await client.post(
                    f"{API_BASE}/api/shop/confirm_case",
                    headers=_internal_headers(),
                    json={
                        "user_id": user_id,
                        "first_name": user.first_name or "",
                    },
                )
                if resp.status_code == 200:
                    reward = resp.json().get("reward", {})
                    title = reward.get("title", "Награда")
                    if reward.get("type") == "nft":
                        await update.message.reply_text(
                            f"📦 Кейс открыт!\n\n🎁 {title}\n\n"
                            f"Открой магазин в игре, чтобы посмотреть NFT.\n"
                            f"Подарок отправляется вручную.",
                        )
                    else:
                        await update.message.reply_text(
                            f"📦 Кейс открыт!\n\n🎁 {title}\n\n"
                            f"Открой магазин в игре — награда уже зачислена.",
                        )
                    logger.info(
                        f"[SHOP] case confirmed: user={user_id} "
                        f"{reward.get('type')}:{reward.get('amount')}"
                    )
                else:
                    logger.error(f"[SHOP] confirm_case failed: {resp.status_code} {resp.text}")

            elif ptype == "regen":
                resp = await client.post(
                    f"{API_BASE}/api/shop/confirm_regen",
                    headers=_internal_headers(),
                    json={"user_id": user_id, "level": amount},
                )
                if resp.status_code == 200:
                    await update.message.reply_text(
                        "⚡ Скорость восстановления энергии улучшена!\n"
                        "Батарея теперь заряжается быстрее.",
                    )
                    logger.info(f"[SHOP] regen confirmed: user={user_id} level={amount}")
                else:
                    logger.error(f"[SHOP] confirm_regen failed: {resp.status_code} {resp.text}")

    except Exception as e:
        logger.error(f"[PAYMENT] successful_payment error: {e}")
        await update.message.reply_text("✅ Оплата получена! Если ресурс не зачислился — напиши в поддержку.")


# ===================== MAIN =====================
def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("bind", bind_chat_command))
    application.add_handler(CommandHandler("unbind", unbind_chat_command))
    application.add_handler(CallbackQueryHandler(rules_callback, pattern="^show_rules$"))
    application.add_handler(InlineQueryHandler(inline_query_handler))
    application.add_handler(PreCheckoutQueryHandler(pre_checkout_query_handler))
    application.add_handler(MessageHandler(
        filters.SUCCESSFUL_PAYMENT, successful_payment_handler
    ))

    logger.info("✅ Бот запущен!")
    logger.info(f"🌐 WebApp: {WEBAPP_URL}")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
