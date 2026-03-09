import ccxt
import pandas as pd
import asyncio
import logging
import os
from datetime import datetime
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Настройка логов
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

CONFIG = {
    'telegram_token': '8227791601:AAHhwkKjeYXzfA2nXqfdJ52hFUCAYVtjUyM',
    'chat_id': '715162339',
    'symbols': [
        'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT',
        'ADA/USDT', 'AVAX/USDT', 'DOT/USDT', 'POL/USDT', 'NEAR/USDT',
        'SUI/USDT', 'RENDER/USDT', 'FET/USDT', 'PEPE/USDT', 'FTM/USDT'
    ],
    'timeframe': '15m',
    'balance': 1000,        # Твой виртуальный баланс
    'leverage': 3,          # Плечо
    'risk_per_trade': 0.02, # Риск 2% на сделку
    'stop_loss_pct': 0.01,  # 1% стоп
    'take_profit_pct': 0.03, # 3% тейк
    'check_interval': 60,
}

class TradeJournal:
    def __init__(self, filename='history.csv'):
        self.filename = filename
        if not os.path.exists(self.filename):
            df = pd.DataFrame(columns=['date', 'symbol', 'side', 'result', 'profit_usdt', 'profit_pct'])
            df.to_csv(self.filename, index=False)

    def log_trade(self, symbol, side, result, entry, exit_p):
        df = pd.read_csv(self.filename)
        # Расчет процента движения цены
        price_diff_pct = ((exit_p - entry) / entry) if side == 'LONG' else ((entry - exit_p) / entry)
        
        # Чистый профит с учетом плеча и объема от риска
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

    def get_history(self):
        try:
            df = pd.read_csv(self.filename).tail(10)
            if df.empty: return "История пуста."
            msg = "<b>📜 Последние сделки:</b>\n\n"
            for _, r in df.iterrows():
                icon = "✅" if r['result'] == 'PROFIT' else "❌"
                msg += f"{icon} {r['symbol']} {r['side']}: <b>{r['profit_usdt']}$</b> ({r['profit_pct']}%)\n"
            return msg
        except: return "Ошибка истории."

class SignalBot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.exchange = ccxt.bybit({'enableRateLimit': True})
        self.journal = TradeJournal()
        self.active_trades = []
        self.last_candle = {}

    async def scan(self, app):
        # 1. Проверка активных сделок
        for trade in self.active_trades[:]:
            try:
                ticker = await asyncio.to_thread(self.exchange.fetch_ticker, trade['symbol'])
                curr_price = ticker['last']
                
                is_tp = (trade['side'] == 'LONG' and curr_price >= trade['tp']) or (trade['side'] == 'SHORT' and curr_price <= trade['tp'])
                is_sl = (trade['side'] == 'LONG' and curr_price <= trade['sl']) or (trade['side'] == 'SHORT' and curr_price >= trade['sl'])

                if is_tp or is_sl:
                    res = 'PROFIT' if is_tp else 'LOSS'
                    exit_p = trade['tp'] if is_tp else trade['sl']
                    data = self.journal.log_trade(trade['symbol'], trade['side'], res, trade['entry'], exit_p)
                    
                    text = f"{'✅' if is_tp else '❌'} <b>Сделка закрыта!</b>\n\n" \
                           f"Монета: {trade['symbol']}\nРезультат: {res}\n" \
                           f"Прибыль: <b>{data['profit_usdt']}$</b> ({data['profit_pct']}%)\n" \
                           f"Цена выхода: {exit_p}"
                    await app.bot.send_message(chat_id=self.cfg['chat_id'], text=text, parse_mode='HTML')
                    self.active_trades.remove(trade)
            except Exception as e: logger.error(f"Track error: {e}")

        # 2. Поиск новых сигналов (заглушка логики, вставь свою add_indicators сюда)
        for symbol in self.cfg['symbols']:
            # Тут идет твой старый код анализа... 
            # Если нашли сигнал:
            # self.active_trades.append({'symbol': symbol, 'side': side, 'entry': price, 'sl': sl, 'tp': tp})
            pass

async def trades_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(TradeJournal().get_history())

if __name__ == '__main__':
    # Порт для Render
    import threading
    from http.server import SimpleHTTPRequestHandler, HTTPServer
    threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.environ.get("PORT", 8080))), SimpleHTTPRequestHandler).serve_forever(), daemon=True).start()

    # Запуск
    bot_logic = SignalBot(CONFIG)
    app = Application.builder().token(CONFIG['telegram_token']).build()
    app.add_handler(CommandHandler("trades", trades_cmd))

    async def loop():
        while True:
            await bot_logic.scan(app)
            await asyncio.sleep(60)

    asyncio.get_event_loop().create_task(loop())
    app.run_polling()
