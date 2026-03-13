# ==================== ПОЛНЫЙ КОД БОТА ====================

import asyncio
import logging
import os
from datetime import datetime
from dotenv import load_dotenv

import ccxt.async_support as ccxt
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode

load_dotenv()

# ==================== НАСТРОЙКИ ====================
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL = "BTC/USDT"
TIMEFRAME = "5m"
POSITION_SIZE = 2000        # Размер позиции в USDT
STOP_LOSS_PCT = 0.7         # Стоп-лосс 0.7%
TAKE_PROFIT_PCT = 3.0       # Тейк-профит 3%
BREAKEVEN_PCT = 0.75        # Безубыток при +0.75%
TRAILING_OFFSET_PCT = 0.7   # Отступ трейлинга 0.7%
COMMISSION_PCT = 0.055      # Комиссия 0.055% (в одну сторону)

# ==================== ЛОГИРОВАНИЕ ====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ==================== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ====================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
exchange = None

position = None  # Текущая позиция
trade_history = []  # История сделок
highest_pnl_pct = 0  # Максимальный PnL для трейлинга


# ==================== СТРУКТУРА ПОЗИЦИИ ====================
def create_position(side, entry_price, amount):
    return {
        "side": side,
        "entry_price": entry_price,
        "amount": amount,
        "stop_loss": entry_price * (1 - STOP_LOSS_PCT / 100) if side == "long" else entry_price * (1 + STOP_LOSS_PCT / 100),
        "take_profit": entry_price * (1 + TAKE_PROFIT_PCT / 100) if side == "long" else entry_price * (1 - TAKE_PROFIT_PCT / 100),
        "trailing_active": False,
        "breakeven_hit": False,
        "highest_price": entry_price if side == "long" else None,
        "lowest_price": entry_price if side == "short" else None,
        "open_time": datetime.now(),
        "position_size_usdt": POSITION_SIZE,
    }


# ==================== КОМИССИЯ ====================
def calc_commission(usdt_size):
    return usdt_size * COMMISSION_PCT / 100 * 2  # вход + выход


# ==================== PnL ====================
def calc_pnl(pos, current_price):
    if pos["side"] == "long":
        pnl_pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
    else:
        pnl_pct = (pos["entry_price"] - current_price) / pos["entry_price"] * 100

    pnl_usdt = pos["position_size_usdt"] * pnl_pct / 100
    commission = calc_commission(pos["position_size_usdt"])
    net_pnl = pnl_usdt - commission

    return pnl_pct, net_pnl, commission


# ==================== ИНДИКАТОРЫ ====================
def calc_ema(closes, period):
    if len(closes) < period:
        return None
    ema = sum(closes[:period]) / period
    multiplier = 2 / (period + 1)
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(0, delta))
        losses.append(max(0, -delta))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_macd(closes, fast=12, slow=26, signal=9):
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    if ema_fast is None or ema_slow is None:
        return None, None, None

    macd_line = ema_fast - ema_slow

    if len(closes) < slow + signal:
        return macd_line, None, None

    macd_values = []
    ema_s = sum(closes[:fast]) / fast
    ema_sl = sum(closes[:slow]) / slow
    m_fast = 2 / (fast + 1)
    m_slow = 2 / (slow + 1)

    for i in range(max(fast, slow), len(closes)):
        ema_s = (closes[i] - ema_s) * m_fast + ema_s
        ema_sl = (closes[i] - ema_sl) * m_slow + ema_sl
        macd_values.append(ema_s - ema_sl)

    if len(macd_values) < signal:
        return macd_line, None, None

    signal_line = sum(macd_values[:signal]) / signal
    m_sig = 2 / (signal + 1)
    for val in macd_values[signal:]:
        signal_line = (val - signal_line) * m_sig + signal_line

    histogram = macd_values[-1] - signal_line
    return macd_values[-1], signal_line, histogram


# ==================== СИГНАЛЫ ====================
def get_signal(closes, volumes):
    if len(closes) < 50:
        return None

    ema9 = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    ema50 = calc_ema(closes, 50)
    rsi = calc_rsi(closes)
    macd_line, signal_line, histogram = calc_macd(closes)

    if None in (ema9, ema21, ema50, rsi, macd_line, signal_line):
        return None

    current_price = closes[-1]
    avg_volume = sum(volumes[-20:]) / 20
    current_volume = volumes[-1]

    # LONG сигнал
    if (ema9 > ema21 > ema50 and
            current_price > ema9 and
            rsi > 45 and rsi < 70 and
            macd_line > signal_line and
            current_volume > avg_volume * 0.8):
        return "long"

    # SHORT сигнал
    if (ema9 < ema21 < ema50 and
            current_price < ema9 and
            rsi < 55 and rsi > 30 and
            macd_line < signal_line and
            current_volume > avg_volume * 0.8):
        return "short"

    return None


# ==================== УПРАВЛЕНИЕ ПОЗИЦИЕЙ ====================
async def check_position(current_price):
    global position, highest_pnl_pct

    if position is None:
        return

    pos = position
    pnl_pct, net_pnl, commission = calc_pnl(pos, current_price)

    # Обновляем максимальную/минимальную цену
    if pos["side"] == "long":
        if current_price > (pos.get("highest_price") or 0):
            pos["highest_price"] = current_price
    else:
        if pos.get("lowest_price") is None or current_price < pos["lowest_price"]:
            pos["lowest_price"] = current_price

    # 1. СТОП-ЛОСС
    if pos["side"] == "long" and current_price <= pos["stop_loss"]:
        await close_position(current_price, "СТОП-ЛОСС")
        return
    if pos["side"] == "short" and current_price >= pos["stop_loss"]:
        await close_position(current_price, "СТОП-ЛОСС")
        return

    # 2. ТЕЙК-ПРОФИТ
    if pos["side"] == "long" and current_price >= pos["take_profit"]:
        await close_position(current_price, "ТЕЙК-ПРОФИТ")
        return
    if pos["side"] == "short" and current_price <= pos["take_profit"]:
        await close_position(current_price, "ТЕЙК-ПРОФИТ")
        return

    # 3. БЕЗУБЫТОК
    if not pos["breakeven_hit"] and pnl_pct >= BREAKEVEN_PCT:
        pos["breakeven_hit"] = True
        pos["trailing_active"] = True
        # Стоп на цену входа (безубыток)
        pos["stop_loss"] = pos["entry_price"]
        highest_pnl_pct = pnl_pct
        await send_message(
            f"🔄 <b>Безубыток активирован</b>\n"
            f"Стоп перенесён на {pos['stop_loss']:.2f}\n"
            f"Трейлинг включён (отступ {TRAILING_OFFSET_PCT}%)"
        )

    # 4. ТРЕЙЛИНГ СТОП
    if pos["trailing_active"]:
        if pos["side"] == "long":
            new_stop = pos["highest_price"] * (1 - TRAILING_OFFSET_PCT / 100)
            if new_stop > pos["stop_loss"]:
                pos["stop_loss"] = new_stop
        else:
            new_stop = pos["lowest_price"] * (1 + TRAILING_OFFSET_PCT / 100)
            if new_stop < pos["stop_loss"]:
                pos["stop_loss"] = new_stop


# ==================== ОТКРЫТИЕ ПОЗИЦИИ ====================
async def open_position(side, price):
    global position

    amount = POSITION_SIZE / price

    try:
        if side == "long":
            order = await exchange.create_market_buy_order(SYMBOL, amount)
        else:
            order = await exchange.create_market_sell_order(SYMBOL, amount)

        position = create_position(side, price, amount)
        commission = calc_commission(POSITION_SIZE)

        emoji = "🟢" if side == "long" else "🔴"
        await send_message(
            f"{emoji} <b>Открыта {side.upper()} позиция</b>\n"
            f"Цена входа: {price:.2f}\n"
            f"Размер: {POSITION_SIZE} USDT ({amount:.6f} BTC)\n"
            f"Стоп: {position['stop_loss']:.2f} (-{STOP_LOSS_PCT}%)\n"
            f"Тейк: {position['take_profit']:.2f} (+{TAKE_PROFIT_PCT}%)\n"
            f"Безубыток при: +{BREAKEVEN_PCT}%\n"
            f"Комиссия: ~{commission:.2f}$"
        )
        logger.info(f"Opened {side} at {price}")

    except Exception as e:
        logger.error(f"Error opening position: {e}")
        await send_message(f"❌ Ошибка открытия позиции: {e}")


# ==================== ЗАКРЫТИЕ ПОЗИЦИИ ====================
async def close_position(current_price, reason):
    global position, highest_pnl_pct

    if position is None:
        return

    pos = position
    pnl_pct, net_pnl, commission = calc_pnl(pos, current_price)

    try:
        if pos["side"] == "long":
            await exchange.create_market_sell_order(SYMBOL, pos["amount"])
        else:
            await exchange.create_market_buy_order(SYMBOL, pos["amount"])

        duration = datetime.now() - pos["open_time"]
        hours = int(duration.total_seconds() // 3600)
        minutes = int((duration.total_seconds() % 3600) // 60)

        emoji = "✅" if net_pnl >= 0 else "❌"

        trade_record = {
            "side": pos["side"],
            "entry_price": pos["entry_price"],
            "exit_price": current_price,
            "pnl_pct": pnl_pct,
            "net_pnl": net_pnl,
            "commission": commission,
            "reason": reason,
            "duration": f"{hours}ч {minutes}м",
            "close_time": datetime.now(),
            "position_size": pos["position_size_usdt"],
        }
        trade_history.append(trade_record)

        await send_message(
            f"{emoji} <b>Позиция закрыта — {reason}</b>\n"
            f"Сторона: {pos['side'].upper()}\n"
            f"Вход: {pos['entry_price']:.2f}\n"
            f"Выход: {current_price:.2f}\n"
            f"PnL: {net_pnl:+.2f}$ ({pnl_pct:+.2f}%)\n"
            f"Комиссия: {commission:.2f}$\n"
            f"Длительность: {hours}ч {minutes}м"
        )

        position = None
        highest_pnl_pct = 0
        logger.info(f"Closed {pos['side']} at {current_price}, reason: {reason}, PnL: {net_pnl:.2f}$")

    except Exception as e:
        logger.error(f"Error closing position: {e}")
        await send_message(f"❌ Ошибка закрытия позиции: {e}")


# ==================== ОСНОВНОЙ ЦИКЛ ====================
async def trading_loop():
    global exchange

    exchange = ccxt.bybit({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "sandbox": False,
        "options": {"defaultType": "swap"},
    })

    await send_message(
        f"🤖 <b>Бот запущен</b>\n"
        f"Пара: {SYMBOL}\n"
        f"Таймфрейм: {TIMEFRAME}\n"
        f"Позиция: {POSITION_SIZE} USDT\n"
        f"Стоп: {STOP_LOSS_PCT}% | Тейк: {TAKE_PROFIT_PCT}%\n"
        f"Безубыток: +{BREAKEVEN_PCT}% | Трейлинг: {TRAILING_OFFSET_PCT}%"
    )

    while True:
        try:
            ohlcv = await exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=100)
            closes = [c[4] for c in ohlcv]
            volumes = [c[5] for c in ohlcv]
            current_price = closes[-1]

            # Проверяем текущую позицию
            await check_position(current_price)

            # Если нет позиции — ищем сигнал
            if position is None:
                signal = get_signal(closes, volumes)
                if signal:
                    await open_position(signal, current_price)

            # Если есть позиция и пришёл обратный сигнал — переворот
            elif position is not None:
                signal = get_signal(closes, volumes)
                if signal and signal != position["side"]:
                    await close_position(current_price, "СИГНАЛ РАЗВОРОТА")
                    await open_position(signal, current_price)

            await asyncio.sleep(30)

        except Exception as e:
            logger.error(f"Loop error: {e}")
            await asyncio.sleep(60)


# ==================== TELEGRAM КОМАНДЫ ====================
async def send_message(text):
    try:
        await bot.send_message(CHAT_ID, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Telegram send error: {e}")


def calc_total_stats(trades):
    if not trades:
        return ""
    total_pnl = sum(t["net_pnl"] for t in trades)
    total_pct = sum(t["pnl_pct"] for t in trades)
    wins = sum(1 for t in trades if t["net_pnl"] > 0)
    losses = sum(1 for t in trades if t["net_pnl"] <= 0)
    winrate = (wins / len(trades) * 100) if trades else 0

    emoji = "📈" if total_pnl >= 0 else "📉"

    return (
        f"\n{emoji} <b>Общая статистика:</b>\n"
        f"Всего сделок: {len(trades)}\n"
        f"Побед: {wins} | Поражений: {losses}\n"
        f"Winrate: {winrate:.1f}%\n"
        f"Общий PnL: {total_pnl:+.2f}$ ({total_pct:+.2f}%)"
    )


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🤖 <b>Торговый бот</b>\n\n"
        "/status — текущая позиция\n"
        "/history — история сделок\n"
        "/active — активная позиция\n"
        "/stats — статистика\n"
        "/balance — баланс",
        parse_mode=ParseMode.HTML
    )


@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    if position is None:
        await message.answer("📭 Нет открытых позиций. Ищу сигнал...")
        return

    try:
        ticker = await exchange.fetch_ticker(SYMBOL)
        current_price = ticker["last"]
        pnl_pct, net_pnl, commission = calc_pnl(position, current_price)

        emoji = "🟢" if position["side"] == "long" else "🔴"
        pnl_emoji = "✅" if net_pnl >= 0 else "❌"

        duration = datetime.now() - position["open_time"]
        hours = int(duration.total_seconds() // 3600)
        minutes = int((duration.total_seconds() % 3600) // 60)

        await message.answer(
            f"{emoji} <b>Активная позиция: {position['side'].upper()}</b>\n"
            f"Вход: {position['entry_price']:.2f}\n"
            f"Текущая: {current_price:.2f}\n"
            f"Стоп: {position['stop_loss']:.2f}\n"
            f"Тейк: {position['take_profit']:.2f}\n"
            f"Безубыток: {'✅' if position['breakeven_hit'] else '❌'}\n"
            f"Трейлинг: {'✅' if position['trailing_active'] else '❌'}\n"
            f"{pnl_emoji} PnL: {net_pnl:+.2f}$ ({pnl_pct:+.2f}%)\n"
            f"Длительность: {hours}ч {minutes}м",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("active"))
async def cmd_active(message: types.Message):
    if position is None:
        text = "📭 Нет активных позиций."
        text += calc_total_stats(trade_history)
        await message.answer(text, parse_mode=ParseMode.HTML)
        return

    try:
        ticker = await exchange.fetch_ticker(SYMBOL)
        current_price = ticker["last"]
        pnl_pct, net_pnl, commission = calc_pnl(position, current_price)

        emoji = "🟢" if position["side"] == "long" else "🔴"
        pnl_emoji = "✅" if net_pnl >= 0 else "❌"

        duration = datetime.now() - position["open_time"]
        hours = int(duration.total_seconds() // 3600)
        minutes = int((duration.total_seconds() % 3600) // 60)

        text = (
            f"{emoji} <b>Активная позиция: {position['side'].upper()}</b>\n"
            f"Вход: {position['entry_price']:.2f}\n"
            f"Текущая: {current_price:.2f}\n"
            f"Размер: {position['position_size_usdt']} USDT\n"
            f"Стоп: {position['stop_loss']:.2f}\n"
            f"Тейк: {position['take_profit']:.2f}\n"
            f"Безубыток: {'✅' if position['breakeven_hit'] else '❌'}\n"
            f"Трейлинг: {'✅' if position['trailing_active'] else '❌'}\n"
            f"{pnl_emoji} PnL: {net_pnl:+.2f}$ ({pnl_pct:+.2f}%)\n"
            f"Длительность: {hours}ч {minutes}м"
        )
        text += calc_total_stats(trade_history)
        await message.answer(text, parse_mode=ParseMode.HTML)

    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("history"))
async def cmd_history(message: types.Message):
    if not trade_history:
        await message.answer("📭 История пуста.")
        return

    last_trades = trade_history[-10:]
    text = "<b>📋 Последние сделки:</b>\n\n"

    for i, t in enumerate(last_trades, 1):
        emoji = "✅" if t["net_pnl"] >= 0 else "❌"
        side_emoji = "🟢" if t["side"] == "long" else "🔴"
        text += (
            f"{i}. {side_emoji}{emoji} {t['side'].upper()} | "
            f"{t['entry_price']:.2f} → {t['exit_price']:.2f}\n"
            f"   PnL: {t['net_pnl']:+.2f}$ ({t['pnl_pct']:+.2f}%) | "
            f"{t['reason']} | {t['duration']}\n\n"
        )

    text += calc_total_stats(trade_history)
    await message.answer(text, parse_mode=ParseMode.HTML)


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if not trade_history:
        await message.answer("📭 Нет данных для статистики.")
        return

    total_pnl = sum(t["net_pnl"] for t in trade_history)
    total_commission = sum(t["commission"] for t in trade_history)
    wins = [t for t in trade_history if t["net_pnl"] > 0]
    losses = [t for t in trade_history if t["net_pnl"] <= 0]
    winrate = len(wins) / len(trade_history) * 100

    avg_win = sum(t["net_pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["net_pnl"] for t in losses) / len(losses) if losses else 0
    best = max(trade_history, key=lambda t: t["net_pnl"])
    worst = min(trade_history, key=lambda t: t["net_pnl"])

    reasons = {}
    for t in trade_history:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1

    reasons_text = "\n".join(f"  {k}: {v}" for k, v in reasons.items())

    emoji = "📈" if total_pnl >= 0 else "📉"

    await message.answer(
        f"{emoji} <b>Статистика</b>\n\n"
        f"Всего сделок: {len(trade_history)}\n"
        f"Побед: {len(wins)} | Поражений: {len(losses)}\n"
        f"Winrate: {winrate:.1f}%\n\n"
        f"Общий PnL: {total_pnl:+.2f}$\n"
        f"Комиссии: {total_commission:.2f}$\n"
        f"Средняя победа: {avg_win:+.2f}$\n"
        f"Средний убыток: {avg_loss:+.2f}$\n\n"
        f"Лучшая: {best['net_pnl']:+.2f}$ ({best['side'].upper()})\n"
        f"Худшая: {worst['net_pnl']:+.2f}$ ({worst['side'].upper()})\n\n"
        f"<b>Причины закрытия:</b>\n{reasons_text}",
        parse_mode=ParseMode.HTML
    )


@dp.message(Command("balance"))
async def cmd_balance(message: types.Message):
    try:
        balance = await exchange.fetch_balance()
        usdt = balance.get("USDT", {})
        total = usdt.get("total", 0)
        free = usdt.get("free", 0)
        used = usdt.get("used", 0)

        await message.answer(
            f"💰 <b>Баланс</b>\n\n"
            f"Всего: {total:.2f} USDT\n"
            f"Свободно: {free:.2f} USDT\n"
            f"В позициях: {used:.2f} USDT",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ==================== ЗАПУСК ====================
async def main():
    asyncio.create_task(trading_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
