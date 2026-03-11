import ccxt
import pandas as pd
import asyncio
import logging
import os
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

# Настройка логов
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.StreamHandler()]
)
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
    'check_interval': 60
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

    def get_history(self):
        try:
            if not os.path.exists(self.filename): return "История пуста."
            df = pd.read_csv(self.filename).tail(10)
            if df.empty: return "История сделок пока пуста."
            msg = "<b>📜 Последние сделки:</b>\n\n"
            for _, r in df.iterrows():
                icon = "✅" if r['result'] in ['PROFIT', 'MANUAL'] else "❌"
                msg += f"{icon} {r['symbol']} {r['side']}: <b>{r['profit_usdt']}$</b> ({r['profit_pct']}%)\n"
            return msg
        except: return "Ошибка истории."

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
        # Авто-проверка SL/TP
        for trade in self.active_trades[:]:
            try:
                ticker = await asyncio.to_thread(self.exchange.fetch_ticker, trade['symbol'])
                curr_p = ticker['last']
                is_tp = (trade['side'] == 'LONG' and curr_p >= trade['tp']) or (trade['side'] == 'SHORT' and curr_p <= trade['tp'])
                is_sl = (trade['side'] == 'LONG' and curr_p <= trade['sl']) or (trade['side'] == 'SHORT' and curr_p >= trade['sl'])
                
                if is_tp or is_sl:
                    res = 'PROFIT' if is_tp else 'LOSS'
                    exit_p = trade['tp'] if is_tp else trade['sl']
                    data = self.journal.log_trade(trade['symbol'], trade['side'], res, trade['entry'], exit_p)
                    
                    text = f"{'✅' if is_tp else '❌'} <b>Сделка закрыта (Авто)!</b>\n\n" \
                           f"Монета: <b>{trade['symbol']}</b>\n" \
                           f"Прибыль: <b>{data['profit_usdt']}$</b> ({data['profit_pct']}%)"
                    await app_bot.send_message(chat_id=self.cfg['chat_id'], text=text, parse_mode='HTML')
                    self.active_trades.remove(trade)
            except Exception as e: logger.error(f"Track error: {e}")

        # Поиск новых сигналов
        for symbol in self.cfg['symbols']:
            try:
                raw = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, self.cfg['timeframe'], limit=50)
                df = pd.DataFrame(raw, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
                df = add_indicators(df.iloc[:-1], self.cfg)
                c = df.iloc[-1]
                candle_id = str(c['ts'])
                
                if self.last_candle.get(symbol) == candle_id: continue
                
                side = 'LONG' if (c['cross_up'] and 30 <= c['rsi'] <= 60 and c['volume'] > c['vol_ma']) else \
                       'SHORT' if (c['cross_down'] and 40 <= c['rsi'] <= 70 and c['volume'] > c['vol_ma']) else None
                
                if side:
                    self.last_candle[symbol] = candle_id
                    price = c['close']
                    precision = 4 if price < 1 else 2
                    sl = round(price * (1 - self.cfg['stop_loss_pct'] if side == 'LONG' else 1 + self.cfg['stop_loss_pct']), precision)
                    tp = round(price * (1 + self.cfg['take_profit_pct'] if side == 'LONG' else 1 - self.cfg['take_profit_pct']), precision)
                    
                    risk_usdt = self.cfg['balance'] * self.cfg['risk_per_trade']
                    pos_size_usdt = risk_usdt / self.cfg['stop_loss_pct']
                    margin = pos_size_usdt / self.cfg['leverage']
                    
                    # Создаем уникальный ID сделки для кнопки
                    trade_id = f"close_{symbol.replace('/', '_')}_{datetime.now().microsecond}"
                    
                    self.active_trades.append({
                        'symbol': symbol, 'side': side, 'entry': price, 
                        'sl': sl, 'tp': tp, 'size_usdt': pos_size_usdt, 'trade_id': trade_id
                    })
                    
                    icon = "🟢 LONG (Покупка)" if side == 'LONG' else "🔴 SHORT (Продажа)"
                    
                    # Кнопка для закрытия
                    keyboard = [[InlineKeyboardButton("❌ Закрыть вручную", callback_data=trade_id)]]
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    msg = (
                        f"═══════════════════════════════════\n"
                        f"<b>{icon}</b>\n"
                        f"💰 <b>Монета: {symbol}</b>\n"
                        f"═══════════════════════════════════\n\n"
                        f"📍 Цена входа:  <code>{price}</code>\n"
                        f"🛑 Стоп-лосс:   <code>{sl}</code>\n"
                        f"🎯 Тейк-профит: <code>{tp}</code>\n\n"
                        f"💵 Позиция:     {round(pos_size_usdt, 2)} USDT\n"
                        f"═══════════════════════════════════"
                    )
                    await app_bot.send_message(chat_id=self.cfg['chat_id'], text=msg, parse_mode='HTML', reply_markup=reply_markup)
            except Exception as e: logger.error(f"Scan error {symbol}: {e}")

# --- ОБРАБОТЧИК КНОПОК ---

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    global bot_instance
    trade_id = query.data
    
    # Ищем сделку в списке активных по trade_id
    trade_to_close = None
    for t in bot_instance.active_trades:
        if t.get('trade_id') == trade_id:
            trade_to_close = t
            break
            
    if trade_to_close:
        try:
            ticker = await asyncio.to_thread(bot_instance.exchange.fetch_ticker, trade_to_close['symbol'])
            exit_price = ticker['last']
            
            # Логируем в историю
            data = bot_instance.journal.log_trade(
                trade_to_close['symbol'], trade_to_close['side'], 'MANUAL', trade_to_close['entry'], exit_price
            )
            
            # Убираем из активных
            bot_instance.active_trades.remove(trade_to_close)
            
            # Редактируем сообщение: убираем кнопку и пишем результат
            res_icon = "🔵" if data['profit_usdt'] >= 0 else "🟠"
            new_text = query.message.text + f"\n\n{res_icon} <b>ЗАКРЫТО ВРУЧНУЮ</b>\nЦена: {exit_price}\nПрофит: <b>{data['profit_usdt']}$</b>"
            await query.edit_message_text(text=new_text, parse_mode='HTML')
            
        except Exception as e:
            logger.error(f"Manual close error: {e}")
            await query.message.reply_text("Ошибка при закрытии.")
    else:
        await query.edit_message_text(text="⚠️ Сделка уже закрыта или не найдена.")

# --- КОМАНДЫ ---

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Бот готов!\n\n/active - текущие сделки\n/pnl - общая прибыль")

async def active_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_instance
    if not bot_instance or not bot_instance.active_trades:
        await update.message.reply_text("Нет открытых позиций.")
        return
    msg = "<b>⏳ Текущие открытые сделки:</b>\n\n"
    for t in bot_instance.active_trades:
        try:
            ticker = await asyncio.to_thread(bot_instance.exchange.fetch_ticker, t['symbol'])
            curr_p = ticker['last']
            diff_pct = ((curr_p - t['entry']) / t['entry']) if t['side'] == 'LONG' else ((t['entry'] - curr_p) / t['entry'])
            pnl = round(t['size_usdt'] * diff_pct, 2)
            msg += f"🔸 <b>{t['symbol']}</b>: {pnl}$ ({round(diff_pct*100, 2)}%)\n"
        except: pass
    await update.message.reply_html(msg)

async def pnl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists('history.csv'):
        await update.message.reply_text("История пуста.")
        return
    df = pd.read_csv('history.csv')
    total = round(df['profit_usdt'].sum(), 2)
    await update.message.reply_html(f"<b>📊 Общий PnL: {total}$</b>\nСделок: {len(df)}")

# --- ЗАПУСК ---

async def handle_render_ping(reader, writer):
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
    await writer.drain()
    writer.close()

async def main():
    global bot_instance
    bot_instance = SignalBot(CONFIG)
    app = Application.builder().token(CONFIG['telegram_token']).build()
    
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("active", active_cmd))
    app.add_handler(CommandHandler("pnl", pnl_cmd))
    app.add_handler(CallbackQueryHandler(button_handler)) # Слушаем нажатия кнопок

    port = int(os.environ.get("PORT", 10000))
    await asyncio.start_server(handle_render_ping, '0.0.0.0', port)

    async with app:
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        while True:
            await bot_instance.scan(app.bot)
            await asyncio.sleep(60)

if __name__ == '__main__':
    asyncio.run(main())
