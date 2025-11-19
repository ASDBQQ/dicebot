import asyncio
import random
import re
from datetime import datetime, timedelta, UTC

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
)

from db import (
    init_db,
    upsert_user,
    upsert_game,
    get_user_games,
    get_all_finished_games,
    upsert_raffle_round,
    add_raffle_bet,
    add_ton_deposit,
    add_transfer,
)

# ========================
#      НАСТРОЙКИ
# ========================

BOT_TOKEN = "8589113961:AAH8bF8umtdtYhkhmBB5oW8NoMBMxI4bLxk"

# TON кошелёк для пополнений
TON_WALLET_ADDRESS = "UQCzzlkNLsCGqHTUj1zkD_3CVBMoXw-9Od3dRKGgHaBxysYe"  # пример: EQC...

# 1 рубль = 1 монета (внутренняя валюта бота — монеты)
# Курс TON→RUB берём через tonapi.io
TONAPI_RATES_URL = "https://tonapi.io/v2/rates?tokens=ton&currencies=rub"
TON_RUB_CACHE_TTL = 60  # секунд кэша курса

START_BALANCE_COINS = 0  # стартовый баланс (в монетах)

HISTORY_LIMIT = 30
HISTORY_PAGE_SIZE = 10
GAME_TTL_SECONDS = 120  # через сколько секунд удалять несыгранные игры без соперника

# розыгрыш (банкир)
RAFFLE_TIMER_SECONDS = 40       # через сколько секунд после появления 2+ игроков запускать розыгрыш
RAFFLE_MIN_BET = 10             # мин. ставка для розыгрыша (в монетах)
DICE_MIN_BET = 10               # мин. ставка для костей (в монетах)
RAFFLE_QUICK_BETS = [10, 100, 1000]

MAIN_ADMIN_ID = 7106398341
ADMIN_IDS = {MAIN_ADMIN_ID, 783924834}  # админы

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ========================
#      ДАННЫЕ В ПАМЯТИ
# ========================

user_balances: dict[int, int] = {}         # user_id -> balance (монеты = рубли)
user_usernames: dict[int, str] = {}        # user_id -> username (для переводов и ссылок)

games: dict[int, dict] = {}                # game_id -> game dict (активные и недавно сыгранные)
pending_bet_input: dict[int, bool] = {}    # user_id -> ждём ставку для костей
next_game_id = 1

# вывод (заявки)
pending_withdraw_step: dict[int, str] = {}  # user_id -> "amount" / "details"
temp_withdraw: dict[int, dict] = {}         # user_id -> {amount: int}

# переводы между пользователями
pending_transfer_step: dict[int, str] = {}  # user_id -> "target" / "amount_transfer"
temp_transfer: dict[int, dict] = {}         # user_id -> {"target_id": int}

# розыгрыш (банкир)
raffle_round: dict | None = None    # текущий розыгрыш
raffle_task: asyncio.Task | None = None
next_raffle_id: int = 1
pending_raffle_bet_input: dict[int, bool] = {}  # ввод произвольной суммы для розыгрыша

# пополнение через TON: храним обработанные транзакции, чтобы не дублировать начисления
processed_ton_tx: set[str] = set()

# кэш курса TON→RUB
_ton_rate_cache: dict[str, float | datetime] = {
    "value": 0.0,
    "updated": datetime.fromtimestamp(0, tz=UTC),
}


# ========================
#      УТИЛИТЫ
# ========================

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def get_balance(uid: int) -> int:
    """Возвращает баланс в монетах (аналог рублей)."""
    if uid not in user_balances:
        user_balances[uid] = START_BALANCE_COINS
    return user_balances[uid]


def _schedule_upsert_user(uid: int):
    """Фоновое сохранение пользователя в БД (баланс + username)."""
    username = user_usernames.get(uid)
    balance = user_balances.get(uid, 0)
    try:
        asyncio.create_task(upsert_user(uid, username, balance))
    except RuntimeError:
        # если event loop ещё не запущен (редкий случай)
        pass


def change_balance(uid: int, delta: int):
    get_balance(uid)
    user_balances[uid] += delta
    _schedule_upsert_user(uid)


def set_balance(uid: int, value: int):
    user_balances[uid] = value
    _schedule_upsert_user(uid)


def format_coins(n: int) -> str:
    return f"{n:,}".replace(",", " ")


async def get_ton_rub_rate() -> float:
    """Получить курс TON→RUB через tonapi.io (с простым кэшем)."""
    now = datetime.now(UTC)
    cached_value = _ton_rate_cache["value"]
    updated: datetime = _ton_rate_cache["updated"]  # type: ignore

    if cached_value and (now - updated).total_seconds() < TON_RUB_CACHE_TTL:
        return float(cached_value)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(TONAPI_RATES_URL, timeout=10) as resp:
                data = await resp.json()
        # структура по доке: {"rates": {"TON": {"prices": {"RUB": 123.45}}}}
        rate = float(data["rates"]["TON"]["prices"]["RUB"])
        _ton_rate_cache["value"] = rate
        _ton_rate_cache["updated"] = now
        return rate
    except Exception:
        # если не удалось взять курс — возвращаем последний кэш или дефолт
        return float(cached_value or 100.0)


async def format_balance_text(uid: int) -> str:
    bal = get_balance(uid)
    rate = await get_ton_rub_rate()
    ton_equiv = bal / rate if rate > 0 else 0
    return (
        f"💼 Ваш баланс: {ton_equiv:.4f} TON\n"
        f"≈ {format_coins(bal)} монет (₽)\n"
        f"Текущий курс: 1 TON ≈ {rate:.2f} ₽ / монет"
    )


def bottom_menu():
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [
                types.KeyboardButton(text="🕹 Игры"),
                types.KeyboardButton(text="💼 Баланс"),
            ],
            [
                types.KeyboardButton(text="🎁 Розыгрыш"),
            ],
            [
                types.KeyboardButton(text="🌐 Поддержка"),
            ],
        ],
        resize_keyboard=True
    )


def register_user(user: types.User):
    if user.username:
        user_usernames[user.id] = user.username
        _schedule_upsert_user(user.id)


# ========================
#      СПИСОК ИГР (КОСТИ)
# ========================

def build_games_keyboard(uid: int) -> InlineKeyboardMarkup:
    rows = []

    rows.append([
        InlineKeyboardButton(text="✅Создать игру", callback_data="create_game"),
        InlineKeyboardButton(text="🔄Обновить", callback_data="refresh_games"),
    ])

    active = [g for g in games.values() if g["opponent_id"] is None]
    active.sort(key=lambda x: x["id"], reverse=True)

    for g in active:
        txt = f"🎲Игра #{g['id']} | {format_coins(g['bet'])} монет"
        if g["creator_id"] == uid:
            rows.append([
                InlineKeyboardButton(text=txt, callback_data=f"game_my:{g['id']}")
            ])
        else:
            rows.append([
                InlineKeyboardButton(text=txt, callback_data=f"game_open:{g['id']}")
            ])

    rows.append([
        InlineKeyboardButton(text="📋 Мои игры", callback_data="my_games:0"),
        InlineKeyboardButton(text="🏆 Рейтинг", callback_data="rating"),
    ])

    rows.append([
        InlineKeyboardButton(text="🎮 Игры", callback_data="menu_games"),
        InlineKeyboardButton(text="🐼 Помощь", callback_data="help"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_games_text() -> str:
    return "Создайте игру или выберите уже имеющуюся:"


async def send_games_list(chat_id: int, uid: int):
    await bot.send_message(
        chat_id,
        build_games_text(),
        reply_markup=build_games_keyboard(uid)
    )


# ========================
#      ИСТОРИЯ / СТАТИСТИКА
# ========================

def calculate_profit(uid: int, g: dict) -> int:
    bet = g["bet"]
    if g["winner"] == "draw":
        return 0
    creator = uid == g["creator_id"]
    if g["winner"] == "creator" and creator:
        return bet
    if g["winner"] == "opponent" and not creator:
        return bet
    return -bet


async def build_user_stats_and_history(uid: int):
    now = datetime.now(UTC)
    finished = await get_user_games(uid)

    stats = {
        "month": {"games": 0, "profit": 0},
        "week": {"games": 0, "profit": 0},
        "day": {"games": 0, "profit": 0},
    }

    for g in finished:
        if not g.get("finished_at"):
            continue
        finished_at = datetime.fromisoformat(g["finished_at"])
        delta = now - finished_at
        p = calculate_profit(uid, g)

        if delta <= timedelta(days=30):
            stats["month"]["games"] += 1
            stats["month"]["profit"] += p
        if delta <= timedelta(days=7):
            stats["week"]["games"] += 1
            stats["week"]["profit"] += p
        if delta <= timedelta(days=1):
            stats["day"]["games"] += 1
            stats["day"]["profit"] += p

    def ps(v): return ("+" if v > 0 else "") + str(v)

    stats_text = (
        f"🎲 Кости за месяц: {stats['month']['games']}\n"
        f"└ 💸 Профит: {ps(stats['month']['profit'])} монет\n\n"
        f"🎲 За неделю: {stats['week']['games']}\n"
        f"└ 💸 Профит: {ps(stats['week']['profit'])} монет\n\n"
        f"🎲 За сутки: {stats['day']['games']}\n"
        f"└ 💸 Профит: {ps(stats['day']['profit'])} монет"
    )

    history = []
    for g in finished[:HISTORY_LIMIT]:
        if uid == g["creator_id"]:
            my = g["creator_roll"]
            opp = g["opponent_roll"]
        else:
            my = g["opponent_roll"]
            opp = g["creator_roll"]

        profit = calculate_profit(uid, g)
        if profit > 0:
            emoji, text = "🟩", "Победа"
        elif profit < 0:
            emoji, text = "🟥", "Проигрыш"
        else:
            emoji, text = "⚪", "Ничья"

        history.append({
            "bet": g["bet"],
            "emoji": emoji,
            "text": text,
            "my": my,
            "opp": opp
        })

    return stats_text, history


def build_history_keyboard(history: list[dict], page: int) -> InlineKeyboardMarkup:
    rows = []

    total = len(history)
    if total == 0:
        rows.append([InlineKeyboardButton(text="История пуста", callback_data="ignore")])
        rows.append([InlineKeyboardButton(text="🎮 Игры", callback_data="menu_games")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    pages = (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE
    page = max(0, min(page, pages - 1))

    start = page * HISTORY_PAGE_SIZE
    end = start + HISTORY_PAGE_SIZE

    for h in history[start:end]:
        text = f"{format_coins(h['bet'])} монет | {h['emoji']} {h['text']} | {h['my']}:{h['opp']}"
        rows.append([InlineKeyboardButton(text=text, callback_data="ignore")])

    if pages > 1:
        rows.append([
            InlineKeyboardButton(text="<<", callback_data="my_games:0"),
            InlineKeyboardButton(text="<", callback_data=f"my_games:{max(0, page - 1)}"),
            InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="ignore"),
            InlineKeyboardButton(text=">", callback_data=f"my_games:{min(pages - 1, page + 1)}"),
            InlineKeyboardButton(text=">>", callback_data=f"my_games:{pages - 1}"),
        ])

    rows.append([InlineKeyboardButton(text="🎮 Игры", callback_data="menu_games")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ========================
#      РЕЙТИНГ
# ========================

async def build_rating_text() -> str:
    profits: dict[int, int] = {}
    finished = await get_all_finished_games()

    for g in finished:
        for uid in (g["creator_id"], g["opponent_id"]):
            if uid is None:
                continue
            profits.setdefault(uid, 0)
            profits[uid] += calculate_profit(uid, g)

    if not profits:
        return "🏆 Рейтинг пока пуст — ещё нет завершённых игр."

    top = sorted(profits.items(), key=lambda x: x[1], reverse=True)[:10]
    place_emoji = ["🥇", "🥈", "🥉"] + ["🏅"] * 7

    lines = ["🏆 ТОП игроков по профиту (монеты):\n"]
    for i, (uid, prof) in enumerate(top, start=1):
        emoji = place_emoji[i - 1] if i <= len(place_emoji) else "🏅"
        sign = "+" if prof > 0 else ""
        lines.append(f"{emoji} {i}. ID {uid}: {sign}{prof} монет")

    return "\n".join(lines)


# ========================
#      ИГРА КОСТИ (1% КОМИССИЯ)
# ========================

async def telegram_roll(uid: int) -> int:
    msg = await bot.send_dice(uid, emoji="🎲")
    await asyncio.sleep(3)
    return msg.dice.value


async def play_game(gid: int):
    g = games.get(gid)
    if not g:
        return

    c = g["creator_id"]
    o = g["opponent_id"]
    bet = g["bet"]

    cr = await telegram_roll(c)
    orr = await telegram_roll(o)

    g["creator_roll"] = cr
    g["opponent_roll"] = orr
    g["finished"] = True
    g["finished_at"] = datetime.now(UTC)

    bank = bet * 2

    if cr > orr:
        winner = "creator"
        commission = bank // 100
        prize = bank - commission
        change_balance(c, prize)
        change_balance(MAIN_ADMIN_ID, commission)
    elif orr > cr:
        winner = "opponent"
        commission = bank // 100
        prize = bank - commission
        change_balance(o, prize)
        change_balance(MAIN_ADMIN_ID, commission)
    else:
        winner = "draw"
        change_balance(c, bet)
        change_balance(o, bet)
        commission = 0

    g["winner"] = winner

    # сохраняем результат игры в БД
    await upsert_game(g)

    for user in (c, o):
        is_creator = (user == c)
        your = cr if is_creator else orr
        their = orr if is_creator else cr

        if winner == "draw":
            result_text = "🤝 Ничья!"
            bank_text = f"💰 Банк: {format_coins(bank)} монет (вернули ставки)"
        else:
            if (winner == "creator" and is_creator) or (winner == "opponent" and not is_creator):
                result_text = "🥳 Поздравляем с победой!"
            else:
                result_text = "😔 К сожалению, вы проиграли!"
            bank_text = (
                f"💰 Банк: {format_coins(bank)} монет\n"
                f"💸 Комиссия: {format_coins(commission)} монет (1%)"
            )

        txt = (
            f"🏁 Кости #{gid}\n"
            f"{bank_text}\n\n"
            f"🫵 Ваш результат: {your}\n"
            f"🧑‍🤝‍🧑 Результат соперника: {their}\n\n"
            f"{result_text}\n"
            f"💼 Баланс: {get_balance(user)} монет"
        )

        await bot.send_message(user, txt)


# ========================
#      АВТОУДАЛЕНИЕ ИГР
# ========================

async def cleanup_worker():
    while True:
        now = datetime.now(UTC)
        to_delete = []

        for gid, g in list(games.items()):
            if g["finished"]:
                continue
            if g["opponent_id"] is not None:
                continue

            created_at = g["created_at"]
            if (now - created_at).total_seconds() > GAME_TTL_SECONDS:
                to_delete.append(gid)

        for gid in to_delete:
            g = games.get(gid)
            if not g:
                continue
            creator_id = g["creator_id"]
            bet = g["bet"]
            change_balance(creator_id, bet)
            del games[gid]
            try:
                await bot.send_message(
                    creator_id,
                    f"⏳ Ваша игра №{gid} была удалена по таймеру.\n"
                    f"💰 {format_coins(bet)} монет возвращены на баланс."
                )
            except Exception:
                pass

        await asyncio.sleep(30)


# ========================
#      РОЗЫГРЫШ (БАНКИР)
# ========================

def build_raffle_text(uid: int) -> str:
    global raffle_round
    if raffle_round is None or not raffle_round.get("bets"):
        return (
            "👥 Розыгрыш начнётся, когда будет минимум два участника.\n"
            "🧔 Станьте первым, кто сделает ставку."
        )
    bets = raffle_round["bets"]
    total_bank = sum(bets.values())
    players_count = len(bets)
    user_bet = bets.get(uid, 0)
    if total_bank > 0 and user_bet > 0:
        chance = user_bet / total_bank * 100
        chance_text = f"{chance:.1f}%"
    else:
        chance_text = "0%"

    return (
        f"🎩 Банкир #{raffle_round['id']}\n"
        f"👨‍👩‍👧 Участников: {players_count}\n"
        f"💰 Банк: {format_coins(total_bank)} монет\n"
        f"🎯 Ваша ставка: {format_coins(user_bet)}\n"
        f"🎲 Ваш шанс: {chance_text}"
    )


def build_raffle_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="💰 Сделать ставку", callback_data="raffle_make_bet")],
        [
            InlineKeyboardButton(text="📋 Мои игры", callback_data="my_games:0"),
            InlineKeyboardButton(text="🏆 Рейтинг", callback_data="rating"),
        ],
        [
            InlineKeyboardButton(text="🎮 Игры", callback_data="menu_games"),
            InlineKeyboardButton(text="🐼 Помощь", callback_data="help"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_raffle_menu(chat_id: int, uid: int):
    await bot.send_message(
        chat_id,
        build_raffle_text(uid),
        reply_markup=build_raffle_menu_keyboard()
    )


async def schedule_raffle_draw():
    global raffle_task
    if raffle_task is not None and not raffle_task.done():
        return
    raffle_task = asyncio.create_task(raffle_draw_worker())


async def raffle_draw_worker():
    await asyncio.sleep(RAFFLE_TIMER_SECONDS)
    await perform_raffle_draw()


async def perform_raffle_draw():
    global raffle_round, raffle_task, next_raffle_id

    if raffle_round is None or not raffle_round.get("bets"):
        return

    bets = raffle_round["bets"]
    if len(bets) < 2:
        return

    total_bank = sum(bets.values())
    if total_bank <= 0:
        return

    # взвешенный рандом
    r = random.uniform(0, total_bank)
    upto = 0
    winner_id = None
    for uid, bet in bets.items():
        if upto + bet >= r:
            winner_id = uid
            break
        upto += bet

    if winner_id is None:
        winner_id = random.choice(list(bets.keys()))

    commission = total_bank // 100
    prize = total_bank - commission

    change_balance(winner_id, prize)
    change_balance(MAIN_ADMIN_ID, commission)

    # обновляем структуру розыгрыша и сохраняем в БД
    raffle_round["winner_id"] = winner_id
    raffle_round["finished_at"] = datetime.now(UTC)
    raffle_round["total_bank"] = total_bank
    await upsert_raffle_round(raffle_round)

    # уведомления участников
    for uid, bet in bets.items():
        if uid == winner_id:
            text = (
                f"🎉 Вы выиграли розыгрыш #{raffle_round['id']}!\n\n"
                f"💰 Банк: {format_coins(total_bank)} монет\n"
                f"💸 Комиссия (1%): {format_coins(commission)}\n"
                f"🏆 Ваш выигрыш: {format_coins(prize)} монет\n"
                f"💼 Баланс: {get_balance(uid)}"
            )
        else:
            text = (
                f"❌ Вы проиграли розыгрыш #{raffle_round['id']}.\n\n"
                f"💰 Банк: {format_coins(total_bank)} монет\n"
                f"💸 Ваша ставка: {format_coins(bet)} монет\n"
                f"💼 Баланс: {get_balance(uid)}"
            )
        try:
            await bot.send_message(uid, text)
        except Exception:
            pass

    # уведомление админу
    try:
        await bot.send_message(
            MAIN_ADMIN_ID,
            f"💰 Розыгрыш #{raffle_round['id']} завершён.\n"
            f"Банк: {format_coins(total_bank)} монет\n"
            f"Комиссия (1%): {format_coins(commission)} монет\n"
            f"Победитель: {winner_id}"
        )
    except Exception:
        pass

    raffle_round = None
    raffle_task = None
    next_raffle_id += 1


async def place_raffle_bet(uid: int, amount: int):
    global raffle_round, next_raffle_id

    if amount < RAFFLE_MIN_BET:
        raise ValueError(f"Минимальная ставка {RAFFLE_MIN_BET} монет")

    if get_balance(uid) < amount:
        raise RuntimeError("Недостаточно монет на балансе")

    change_balance(uid, -amount)

    if raffle_round is None:
        raffle_round = {
            "id": next_raffle_id,
            "bets": {},
            "created_at": datetime.now(UTC),
            "finished_at": None,
            "winner_id": None,
            "total_bank": 0,
        }
        await upsert_raffle_round(raffle_round)

    bets = raffle_round["bets"]
    bets[uid] = bets.get(uid, 0) + amount

    await add_raffle_bet(raffle_round["id"], uid, amount)

    total_bank = sum(bets.values())
    user_bet = bets[uid]
    chance = user_bet / total_bank * 100 if total_bank > 0 else 0.0

    if len(bets) >= 2:
        await schedule_raffle_draw()

    return total_bank, user_bet, chance


# ========================
#      СТАРТ, МЕНЮ
# ========================

@dp.message(Command("start"))
async def cmd_start(m: types.Message):
    register_user(m.from_user)
    get_balance(m.from_user.id)
    await m.answer(
        "Добро пожаловать в игровой бот TON!\n"
        "Здесь вы найдёте кости, розыгрыши и честные игры на монеты.\n"
        "Пополняйте TON, играйте — выигрывайте!",
        reply_markup=bottom_menu(),
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎲 Кости", callback_data="mode_dice")],
            [InlineKeyboardButton(text="🎩 Банкир", callback_data="mode_banker")],
        ]
    )
    await m.answer("Выберите режим игры:", reply_markup=kb)


@dp.message(F.text == "🕹 Игры")
async def msg_games(m: types.Message):
    register_user(m.from_user)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎲 Кости", callback_data="mode_dice")],
            [InlineKeyboardButton(text="🎩 Банкир", callback_data="mode_banker")],
        ]
    )
    await m.answer("Выберите режим игры:", reply_markup=kb)


@dp.message(F.text == "🎁 Розыгрыш")
async def msg_raffle_main(m: types.Message):
    register_user(m.from_user)
    await send_raffle_menu(m.chat.id, m.from_user.id)


@dp.callback_query(F.data == "mode_dice")
async def cb_mode_dice(callback: CallbackQuery):
    await send_games_list(callback.message.chat.id, callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "mode_banker")
async def cb_mode_banker(callback: CallbackQuery):
    await send_raffle_menu(callback.message.chat.id, callback.from_user.id)
    await callback.answer()


@dp.message(F.text == "💼 Баланс")
async def msg_balance(m: types.Message):
    register_user(m.from_user)
    uid = m.from_user.id
    bal_text = await format_balance_text(uid)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 Пополнить (TON)", callback_data="deposit_menu")],
            [InlineKeyboardButton(text="🔄 Перевод", callback_data="transfer_menu")],
            [InlineKeyboardButton(text="💸 Вывод TON", callback_data="withdraw_menu")],
        ]
    )
    await m.answer(bal_text, reply_markup=kb)


@dp.message(F.text == "🌐 Поддержка")
async def msg_support(m: types.Message):
    register_user(m.from_user)
    await m.answer("Поддержка: @Btcbqq")


# ========================
#      АДМИН-КОМАНДЫ
# ========================

@dp.message(Command("addbalance"))
async def cmd_addbalance(m: types.Message):
    register_user(m.from_user)
    if not is_admin(m.from_user.id):
        return await m.answer("⛔ Нет прав.")
    parts = m.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        return await m.answer("Использование: /addbalance user_id amount")

    uid = int(parts[1])
    amount = int(parts[2])
    change_balance(uid, amount)
    await m.answer(f"✅ Баланс {uid} увеличен на {amount} монет. Теперь: {get_balance(uid)}")


@dp.message(Command("removebalance"))
async def cmd_removebalance(m: types.Message):
    register_user(m.from_user)
    if not is_admin(m.from_user.id):
        return await m.answer("⛔ Нет прав.")
    parts = m.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        return await m.answer("Использование: /removebalance user_id amount")

    uid = int(parts[1])
    amount = int(parts[2])
    change_balance(uid, -amount)
    await m.answer(f"✅ Баланс {uid} уменьшен на {amount} монет. Теперь: {get_balance(uid)}")


@dp.message(Command("setbalance"))
async def cmd_setbalance(m: types.Message):
    register_user(m.from_user)
    if not is_admin(m.from_user.id):
        return await m.answer("⛔ Нет прав.")
    parts = m.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        return await m.answer("Использование: /setbalance user_id amount")

    uid = int(parts[1])
    amount = int(parts[2])
    set_balance(uid, amount)
    await m.answer(f"✅ Баланс {uid} установлен на {amount} монет")


@dp.message(Command("adminprofit"))
async def cmd_adminprofit(m: types.Message):
    register_user(m.from_user)
    if m.from_user.id != MAIN_ADMIN_ID:
        return await m.answer("⛔ Только основной админ.")
    bal = get_balance(MAIN_ADMIN_ID)
    rate = await get_ton_rub_rate()
    ton_equiv = bal / rate if rate > 0 else 0
    await m.answer(
        f"💸 Баланс админа (накопленная комиссия и игры): {format_coins(bal)} монет.\n"
        f"≈ {ton_equiv:.4f} TON по текущему курсу ({rate:.2f} ₽ за 1 TON).\n"
        f"Эти монеты можно вывести, обменяв TON на рубли."
    )


# ========================
#      ПОПОЛНЕНИЕ ЧЕРЕЗ TON
# ========================

@dp.callback_query(F.data == "deposit_menu")
async def cb_deposit_menu(callback: CallbackQuery):
    uid = callback.from_user.id
    rate = await get_ton_rub_rate()
    half_ton = int(rate * 0.5)
    one_ton = int(rate * 1)

    ton_url = f"ton://transfer/{TON_WALLET_ADDRESS}?text=ID{uid}"

    text = (
        "💎 Пополнение через TON\n\n"
        f"1 TON ≈ {rate:.2f} монет (₽).\n"
        f"0.5 TON ≈ {format_coins(half_ton)} монет.\n"
        f"1 TON ≈ {format_coins(one_ton)} монет.\n\n"
        "Как пополнить:\n"
        "1️⃣ Откройте TON-кошелёк (Tonkeeper/@wallet).\n"
        f"2️⃣ Отправьте TON на адрес: <code>{TON_WALLET_ADDRESS}</code>\n"
        f"3️⃣ В комментарии к переводу укажите: <code>ID{uid}</code> (обязательно!).\n"
        "4️⃣ Бот автоматически зачислит монеты по этому ID и отправит уведомление.\n\n"
        "Важно: 1 монета = 1 рубль (внутренняя валюта бота)."
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 Открыть кошелёк", url=ton_url)],
        ]
    )

    await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


async def ton_deposit_worker():
    """Периодически опрашивает tonapi по адресу кошелька и ищет новые входящие переводы.

    Для зачисления бот ищет в комментарии текст вида ID<user_id>, например ID123456789.
    Это значение мы просим пользователя указывать при пополнении.
    """
    if not TON_WALLET_ADDRESS:
        print("TON_WALLET_ADDRESS не задан, ton_deposit_worker не запускается.")
        return

    url = f"https://tonapi.io/v2/blockchain/accounts/{TON_WALLET_ADDRESS}/transactions?limit=50"

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    data = await resp.json()

            tx_list = data.get("transactions") or data.get("data") or []

            for tx in tx_list:
                tx_hash = tx.get("hash") or tx.get("transaction_id") or ""
                if not tx_hash or tx_hash in processed_ton_tx:
                    continue

                # пробуем вытащить комментарий (text) из разных полей
                comment = ""
                in_msg = tx.get("in_msg") or tx.get("in_message") or {}
                if isinstance(in_msg, dict):
                    comment = in_msg.get("message") or ""
                    msg_data = in_msg.get("msg_data") or {}
                    if isinstance(msg_data, dict):
                        comment = msg_data.get("text") or comment

                if not comment:
                    processed_ton_tx.add(tx_hash)
                    continue

                # ищем ID<user_id>
                m = re.search(r"ID(\d{5,15})", str(comment))
                if not m:
                    processed_ton_tx.add(tx_hash)
                    continue

                user_id = int(m.group(1))

                # сумма перевода в nanotons, поле value может быть строкой
                value_nanoton = 0
                if isinstance(in_msg, dict):
                    v = in_msg.get("value")
                    if isinstance(v, str) and v.isdigit():
                        value_nanoton = int(v)
                    elif isinstance(v, int):
                        value_nanoton = v

                if value_nanoton <= 0:
                    processed_ton_tx.add(tx_hash)
                    continue

                ton_amount = value_nanoton / 1e9
                rate = await get_ton_rub_rate()
                coins = int(ton_amount * rate)

                if coins <= 0:
                    processed_ton_tx.add(tx_hash)
                    continue

                change_balance(user_id, coins)
                processed_ton_tx.add(tx_hash)

                await add_ton_deposit(tx_hash, user_id, ton_amount, coins, comment)

                try:
                    await bot.send_message(
                        user_id,
                        f"✅ Пополнение через TON успешно!\n\n"
                        f"Получено: {ton_amount:.4f} TON\n"
                        f"Курс: 1 TON ≈ {rate:.2f} монет (₽)\n"
                        f"Зачислено: {format_coins(coins)} монет\n"
                        f"Текущий баланс: {format_coins(get_balance(user_id))} монет."
                    )
                except Exception:
                    pass

                try:
                    await bot.send_message(
                        MAIN_ADMIN_ID,
                        f"💎 Новое пополнение через TON\n"
                        f"User ID: {user_id}\n"
                        f"Комментарий: {comment}\n"
                        f"Сумма: {ton_amount:.4f} TON ≈ {format_coins(coins)} монет"
                    )
                except Exception:
                    pass

        except Exception as e:
            print("Ошибка в ton_deposit_worker:", e)

        await asyncio.sleep(20)


# ========================
#      ВЫВОД (ТОН)
# ========================

@dp.callback_query(F.data == "withdraw_menu")
async def cb_withdraw_menu(callback: CallbackQuery):
    uid = callback.from_user.id
    bal = get_balance(uid)
    if bal <= 0:
        await callback.answer("Баланс нулевой.", show_alert=True)
        return
    pending_withdraw_step[uid] = "amount"
    temp_withdraw[uid] = {}

    rate = await get_ton_rub_rate()
    ton_equiv = bal / rate if rate > 0 else 0

    await callback.message.answer(
        f"💸 Вывод средств в TON\n"
        f"Ваш баланс: {format_coins(bal)} монет (≈ {ton_equiv:.4f} TON)\n"
        f"1 TON ≈ {rate:.2f} монет.\n\n"
        f"Введите сумму монет для вывода (целое число):"
    )
    await callback.answer()


# ========================
#      ПЕРЕВОДЫ МЕЖДУ ПОЛЬЗОВАТЕЛЯМИ
# ========================

@dp.callback_query(F.data == "transfer_menu")
async def cb_transfer_menu(callback: CallbackQuery):
    uid = callback.from_user.id
    pending_transfer_step[uid] = "target"
    temp_transfer[uid] = {}
    await callback.message.answer(
        "🔄 Перевод монет\n"
        "Введите ID или @username получателя.\n"
        "Важно: получатель должен хотя бы раз написать боту."
    )
    await callback.answer()


def resolve_user_by_username(username_str: str) -> int | None:
    uname = username_str.strip().lstrip("@").lower()
    for uid, uname_stored in user_usernames.items():
        if uname_stored and uname_stored.lower() == uname:
            return uid
    return None


# ========================
#      СОЗДАНИЕ ИГРЫ (КОСТИ)
# ========================

@dp.callback_query(F.data == "create_game")
async def cb_create_game(callback: CallbackQuery):
    uid = callback.from_user.id
    pending_bet_input[uid] = True
    await callback.message.answer(
        f"Введите ставку (числом, в монетах). Минимум {DICE_MIN_BET} монет:"
    )
    await callback.answer()


# ========================
#      РОЗЫГРЫШ: КНОПКИ
# ========================

@dp.callback_query(F.data == "raffle_make_bet")
async def cb_raffle_make_bet(callback: CallbackQuery):
    rows = [
        [
            InlineKeyboardButton(
                text=f"💰 {format_coins(RAFFLE_QUICK_BETS[0])} монет",
                callback_data=f"raffle_quick:{RAFFLE_QUICK_BETS[0]}"
            ),
            InlineKeyboardButton(
                text=f"💰 {format_coins(RAFFLE_QUICK_BETS[1])} монет",
                callback_data=f"raffle_quick:{RAFFLE_QUICK_BETS[1]}"
            ),
            InlineKeyboardButton(
                text=f"💰 {format_coins(RAFFLE_QUICK_BETS[2])} монет",
                callback_data=f"raffle_quick:{RAFFLE_QUICK_BETS[2]}"
            ),
        ],
        [InlineKeyboardButton(text="🔢 Ввести сумму", callback_data="raffle_enter_amount")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="raffle_back")],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await callback.message.answer(
        f"Выберите сумму (минимум {RAFFLE_MIN_BET} монет):",
        reply_markup=kb
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("raffle_quick:"))
async def cb_raffle_quick(callback: CallbackQuery):
    uid = callback.from_user.id
    amount = int(callback.data.split(":", 1)[1])
    try:
        total, user_bet, chance = await place_raffle_bet(uid, amount)
    except ValueError as e:
        await callback.message.answer(str(e))
        await callback.answer()
        return
    except RuntimeError as e:
        await callback.message.answer(str(e))
        await callback.answer()
        return

    await callback.message.answer(
        f"✅ Ставка в розыгрыше принята!\n"
        f"Ваша общая ставка: {format_coins(user_bet)} монет\n"
        f"Общий банк: {format_coins(total)} монет\n"
        f"Ваш шанс: {chance:.1f}%"
    )
    await callback.answer()


@dp.callback_query(F.data == "raffle_enter_amount")
async def cb_raffle_enter_amount(callback: CallbackQuery):
    uid = callback.from_user.id
    pending_raffle_bet_input[uid] = True
    await callback.message.answer(
        f"Введите сумму ставки для розыгрыша (целое число, минимум {RAFFLE_MIN_BET} монет):"
    )
    await callback.answer()


@dp.callback_query(F.data == "raffle_back")
async def cb_raffle_back(callback: CallbackQuery):
    await send_raffle_menu(callback.message.chat.id, callback.from_user.id)
    await callback.answer()


# ========================
#      ОБРАБОТКА ТЕКСТА
# ========================

@dp.message()
async def process_text(m: types.Message):
    register_user(m.from_user)
    uid = m.from_user.id
    text = (m.text or "").strip()

    # не перехватываем команды
    if text.startswith("/"):
        return

    # 1) ввод ставки для костей
    if pending_bet_input.get(uid):
        if not text.isdigit():
            return await m.answer("Введите корректную ставку (число):")
        bet = int(text)
        if bet < DICE_MIN_BET:
            return await m.answer(f"Минимальная ставка {DICE_MIN_BET} монет.")
        if bet > get_balance(uid):
            return await m.answer("Недостаточно монет на балансе!")

        global next_game_id
        gid = next_game_id
        next_game_id += 1

        games[gid] = {
            "id": gid,
            "creator_id": uid,
            "opponent_id": None,
            "bet": bet,
            "creator_roll": None,
            "opponent_roll": None,
            "winner": None,
            "finished": False,
            "created_at": datetime.now(UTC),
            "finished_at": None,
        }

        change_balance(uid, -bet)
        pending_bet_input.pop(uid)

        # сохраняем игру в БД
        await upsert_game(games[gid])

        await m.answer(f"✅ Игра №{gid} создана!")
        return await send_games_list(m.chat.id, uid)

    # 2) вывод — шаг суммы
    if pending_withdraw_step.get(uid) == "amount":
        if not text.isdigit():
            return await m.answer("Введите сумму числом:")
        amount = int(text)
        bal = get_balance(uid)
        if amount <= 0:
            return await m.answer("Сумма должна быть > 0.")
        if amount > bal:
            return await m.answer(f"Недостаточно монет. Ваш баланс: {bal}.")
        temp_withdraw[uid]["amount"] = amount
        pending_withdraw_step[uid] = "details"

        rate = await get_ton_rub_rate()
        ton_amount = amount / rate if rate > 0 else 0
        approx = f"{ton_amount:.4f} TON"
        return await m.answer(
            f"💸 Вывод в TON\n"
            f"Сумма: {amount} монет (≈ {approx})\n\n"
            f"Напишите комментарий к выводу (например, удобное время, TON-кошелёк, доп. информация):"
        )

    # 3) вывод — шаг реквизитов
    if pending_withdraw_step.get(uid) == "details":
        details = text
        amount = temp_withdraw[uid]["amount"]
        user = m.from_user
        username = user.username
        if username:
            mention = f"@{username}"
            link = f"https://t.me/{username}"
        else:
            mention = f"id {uid}"
            link = f"tg://user?id={uid}"

        rate = await get_ton_rub_rate()
        ton_amount = amount / rate if rate > 0 else 0
        ton_text = f"{ton_amount:.4f} TON"

        msg_admin = (
            f"💸 НОВАЯ ЗАЯВКА НА ВЫВОД (TON)\n\n"
            f"👤 Пользователь: {mention}\n"
            f"🆔 user_id: {uid}\n"
            f"🔗 Профиль: {link}\n\n"
            f"💰 Сумма: {amount} монет\n"
            f"💎 Эквивалент: {ton_text}\n"
            f"📄 Комментарий: {details}\n\n"
            f"После фактической отправки TON уменьшите баланс через /removebalance или /setbalance."
        )
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, msg_admin)
            except Exception:
                pass

        await m.answer(
            "✅ Заявка на вывод отправлена администратору.\n"
            "После обработки вам отправят TON на указанные реквизиты."
        )

        pending_withdraw_step.pop(uid, None)
        temp_withdraw.pop(uid, None)
        return

    # 4) перевод — выбор получателя
    if pending_transfer_step.get(uid) == "target":
        target_id: int | None = None
        if text.startswith("@"):
            target_id = resolve_user_by_username(text)
        elif text.isdigit():
            target_id = int(text)
        else:
            target_id = resolve_user_by_username(text)

        if not target_id:
            return await m.answer(
                "Не удалось найти пользователя.\n"
                "Убедитесь, что он уже писал боту, и введите его ID или @username."
            )
        if target_id == uid:
            return await m.answer("Нельзя переводить самому себе.")

        temp_transfer[uid]["target_id"] = target_id
        pending_transfer_step[uid] = "amount_transfer"
        return await m.answer(
            "Введите сумму монет для перевода (минимум 1):"
        )

    # 5) перевод — сумма
    if pending_transfer_step.get(uid) == "amount_transfer":
        if not text.isdigit():
            return await m.answer("Введите сумму числом:")
        amount = int(text)
        if amount <= 0:
            return await m.answer("Сумма должна быть > 0.")
        bal = get_balance(uid)
        if amount > bal:
            return await m.answer(f"Недостаточно монет. Ваш баланс: {bal}.")

        target_id = temp_transfer[uid].get("target_id")
        if not target_id:
            pending_transfer_step.pop(uid, None)
            temp_transfer.pop(uid, None)
            return await m.answer("Ошибка: не найден получатель, попробуйте ещё раз.")

        change_balance(uid, -amount)
        change_balance(target_id, amount)

        await add_transfer(uid, target_id, amount)

        await m.answer(
            f"✅ Перевод выполнен.\n"
            f"Вы отправили {format_coins(amount)} монет пользователю ID {target_id}.\n"
            f"Ваш новый баланс: {get_balance(uid)} монет."
        )
        try:
            await bot.send_message(
                target_id,
                f"🔄 Вам перевели {format_coins(amount)} монет от пользователя ID {uid}.\n"
                f"Ваш новый баланс: {get_balance(target_id)} монет."
            )
        except Exception:
            pass

        pending_transfer_step.pop(uid, None)
        temp_transfer.pop(uid, None)
        return

    # 6) ввод суммы ставки для розыгрыша
    if pending_raffle_bet_input.get(uid):
        if not text.isdigit():
            return await m.answer("Введите сумму числом:")
        amount = int(text)
        try:
            total, user_bet, chance = await place_raffle_bet(uid, amount)
        except ValueError as e:
            return await m.answer(str(e))
        except RuntimeError as e:
            return await m.answer(str(e))

        pending_raffle_bet_input.pop(uid, None)

        return await m.answer(
            f"✅ Ставка в розыгрыше принята!\n"
            f"Ваша общая ставка: {format_coins(user_bet)} монет\n"
            f"Общий банк: {format_coins(total)} монет\n"
            f"Ваш шанс: {chance:.1f}%"
        )

    await m.answer("Используйте меню или /start.")


# ========================
#      ОКНО ЧУЖОЙ ИГРЫ (КОСТИ)
# ========================

@dp.callback_query(F.data.startswith("game_open:"))
async def cb_game_open(callback: CallbackQuery):
    gid = int(callback.data.split(":", 1)[1])
    g = games.get(gid)

    if not g:
        return await callback.answer("Игра не найдена.", show_alert=True)
    if g["opponent_id"] is not None:
        return await callback.answer("Кто-то уже вступил!", show_alert=True)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✔ Вступить", callback_data=f"join_confirm:{gid}")],
            [InlineKeyboardButton(text="⬅ Назад", callback_data="menu_games")],
        ]
    )

    await callback.message.answer(
        f"🎲 Игра №{gid}\n"
        f"💰 Ставка: {format_coins(g['bet'])} монет\n\n"
        f"Хотите вступить?",
        reply_markup=kb
    )
    await callback.answer()


# ========================
#      ОКНО СВОЕЙ ИГРЫ (КОСТИ)
# ========================

@dp.callback_query(F.data.startswith("game_my:"))
async def cb_game_my(callback: CallbackQuery):
    uid = callback.from_user.id
    gid = int(callback.data.split(":", 1)[1])

    g = games.get(gid)
    if not g:
        return await callback.answer("Игра не найдена.", show_alert=True)
    if g["creator_id"] != uid:
        return await callback.answer("Это не ваша игра.", show_alert=True)
    if g["opponent_id"] is not None:
        return await callback.answer("Уже есть соперник.", show_alert=True)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить ставку", callback_data=f"cancel_game:{gid}")],
            [InlineKeyboardButton(text="⬅ Назад", callback_data="menu_games")],
        ]
    )

    await callback.message.answer(
        f"🎲 Ваша игра №{gid}\n"
        f"💰 Ставка: {format_coins(g['bet'])} монет\n\n"
        f"Ожидание соперника...",
        reply_markup=kb
    )
    await callback.answer()


# ========================
#      ОТМЕНА СТАВКИ (КОСТИ)
# ========================

@dp.callback_query(F.data.startswith("cancel_game:"))
async def cb_cancel_game(callback: CallbackQuery):
    uid = callback.from_user.id
    gid = int(callback.data.split(":", 1)[1])

    g = games.get(gid)
    if not g:
        return await callback.answer("Игра не найдена.", show_alert=True)
    if g["creator_id"] != uid:
        return await callback.answer("Это не ваша игра.", show_alert=True)
    if g["opponent_id"] is not None:
        return await callback.answer("Уже есть соперник.", show_alert=True)

    bet = g["bet"]
    change_balance(uid, bet)
    del games[gid]

    await callback.message.answer(
        f"❌ Ставка №{gid} отменена. {format_coins(bet)} монет возвращены на баланс."
    )
    await send_games_list(callback.message.chat.id, uid)
    await callback.answer()


# ========================
#      ПОДТВЕРЖДЕНИЕ ВСТУПЛЕНИЯ (КОСТИ)
# ========================

@dp.callback_query(F.data.startswith("join_confirm:"))
async def cb_join_confirm(callback: CallbackQuery):
    uid = callback.from_user.id
    gid = int(callback.data.split(":", 1)[1])

    g = games.get(gid)
    if not g:
        return await callback.answer("Игра не найдена.", show_alert=True)
    if g["opponent_id"] is not None:
        return await callback.answer("Кто-то уже вступил!", show_alert=True)

    bet = g["bet"]
    if get_balance(uid) < bet:
        return await callback.answer("Недостаточно монет.", show_alert=True)

    g["opponent_id"] = uid
    change_balance(uid, -bet)

    # обновляем игру в БД (добавился соперник)
    await upsert_game(g)

    await callback.message.answer(f"✅ Вы присоединились к игре №{gid}!")
    await callback.answer()

    await play_game(gid)


# ========================
#      МОИ ИГРЫ (СТАТИСТИКА)
# ========================

@dp.callback_query(F.data.startswith("my_games"))
async def cb_my_games(callback: CallbackQuery):
    uid = callback.from_user.id
    page = int(callback.data.split(":", 1)[1])

    stats, history = await build_user_stats_and_history(uid)
    kb = build_history_keyboard(history, page)

    await callback.message.answer(stats, reply_markup=kb)
    await callback.answer()


# ========================
#      ОБНОВИТЬ СПИСОК ИГР
# ========================

@dp.callback_query(F.data == "refresh_games")
async def cb_refresh_games(callback: CallbackQuery):
    uid = callback.from_user.id
    try:
        await callback.message.edit_text(
            build_games_text(),
            reply_markup=build_games_keyboard(uid)
        )
    except Exception:
        await callback.message.answer(
            build_games_text(),
            reply_markup=build_games_keyboard(uid)
        )
    await callback.answer("Обновлено!")


# ========================
#      РЕЙТИНГ
# ========================

@dp.callback_query(F.data == "rating")
async def cb_rating(callback: CallbackQuery):
    text = await build_rating_text()
    await callback.message.answer(text)
    await callback.answer()


# ========================
#      ПРОЧЕЕ
# ========================

@dp.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery):
    await callback.message.answer(
        "🐼 Помощь:\n"
        "1. Пополните баланс монет, отправив TON на указанный кошелёк.\n"
        "2. 'Кости' — дуэль 1 на 1.\n"
        "3. 'Банкир' — розыгрыш, шанс зависит от вашей ставки.\n"
        "4. С каждой игры удерживается 1% комиссии в пользу админа.\n"
        "5. Вывод — в TON по курсу.\n"
        "Переводы между игроками доступны в разделе Баланс."
    )
    await callback.answer()


@dp.callback_query(F.data == "menu_games")
async def cb_menu_games(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎲 Кости", callback_data="mode_dice")],
            [InlineKeyboardButton(text="🎩 Банкир", callback_data="mode_banker")],
        ]
    )
    await callback.message.answer("Выберите режим игры:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "ignore")
async def cb_ignore(callback: CallbackQuery):
    await callback.answer()


# ========================
#      ЗАПУСК БОТА
# ========================

async def main():
    print("Бот запущен (TON + Кости + Банкир + переводы, SQLite).")
    # инициализация БД и загрузка данных
    await init_db(user_balances, user_usernames, processed_ton_tx)
    asyncio.create_task(cleanup_worker())
    asyncio.create_task(ton_deposit_worker())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
