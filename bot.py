import ccxt
import pandas as pd
import asyncio
import logging
import os
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Настройка логов
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ╔══════════════════════════════════════════════╗
# ║                  CONFIG                      ║
# ╚══════════════════════════════════════════════╝

CONFIG = {
    'telegram_token': '8227791601:AAHhwkKjeYXzfA2nXqfdJ52hFUCAYVtjUyM',
    'chat_id': '715162339',
    'symbols': [
        'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT',
        'ADA/USDT', 'AVAX/USDT', 'DOT/USDT', 'POL/USDT', 'NEAR/USDT',
        'SUI/USDT', 'RENDER/USDT', 'FET/USDT', 'PEPE/USDT', 'FTM/USDT'
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
    'check_interval': 60,
}

# ╔══════════════════════════════════════════════╗
# ║           ЖУРНАЛ И ИНДИКАТОРЫ                ║
# ╚══════════════════════════════════════════════╝

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
                'symbol': symbol,
                'side': side,
                'result': result,
                'profit_usdt': round(profit_usdt, 2),
                'profit_pct': round(price_diff_pct * 100, 2)
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
                icon = "✅" if r['result'] == 'PROFIT' else "❌"
                msg += f"{icon} {r['symbol']} {r['side']}: <b>{r['profit_usdt']}$</b> ({r['profit_pct']}%)\n"
            return msg
        except: return "Ошибка чтения истории."

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

# ╔══════════════════════════════════════════════╗
# ║                ЛОГИКА БОТА                   ║
# ╚══════════════════════════════════════════════╝

class SignalBot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.exchange = ccxt.bybit({'enableRateLimit': True})
        self.journal = TradeJournal()
        self.active_trades = []
        self.last_candle = {}

    async def scan(self, app_bot):
        # 1. Проверка активных сделок (трекинг цен)
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
                    
                    text = f"{'✅' if is_tp else '❌'} <b>Сделка закрыта!</b>\n\n" \
                           f"Монета: {trade['symbol']}\nРезультат: {res}\n" \
                           f"Прибыль: <b>{data['profit_usdt']}$</b> ({data['profit_pct']}%)\n" \
                           f"Цена выхода: {exit_p}"
                    await app_bot.send_message(chat_id=self.cfg['chat_id'], text=text, parse_mode='HTML')
                    self.active_trades.remove(trade)
            except Exception as e: logger.error(f"Track error {trade['symbol']}: {e}")

        # 2. Поиск новых сигналов
        for symbol in self.cfg['symbols']:
            try:
                raw = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, self.cfg['timeframe'], limit=50)
                df = pd.DataFrame(raw, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
                df['ts'] = pd.to_datetime(df['ts'], unit='ms')
                df = add_indicators(df.iloc[:-1], self.cfg)
                c = df.iloc[-1]
                candle_id = str(c['ts'])

                if self.last_candle.get(symbol) == candle_id: continue

                side = None
                if c['cross_up'] and 30 <= c['rsi'] <= 60 and c['volume'] > c['vol_ma']:
                    side = 'LONG'
                elif c['cross_down'] and 40 <= c['rsi'] <= 70 and c['volume'] > c['vol_ma']:
                    side = 'SHORT'

                if side:
                    self.last_candle[symbol] = candle_id
                    # Расчет уровней
                    price = c['close']
                    precision = 4 if price < 1 else 2
                    sl = price * (1 - self.cfg['stop_loss_pct']) if side == 'LONG' else price * (1 + self.cfg['stop_loss_pct'])
                    tp = price * (1 + self.cfg['take_profit_pct']) if side == 'LONG' else price * (1 - self.cfg['take_profit_pct'])
                    sl, tp = round(sl, precision), round(tp, precision)

                    self.active_trades.append({'symbol': symbol, 'side': side, 'entry': price, 'sl': sl, 'tp': tp})

                    msg = f"{'🟢' if side=='LONG' else '🔴'} <b>{side} {symbol}</b>\n" \
                          f"Вход: <code>{price}</code>\nТейк: <code>{tp}</code>\nСтоп: <code>{sl}</code>"
                    await app_bot.send_message(chat_id=self.cfg['chat_id'], text=msg, parse_mode='HTML')
            except Exception as e: logger.error(f"Scan error {symbol}: {e}")

# ╔══════════════════════════════════════════════╗
# ║               ЗАПУСК СИСТЕМЫ                 ║
# ╚══════════════════════════════════════════════╝

async def trades_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    journal = TradeJournal()
    await update.message.reply_html(journal.get_history())

if __name__ == '__main__':
    import threading
    import time
    from http.server import SimpleHTTPRequestHandler, HTTPServer

    # 1. Сервер-"обманка" для Render
    def run_dummy():
        port = int(os.environ.get("PORT", 10000))
        for i in range(5):
            try:
                server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
                logger.info(f"✅ Web server active on port {port}")
                server.serve_forever()
                break
            except OSError:
                time.sleep(5)

    threading.Thread(target=run_dummy, daemon=True).start()

    # 2. Основной цикл
    async def main():
        bot_logic = SignalBot(CONFIG)
        application = Application.builder().token(CONFIG['telegram_token']).build()
        application.add_handler(CommandHandler("trades", trades_cmd))

        async def scan_loop():
            await application.bot.send_message(chat_id=CONFIG['chat_id'], text="🤖 Бот запущен! Команда /trades активна.")
            while True:
                try:
                    await bot_logic.scan(application.bot)
                except Exception as e:
                    logger.error(f"Scan loop error: {e}")
                await asyncio.sleep(60)

        async with application:
            await application.initialize()
            await application.start()
            await application.updater.start_polling()
            await scan_loop()

    try:
        import nest_asyncio
        nest_asyncio.apply()
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
