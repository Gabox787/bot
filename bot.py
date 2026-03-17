import ccxt
import pandas as pd
import asyncio
import logging
import os
import numpy as np
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
    'adx_period': 14,
    'vol_ma_period': 20,
    'balance': 1000,
    'leverage': 20,
    'risk_per_trade': 0.02,
    'stop_loss_pct': 0.012,        # Увеличено до 1.2% для защиты от шума
    'take_profit_pct': 0.035,       # Цель 3.5%
    'breakeven_trigger': 0.01,     # Перенос в БУ при +1%
    'trailing_distance': 0.008,    # Трейлинг 0.8%
    'commission_rate': 0.0011,     # Средняя комиссия Bybit (вход + выход)
}

bot_instance = None

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_current_balance():
    if not os.path.exists('history.csv'):
        return CONFIG['balance']
    try:
        df = pd.read_csv('history.csv')
        return round(CONFIG['balance'] + df['profit_usdt'].sum(), 2)
    except:
        return CONFIG['balance']

class TradeJournal:
    def __init__(self, filename='history.csv'):
        self.filename = filename
        if not os.path.exists(self.filename):
            pd.DataFrame(columns=['date', 'timestamp', 'symbol', 'side', 'result', 'profit_usdt', 'profit_pct', 'duration_min']).to_csv(self.filename, index=False)

    def log_trade(self, symbol, side, result, entry, exit_p, start_time):
        try:
            df = pd.read_csv(self.filename)
            price_diff_pct = ((exit_p - entry) / entry) if side == 'LONG' else ((entry - exit_p) / entry)
            current_balance = get_current_balance()
            risk_amount = current_balance * CONFIG['risk_per_trade']
            position_size_usdt = risk_amount / CONFIG['stop_loss_pct']
            profit_usdt = (position_size_usdt * price_diff_pct) - (position_size_usdt * CONFIG['commission_rate'])
            now = datetime.now()
            new_row = {
                'date': now.strftime('%d.%m %H:%M'),
                'timestamp': now.timestamp(),
                'symbol': symbol, 'side': side, 'result': result,
                'profit_usdt': round(profit_usdt, 2),
                'profit_pct': round((price_diff_pct - CONFIG['commission_rate']) * 100, 2),
                'duration_min': int((now - start_time).total_seconds() / 60)
            }
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            df.to_csv(self.filename, index=False)
            return new_row
        except Exception as e:
            logger.error(f"Journal error: {e}")
            return None

# --- ТЕХНИЧЕСКИЙ АНАЛИЗ ---
def add_indicators(df, cfg):
    df = df.copy()
    # EMA
    df['ema_fast'] = df['close'].ewm(span=cfg['ema_fast'], adjust=False).mean()
    df['ema_mid'] = df['close'].ewm(span=cfg['ema_mid'], adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=cfg['ema_slow'], adjust=False).mean()
    
    # RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=cfg['rsi_period']).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=cfg['rsi_period']).mean()
    df['rsi'] = 100 - (100 / (1 + (gain / loss)))

    # MACD
    exp1 = df['close'].ewm(span=cfg['macd_fast'], adjust=False).mean()
    exp2 = df['close'].ewm(span=cfg['macd_slow'], adjust=False).mean()
    df['macd_line'] = exp1 - exp2
    df['macd_signal'] = df['macd_line'].ewm(span=cfg['macd_signal'], adjust=False).mean()

    # ADX (Сила тренда)
    plus_dm = df['high'].diff()
    minus_dm = df['low'].diff()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm > 0] = 0
    tr = pd.concat([df['high'] - df['low'], abs(df['high'] - df['close'].shift()), abs(df['low'] - df['close'].shift())], axis=1).max(axis=1)
    atr = tr.rolling(cfg['adx_period']).mean()
    df['plus_di'] = 100 * (plus_dm.rolling(cfg['adx_period']).mean() / atr)
    df['minus_di'] = 100 * (abs(minus_dm).rolling(cfg['adx_period']).mean() / atr)
    dx = (abs(df['plus_di'] - df['minus_di']) / (df['plus_di'] + df['minus_di'])) * 100
    df['adx'] = dx.rolling(cfg['adx_period']).mean()

    df['vol_ma'] = df['volume'].rolling(cfg['vol_ma_period']).mean()
    return df

def get_signal(df):
    if len(df) < 50: return None
    c = df.iloc[-1]
    
    # Фильтр: ADX > 20 означает наличие тренда (защита от боковика)
    if c['adx'] < 20: return None

    # Условия LONG
    if (c['ema_fast'] > c['ema_mid'] > c['ema_slow'] and 
        c['close'] > c['ema_fast'] and 
        45 < c['rsi'] < 65 and 
        c['macd_line'] > c['macd_signal'] and 
        c['volume'] > c['vol_ma'] * 0.9):
        return 'LONG'

    # Условия SHORT
    if (c['ema_fast'] < c['ema_mid'] < c['ema_slow'] and 
        c['close'] < c['ema_fast'] and 
        35 < c['rsi'] < 55 and 
        c['macd_line'] < c['macd_signal'] and 
        c['volume'] > c['vol_ma'] * 0.9):
        return 'SHORT'
    
    return None

# --- КЛАСС БОТА ---
class SignalBot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.exchange = ccxt.bybit({'enableRateLimit': True})
        self.journal = TradeJournal()
        self.active_trades = []
        self.last_signal_time = {}

    async def scan(self, app_bot):
        # 1. Мониторинг открытых сделок
        for trade in self.active_trades[:]:
            try:
                ticker = await asyncio.to_thread(self.exchange.fetch_ticker, trade['symbol'])
                curr_p = ticker['last']
                
                # Обновление экстремумов для трейлинга
                if trade['side'] == 'LONG':
                    trade['highest_price'] = max(trade.get('highest_price', curr_p), curr_p)
                    profit_pct = (curr_p - trade['entry']) / trade['entry']
                else:
                    trade['lowest_price'] = min(trade.get('lowest_price', curr_p), curr_p)
                    profit_pct = (trade['entry'] - curr_p) / trade['entry']

                # Безубыток
                if not trade['breakeven_hit'] and profit_pct >= self.cfg['breakeven_trigger']:
                    trade['breakeven_hit'] = True
                    trade['sl'] = trade['entry']
                    await app_bot.send_message(self.cfg['chat_id'], f"🛡 <b>{trade['symbol']}</b>: Стоп перенесен в БУ", parse_mode='HTML')

                # Трейлинг-стоп (активируется после БУ)
                if trade['breakeven_hit']:
                    if trade['side'] == 'LONG':
                        new_sl = round(trade['highest_price'] * (1 - self.cfg['trailing_distance']), 8)
                        if new_sl > trade['sl']: trade['sl'] = new_sl
                    else:
                        new_sl = round(trade['lowest_price'] * (1 + self.cfg['trailing_distance']), 8)
                        if new_sl < trade['sl']: trade['sl'] = new_sl

                # Проверка выхода
                is_sl = (trade['side'] == 'LONG' and curr_p <= trade['sl']) or (trade['side'] == 'SHORT' and curr_p >= trade['sl'])
                is_tp = (trade['side'] == 'LONG' and curr_p >= trade['tp']) or (trade['side'] == 'SHORT' and curr_p <= trade['tp'])

                if is_sl or is_tp:
                    res = 'PROFIT' if is_tp else ('TRAILING' if trade['breakeven_hit'] else 'STOP')
                    data = self.journal.log_trade(trade['symbol'], trade['side'], res, trade['entry'], curr_p, trade['start_time'])
                    self.active_trades.remove(trade)
                    if data:
                        icon = "✅" if data['profit_usdt'] > 0 else "❌"
                        await app_bot.send_message(self.cfg['chat_id'], f"{icon} <b>Закрыто: {trade['symbol']}</b>\nИтог: {data['profit_usdt']}$ ({data['profit_pct']}%)", parse_mode='HTML')

            except Exception as e: logger.error(f"Monitor error {trade['symbol']}: {e}")

        # 2. Поиск новых сигналов
        for symbol in self.cfg['symbols']:
            if any(t['symbol'] == symbol for t in self.active_trades): continue
            try:
                raw = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, self.cfg['timeframe'], limit=100)
                df = add_indicators(pd.DataFrame(raw, columns=['ts', 'open', 'high', 'low', 'close', 'volume']).iloc[:-1], self.cfg)
                
                last_ts = df.iloc[-1]['ts']
                if self.last_signal_time.get(symbol) == last_ts: continue

                side = get_signal(df)
                if side:
                    self.last_signal_time[symbol] = last_ts
                    await self._open_trade(app_bot, symbol, side, df.iloc[-1]['close'])
            except Exception as e: logger.error(f"Scan error {symbol}: {e}")

    async def _open_trade(self, app_bot, symbol, side, price):
        prec = 8 if price < 0.01 else (4 if price < 1 else 2)
        sl = round(price * (1 - self.cfg['stop_loss_pct']) if side == 'LONG' else price * (1 + self.cfg['stop_loss_pct']), prec)
        tp = round(price * (1 + self.cfg['take_profit_pct']) if side == 'LONG' else price * (1 - self.cfg['take_profit_pct']), prec)
        
        balance = get_current_balance()
        size = round((balance * self.cfg['risk_per_trade']) / self.cfg['stop_loss_pct'], 2)

        self.active_trades.append({
            'symbol': symbol, 'side': side, 'entry': price, 'sl': sl, 'tp': tp,
            'start_time': datetime.now(), 'breakeven_hit': False, 'highest_price': price, 'lowest_price': price
        })

        msg = f"🚀 <b>ВХОД: {symbol}</b> ({side})\nЦена: {price}\nSL: {sl} | TP: {tp}\nADX тренд подтвержден ✅"
        await app_bot.send_message(self.cfg['chat_id'], msg, parse_mode='HTML')

# --- ТЕЛЕГРАМ КОМАНДЫ ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(f"💎 <b>Бот запущен!</b>\nБаланс: {get_current_balance()} USDT\nРежим: ТЕСТОВЫЙ\n\nИспользую ADX фильтр для борьбы со спамом.")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists('history.csv'): return await update.message.reply_text("История пуста.")
    df = pd.read_csv('history.csv')
    if df.empty: return await update.message.reply_text("Сделок еще не было.")
    
    total_pnl = df['profit_usdt'].sum()
    winrate = (len(df[df['profit_usdt'] > 0]) / len(df)) * 100
    await update.message.reply_html(f"📊 <b>СТАТИСТИКА:</b>\nОбщий PnL: {round(total_pnl, 2)}$\nWinRate: {round(winrate, 1)}%\nВсего сделок: {len(df)}")

async def active_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bot_instance or not bot_instance.active_trades: return await update.message.reply_text("Нет активных сделок.")
    msg = "<b>⏳ АКТИВНЫЕ:</b>\n\n"
    for t in bot_instance.active_trades:
        msg += f"• {t['symbol']} ({t['side']}) | Вход: {t['entry']} | SL: {t['sl']}\n"
    await update.message.reply_html(msg)

async def health_handler(reader, writer):
    await reader.read(1024)
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
    await writer.drain()
    writer.close()

async def main():
    global bot_instance
    bot_instance = SignalBot(CONFIG)
    app = Application.builder().token(CONFIG['telegram_token']).build()
    
    app.add_handlers([CommandHandler("start", start_cmd), CommandHandler("stats", stats_cmd), CommandHandler("active", active_cmd)])
    
    await asyncio.start_server(health_handler, '0.0.0.0', int(os.environ.get("PORT", 10000)))
    
    async with app:
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        
        while True:
            await bot_instance.scan(app.bot)
            await asyncio.sleep(30)

if __name__ == '__main__':
    asyncio.run(main())
