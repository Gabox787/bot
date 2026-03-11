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

# --- ЛОГИКА ---

class TradeJournal:
    def __init__(self, filename='history.csv'):
        self.filename = filename
        if not os.path.exists(self.filename):
            pd.DataFrame(columns=['date', 'symbol', 'side', 'result', 'profit_usdt', 'profit_pct']).to_csv(self.filename, index=False)

    def log_trade(self, symbol, side, result, entry, exit_p):
        try:
            df = pd.read_csv(self.filename)
            price_diff_pct = ((exit_p - entry) / entry) if side == 'LONG' else ((entry - exit_p) / entry)
            risk_amount = CONFIG['balance'] * CONFIG['risk_per_trade']
            position_size_usdt = risk_amount / CONFIG['stop_loss_pct']
            profit_usdt = position_size_usdt * price_diff_pct
            new_row = {
                'date': datetime.now().strftime('%d.%m %H:%M'),
                'symbol': symbol, 'side': side, 'result': result,
                'profit_usdt': round(profit_usdt, 2), 'profit_pct': round(price_diff_pct * 100, 2)
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
        # 1. МОНИТОРИНГ
        for trade in self.active_trades[:]:
            try:
                ticker = await asyncio.to_thread(self.exchange.fetch_ticker, trade['symbol'])
                curr_p = ticker['last']
                profit_now = ((curr_p - trade['entry']) / trade['entry']) if trade['side'] == 'LONG' else ((trade['entry'] - curr_p) / trade['entry'])
                
                # TRAILING LOGIC
                if profit_now >= self.cfg['breakeven_trigger']:
                    new_sl = round(curr_p * (1 - self.cfg['trailing_distance']), 8) if trade['side'] == 'LONG' else round(curr_p * (1 + self.cfg['trailing_distance']), 8)
                    if (trade['side'] == 'LONG' and new_sl > trade['sl']) or (trade['side'] == 'SHORT' and new_sl < trade['sl']):
                        trade['sl'] = new_sl

                # EXIT CONDITIONS
                is_tp = (trade['side'] == 'LONG' and curr_p >= trade['tp']) or (trade['side'] == 'SHORT' and curr_p <= trade['tp'])
                is_sl = (trade['side'] == 'LONG' and curr_p <= trade['sl']) or (trade['side'] == 'SHORT' and curr_p >= trade['sl'])

                if is_tp or is_sl:
                    res = 'PROFIT' if is_tp else 'TRAILING/STOP'
                    data = self.journal.log_trade(trade['symbol'], trade['side'], res, trade['entry'], curr_p)
                    icon = "✅" if data['profit_usdt'] > 0 else "❌"
                    await app_bot.send_message(chat_id=self.cfg['chat_id'], text=f"{icon} <b>Сделка закрыта</b>: {trade['symbol']}\nИтог: <b>{data['profit_usdt']}$</b> ({data['profit_pct']}%)", parse_mode='HTML')
                    self.active_trades.remove(trade)
            except: pass

        # 2. ПОИСК
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
                    # Динамическая точность
                    prec = 8 if price < 0.01 else (4 if price < 1 else 2)
                    sl = round(price * (0.99 if side == 'LONG' else 1.01), prec)
                    tp = round(price * (1.03 if side == 'LONG' else 0.97), prec)
                    
                    risk_amount = self.cfg['balance'] * self.cfg['risk_per_trade']
                    total_size = round(risk_amount / self.cfg['stop_loss_pct'], 2)
                    margin = round(total_size / self.cfg['leverage'], 2)
                    
                    trade_id = f"cl_{symbol.replace('/', '_')}_{datetime.now().microsecond}"
                    self.active_trades.append({
                        'symbol': symbol, 'side': side, 'entry': price, 
                        'sl': sl, 'tp': tp, 'size_usdt': total_size, 
                        'trade_id': trade_id, 'start_time': datetime.now()
                    })
                    
                    keyboard = [[InlineKeyboardButton("❌ Закрыть вручную", callback_data=trade_id)]]
                    msg = (
                        f"💎 <b>НОВАЯ СДЕЛКА: {symbol}</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"Тип: {'🟢 LONG' if side == 'LONG' else '🔴 SHORT'}\n"
                        f"Плечо: <b>x{self.cfg['leverage']}</b>\n"
                        f"Причина: EMA Cross + RSI ({round(c['rsi'], 1)})\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📍 Вход: <code>{price}</code>\n"
                        f"🛑 Стоп: <code>{sl}</code>\n"
                        f"🎯 Тейк: <code>{tp}</code>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"💰 Объем: <b>{total_size} USDT</b>\n"
                        f"💵 Маржа: <b>{margin} USDT</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━"
                    )
                    await app_bot.send_message(chat_id=self.cfg['chat_id'], text=msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
            except: pass

# --- КОМАНДЫ ---

async def active_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bot_instance.active_trades: return await update.message.reply_text("Нет сделок.")
    msg = "<b>⏳ Текущие сделки:</b>\n\n"
    for t in bot_instance.active_trades:
        ticker = await asyncio.to_thread(bot_instance.exchange.fetch_ticker, t['symbol'])
        curr_p = ticker['last']
        diff = ((curr_p - t['entry']) / t['entry']) if t['side'] == 'LONG' else ((t['entry'] - curr_p) / t['entry'])
        # Расчет времени
        duration = datetime.now() - t['start_time']
        hours, remainder = divmod(duration.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        time_str = f"{hours}ч {minutes}м" if hours > 0 else f"{minutes}м"
        
        msg += (f"🔸 <b>{t['symbol']}</b> ({t['side']})\n"
                f"   PNL: {round(t['size_usdt'] * diff, 2)}$ ({round(diff*100, 2)}%)\n"
                f"   В сделке: {time_str}\n"
                f"   SL: {t['sl']} | TP: {t['tp']}\n\n")
    await update.message.reply_html(msg)

async def set_sl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        symbol, new_price = context.args[0].upper(), float(context.args[1])
        trade = next((t for t in bot_instance.active_trades if t['symbol'] == symbol), None)
        if trade:
            trade['sl'] = new_price
            await update.message.reply_html(f"🛡️ Для <b>{symbol}</b> новый Стоп-лосс: <code>{new_price}</code>")
        else: await update.message.reply_text("Монета не найдена.")
    except: await update.message.reply_text("Формат: /set_sl BTC/USDT 65000")

async def set_tp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        symbol, new_price = context.args[0].upper(), float(context.args[1])
        trade = next((t for t in bot_instance.active_trades if t['symbol'] == symbol), None)
        if trade:
            trade['tp'] = new_price
            await update.message.reply_html(f"🎯 Для <b>{symbol}</b> новый Тейк-профит: <code>{new_price}</code>")
        else: await update.message.reply_text("Монета не найдена.")
    except: await update.message.reply_text("Формат: /set_tp BTC/USDT 72000")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    trade = next((t for t in bot_instance.active_trades if t.get('trade_id') == query.data), None)
    if trade:
        ticker = await asyncio.to_thread(bot_instance.exchange.fetch_ticker, trade['symbol'])
        data = bot_instance.journal.log_trade(trade['symbol'], trade['side'], 'MANUAL', trade['entry'], ticker['last'])
        bot_instance.active_trades.remove(trade)
        await query.edit_message_text(f"🔵 Закрыто вручную: <b>{data['profit_usdt']}$</b>", parse_mode='HTML')

async def main():
    global bot_instance
    bot_instance = SignalBot(CONFIG)
    app = Application.builder().token(CONFIG['telegram_token']).build()
    app.add_handlers([
        CommandHandler("start", lambda u, c: u.message.reply_text("Бот в сети!")),
        CommandHandler("active", active_cmd),
        CommandHandler("set_sl", set_sl_cmd),
        CommandHandler("set_tp", set_tp_cmd),
        CommandHandler("pnl", lambda u, c: u.message.reply_text(f"PnL: {round(pd.read_csv('history.csv')['profit_usdt'].sum(), 2)}$")),
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
