import ccxt
import pandas as pd
import asyncio
import logging
import os
from datetime import datetime
from typing import Optional, Dict
from telegram import Bot

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler('signals.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ╔══════════════════════════════════════════════╗
# ║                  CONFIG                      ║
# ╚══════════════════════════════════════════════╝

CONFIG = {
    'telegram_token': '8227791601:AAHhwkKjeYXzfA2nXqfdJ52hFUCAYVtjUyM',
    'chat_id': '715162339',
   'symbols': [
        'BTC/USDT', 'ETH/USDT', 'BNB/USDT', 'SOL/USDT',
        'XRP/USDT', 'DOGE/USDT', 'ADA/USDT', 'AVAX/USDT',
        'DOT/USDT', 'POL/USDT',   # Вместо MATIC
        'LTC/USDT', 'TRX/USDT', 
        'ATOM/USDT', 'LINK/USDT', 'NEAR/USDT', 'APT/USDT',
        'ARB/USDT', 'OP/USDT', 'SUI/USDT', 'PEPE/USDT',
        'AAVE/USDT', 'RENDER/USDT', # Вместо RNDR
        'INJ/USDT', 'IMX/USDT', 'RUNE/USDT', 'GALA/USDT', 
        'FET/USDT', 'SEI/USDT', 'ICP/USDT', 'WLD/USDT',
        'BONK/USDT', 'SHIB/USDT', 'FLOKI/USDT'
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
# ║           ИСТОРИЯ И СТАТИСТИКА               ║
# ╚══════════════════════════════════════════════╝

class TradeJournal:
    def __init__(self, filename='history.csv'):
        self.filename = filename
        if not os.path.exists(self.filename):
            df = pd.DataFrame(columns=['date', 'symbol', 'side', 'price', 'sl', 'tp', 'status', 'profit_usdt'])
            df.to_csv(self.filename, index=False)

    def add_signal(self, symbol, side, price, sl, tp):
        df = pd.read_csv(self.filename)
        new_row = {
            'date': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'symbol': symbol,
            'side': side,
            'price': price,
            'sl': sl,
            'tp': tp,
            'status': 'OPEN',
            'profit_usdt': 0
        }
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        df.to_csv(self.filename, index=False)

    def get_stats(self):
        df = pd.read_csv(self.filename)
        if df.empty: return "История пуста"
        total_trades = len(df)
        # Это упрощенная статистика, так как бот не знает, закрылась ли сделка реально
        return f"Всего сигналов в базе: {total_trades}"

# ╔══════════════════════════════════════════════╗
# ║               МАТЕМАТИКА                     ║
# ╚══════════════════════════════════════════════╝

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

def calc_trade_params(signal: dict, cfg: dict) -> dict:
    price = signal['price']
    side = signal['side']
    
    # Определяем точность (сколько знаков после запятой)
    precision = 4 if price < 1 else 2
    
    if side == 'LONG':
        sl = price * (1 - cfg['stop_loss_pct'])
        tp = price * (1 + cfg['take_profit_pct'])
    else:
        sl = price * (1 + cfg['stop_loss_pct'])
        tp = price * (1 - cfg['take_profit_pct'])

    risk_usdt = cfg['balance'] * cfg['risk_per_trade']
    sl_dist_pct = cfg['stop_loss_pct']
    # Размер позиции = Риск / Дистанция до стопа
    size = risk_usdt / (price * sl_dist_pct)
    margin = (size * price) / cfg['leverage']

    return {
        'sl': round(sl, precision),
        'tp': round(tp, precision),
        'size': round(size, 4),
        'margin': round(margin, 2),
        'risk_usdt': round(risk_usdt, 2),
        'pos_value': round(size * price, 2)
    }

# ╔══════════════════════════════════════════════╗
# ║                ОСНОВНОЙ БОТ                  ║
# ╚══════════════════════════════════════════════╝

class SignalBot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.tg = Bot(token=cfg['telegram_token'])
        self.exchange = ccxt.bybit({'enableRateLimit': True})
        self.journal = TradeJournal()
        self.last_candle = {}

    async def notify(self, text: str):
        try:
            await self.tg.send_message(chat_id=self.cfg['chat_id'], text=text, parse_mode='HTML')
        except Exception as e:
            logger.error(f"TG Error: {e}")

    async def scan(self):
        for symbol in self.cfg['symbols']:
            try:
                raw = self.exchange.fetch_ohlcv(symbol, self.cfg['timeframe'], limit=50)
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
                    params = calc_trade_params({'side': side, 'price': c['close']}, self.cfg)
                    
                    # Сохраняем в историю
                    self.journal.add_signal(symbol, side, c['close'], params['sl'], params['tp'])
                    
                    msg = (
                        f"{'🟢' if side=='LONG' else '🔴'} <b>{side} {symbol}</b>\n"
                        f"Вход: <code>{c['close']}</code>\n"
                        f"Стоп: <code>{params['sl']}</code>\n"
                        f"Тейк: <code>{params['tp']}</code>\n"
                        f"Риск: {params['risk_usdt']} USDT (x{self.cfg['leverage']})\n"
                        f"📊 {self.journal.get_stats()}"
                    )
                    await self.notify(msg)
                    logger.info(f"Signal sent: {side} {symbol}")

            except Exception as e:
                logger.error(f"Error {symbol}: {e}")

    async def run(self):
        await self.notify("🤖 Бот обновлен и запущен!\nИсправлен расчет SHORT и добавлена история.")
        while True:
            await self.scan()
            await asyncio.sleep(self.cfg['check_interval'])

if __name__ == '__main__':
    # Для Render оставляем "обманку" порта
    import threading
    import os
    from http.server import SimpleHTTPRequestHandler, HTTPServer
    
    def run_dummy_server():
        port = int(os.environ.get("PORT", 8080))
        server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
        server.serve_forever()

    threading.Thread(target=run_dummy_server, daemon=True).start()
    
    bot = SignalBot(CONFIG)
    asyncio.run(bot.run())
