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
    'timeframe': '15m',
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
    'fixed_volume_usdt': 50.0,  # Твой фиксированный вход
    'stop_loss_pct': 0.007,
    'take_profit_pct': 0.03,
    'breakeven_trigger': 0.0075,
    'trailing_distance': 0.007,
    'commission_rate': 0.00055 * 2,
}

bot_instance = None

# --- ПЕРЕСЧЁТ БАЛАНСА ---
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
            
            # Расчет от ФИКСИРОВАННОГО объема
            position_size_usdt = CONFIG['fixed_volume_usdt'] * CONFIG['leverage']
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
    if len(df) < 50: return None
    c = df.iloc[-1]
    if any(pd.isna(c[col]) for col in ['ema_fast', 'ema_mid', 'ema_slow', 'rsi', 'macd_line', 'vol_ma']): return None
    if (c['ema_fast'] > c['ema_mid'] > c['ema_slow'] and c['close'] > c['ema_fast'] and 
        45 < c['rsi'] < 70 and c['macd_line'] > c['macd_signal'] and c['volume'] > c['vol_ma'] * 0.8):
        return 'LONG'
    if (c['ema_fast'] < c['ema_mid'] < c['ema_slow'] and c['close'] < c['ema_fast'] and 
        30 < c['rsi'] < 55 and c['macd_line'] < c['macd_signal'] and c['volume'] > c['vol_ma'] * 0.8):
        return 'SHORT'
    return None

# --- БОТ ---
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
                    trade['highest_price'] = max(trade.get('highest_price', curr_p), curr_p)
                else:
                    profit_now = (trade['entry'] - curr_p) / trade['entry']
                    trade['lowest_price'] = min(trade.get('lowest_price', curr_p), curr_p)

                # Безубыток
                if not trade.get('breakeven_hit') and profit_now >= self.cfg['breakeven_trigger']:
                    trade['breakeven_hit'] = trade['trailing_active'] = True
                    trade['sl'] = trade['entry']
                    await app_bot.send_message(chat_id=self.cfg['chat_id'], text=f"🔄 <b>Безубыток: {trade['symbol']}</b>\nСтоп на входе. Трейлинг включен.", parse_mode='HTML')

                # Трейлинг
                if trade.get('trailing_active'):
                    if trade['side'] == 'LONG':
                        new_sl = round(trade['highest_price'] * (1 - self.cfg['trailing_distance']), 8)
                        if new_sl > trade['sl']: trade['sl'] = new_sl
                    else:
                        new_sl = round(trade['lowest_price'] * (1 + self.cfg['trailing_distance']), 8)
                        if new_sl < trade['sl']: trade['sl'] = new_sl

                # Выход
                is_sl = (trade['side'] == 'LONG' and curr_p <= trade['sl']) or (trade['side'] == 'SHORT' and curr_p >= trade['sl'])
                is_tp = (trade['side'] == 'LONG' and curr_p >= trade['tp']) or (trade['side'] == 'SHORT' and curr_p <= trade['tp'])

                if is_sl or is_tp:
                    res = 'PROFIT' if is_tp else ('TRAILING' if trade.get('trailing_active') else 'STOP')
                    data = self.journal.log_trade(trade['symbol'], trade['side'], res, trade['entry'], curr_p, trade['start_time'])
                    if data:
                        icon = "✅" if data['profit_usdt'] > 0 else "❌"
                        await app_bot.send_message(chat_id=self.cfg['chat_id'], 
                            text=f"{icon} <b>Закрыто</b>: {trade['symbol']}\nРезультат: {res}\nИтог: <b>{data['profit_usdt']}$</b> ({data['profit_pct']}%)", 
                            parse_mode='HTML')
                    self.active_trades.remove(trade)
                
                # Проверка разворота
                else:
                    raw = await asyncio.to_thread(self.exchange.fetch_ohlcv, trade['symbol'], self.cfg['timeframe'], limit=100)
                    df_check = add_indicators(pd.DataFrame(raw, columns=['ts','open','high','low','close','volume']).iloc[:-1], self.cfg)
                    rev = get_signal(df_check)
                    if rev and rev != trade['side']:
                        self.journal.log_trade(trade['symbol'], trade['side'], 'REVERSAL', trade['entry'], curr_p, trade['start_time'])
                        self.active_trades.remove(trade)
                        await self._open_trade(app_bot, trade['symbol'], rev, curr_p)

            except Exception as e: logger.error(f"Monitor error: {e}")

        for symbol in self.cfg['symbols']:
            if any(t['symbol'] == symbol for t in self.active_trades): continue
            try:
                raw = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, self.cfg['timeframe'], limit=100)
                df = add_indicators(pd.DataFrame(raw, columns=['ts','open','high','low','close','volume']).iloc[:-1], self.cfg)
                last_ts = str(df.iloc[-1]['ts'])
                if self.last_signal.get(symbol) == last_ts: continue
                side = get_signal(df)
                if side:
                    self.last_signal[symbol] = last_ts
                    await self._open_trade(app_bot, symbol, side, df.iloc[-1]['close'])
            except Exception as e: logger.error(f"Scan error: {e}")

    async def _open_trade(self, app_bot, symbol, side, price):
        prec = 8 if price < 0.01 else (6 if price < 0.1 else (4 if price < 1 else 2))
        sl = round(price * (1 - self.cfg['stop_loss_pct']) if side == 'LONG' else price * (1 + self.cfg['stop_loss_pct']), prec)
        tp = round(price * (1 + self.cfg['take_profit_pct']) if side == 'LONG' else price * (1 - self.cfg['take_profit_pct']), prec)
        
        total_size = round(self.cfg['fixed_volume_usdt'] * self.cfg['leverage'], 2)
        
        trade_id = f"cl_{symbol.replace('/', '_')}_{datetime.now().microsecond}"
        self.active_trades.append({
            'symbol': symbol, 'side': side, 'entry': price, 'sl': sl, 'tp': tp, 
            'size_usdt': total_size, 'trade_id': trade_id, 'start_time': datetime.now(),
            'highest_price': price if side == 'LONG' else None,
            'lowest_price': price if side == 'SHORT' else None,
            'breakeven_hit': False, 'trailing_active': False
        })
        
        msg = (f"💎 <b>НОВАЯ СДЕЛКА: {symbol}</b>\nТип: {side}\n📍 Вход: {price}\n"
               f"🛑 SL: {sl} | 🎯 TP: {tp}\n💰 Объем: {total_size} USDT (x{self.cfg['leverage']})\n⚠️ ТЕСТОВЫЙ РЕЖИМ")
        await app_bot.send_message(chat_id=self.cfg['chat_id'], text=msg, parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Закрыть вручную", callback_data=trade_id)]]))

# --- КОМАНДЫ ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(f"✅ <b>Бот активен!</b>\n💰 Баланс: {get_current_balance()} USDT\n⚙️ Фикс. вход: {CONFIG['fixed_volume_usdt']} USDT")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = ("📖 <b>КОМАНДЫ:</b>\n/stats — Полная статистика\n/history — Последние 10 сделок\n/active — Текущие позиции с PnL\n/start — Статус бота")
    await update.message.reply_html(msg)

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists('history.csv') or pd.read_csv('history.csv').empty:
        return await update.message.reply_text("Статистики еще нет.")
    df = pd.read_csv('history.csv')
    total_pnl = df['profit_usdt'].sum()
    win_rate = (len(df[df['profit_usdt'] > 0]) / len(df) * 100)
    msg = (f"📊 <b>СТАТИСТИКА</b>\n━━━━━━━━━━━━\n"
           f"💰 Баланс: <b>{get_current_balance()} USDT</b>\n"
           f"📈 Общий PnL: <b>{round(total_pnl, 2)} USDT</b>\n"
           f"🎯 Win Rate: <b>{round(win_rate, 1)}%</b>\n"
           f"⚙️ Вход: <b>{CONFIG['fixed_volume_usdt']} USDT</b>")
    await update.message.reply_html(msg)

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists('history.csv') or pd.read_csv('history.csv').empty:
        return await update.message.reply_text("История пуста.")
    df = pd.read_csv('history.csv').tail(10)
    msg = "<b>📜 ПОСЛЕДНИЕ 10 СДЕЛОК:</b>\n\n"
    for _, r in df.iterrows():
        icon = "✅" if r['profit_usdt'] > 0 else "❌"
        msg += f"{icon} {r['symbol']} | {r['side']} | {round(r['profit_usdt'], 2)}$ ({round(r['profit_pct'], 2)}%)\n"
    await update.message.reply_html(msg)

async def active_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bot_instance or not bot_instance.active_trades:
        return await update.message.reply_text("Нет открытых сделок.")
    msg = "<b>⏳ ТЕКУЩИЕ ПОЗИЦИИ:</b>\n\n"
    for t in bot_instance.active_trades:
        ticker = await asyncio.to_thread(bot_instance.exchange.fetch_ticker, t['symbol'])
        curr_p = ticker['last']
        diff = ((curr_p - t['entry']) / t['entry']) if t['side'] == 'LONG' else ((t['entry'] - curr_p) / t['entry'])
        pnl = round(t['size_usdt'] * diff, 2)
        msg += f"• <b>{t['symbol']}</b> ({t['side']})\nPnL: {pnl}$ ({round(diff*100, 2)}%)\nSL: {t['sl']} | TP: {t['tp']}\n\n"
    await update.message.reply_html(msg)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    trade = next((t for t in bot_instance.active_trades if t.get('trade_id') == query.data), None)
    if trade:
        ticker = await asyncio.to_thread(bot_instance.exchange.fetch_ticker, trade['symbol'])
        bot_instance.journal.log_trade(trade['symbol'], trade['side'], 'MANUAL', trade['entry'], ticker['last'], trade['start_time'])
        bot_instance.active_trades.remove(trade)
        await query.edit_message_text(f"🔵 {trade['symbol']} закрыт вручную.")

async def health_handler(reader, writer):
    await reader.read(1024)
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK"); await writer.drain(); writer.close()

async def main():
    global bot_instance
    bot_instance = SignalBot(CONFIG)
    app = Application.builder().token(CONFIG['telegram_token']).build()
    app.add_handlers([CommandHandler("start", start_cmd), CommandHandler("help", help_cmd),
                      CommandHandler("stats", stats_cmd), CommandHandler("history", history_cmd),
                      CommandHandler("active", active_cmd), CallbackQueryHandler(button_handler)])
    await asyncio.start_server(health_handler, '0.0.0.0', int(os.environ.get("PORT", 10000)))
    async with app:
        await app.initialize(); await app.start(); await app.updater.start_polling(drop_pending_updates=True)
        while True:
            try: await bot_instance.scan(app.bot)
            except Exception as e: logger.error(f"Loop error: {e}")
            await asyncio.sleep(30)

if __name__ == '__main__':
    asyncio.run(main())
