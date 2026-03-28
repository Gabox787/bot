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

# Настройка логирования
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
    'balance': 2000,                                # Депозит для статистики
    'leverage': 20,
    'risk_per_trade': 0.02,                         # 2% от 1000$ = 20$ риска на стоп
    'stop_loss_pct': 0.015,
    'take_profit_pct': 0.045,
    'breakeven_trigger': 0.02,
    'trailing_distance': 0.01,
    'commission_rate': 0.00055 * 2,
    'max_open_trades': 10,                          # Лимит позиций
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
    # Логика LONG
    if (c['ema_fast'] > c['ema_mid'] > c['ema_slow'] and c['close'] > c['ema_fast'] and
            45 < c['rsi'] < 70 and c['macd_line'] > c['macd_signal'] and
            c['volume'] > c['vol_ma'] * 0.8):
        return 'LONG'
    # Логика SHORT
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
                raw = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, self.cfg['timeframe'], limit=100)
                df = add_indicators(pd.DataFrame(raw, columns=['ts', 'open', 'high', 'low', 'close', 'volume']).iloc[:-1], self.cfg)
                
                last_ts = str(df.iloc[-1]['ts'])
                if self.last_signal.get(symbol) == last_ts:
                    continue
                
                side = get_signal(df)
                if side:
                    self.last_signal[symbol] = last_ts
                    await self._open_trade(app_bot, symbol, side, df.iloc[-1]['close'])
            except Exception as e:
                logger.error(f"Scan error {symbol}: {e}")

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

                # Обновление экстремумов для трейлинга
                if trade['side'] == 'LONG':
                    trade['highest_price'] = max(trade.get('highest_price', curr_p), curr_p)
                else:
                    trade['lowest_price'] = min(trade.get('lowest_price', curr_p), curr_p)

                # Проверка безубытка
                if not trade.get('breakeven_hit') and profit_now >= self.cfg['breakeven_trigger']:
                    trade['breakeven_hit'] = True
                    trade['trailing_active'] = True
                    trade['sl'] = trade['entry']
                    await app_bot.send_message(chat_id=self.cfg['chat_id'], text=f"🔄 <b>Безубыток: {trade['symbol']}</b>", parse_mode='HTML')

                # Логика трейлинг-стопа
                if trade.get('trailing_active'):
                    if trade['side'] == 'LONG':
                        new_sl = round(trade['highest_price'] * (1 - self.cfg['trailing_distance']), 8)
                        if new_sl > trade['sl']: trade['sl'] = new_sl
                    else:
                        new_sl = round(trade['lowest_price'] * (1 + self.cfg['trailing_distance']), 8)
                        if new_sl < trade['sl']: trade['sl'] = new_sl

                # Проверка выхода по SL/TP
                is_sl = (trade['side'] == 'LONG' and curr_p <= trade['sl']) or (trade['side'] == 'SHORT' and curr_p >= trade['sl'])
                is_tp = (trade['side'] == 'LONG' and curr_p >= trade['tp']) or (trade['side'] == 'SHORT' and curr_p <= trade['tp'])

                if is_sl or is_tp:
                    res_type = "TAKE PROFIT 🎯" if is_tp else ("TRAILING STOP 📈" if trade.get('trailing_active') else "STOP LOSS 🛑")
                    
                    async with self.trade_lock:
                        if trade in self.active_trades:
                            self.active_trades.remove(trade)
                        else:
                            continue

                    data = self.journal.log_trade(trade['symbol'], trade['side'], res_type, trade['entry'], curr_p, trade['start_time'], trade['size_usdt'])
                    if data:
                        icon = "✅" if data['profit_usdt'] > 0 else "❌"
                        msg = f"{icon} <b>ЗАКРЫТО: {trade['symbol']}</b>\nPnL: {data['profit_usdt']}$ ({data['profit_pct']}%)"
                        await app_bot.send_message(chat_id=self.cfg['chat_id'], text=msg, parse_mode='HTML')
            except Exception as e:
                logger.error(f"Monitor error {trade['symbol']}: {e}")

    async def _open_trade(self, app_bot, symbol, side, price):
        prec = 8 if price < 0.01 else (4 if price < 1 else 2)
        sl = round(price * (1 - self.cfg['stop_loss_pct']) if side == 'LONG' else price * (1 + self.cfg['stop_loss_pct']), prec)
        tp = round(price * (1 + self.cfg['take_profit_pct']) if side == 'LONG' else price * (1 - self.cfg['take_profit_pct']), prec)
        
        # РАСЧЕТ ОБЪЕМА ОТ 1000 USDT
        fixed_base = 1000
        risk_amount = fixed_base * self.cfg['risk_per_trade']
        total_size = round(risk_amount / self.cfg['stop_loss_pct'], 2)
        trade_id = str(uuid.uuid4())

        trade = {
            'symbol': symbol, 'side': side, 'entry': price, 'sl': sl, 'tp': tp,
            'size_usdt': total_size, 'trade_id': trade_id, 'start_time': datetime.now(),
            'breakeven_hit': False, 'trailing_active': False
        }

        async with self.trade_lock:
            if len(self.active_trades) >= self.cfg['max_open_trades']: return
            if any(t['symbol'] == symbol for t in self.active_trades): return
            self.active_trades.append(trade)

        msg = (f"💎 <b>НОВАЯ СДЕЛКА: {symbol}</b>\nТип: {side}\n📍 Вход: {price}\n"
               f"🛑 SL: {sl} | 🎯 TP: {tp}\n💰 Объем: {total_size} USDT")
        await app_bot.send_message(
            chat_id=self.cfg['chat_id'], text=msg, parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Закрыть вручную", callback_data=trade_id)]])
        )

# --- КОМАНДЫ ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = context.application.bot_data['signal_bot']
    await update.message.reply_html(f"✅ Бот активен. Баланс: {get_current_balance()} USDT. Сделок: {len(bot.active_trades)}")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists('history.csv'): return await update.message.reply_text("Нет данных.")
    df = pd.read_csv('history.csv')
    if df.empty: return await update.message.reply_text("История пуста.")
    total_pnl = round(df['profit_usdt'].sum(), 2)
    wr = round((len(df[df['profit_usdt'] > 0]) / len(df) * 100), 1)
    await update.message.reply_html(f"📊 <b>Статистика</b>\nPnL: {total_pnl} USDT\nWinRate: {wr}%")

async def active_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = context.application.bot_data['signal_bot']
    if not bot.active_trades: return await update.message.reply_text("Нет сделок.")
    msg = "<b>Активные:</b>\n"
    for t in bot.active_trades: msg += f"• {t['symbol']} ({t['side']})\n"
    await update.message.reply_html(msg)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bot = context.application.bot_data['signal_bot']
    async with bot.trade_lock:
        trade = next((t for t in bot.active_trades if t['trade_id'] == query.data), None)
        if not trade: return await query.edit_message_text("Закрыта.")
        bot.active_trades.remove(trade)
    ticker = await asyncio.to_thread(bot.exchange.fetch_ticker, trade['symbol'])
    bot.journal.log_trade(trade['symbol'], trade['side'], 'MANUAL', trade['entry'], ticker['last'], trade['start_time'], trade['size_usdt'])
    await query.edit_message_text(f"🔵 Закрыто вручную: {trade['symbol']}")

async def health_handler(reader, writer):
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
    await writer.drain()
    writer.close()

async def main():
    bot_inst = SignalBot(CONFIG)
    app = Application.builder().token(CONFIG['telegram_token']).request(HTTPXRequest(connect_timeout=20)).build()
    app.bot_data['signal_bot'] = bot_inst
    app.add_handlers([CommandHandler("start", start_cmd), CommandHandler("stats", stats_cmd), 
                      CommandHandler("active", active_cmd), CallbackQueryHandler(button_handler)])
    
    await asyncio.start_server(health_handler, '0.0.0.0', int(os.environ.get("PORT", 10000)))
    
    async with app:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        while True:
            await bot_inst.scan(app.bot)
            await asyncio.sleep(30)

if __name__ == '__main__':
    asyncio.run(main())
