import ccxt
import pandas as pd
import asyncio
import logging
import os
from datetime import datetime, timedelta, time as dt_time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

# Настройка логов
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG ---
CONFIG = {
    'telegram_token': os.environ.get('TELEGRAM_TOKEN'),
    'chat_id': os.environ.get('CHAT_ID'),
    'symbols': [
        'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT',
        'ADA/USDT', 'AVAX/USDT', 'DOT/USDT', 'NEAR/USDT',
        'SUI/USDT', 'RENDER/USDT', 'FET/USDT', 'PEPE/USDT', 'POL/USDT'
    ],
    'timeframe': '5m',
    'ema_fast': 9,
    'ema_mid': 21,
    'ema_slow': 50,
    'rsi_period': 14,
    'macd_fast': 12,
    'macd_slow': 26,
    'macd_signal': 9,
    'vol_ma_period': 20,
    'balance': 1000,
    'leverage': 20,
    'risk_per_trade': 0.02,
    'stop_loss_pct': 0.007,
    'take_profit_pct': 0.03,
    'breakeven_trigger': 0.0075,
    'trailing_distance': 0.007,
    'commission_rate': 0.00055 * 2,
}

bot_instance = None


# --- ПЕРЕСЧЁТ БАЛАНСА ИЗ ИСТОРИИ ---
def get_current_balance():
    if not os.path.exists('history.csv'):
        return CONFIG['balance']
    df = pd.read_csv('history.csv')
    if df.empty:
        return CONFIG['balance']
    return round(CONFIG['balance'] + df['profit_usdt'].sum(), 2)


# --- ЖУРНАЛ ---

class TradeJournal:
    def __init__(self, filename='history.csv'):
        self.filename = filename
        if not os.path.exists(self.filename):
            pd.DataFrame(columns=[
                'date', 'timestamp', 'symbol', 'side', 'result',
                'profit_usdt', 'profit_pct', 'duration_min'
            ]).to_csv(self.filename, index=False)

    def log_trade(self, symbol, side, result, entry, exit_p, start_time):
        try:
            df = pd.read_csv(self.filename)
            price_diff_pct = ((exit_p - entry) / entry) if side == 'LONG' else ((entry - exit_p) / entry)
            current_balance = get_current_balance()
            risk_amount = current_balance * CONFIG['risk_per_trade']
            position_size_usdt = risk_amount / CONFIG['stop_loss_pct']

            commission_usdt = position_size_usdt * CONFIG['commission_rate']
            profit_usdt = (position_size_usdt * price_diff_pct) - commission_usdt

            now = datetime.now()
            duration = int((now - start_time).total_seconds() / 60)

            new_row = {
                'date': now.strftime('%d.%m %H:%M'),
                'timestamp': now.timestamp(),
                'symbol': symbol,
                'side': side,
                'result': result,
                'profit_usdt': round(profit_usdt, 2),
                'profit_pct': round((price_diff_pct - CONFIG['commission_rate']) * 100, 2),
                'duration_min': duration
            }
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            df.to_csv(self.filename, index=False)
            return new_row
        except Exception as e:
            logger.error(f"Journal error: {e}")
            return None


# --- ИНДИКАТОРЫ ---

def add_indicators(df, cfg):
    df = df.copy()

    df['ema_fast'] = df['close'].ewm(span=cfg['ema_fast'], adjust=False).mean()
    df['ema_mid'] = df['close'].ewm(span=cfg['ema_mid'], adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=cfg['ema_slow'], adjust=False).mean()

    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / cfg['rsi_period'], min_periods=cfg['rsi_period']).mean()
    avg_loss = loss.ewm(alpha=1 / cfg['rsi_period'], min_periods=cfg['rsi_period']).mean()
    df['rsi'] = 100 - (100 / (1 + (avg_gain / avg_loss)))

    ema_macd_fast = df['close'].ewm(span=cfg['macd_fast'], adjust=False).mean()
    ema_macd_slow = df['close'].ewm(span=cfg['macd_slow'], adjust=False).mean()
    df['macd_line'] = ema_macd_fast - ema_macd_slow
    df['macd_signal'] = df['macd_line'].ewm(span=cfg['macd_signal'], adjust=False).mean()
    df['macd_histogram'] = df['macd_line'] - df['macd_signal']

    df['vol_ma'] = df['volume'].rolling(cfg['vol_ma_period']).mean()

    return df


# --- СИГНАЛЫ ---

def get_signal(df):
    if len(df) < 50:
        return None

    c = df.iloc[-1]

    ema_fast = c['ema_fast']
    ema_mid = c['ema_mid']
    ema_slow = c['ema_slow']
    price = c['close']
    rsi = c['rsi']
    macd_line = c['macd_line']
    macd_signal = c['macd_signal']
    volume = c['volume']
    vol_ma = c['vol_ma']

    if pd.isna(ema_fast) or pd.isna(ema_mid) or pd.isna(ema_slow):
        return None
    if pd.isna(rsi) or pd.isna(macd_line) or pd.isna(macd_signal) or pd.isna(vol_ma):
        return None

    if (ema_fast > ema_mid > ema_slow and
            price > ema_fast and
            45 < rsi < 70 and
            macd_line > macd_signal and
            volume > vol_ma * 0.8):
        return 'LONG'

    if (ema_fast < ema_mid < ema_slow and
            price < ema_fast and
            30 < rsi < 55 and
            macd_line < macd_signal and
            volume > vol_ma * 0.8):
        return 'SHORT'

    return None


# --- ТОРГОВЫЙ БОТ ---

class SignalBot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.exchange = ccxt.bybit({'enableRateLimit': True})
        self.journal = TradeJournal()
        self.active_trades = []
        self.last_signal = {}

    async def scan(self, app_bot):
        for trade in self.active_trades[:]:
            try:
                ticker = await asyncio.to_thread(self.exchange.fetch_ticker, trade['symbol'])
                curr_p = ticker['last']

                if trade['side'] == 'LONG':
                    profit_now = (curr_p - trade['entry']) / trade['entry']
                else:
                    profit_now = (trade['entry'] - curr_p) / trade['entry']

                if trade['side'] == 'LONG':
                    if curr_p > trade.get('highest_price', trade['entry']):
                        trade['highest_price'] = curr_p
                else:
                    if curr_p < trade.get('lowest_price', trade['entry']):
                        trade['lowest_price'] = curr_p

                if not trade.get('breakeven_hit') and profit_now >= self.cfg['breakeven_trigger']:
                    trade['breakeven_hit'] = True
                    trade['trailing_active'] = True
                    trade['sl'] = trade['entry']
                    await app_bot.send_message(
                        chat_id=self.cfg['chat_id'],
                        text=(
                            f"🔄 <b>Безубыток: {trade['symbol']}</b>\n"
                            f"Стоп → {trade['sl']}\n"
                            f"Трейлинг включён ({self.cfg['trailing_distance'] * 100}%)"
                        ),
                        parse_mode='HTML'
                    )

                if trade.get('trailing_active'):
                    if trade['side'] == 'LONG':
                        new_sl = round(trade['highest_price'] * (1 - self.cfg['trailing_distance']), 8)
                        if new_sl > trade['sl']:
                            trade['sl'] = new_sl
                    else:
                        new_sl = round(trade['lowest_price'] * (1 + self.cfg['trailing_distance']), 8)
                        if new_sl < trade['sl']:
                            trade['sl'] = new_sl

                is_sl = (trade['side'] == 'LONG' and curr_p <= trade['sl']) or (
                    trade['side'] == 'SHORT' and curr_p >= trade['sl'])
                is_tp = (trade['side'] == 'LONG' and curr_p >= trade['tp']) or (
                    trade['side'] == 'SHORT' and curr_p <= trade['tp'])

                if is_sl or is_tp:
                    if is_tp:
                        res = 'PROFIT'
                    elif trade.get('trailing_active'):
                        res = 'TRAILING'
                    else:
                        res = 'STOP'

                    data = self.journal.log_trade(
                        trade['symbol'], trade['side'], res,
                        trade['entry'], curr_p, trade['start_time']
                    )
                    if data:
                        icon = "✅" if data['profit_usdt'] > 0 else "❌"
                        await app_bot.send_message(
                            chat_id=self.cfg['chat_id'],
                            text=(
                                f"{icon} <b>Закрыто</b>: {trade['symbol']}\n"
                                f"Тип: {trade['side']} | Результат: {res}\n"
                                f"Итог: <b>{data['profit_usdt']}$</b> ({data['profit_pct']}%)\n"
                                f"⏱ Длительность: {data['duration_min']} мин."
                            ),
                            parse_mode='HTML'
                        )
                    self.active_trades.remove(trade)

                else:
                    try:
                        raw = await asyncio.to_thread(
                            self.exchange.fetch_ohlcv, trade['symbol'], self.cfg['timeframe'], limit=100
                        )
                        df_check = add_indicators(
                            pd.DataFrame(raw, columns=['ts', 'open', 'high', 'low', 'close', 'volume']).iloc[:-1],
                            self.cfg
                        )
                        reversal = get_signal(df_check)
                        if reversal and reversal != trade['side']:
                            data = self.journal.log_trade(
                                trade['symbol'], trade['side'], 'REVERSAL',
                                trade['entry'], curr_p, trade['start_time']
                            )
                            if data:
                                icon = "✅" if data['profit_usdt'] > 0 else "❌"
                                await app_bot.send_message(
                                    chat_id=self.cfg['chat_id'],
                                    text=(
                                        f"🔄 <b>Разворот</b>: {trade['symbol']}\n"
                                        f"Закрыт {trade['side']} | {icon} {data['profit_usdt']}$\n"
                                        f"Открывается {reversal}..."
                                    ),
                                    parse_mode='HTML'
                                )
                            self.active_trades.remove(trade)
                            await self._open_trade(app_bot, trade['symbol'], reversal, curr_p)
                    except Exception as e:
                        logger.error(f"Reversal check error [{trade['symbol']}]: {e}")

            except Exception as e:
                logger.error(f"Trade monitor error [{trade['symbol']}]: {e}")

        for symbol in self.cfg['symbols']:
            if any(t['symbol'] == symbol for t in self.active_trades):
                continue
            try:
                raw = await asyncio.to_thread(
                    self.exchange.fetch_ohlcv, symbol, self.cfg['timeframe'], limit=100
                )
                df = add_indicators(
                    pd.DataFrame(raw, columns=['ts', 'open', 'high', 'low', 'close', 'volume']).iloc[:-1],
                    self.cfg
                )

                last_ts = str(df.iloc[-1]['ts'])
                if self.last_signal.get(symbol) == last_ts:
                    continue

                side = get_signal(df)

                if side:
                    self.last_signal[symbol] = last_ts
                    price = df.iloc[-1]['close']
                    await self._open_trade(app_bot, symbol, side, price)

            except Exception as e:
                logger.error(f"Signal scan error [{symbol}]: {e}")

    async def _open_trade(self, app_bot, symbol, side, price):
        prec = 8 if price < 0.01 else (6 if price < 0.1 else (4 if price < 1 else 2))

        sl = round(price * (1 - self.cfg['stop_loss_pct']) if side == 'LONG' else price * (1 + self.cfg['stop_loss_pct']), prec)
        tp = round(price * (1 + self.cfg['take_profit_pct']) if side == 'LONG' else price * (1 - self.cfg['take_profit_pct']), prec)

        current_balance = get_current_balance()
        risk_amount = current_balance * self.cfg['risk_per_trade']
        total_size = round(risk_amount / self.cfg['stop_loss_pct'], 2)

        trade_id = f"cl_{symbol.replace('/', '_')}_{datetime.now().microsecond}"
        self.active_trades.append({
            'symbol': symbol, 'side': side, 'entry': price,
            'sl': sl, 'tp': tp, 'size_usdt': total_size,
            'trade_id': trade_id, 'start_time': datetime.now(),
            'highest_price': price if side == 'LONG' else None,
            'lowest_price': price if side == 'SHORT' else None,
            'breakeven_hit': False, 'trailing_active': False,
        })

        msg = (
            f"💎 <b>НОВАЯ СДЕЛКА: {symbol}</b>\n"
            f"Тип: {side}\n"
            f"📍 Вход: {price}\n"
            f"🛑 SL: {sl} (-{self.cfg['stop_loss_pct'] * 100}%)\n"
            f"🎯 TP: {tp} (+{self.cfg['take_profit_pct'] * 100}%)\n"
            f"🔄 Безубыток при: +{self.cfg['breakeven_trigger'] * 100}%\n"
            f"💰 Объем: {total_size} USDT (x{self.cfg['leverage']})\n"
            f"⚠️ Тестовый режим — сделка виртуальная"
        )
        await app_bot.send_message(
            chat_id=self.cfg['chat_id'], text=msg, parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Закрыть вручную", callback_data=trade_id)]]
            )
        )


# --- КОМАНДЫ ---

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    balance = get_current_balance()
    active = len(bot_instance.active_trades) if bot_instance else 0
    await update.message.reply_html(
        f"✅ <b>Бот в сети!</b>\n"
        f"💰 Баланс: {balance} USDT\n"
        f"📊 Активных сделок: {active}\n"
        f"⚠️ Режим: ТЕСТОВЫЙ (виртуальные сделки)\n"
        f"📋 Команды: /help"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📖 <b>СПРАВОЧНИК КОМАНД БОТА:</b>\n\n"
        "📊 <b>Аналитика:</b>\n"
        "• /stats — Торговый отчет: баланс, WinRate, лучший/худший актив.\n"
        "• /history — Последние 10 закрытых сделок.\n\n"
        "⏳ <b>Текущее:</b>\n"
        "• /active — Открытые сделки в реальном времени с PnL.\n\n"
        "⚙️ <b>Управление:</b>\n"
        "• /set_sl [ПАРА] [ЦЕНА] — Изменить SL. Пример: <code>/set_sl BTC/USDT 64000</code>\n"
        "• /set_tp [ПАРА] [ЦЕНА] — Изменить TP. Пример: <code>/set_tp ETH/USDT 3800</code>\n"
        "• /start — Проверить статус бота.\n\n"
        "ℹ️ <i>Ежедневный отчёт приходит автоматически в 23:50.</i>"
    )
    await update.message.reply_html(msg)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists('history.csv') or pd.read_csv('history.csv').empty:
        return await update.message.reply_text(
            "📊 Нет закрытых сделок. Статистика появится после первой фиксации."
        )

    df = pd.read_csv('history.csv')
    total_pnl = df['profit_usdt'].sum()
    total_trades = len(df)
    wins = len(df[df['profit_usdt'] > 0])
    losses = total_trades - wins
    win_rate = (wins / total_trades * 100)
    avg_duration = df['duration_min'].mean()
    balance = get_current_balance()

    coin_stats = df.groupby('symbol')['profit_usdt'].sum()
    best_coin = coin_stats.idxmax()
    worst_coin = coin_stats.idxmin()

    df['cumulative'] = df['profit_usdt'].cumsum() + CONFIG['balance']
    df['peak'] = df['cumulative'].cummax()
    df['drawdown'] = ((df['cumulative'] - df['peak']) / df['peak']) * 100
    max_drawdown = round(df['drawdown'].min(), 2)

    msg = (
        f"📊 <b>ПОЛНАЯ СТАТИСТИКА</b>\n━━━━━━━━━━━━\n"
        f"💰 Баланс: <b>{balance} USDT</b>\n"
        f"📈 Общий PnL: <b>{round(total_pnl, 2)} USDT</b>\n"
        f"🎯 Win Rate: <b>{round(win_rate, 1)}%</b> ({wins}W / {losses}L)\n"
        f"⏱ Ср. время сделки: <b>{int(avg_duration)} мин.</b>\n"
        f"📉 Макс. просадка: <b>{max_drawdown}%</b>\n"
        f"🏆 Лучшая монета: <code>{best_coin}</code> ({round(coin_stats[best_coin], 2)}$)\n"
        f"🆘 Худшая монета: <code>{worst_coin}</code> ({round(coin_stats[worst_coin], 2)}$)"
    )
    await update.message.reply_html(msg)


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists('history.csv') or pd.read_csv('history.csv').empty:
        return await update.message.reply_text("📜 История пока пуста.")

    df = pd.read_csv('history.csv').tail(10)
    msg = "<b>📜 ПОСЛЕДНИЕ 10 СДЕЛОК:</b>\n\n"
    for _, r in df.iterrows():
        icon = "✅" if r['profit_usdt'] > 0 else "❌"
        msg += (
            f"{icon} {r['date']} | {r['symbol']} | {r['side']} | "
            f"{round(r['profit_usdt'], 2)}$ ({round(r['profit_pct'], 2)}%) | "
            f"{int(r['duration_min'])} мин.\n"
        )
    await update.message.reply_html(msg)


async def active_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bot_instance or not bot_instance.active_trades:
        return await update.message.reply_text("📭 Нет активных сделок.")

    msg = "<b>⏳ ТЕКУЩИЕ ПОЗИЦИИ:</b>\n\n"
    for t in bot_instance.active_trades:
        try:
            ticker = await asyncio.to_thread(bot_instance.exchange.fetch_ticker, t['symbol'])
            curr_p = ticker['last']
            diff = ((curr_p - t['entry']) / t['entry']) if t['side'] == 'LONG' else (
                (t['entry'] - curr_p) / t['entry'])
            pnl_usdt = round(t['size_usdt'] * diff, 2)
            pnl_pct = round(diff * 100, 2)
            dur = int((datetime.now() - t['start_time']).total_seconds() / 60)
            icon = "🟢" if pnl_usdt >= 0 else "🔴"

            be_status = "✅" if t.get('breakeven_hit') else "❌"
            trail_status = "✅" if t.get('trailing_active') else "❌"

            msg += (
                f"{icon} <b>{t['symbol']}</b> ({t['side']})\n"
                f"   Вход: {t['entry']} | Текущая: {curr_p}\n"
                f"   PNL: {pnl_usdt}$ ({pnl_pct}%)\n"
                f"   SL: {t['sl']} | TP: {t['tp']}\n"
                f"   Безубыток: {be_status} | Трейлинг: {trail_status}\n"
                f"   ⏱ {dur} мин.\n\n"
            )
        except Exception as e:
            logger.error(f"Active cmd error [{t['symbol']}]: {e}")
            msg += f"⚠️ <b>{t['symbol']}</b> — ошибка получения данных\n\n"

    await update.message.reply_html(msg)


async def set_sl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text("Формат: /set_sl BTC/USDT 64000")
    try:
        symbol = context.args[0].upper()
        new_sl = float(context.args[1])
    except ValueError:
        return await update.message.reply_text("❌ Неверный формат цены.")

    trade = next((t for t in bot_instance.active_trades if t['symbol'] == symbol), None)
    if not trade:
        return await update.message.reply_text(f"❌ Нет открытой позиции по {symbol}")

    old_sl = trade['sl']
    trade['sl'] = new_sl
    await update.message.reply_html(
        f"✅ SL для <b>{symbol}</b> изменён: {old_sl} → <b>{new_sl}</b>"
    )


async def set_tp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text("Формат: /set_tp ETH/USDT 3800")
    try:
        symbol = context.args[0].upper()
        new_tp = float(context.args[1])
    except ValueError:
        return await update.message.reply_text("❌ Неверный формат цены.")

    trade = next((t for t in bot_instance.active_trades if t['symbol'] == symbol), None)
    if not trade:
        return await update.message.reply_text(f"❌ Нет открытой позиции по {symbol}")

    old_tp = trade['tp']
    trade['tp'] = new_tp
    await update.message.reply_html(
        f"✅ TP для <b>{symbol}</b> изменён: {old_tp} → <b>{new_tp}</b>"
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    trade = next((t for t in bot_instance.active_trades if t.get('trade_id') == query.data), None)
    if not trade:
        return await query.edit_message_text("⚠️ Сделка уже закрыта или не найдена.")

    try:
        ticker = await asyncio.to_thread(bot_instance.exchange.fetch_ticker, trade['symbol'])
        curr_p = ticker['last']
        data = bot_instance.journal.log_trade(
            trade['symbol'], trade['side'], 'MANUAL',
            trade['entry'], curr_p, trade['start_time']
        )
        bot_instance.active_trades.remove(trade)

        if data:
            icon = "✅" if data['profit_usdt'] > 0 else "❌"
            await query.edit_message_text(
                f"🔵 <b>Закрыто вручную</b>: {trade['symbol']}\n"
                f"{icon} Итог: {data['profit_usdt']}$ ({data['profit_pct']}%)",
                parse_mode='HTML'
            )
        else:
            await query.edit_message_text(f"🔵 Закрыто вручную: {trade['symbol']}")
    except Exception as e:
        logger.error(f"Manual close error: {e}")
        await query.edit_message_text("⚠️ Ошибка при закрытии сделки.")


# --- DAILY REPORT ---

async def send_daily_report(context: ContextTypes.DEFAULT_TYPE):
    try:
        if not os.path.exists('history.csv') or pd.read_csv('history.csv').empty:
            return await context.bot.send_message(
                chat_id=CONFIG['chat_id'],
                text="📋 За сегодня сделок не было."
            )

        df = pd.read_csv('history.csv')
        today = datetime.now().strftime('%d.%m')
        today_df = df[df['date'].str.startswith(today)]

        if today_df.empty:
            return await context.bot.send_message(
                chat_id=CONFIG['chat_id'],
                text="📋 За сегодня сделок не было."
            )

        pnl = today_df['profit_usdt'].sum()
        wins = len(today_df[today_df['profit_usdt'] > 0])
        total = len(today_df)
        losses = total - wins
        balance = get_current_balance()

        msg = (
            f"📋 <b>ИТОГИ ДНЯ ({today})</b>\n━━━━━━━━━━━━\n"
            f"📊 Сделок: {total}\n"
            f"✅ Прибыльных: {wins} | ❌ Убыточных: {losses}\n"
            f"💰 PnL за день: <b>{round(pnl, 2)} USDT</b>\n"
            f"💼 Баланс: <b>{balance} USDT</b>"
        )
        await context.bot.send_message(
            chat_id=CONFIG['chat_id'], text=msg, parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"Daily report error: {e}")


# --- HEALTH CHECK ---

async def health_handler(reader, writer):
    try:
        await reader.read(1024)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


# --- MAIN ---

async def main():
    global bot_instance
    bot_instance = SignalBot(CONFIG)

    app = Application.builder().token(CONFIG['telegram_token']).build()

    app.add_handlers([
        CommandHandler("start", start_cmd),
        CommandHandler("help", help_cmd),
        CommandHandler("stats", stats_cmd),
        CommandHandler("active", active_cmd),
        CommandHandler("history", history_cmd),
        CommandHandler("set_sl", set_sl_cmd),
        CommandHandler("set_tp", set_tp_cmd),
        CallbackQueryHandler(button_handler),
    ])

    await asyncio.start_server(
        health_handler, '0.0.0.0', int(os.environ.get("PORT", 10000))
    )
    logger.info("Health check server started")

    async with app:
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        app.job_queue.run_daily(send_daily_report, time=dt_time(hour=23, minute=50))

        logger.info("Bot fully started")

        while True:
            try:
                await bot_instance.scan(app.bot)
            except Exception as e:
                logger.error(f"Main loop error: {e}")
            await asyncio.sleep(30)


if __name__ == '__main__':
    asyncio.run(main())
