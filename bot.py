"""
Signal Bot — Binance + Telegram
Отправляет торговые сигналы, НЕ торгует сам.
Стратегия: EMA(9/21) Crossover + RSI(14) + Volume
"""

import ccxt
import pandas as pd
import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict
from telegram import Bot

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
    # ── Telegram ──
    'telegram_token': '8227791601:AAHhwkKjeYXzfA2nXqfdJ52hFUCAYVtjUyM',
    'chat_id':        '715162339',

    # ── Торговые пары ──
    'symbols': [
        'BTC/USDT', 'ETH/USDT', 'SOL/USDT',
        'BNB/USDT', 'XRP/USDT', 'DOGE/USDT',
    ],
    'timeframe': '15m',

    # ── Параметры стратегии ──
    'ema_fast':      9,
    'ema_slow':      21,
    'rsi_period':    14,
    'vol_ma_period': 20,

    # ── Риск-менеджмент (для расчётов) ──
    'balance':         1000,     # твой депозит в USDT
    'leverage':        3,
    'risk_per_trade':  0.02,     # 2% от баланса
    'stop_loss_pct':   0.01,     # 1%
    'take_profit_pct': 0.03,     # 3%  → RR 1:3

    # ── Интервал проверки ──
    'check_interval': 30,        # секунд
}


# ╔══════════════════════════════════════════════╗
# ║              ИНДИКАТОРЫ                      ║
# ╚══════════════════════════════════════════════╝

def calc_ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False).mean()


def calc_rsi(s: pd.Series, period: int = 14) -> pd.Series:
    delta = s.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def add_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    df = df.copy()
    df['ema_fast'] = calc_ema(df['close'], cfg['ema_fast'])
    df['ema_slow'] = calc_ema(df['close'], cfg['ema_slow'])
    df['rsi']      = calc_rsi(df['close'], cfg['rsi_period'])
    df['vol_ma']   = df['volume'].rolling(cfg['vol_ma_period']).mean()

    fast = df['ema_fast']
    slow = df['ema_slow']
    df['cross_up']   = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    df['cross_down'] = (fast < slow) & (fast.shift(1) >= slow.shift(1))

    return df


# ╔══════════════════════════════════════════════╗
# ║           АНАЛИЗ СИГНАЛА                     ║
# ╚══════════════════════════════════════════════╝

def analyze(df: pd.DataFrame) -> Optional[dict]:
    """
    Возвращает словарь с деталями сигнала
    или None, если сигнала нет.
    """
    c = df.iloc[-1]  # последняя завершённая свеча

    reasons = []
    side = None

    # ── LONG ──
    if c['cross_up']:
        if 30 <= c['rsi'] <= 60 and c['volume'] > c['vol_ma']:
            side = 'LONG'
            reasons = [
                f"EMA({CONFIG['ema_fast']}) пересекла EMA({CONFIG['ema_slow']}) снизу вверх",
                f"RSI({CONFIG['rsi_period']}) = {c['rsi']:.1f} (зона 30–60 ✓)",
                f"Объём {c['volume']:.0f} > среднего {c['vol_ma']:.0f} ✓",
            ]

    # ── SHORT ──
    elif c['cross_down']:
        if 40 <= c['rsi'] <= 70 and c['volume'] > c['vol_ma']:
            side = 'SHORT'
            reasons = [
                f"EMA({CONFIG['ema_fast']}) пересекла EMA({CONFIG['ema_slow']}) сверху вниз",
                f"RSI({CONFIG['rsi_period']}) = {c['rsi']:.1f} (зона 40–70 ✓)",
                f"Объём {c['volume']:.0f} > среднего {c['vol_ma']:.0f} ✓",
            ]

    if not side:
        return None

    return {
        'side': side,
        'price': c['close'],
        'rsi': c['rsi'],
        'ema_fast': c['ema_fast'],
        'ema_slow': c['ema_slow'],
        'volume': c['volume'],
        'vol_avg': c['vol_ma'],
        'reasons': reasons,
    }


# ╔══════════════════════════════════════════════╗
# ║         РАСЧЁТ ПАРАМЕТРОВ СДЕЛКИ             ║
# ╚══════════════════════════════════════════════╝

def calc_trade_params(signal: dict, cfg: dict) -> dict:
    price    = signal['price']
    side     = signal['side']
    balance  = cfg['balance']
    leverage = cfg['leverage']
    sl_pct   = cfg['stop_loss_pct']
    tp_pct   = cfg['take_profit_pct']
    risk_pct = cfg['risk_per_trade']

    # SL / TP
    if side == 'LONG':
        sl = price * (1 - sl_pct)
        tp = price * (1 + tp_pct)
    else:
        sl = price * (1 + sl_pct)
        tp = price * (1 - tp_pct)

    # Размер позиции (risk-based)
    risk_usdt   = balance * risk_pct           # сколько $ рискуем
    sl_distance = price * sl_pct               # потеря на 1 монету
    size        = risk_usdt / sl_distance      # кол-во монет

    margin      = (size * price) / leverage    # маржа
    position_value = size * price              # полный размер позиции

    return {
        'sl': round(sl, 2),
        'tp': round(tp, 2),
        'size': size,
        'margin': round(margin, 2),
        'risk_usdt': round(risk_usdt, 2),
        'position_value': round(position_value, 2),
    }


# ╔══════════════════════════════════════════════╗
# ║         ФОРМАТИРОВАНИЕ СООБЩЕНИЯ             ║
# ╚══════════════════════════════════════════════╝

def format_message(symbol: str, signal: dict, params: dict, cfg: dict) -> str:
    side = signal['side']
    icon = '🟢 LONG (Покупка)' if side == 'LONG' else '🔴 SHORT (Продажа)'
    action = 'купил' if side == 'LONG' else 'продал'

    # Причины — нумерованный список
    reasons_text = '\n'.join(
        f"   {i+1}. {r}" for i, r in enumerate(signal['reasons'])
    )

    msg = (
        f"{'═' * 35}\n"
        f"{icon}\n"
        f"{'═' * 35}\n"
        f"\n"
        f"Я бы {action} сейчас <b>{symbol}</b>\n"
        f"\n"
        f"📍 <b>Цена входа:</b>  <code>{signal['price']}</code>\n"
        f"🛑 <b>Стоп-лосс:</b>   <code>{params['sl']}</code>  "
        f"(-{cfg['stop_loss_pct']*100}%)\n"
        f"🎯 <b>Тейк-профит:</b> <code>{params['tp']}</code>  "
        f"(+{cfg['take_profit_pct']*100}%)\n"
        f"\n"
        f"⚖️  <b>Плечо:</b>       x{cfg['leverage']}\n"
        f"💰 <b>Маржа:</b>       {params['margin']} USDT\n"
        f"📦 <b>Размер:</b>      {params['size']:.6f}\n"
        f"💵 <b>Позиция:</b>     {params['position_value']} USDT\n"
        f"⚠️  <b>Риск:</b>        {params['risk_usdt']} USDT "
        f"({cfg['risk_per_trade']*100}% депо)\n"
        f"\n"
        f"📊 <b>RR:</b>  1 : {cfg['take_profit_pct']/cfg['stop_loss_pct']:.0f}\n"
        f"\n"
        f"📋 <b>Причина входа:</b>\n"
        f"{reasons_text}\n"
        f"\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"{'═' * 35}"
    )
    return msg


# ╔══════════════════════════════════════════════╗
# ║             ОСНОВНОЙ БОТ                     ║
# ╚══════════════════════════════════════════════╝

class SignalBot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.tg = Bot(token=cfg['telegram_token'])

        # Binance — только чтение данных, ключи НЕ нужны
        self.exchange = ccxt.bybit({
            'enableRateLimit': True,
        })

        # Не отправлять сигнал по одной свече дважды
        self.last_candle: Dict[str, str] = {}

        logger.info("Signal bot initialized")

    async def notify(self, text: str):
        try:
            await self.tg.send_message(
                chat_id=self.cfg['chat_id'],
                text=text,
                parse_mode='HTML',
            )
        except Exception as e:
            logger.error(f"TG send error: {e}")

    def fetch_data(self, symbol: str) -> pd.DataFrame:
        raw = self.exchange.fetch_ohlcv(
            symbol, self.cfg['timeframe'], limit=100
        )
        df = pd.DataFrame(
            raw, columns=['ts', 'open', 'high', 'low', 'close', 'volume']
        )
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
        # Убираем незавершённую свечу
        df = df.iloc[:-1]
        return df

    async def scan(self):
        """Проверяем все пары на наличие сигнала."""
        for symbol in self.cfg['symbols']:
            try:
                df = self.fetch_data(symbol)
                df = add_indicators(df, self.cfg)

                # Защита от дублей
                candle_id = str(df.iloc[-1]['ts'])
                if self.last_candle.get(symbol) == candle_id:
                    continue

                signal = analyze(df)

                if signal:
                    self.last_candle[symbol] = candle_id
                    params = calc_trade_params(signal, self.cfg)
                    msg = format_message(symbol, signal, params, self.cfg)
                    await self.notify(msg)
                    logger.info(
                        f"SIGNAL: {signal['side']} {symbol} @ {signal['price']}"
                    )

            except Exception as e:
                logger.error(f"Scan {symbol}: {e}")

    async def run(self):
        await self.notify(
            f"🤖 <b>Сигнальный бот запущен!</b>\n\n"
            f"📊 Стратегия: EMA({self.cfg['ema_fast']}/{self.cfg['ema_slow']}) "
            f"+ RSI({self.cfg['rsi_period']}) + Volume\n"
            f"⏱ Таймфрейм: {self.cfg['timeframe']}\n"
            f"💰 Расчётный баланс: {self.cfg['balance']} USDT\n"
            f"📋 Пары: {', '.join(self.cfg['symbols'])}\n\n"
            f"Жду сигналы..."
        )

        while True:
            try:
                await self.scan()
                await asyncio.sleep(self.cfg['check_interval'])
            except Exception as e:
                logger.error(f"Loop error: {e}")
                await asyncio.sleep(30)


# ╔══════════════════════════════════════════════╗
# ║                 ЗАПУСК                       ║
# ╚══════════════════════════════════════════════╝

if __name__ == '__main__':
    bot = SignalBot(CONFIG)
    asyncio.run(bot.run())
