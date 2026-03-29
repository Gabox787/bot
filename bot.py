import ccxt
import pandas as pd
import asyncio
import logging
import os
import uuid
import signal
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.request import HTTPXRequest

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
    'timeframe': '15m',
    'ema_fast': 9,
    'ema_mid': 21,
    'ema_slow': 50,
    'rsi_period': 14,
    'macd_fast': 12,
    'macd_slow': 26,
    'macd_signal': 9,
    'vol_ma_period': 20,
    'balance': 2000,
    'leverage': 20,
    'risk_per_trade': 0.02,
    'stop_loss_pct': 0.015,
    'take_profit_pct': 0.045,
    'breakeven_trigger': 0.02,
    'trailing_distance': 0.01,
    'commission_rate': 0.00055 * 2,
    'max_open_trades': 10,
}


def get_current_balance():
    if not os.path.exists('history.csv'):
        return CONFIG['balance']
    try:
        df = pd.read_csv('history.csv')
        if df.empty:
            return CONFIG['balance']
        return round(CONFIG['balance'] + df['profit_usdt'].sum(), 2)
    except Exception:
        return CONFIG['balance']


class TradeJournal:
    def __init__(self, filename='history.csv'):
        self.filename = filename
        if not os.path.exists(self.filename):
            pd.DataFrame(columns=[
                'date', 'timestamp', 'symbol', 'side', 'result',
                'profit_usdt', 'profit_pct', 'duration_min'
            ]).to_csv(self.filename, index=False)

    def log_trade(self, symbol, side, result, entry, exit_p, start_time, size_usdt):
        try:
            df = pd.read_csv(self.filename)
            price_diff_pct = ((exit_p - entry) / entry) if side == 'LONG' else ((entry - exit_p) / entry)
            commission_usdt = size_usdt * CONFIG['commission_rate']
            profit_usdt = (size_usdt * price_diff_pct) - commission_usdt
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


def get_signal(df):
    if len(df) < 50:
        return None
    c = df.iloc[-1]
    if (c['ema_fast'] > c['ema_mid'] > c['ema_slow'] and c['close'] > c['ema_fast'] and
            45 < c['rsi'] < 70 and c['macd_line'] > c['macd_signal'] and
            c['volume'] > c['vol_ma'] * 0.8):
        return 'LONG'
    if (c['ema_fast'] < c['ema_mid'] < c['ema_slow'] and c['close'] < c['ema_fast'] and
            30 < c['rsi'] < 55 and c['macd_line'] < c['macd_signal'] and
            c['volume'] > c['vol_ma'] * 0.8):
        return 'SHORT'
    return None


class SignalBot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.exchange = ccxt.bybit({'enableRateLimit': True})
        self.journal = TradeJournal()
        self.active_trades = []
        self.last_signal = {}
        self.trade_lock = asyncio.Lock()

    async def scan(self, app_bot):
        await self._monitor_active_trades(app_bot)

        async with self.trade_lock:
            if len(self.active_trades) >= self.cfg['max_open_trades']:
                return
            active_symbols = {t['symbol'] for t in self.active_trades}

        for symbol in self.cfg['symbols']:
            if symbol in active_symbols:
                continue
            async with self.trade_lock:
                if len(self.active_trades) >= self.cfg['max_open_trades']:
                    break
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
                    await self._open_trade(app_bot, symbol, side, df.iloc[-1]['close'])
            except Exception as e:
                logger.error(f"Scan error {symbol}: {e}")

        valid = set(self.cfg['symbols'])
        self.last_signal = {k: v for k, v in self.last_signal.items() if k in valid}

    async def _monitor_active_trades(self, app_bot):
        async with self.trade_lock:
            snapshot = self.active_trades[:]
        if not snapshot:
            return

        try:
            symbols = list({t['symbol'] for t in snapshot})
            tickers = await asyncio.to_thread(self.exchange.fetch_tickers, symbols)
        except Exception as e:
            logger.error(f"Fetch tickers error: {e}")
            return

        for trade in snapshot:
            try:
                ticker = tickers.get(trade['symbol'])
                if not ticker or not ticker.get('last'):
                    continue
                curr_p = ticker['last']
                side_mult = 1 if trade['side'] == 'LONG' else -1
                profit_now = (curr_p - trade['entry']) / trade['entry'] * side_mult

                if trade['side'] == 'LONG':
                    trade['highest_price'] = max(trade.get('highest_price', curr_p), curr_p)
                else:
                    trade['lowest_price'] = min(trade.get('lowest_price', curr_p), curr_p)

                if not trade.get('breakeven_hit') and profit_now >= self.cfg['breakeven_trigger']:
                    trade['breakeven_hit'] = True
                    trade['trailing_active'] = True
                    trade['sl'] = trade['entry']
                    await app_bot.send_message(
                        chat_id=self.cfg['chat_id'],
                        text=f"🔄 <b>Безубыток: {trade['symbol']}</b>\nСтоп → {trade['sl']}",
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

                is_sl = ((trade['side'] == 'LONG' and curr_p <= trade['sl']) or
                         (trade['side'] == 'SHORT' and curr_p >= trade['sl']))
                is_tp = ((trade['side'] == 'LONG' and curr_p >= trade['tp']) or
                         (trade['side'] == 'SHORT' and curr_p <= trade['tp']))

                if is_sl or is_tp:
                    res_type = ("TAKE PROFIT 🎯" if is_tp else
                                ("TRAILING STOP 📈" if trade.get('trailing_active') else "STOP LOSS 🛑"))

                    async with self.trade_lock:
                        if trade not in self.active_trades:
                            continue
                        self.active_trades.remove(trade)

                    data = self.journal.log_trade(
                        trade['symbol'], trade['side'], res_type,
                        trade['entry'], curr_p, trade['start_time'], trade['size_usdt']
                    )
                    if data:
                        icon = "✅" if data['profit_usdt'] > 0 else "❌"
                        msg = (
                            f"{icon} <b>СДЕЛКА ЗАКРЫТА: {trade['symbol']}</b>\n"
                            f"━━━━━━━━━━━━━━━\n"
                            f"📝 Причина: <b>{res_type}</b>\n"
                            f"💰 PnL: <b>{data['profit_usdt']}$</b> ({data['profit_pct']}%)\n"
                            f"📍 Вход: {trade['entry']}\n"
                            f"🏁 Выход: {curr_p}\n"
                            f"⏱ Длительность: {data['duration_min']} мин."
                        )
                        await app_bot.send_message(
                            chat_id=self.cfg['chat_id'], text=msg, parse_mode='HTML'
                        )
            except Exception as e:
                logger.error(f"Trade monitor error {trade.get('symbol')}: {e}")

    async def _open_trade(self, app_bot, symbol, side, price):
        prec = 8 if price < 0.01 else (4 if price < 1 else 2)
        sl = round(
            price * (1 - self.cfg['stop_loss_pct']) if side == 'LONG'
            else price * (1 + self.cfg['stop_loss_pct']), prec
        )
        tp = round(
            price * (1 + self.cfg['take_profit_pct']) if side == 'LONG'
            else price * (1 - self.cfg['take_profit_pct']), prec
        )
        
        total_size = 1000.0
        trade_id = str(uuid.uuid4())

        trade = {
            'symbol': symbol, 'side': side, 'entry': price,
            'sl': sl, 'tp': tp, 'size_usdt': total_size,
            'trade_id': trade_id, 'start_time': datetime.now(),
            'breakeven_hit': False, 'trailing_active': False
        }

        async with self.trade_lock:
            if len(self.active_trades) >= self.cfg['max_open_trades']:
                return
            if any(t['symbol'] == symbol for t in self.active_trades):
                return
            self.active_trades.append(trade)

        msg = (
            f"💎 <b>НОВАЯ СДЕЛКА: {symbol}</b>\n"
            f"Тип: {side}\n"
            f"📍 Вход: {price}\n"
            f"🛑 SL: {sl} (-{self.cfg['stop_loss_pct'] * 100}%)\n"
            f"🎯 TP: {tp} (+{self.cfg['take_profit_pct'] * 100}%)\n"
            f"💰 Объем: {total_size} USDT (x{self.cfg['leverage']})"
        )
        await app_bot.send_message(
            chat_id=self.cfg['chat_id'], text=msg, parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Закрыть вручную", callback_data=trade_id)]
            ])
        )


def get_bot(context: ContextTypes.DEFAULT_TYPE) -> SignalBot:
    return context.application.bot_data['signal_bot']


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = get_bot(context)
    balance = get_current_balance()
    active = len(bot.active_trades)
    limit = bot.cfg['max_open_trades']
    await update.message.reply_html(
        f"✅ <b>Бот в сети!</b>\n"
        f"💰 Баланс: {balance} USDT\n"
        f"📊 Активных: {active}/{limit}"
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists('history.csv'):
        return await update.message.reply_text("📊 Нет данных.")
    df = pd.read_csv('history.csv')
    if df.empty:
        return await update.message.reply_text("История пуста.")
    cur_bal = get_current_balance()
    total_pnl = round(df['profit_usdt'].sum(), 2)
    wins = len(df[df['profit_usdt'] > 0])
    losses = len(df[df['profit_usdt'] <= 0])
    wr = round((wins / len(df) * 100), 1)
    avg_time = int(df['duration_min'].mean())
    initial_bal = CONFIG['balance']
    df['equity'] = initial_bal + df['profit_usdt'].cumsum()
    peak = df['equity'].cummax()
    drawdown = round(((df['equity'] - peak) / peak * 100).min(), 2)
    coin_stats = df.groupby('symbol')['profit_usdt'].sum()
    best_c, worst_c = coin_stats.idxmax(), coin_stats.idxmin()
    best_p, worst_p = round(coin_stats.max(), 2), round(coin_stats.min(), 2)
    msg = (
        f"📊 <b>ПОЛНАЯ СТАТИСТИКА</b>\n━━━━━━━━━━━━\n"
        f"💰 Баланс: <b>{cur_bal} USDT</b>\n"
        f"📈 Общий PnL: <b>{total_pnl} USDT</b>\n"
        f"🎯 Win Rate: <b>{wr}%</b> ({wins}W / {losses}L)\n"
        f"⏱ Ср. время сделки: <b>{avg_time} мин.</b>\n"
        f"📉 Макс. просадка: <b>{drawdown}%</b>\n"
        f"🏆 Лучшая монета: <b>{best_c} ({best_p}$)</b>\n"
        f"🆘 Худшая монета: <b>{worst_c} ({worst_p}$)</b>"
    )
    await update.message.reply_html(msg)


async def active_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = get_bot(context)
    if not bot.active_trades:
        return await update.message.reply_text("📭 Нет активных сделок.")

    try:
        symbols = list({t['symbol'] for t in bot.active_trades})
        tickers = await asyncio.to_thread(bot.exchange.fetch_tickers, symbols)
    except Exception as e:
        logger.error(f"Active cmd fetch error: {e}")
        return await update.message.reply_text("⚠️ Ошибка получения данных.")

    msg = "<b>⏳ ТЕКУЩИЕ ПОЗИЦИИ:</b>\n━━━━━━━━━━━━━━━\n"
    total_current_pnl = 0
    for t in bot.active_trades:
        try:
            ticker = tickers.get(t['symbol'])
            if not ticker or not ticker.get('last'):
                continue
            curr_p = ticker['last']
            side_mult = 1 if t['side'] == 'LONG' else -1
            roi_pct = round((curr_p - t['entry']) / t['entry'] * 100 * side_mult, 2)
            pnl_usdt = round(t['size_usdt'] * (roi_pct / 100), 2)
            total_current_pnl += pnl_usdt
            side_icon = "🟢 LONG" if t['side'] == 'LONG' else "🔴 SHORT"
            pnl_icon = "📈" if pnl_usdt >= 0 else "📉"
            msg += (
                f"<b>{t['symbol']} | {side_icon}</b>\n"
                f"📍 Вход: {t['entry']}\n"
                f"🎯 TP: {t['tp']} | 🛑 SL: {t['sl']}\n"
                f"{pnl_icon} PnL: <b>{pnl_usdt}$</b> | ROI: <b>{roi_pct}%</b>\n\n"
            )
        except Exception as e:
            logger.error(f"Active error: {e}")
    total_icon = "💰" if total_current_pnl >= 0 else "💸"
    msg += f"━━━━━━━━━━━━━━━\n{total_icon} <b>ОБЩИЙ PNL: {round(total_current_pnl, 2)}$</b>"
    await update.message.reply_html(msg)


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists('history.csv'):
        return await update.message.reply_text("История пуста.")
    df = pd.read_csv('history.csv')
    if df.empty:
        return await update.message.reply_text("История пуста.")
    df = df.tail(10)
    msg = "<b>📜 ПОСЛЕДНИЕ 10 СДЕЛОК:</b>\n\n"
    for _, r in df.iterrows():
        icon = "✅" if r['profit_usdt'] > 0 else "❌"
        msg += f"{icon} {r['date']} | {r['symbol']} | {r['profit_usdt']}$\n"
    await update.message.reply_html(msg)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "<b>📋 Команды:</b>\n"
        "/start — статус бота\n"
        "/stats — полная статистика\n"
        "/active — текущие позиции\n"
        "/history — последние 10 сделок\n"
        "/help — эта справка"
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bot = get_bot(context)

    async with bot.trade_lock:
        trade = next((t for t in bot.active_trades if t.get('trade_id') == query.data), None)
        if not trade:
            return await query.edit_message_text("⚠️ Сделка уже закрыта.")
        bot.active_trades.remove(trade)

    try:
        ticker = await asyncio.to_thread(bot.exchange.fetch_ticker, trade['symbol'])
        curr_p = ticker['last']
    except Exception as e:
        logger.error(f"Button fetch error: {e}")
        async with bot.trade_lock:
            bot.active_trades.append(trade)
        return await query.edit_message_text("⚠️ Ошибка получения цены. Попробуйте снова.")

    data = bot.journal.log_trade(
        trade['symbol'], trade['side'], 'MANUAL EXIT 🔵',
        trade['entry'], curr_p, trade['start_time'], trade['size_usdt']
    )
    if data:
        msg = (
            f"🔵 <b>ЗАКРЫТО ВРУЧНУЮ: {trade['symbol']}</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💰 PnL: <b>{data['profit_usdt']}$</b> ({data['profit_pct']}%)\n"
            f"📍 Вход: {trade['entry']} | 🏁 Выход: {curr_p}"
        )
        await query.edit_message_text(msg, parse_mode='HTML')


async def health_handler(reader, writer):
    try:
        await asyncio.wait_for(reader.read(4096), timeout=2.0)
    except Exception:
        pass
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def scan_loop(signal_bot, app_bot, shutdown_event):
    logger.info("Scan loop started.")
    while not shutdown_event.is_set():
        try:
            await signal_bot.scan(app_bot)
        except Exception as e:
            logger.error(f"Scan loop error: {e}")
        
        # Спим короткими интервалами, чтобы быстрее реагировать на shutdown
        for _ in range(30): 
            if shutdown_event.is_set():
                break
            await asyncio.sleep(1)
    logger.info("Scan loop stopped.")


async def main():
    signal_bot = SignalBot(CONFIG)
    request_config = HTTPXRequest(connect_timeout=20.0, read_timeout=20.0)
    app = Application.builder().token(CONFIG['telegram_token']).request(request_config).build()
    app.bot_data['signal_bot'] = signal_bot

    app.add_handlers([
        CommandHandler("start", start_cmd),
        CommandHandler("active", active_cmd),
        CommandHandler("history", history_cmd),
        CommandHandler("help", help_cmd),
        CommandHandler("stats", stats_cmd),
        CallbackQueryHandler(button_handler),
    ])

    port = int(os.environ.get("PORT", 10000))
    server = await asyncio.start_server(health_handler, '0.0.0.0', port)
    logger.info(f"Health check on port {port}")

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    
    # Обработка сигналов завершения
    def stop():
        shutdown_event.set()

    if os.name != 'nt': # Для Linux (Render)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop)

    async with app:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        
        # Запускаем цикл сканирования как фоновую задачу
        scanner_task = asyncio.create_task(scan_loop(signal_bot, app.bot, shutdown_event))
        
        logger.info("Bot is running.")
        await shutdown_event.wait()
        
        logger.info("Shutting down...")
        scanner_task.cancel()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        server.close()
        await server.wait_closed()
        logger.info("Bot stopped.")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
