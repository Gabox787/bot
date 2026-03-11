import ccxt
import pandas as pd
import asyncio
import logging
import os
from datetime import datetime
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
    'risk_per_trade': 0.02,
    'stop_loss_pct': 0.01,
    'take_profit_pct': 0.03,
    'breakeven_trigger': 0.01,    # Активация трейлинга при +1.0%
    'trailing_distance': 0.007    # Дистанция 0.7%
}

bot_instance = None

# --- ЛОГИКА ТОРГОВЛИ ---

class TradeJournal:
    def __init__(self, filename='history.csv'):
        self.filename = filename
        if not os.path.exists(self.filename):
            df = pd.DataFrame(columns=['date', 'symbol', 'side', 'result', 'profit_usdt', 'profit_pct'])
            df.to_csv(self.filename, index=False)

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

def add_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    df = df.copy()
    df['ema_fast'] = df['close'].ewm(span=cfg['ema_fast'], adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=cfg['ema_slow'], adjust=False).mean()
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/cfg['rsi_period'], min_periods=cfg['rsi_period']).mean()
    avg_loss = loss.ewm(alpha=1/cfg['rsi_period'], min_periods=cfg['rsi_period']).mean()
    rs = avg_gain / avg_loss
    df['rsi'] = 100 - (100 / (1 + rs))
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
        # 1. МОНИТОРИНГ СДЕЛОК
        for trade in self.active_trades[:]:
            try:
                ticker = await asyncio.to_thread(self.exchange.fetch_ticker, trade['symbol'])
                curr_p = ticker['last']
                profit_now = ((curr_p - trade['entry']) / trade['entry']) if trade['side'] == 'LONG' else ((trade['entry'] - curr_p) / trade['entry'])
                
                # Обновление Трейлинг-стопа
                if profit_now >= self.cfg['breakeven_trigger']:
                    if trade['side'] == 'LONG':
                        new_sl = round(curr_p * (1 - self.cfg['trailing_distance']), 6)
                        if new_sl > trade['sl']: trade['sl'] = new_sl
                    else:
                        new_sl = round(curr_p * (1 + self.cfg['trailing_distance']), 6)
                        if new_sl < trade['sl']: trade['sl'] = new_sl

                # Проверка выхода по индикаторам (разворот тренда)
                raw = await asyncio.to_thread(self.exchange.fetch_ohlcv, trade['symbol'], self.cfg['timeframe'], limit=50)
                df = add_indicators(pd.DataFrame(raw, columns=['ts','open','high','low','close','volume']), self.cfg)
                c = df.iloc[-1]
                
                trend_exit = False
                if trade['side'] == 'LONG' and (c['ema_fast'] < c['ema_slow'] or c['rsi'] > 75): trend_exit = True
                if trade['side'] == 'SHORT' and (c['ema_fast'] > c['ema_slow'] or c['rsi'] < 25): trend_exit = True

                is_tp = (trade['side'] == 'LONG' and curr_p >= trade['tp']) or (trade['side'] == 'SHORT' and curr_p <= trade['tp'])
                is_sl = (trade['side'] == 'LONG' and curr_p <= trade['sl']) or (trade['side'] == 'SHORT' and curr_p >= trade['sl'])

                if is_tp or is_sl or trend_exit:
                    res = 'PROFIT' if is_tp else ('BE/TRAIL' if profit_now > 0 else ('SIGNAL_EXIT' if trend_exit else 'LOSS'))
                    data = self.journal.log_trade(trade['symbol'], trade['side'], res, trade['entry'], curr_p)
                    icon = "✅" if data['profit_usdt'] > 0 else "❌"
                    await app_bot.send_message(chat_id=self.cfg['chat_id'], text=f"{icon} <b>Сделка закрыта: {trade['symbol']}</b>\nПрофит: {data['profit_usdt']}$", parse_mode='HTML')
                    self.active_trades.remove(trade)
            except Exception as e: logger.error(f"Track error: {e}")

        # 2. ПОИСК НОВЫХ СИГНАЛОВ
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
                    sl = round(price * (0.99 if side == 'LONG' else 1.01), 6)
                    tp = round(price * (1.03 if side == 'LONG' else 0.97), 6)
                    trade_id = f"cl_{symbol.replace('/', '_')}_{datetime.now().microsecond}"
                    
                    self.active_trades.append({'symbol': symbol, 'side': side, 'entry': price, 'sl': sl, 'tp': tp, 'trade_id': trade_id})
                    await app_bot.send_message(chat_id=self.cfg['chat_id'], text=f"🚀 <b>Вход {side}: {symbol}</b>\nЦена: {price}", parse_mode='HTML')
            except Exception as e: logger.error(f"Scan error: {e}")

# --- (Команды /start, /active, /pnl и main остаются такими же) ---

async def handle_render_ping(reader, writer):
    writer.write(b"HTTP/1.1 200 OK\r\n\r\nOK"); await writer.drain(); writer.close()

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот запущен в свободном режиме!")

async def active_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bot_instance.active_trades: return await update.message.reply_text("Нет сделок.")
    msg = "<b>Активные сделки:</b>\n" + "\n".join([f"🔸 {t['symbol']} ({t['side']})" for t in bot_instance.active_trades])
    await update.message.reply_html(msg)

async def pnl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists('history.csv'): return await update.message.reply_text("История пуста.")
    df = pd.read_csv('history.csv')
    await update.message.reply_html(f"<b>Общий PnL: {round(df['profit_usdt'].sum(), 2)}$</b>")

async def main():
    global bot_instance
    bot_instance = SignalBot(CONFIG)
    app = Application.builder().token(CONFIG['telegram_token']).build()
    app.add_handlers([CommandHandler("start", start_cmd), CommandHandler("active", active_cmd), CommandHandler("pnl", pnl_cmd)])
    await asyncio.start_server(handle_render_ping, '0.0.0.0', int(os.environ.get("PORT", 10000)))
    async with app:
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.initialize(); await app.start(); await app.updater.start_polling(drop_pending_updates=True)
        while True:
            await bot_instance.scan(app.bot)
            await asyncio.sleep(60)

if __name__ == '__main__':
    asyncio.run(main())
