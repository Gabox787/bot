import ccxt
import pandas as pd
import asyncio
import logging
import os
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

# Настройка логов
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG ---
CONFIG = {
    'telegram_token': '8746717150:AAEz2ugYWK_7gig48Y_-QZHb9VQ74x7gqTw',
    'chat_id': '715162339',
    'symbols': [
        'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT',
        'ADA/USDT', 'AVAX/USDT', 'DOT/USDT', 'NEAR/USDT',
        'SUI/USDT', 'RENDER/USDT', 'FET/USDT', 'PEPE/USDT', 'POL/USDT'
    ],
    'timeframe': '15m',
    'ema_fast': 9,
    'ema_slow': 21,
    'rsi_period': 14,
    'vol_ma_period': 20,
    'balance': 1000,
    'leverage': 3,
    'risk_per_trade': 0.02,
    'stop_loss_pct': 0.01,
    'take_profit_pct': 0.03,
    'breakeven_trigger': 0.01,
    'trailing_distance': 0.007,
}

bot_instance = None

# --- АНАЛИТИКА И ЖУРНАЛ ---

class TradeJournal:
    def __init__(self, filename='history.csv'):
        self.filename = filename
        if not os.path.exists(self.filename):
            pd.DataFrame(columns=['date', 'timestamp', 'symbol', 'side', 'result', 'profit_usdt', 'profit_pct', 'duration_min']).to_csv(self.filename, index=False)

    def log_trade(self, symbol, side, result, entry, exit_p, start_time):
        try:
            df = pd.read_csv(self.filename)
            price_diff_pct = ((exit_p - entry) / entry) if side == 'LONG' else ((entry - exit_p) / entry)
            risk_amount = CONFIG['balance'] * CONFIG['risk_per_trade']
            position_size_usdt = (risk_amount / CONFIG['stop_loss_pct'])
            profit_usdt = position_size_usdt * price_diff_pct
            
            now = datetime.now()
            duration = int((now - start_time).total_seconds() / 60)
            
            new_row = {
                'date': now.strftime('%d.%m %H:%M'),
                'timestamp': now.timestamp(),
                'symbol': symbol, 'side': side, 'result': result,
                'profit_usdt': round(profit_usdt, 2), 
                'profit_pct': round(price_diff_pct * 100, 2),
                'duration_min': duration
            }
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            df.to_csv(self.filename, index=False)
            return new_row
        except Exception as e:
            logger.error(f"Journal error: {e}"); return None

def add_indicators(df, cfg):
    df = df.copy()
    df['ema_fast'] = df['close'].ewm(span=cfg['ema_fast'], adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=cfg['ema_slow'], adjust=False).mean()
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/cfg['rsi_period'], min_periods=cfg['rsi_period']).mean()
    avg_loss = loss.ewm(alpha=1/cfg['rsi_period'], min_periods=cfg['rsi_period']).mean()
    df['rsi'] = 100 - (100 / (1 + (avg_gain / avg_loss)))
    df['vol_ma'] = df['volume'].rolling(cfg['vol_ma_period']).mean()
    df['cross_up'] = (df['ema_fast'] > df['ema_slow']) & (df['ema_fast'].shift(1) <= df['ema_slow'].shift(1))
    df['cross_down'] = (df['ema_fast'] < df['ema_slow']) & (df['ema_fast'].shift(1) >= df['ema_slow'].shift(1))
    return df

class SignalBot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.exchange = ccxt.bybit({'enableRateLimit': True})
        self.journal = TradeJournal()
        self.active_trades = []
        self.last_candle = {}

    async def scan(self, app_bot):
        for trade in self.active_trades[:]:
            try:
                ticker = await asyncio.to_thread(self.exchange.fetch_ticker, trade['symbol'])
                curr_p = ticker['last']
                profit_now = ((curr_p - trade['entry']) / trade['entry']) if trade['side'] == 'LONG' else ((trade['entry'] - curr_p) / trade['entry'])
                
                if profit_now >= self.cfg['breakeven_trigger']:
                    new_sl = round(curr_p * (1 - self.cfg['trailing_distance']), 8) if trade['side'] == 'LONG' else round(curr_p * (1 + self.cfg['trailing_distance']), 8)
                    if (trade['side'] == 'LONG' and new_sl > trade['sl']) or (trade['side'] == 'SHORT' and new_sl < trade['sl']):
                        trade['sl'] = new_sl

                is_tp = (trade['side'] == 'LONG' and curr_p >= trade['tp']) or (trade['side'] == 'SHORT' and curr_p <= trade['tp'])
                is_sl = (trade['side'] == 'LONG' and curr_p <= trade['sl']) or (trade['side'] == 'SHORT' and curr_p >= trade['sl'])

                if is_tp or is_sl:
                    res = 'PROFIT' if is_tp else 'STOP'
                    data = self.journal.log_trade(trade['symbol'], trade['side'], res, trade['entry'], curr_p, trade['start_time'])
                    icon = "✅" if data['profit_usdt'] > 0 else "❌"
                    await app_bot.send_message(chat_id=self.cfg['chat_id'], text=f"{icon} <b>Закрыто</b>: {trade['symbol']}\nИтог: <b>{data['profit_usdt']}$</b>", parse_mode='HTML')
                    self.active_trades.remove(trade)
            except Exception as e: logger.error(f"Scan error: {e}")

        for symbol in self.cfg['symbols']:
            if any(t['symbol'] == symbol for t in self.active_trades): continue
            try:
                raw = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, self.cfg['timeframe'], limit=50)
                df = add_indicators(pd.DataFrame(raw, columns=['ts','open','high','low','close','volume']).iloc[:-1], self.cfg)
                c = df.iloc[-1]
                if self.last_candle.get(symbol) == str(c['ts']): continue
                
                side = 'LONG' if (c['cross_up'] and 30 <= c['rsi'] <= 60 and c['volume'] > c['vol_ma']) else \
                       'SHORT' if (c['cross_down'] and 40 <= c['rsi'] <= 70 and c['volume'] > c['vol_ma']) else None
                
                if side:
                    self.last_candle[symbol] = str(c['ts'])
                    price = c['close']
                    prec = 8 if price < 0.01 else (4 if price < 1 else 2)
                    sl = round(price * (0.99 if side == 'LONG' else 1.01), prec)
                    tp = round(price * (1.03 if side == 'LONG' else 0.97), prec)
                    risk_amount = self.cfg['balance'] * self.cfg['risk_per_trade']
                    total_size = round(risk_amount / self.cfg['stop_loss_pct'], 2)
                    
                    trade_id = f"cl_{symbol.replace('/', '_')}_{datetime.now().microsecond}"
                    self.active_trades.append({'symbol': symbol, 'side': side, 'entry': price, 'sl': sl, 'tp': tp, 'size_usdt': total_size, 'trade_id': trade_id, 'start_time': datetime.now()})
                    
                    msg = f"💎 <b>НОВАЯ СДЕЛКА: {symbol}</b>\nТип: {side}\n📍 Вход: {price}\n💰 Объем: {total_size} USDT"
                    await app_bot.send_message(chat_id=self.cfg['chat_id'], text=msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Закрыть", callback_data=trade_id)]]))
            except: pass

# --- КОМАНДЫ ---

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📖 <b>СПРАВОЧНИК КОМАНД БОТА:</b>\n\n"
        "📊 <b>Аналитика:</b>\n"
        "• /stats — Твой торговый отчет. Показывает баланс, WinRate, лучший/худший актив и профит за разные периоды.\n"
        "• /history — Список последних 10 закрытых сделок.\n\n"
        "⏳ <b>Текущее:</b>\n"
        "• /active — Список всех открытых сделок в реальном времени. Показывает PnL в % и $, сколько времени ты в сделке.\n\n"
        "⚙️ <b>Управление:</b>\n"
        "• /set_sl [ПАРА] [ЦЕНА] — Вручную изменить Стоп-Лосс. Пример: <code>/set_sl BTC/USDT 64000</code>\n"
        "• /set_tp [ПАРА] [ЦЕНА] — Вручную изменить Тейк-Профит. Пример: <code>/set_tp ETH/USDT 3800</code>\n"
        "• /start — Проверить, запущен ли бот.\n\n"
        "ℹ️ <i>Статистика и история станут доступны после того, как бот закроет хотя бы одну сделку.</i>"
    )
    await update.message.reply_html(msg)

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists('history.csv') or pd.read_csv('history.csv').empty:
        return await update.message.reply_text("📊 У вас еще нет закрытых сделок. Статистика появится после первой фиксации результата.")
    
    df = pd.read_csv('history.csv')
    total_pnl = df['profit_usdt'].sum()
    win_rate = (len(df[df['profit_usdt'] > 0]) / len(df) * 100)
    avg_duration = df['duration_min'].mean()
    coin_stats = df.groupby('symbol')['profit_usdt'].sum()
    best_coin = coin_stats.idxmax(); worst_coin = coin_stats.idxmin()
    
    msg = (
        f"📊 <b>ПОЛНАЯ СТАТИСТИКА</b>\n━━━━━━━━━━━━\n"
        f"💰 Баланс: <b>{round(CONFIG['balance'] + total_pnl, 2)} USDT</b>\n"
        f"📈 Общий PnL: <b>{round(total_pnl, 2)} USDT</b>\n"
        f"🎯 Win Rate: <b>{round(win_rate, 1)}%</b>\n"
        f"⏱ Ср. время сделки: <b>{int(avg_duration)} мин.</b>\n"
        f"🏆 Топ (+): <code>{best_coin}</code>\n"
        f"🆘 Топ (-): <code>{worst_coin}</code>"
    )
    await update.message.reply_html(msg)

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists('history.csv') or pd.read_csv('history.csv').empty:
        return await update.message.reply_text("📜 История пока пуста.")
    
    df = pd.read_csv('history.csv').tail(10)
    msg = "<b>📜 ПОСЛЕДНИЕ 10 СДЕЛОК:</b>\n\n"
    for _, r in df.iterrows():
        icon = "✅" if r['profit_usdt'] > 0 else "❌"
        msg += f"{icon} {r['date']} | {r['symbol']} | {round(r['profit_usdt'], 2)}$\n"
    await update.message.reply_html(msg)

async def active_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bot_instance.active_trades: return await update.message.reply_text("Нет активных сделок.")
    msg = "<b>⏳ ТЕКУЩИЕ ПОЗИЦИИ:</b>\n\n"
    for t in bot_instance.active_trades:
        ticker = await asyncio.to_thread(bot_instance.exchange.fetch_ticker, t['symbol'])
        diff = ((ticker['last'] - t['entry']) / t['entry']) if t['side'] == 'LONG' else ((t['entry'] - ticker['last']) / t['entry'])
        dur = int((datetime.now() - t['start_time']).total_seconds() / 60)
        msg += f"🔸 <b>{t['symbol']}</b>\n   PNL: {round(t['size_usdt'] * diff, 2)}$ ({round(diff*100, 2)}%)\n   Время: {dur} мин.\n\n"
    await update.message.reply_html(msg)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    trade = next((t for t in bot_instance.active_trades if t.get('trade_id') == query.data), None)
    if trade:
        ticker = await asyncio.to_thread(bot_instance.exchange.fetch_ticker, trade['symbol'])
        bot_instance.journal.log_trade(trade['symbol'], trade['side'], 'MANUAL', trade['entry'], ticker['last'], trade['start_time'])
        bot_instance.active_trades.remove(trade)
        await query.edit_message_text(f"🔵 Закрыто вручную.")

async def main():
    global bot_instance
    bot_instance = SignalBot(CONFIG)
    app = Application.builder().token(CONFIG['telegram_token']).build()
    app.add_handlers([
        CommandHandler("start", lambda u, c: u.message.reply_text("Бот в сети!")),
        CommandHandler("help", help_cmd),
        CommandHandler("stats", stats_cmd),
        CommandHandler("active", active_cmd),
        CommandHandler("history", history_cmd),
        CallbackQueryHandler(button_handler)
    ])
    await asyncio.start_server(lambda r, w: (w.write(b"HTTP/1.1 200 OK\r\n\r\nOK"), w.drain(), w.close()), '0.0.0.0', int(os.environ.get("PORT", 10000)))
    async with app:
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.initialize(); await app.start(); await app.updater.start_polling(drop_pending_updates=True)
        while True:
            await bot_instance.scan(app.bot)
            await asyncio.sleep(60)

if __name__ == '__main__':
    asyncio.run(main())
