import logging
import google.generativeai as genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, PreCheckoutQueryHandler, MessageHandler, filters
import requests
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
import os
import psycopg2
from psycopg2.extras import RealDictCursor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

TOKEN = "8669316559:AAH9P3_17oejkqt03M0f75O0dEJABFix32s"
GEMINI_KEY = os.environ.get("GEMINI_KEY", "AIzaSyCF-gdX-fUCuQCNNukDbDgtx-KfIcbSm68")
ADMIN_ID = 6442924765
DATABASE_URL = os.environ.get("DATABASE_URL")

PREMIUM_STARS_30 = 500
PREMIUM_STARS_90 = 1200
TRIAL_DAYS = 1

genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel("gemini-2.0-flash")

COINS = ["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TON","AVAX","DOT"]
stats = {"total_requests": 0, "total_users": 0}

MAIN_KEYBOARD = ReplyKeyboardMarkup([
    ["📊 Цены", "🤖 Сигнал"],
    ["📰 Новости", "😱 Индекс страха"],
    ["🚀 Топ растущих", "📉 Топ падающих"],
    ["🔥 Тренды", "🌍 Доминация"],
    ["🔍 Скринер", "📊 Топ объёма"],
    ["💸 Funding BTC", "📖 Стакан BTC"],
    ["⚔️ Сравнение", "🔗 Корреляция"],
    ["📊 MACD", "📐 Фибоначчи"],
    ["🏆 ATH", "🐋 Киты BTC"],
    ["🔮 Прогноз BTC", "🗞 Крипто новости"],
    ["💼 Портфель", "🧮 Калькулятор"],
    ["⚡ Халвинг", "💱 Конвертер"],
    ["⭐ Premium", "👤 Мой статус"],
    ["👥 Реферал", "🔔 Авто-сигналы"],
    ["🎯 Расш. сигнал", "📉 Бэктест"],
    ["📅 Дневник", "🏆 Лидерборд"],
    ["🌐 Язык", "🔔 RSI/MACD алерты"],
], resize_keyboard=True)

def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        premium INTEGER DEFAULT 0,
        premium_until TIMESTAMP,
        requests_today INTEGER DEFAULT 0,
        last_reset DATE,
        total_requests INTEGER DEFAULT 0,
        joined TIMESTAMP,
        ref_by BIGINT,
        refs INTEGER DEFAULT 0,
        trial_used INTEGER DEFAULT 0
    )""")
    c.execute("""ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_used INTEGER DEFAULT 0""")
    c.execute("""CREATE TABLE IF NOT EXISTS portfolios (
        user_id BIGINT, coin TEXT, amount REAL, PRIMARY KEY (user_id, coin)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS subscribers (user_id BIGINT PRIMARY KEY)""")
    c.execute("""CREATE TABLE IF NOT EXISTS alerts (
        user_id BIGINT, symbol TEXT, threshold REAL, PRIMARY KEY (user_id, symbol)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS user_settings (
        user_id BIGINT PRIMARY KEY,
        lang TEXT DEFAULT 'ru'
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS trades (
        id SERIAL PRIMARY KEY,
        user_id BIGINT,
        symbol TEXT,
        action TEXT,
        price REAL,
        amount REAL,
        created_at TIMESTAMP DEFAULT NOW()
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS indicator_alerts (
        user_id BIGINT,
        symbol TEXT,
        indicator TEXT,
        level REAL,
        direction TEXT,
        PRIMARY KEY (user_id, symbol, indicator)
    )""")
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = get_conn()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT * FROM users WHERE user_id=%s", (user_id,))
    row = c.fetchone()
    today = datetime.now().date()
    if not row:
        c.execute("INSERT INTO users (user_id, last_reset, joined, trial_used) VALUES (%s, %s, %s, 0)",
                  (user_id, today, datetime.now()))
        conn.commit()
        c.execute("SELECT * FROM users WHERE user_id=%s", (user_id,))
        row = c.fetchone()
        stats["total_users"] += 1
    if row["last_reset"] != today:
        c.execute("UPDATE users SET requests_today=0, last_reset=%s WHERE user_id=%s", (today, user_id))
        conn.commit()
        row["requests_today"] = 0
        row["last_reset"] = today
    conn.close()
    return dict(row)

def is_premium(user_id):
    u = get_user(user_id)
    if u["premium_until"] and datetime.now() < u["premium_until"]:
        return True
    return bool(u["premium"])

def can_use(user_id):
    if is_premium(user_id):
        return True
    return get_user(user_id)["requests_today"] < 3

def use_request(user_id):
    conn = get_conn()
    c = conn.cursor()
    if not is_premium(user_id):
        c.execute("UPDATE users SET requests_today=requests_today+1 WHERE user_id=%s", (user_id,))
    c.execute("UPDATE users SET total_requests=total_requests+1 WHERE user_id=%s", (user_id,))
    conn.commit()
    conn.close()
    stats["total_requests"] += 1

def activate_premium(user_id, days=30):
    get_user(user_id)
    until = datetime.now() + timedelta(days=days)
    conn = get_conn()
    conn.cursor().execute("UPDATE users SET premium=1, premium_until=%s WHERE user_id=%s", (until, user_id))
    conn.commit()
    conn.close()

def activate_trial(user_id):
    get_user(user_id)
    until = datetime.now() + timedelta(days=TRIAL_DAYS)
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET premium=1, premium_until=%s, trial_used=1 WHERE user_id=%s", (until, user_id))
    conn.commit()
    conn.close()

def load_subscribers():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id FROM subscribers")
    rows = c.fetchall()
    conn.close()
    return set(r[0] for r in rows)

def save_subscriber(user_id, add=True):
    conn = get_conn()
    c = conn.cursor()
    if add:
        c.execute("INSERT INTO subscribers (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
    else:
        c.execute("DELETE FROM subscribers WHERE user_id=%s", (user_id,))
    conn.commit()
    conn.close()

def load_alerts():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id, symbol, threshold FROM alerts")
    rows = c.fetchall()
    conn.close()
    result = {}
    for user_id, symbol, threshold in rows:
        if user_id not in result:
            result[user_id] = {}
        result[user_id][symbol] = threshold
    return result

def save_alert(user_id, symbol, threshold):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO alerts VALUES (%s,%s,%s) ON CONFLICT (user_id, symbol) DO UPDATE SET threshold=%s",
              (user_id, symbol, threshold, threshold))
    conn.commit()
    conn.close()

def get_portfolio(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT coin, amount FROM portfolios WHERE user_id=%s", (user_id,))
    rows = c.fetchall()
    conn.close()
    return {coin: amount for coin, amount in rows}

def save_portfolio(user_id, coin, amount):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO portfolios VALUES (%s,%s,%s) ON CONFLICT (user_id, coin) DO UPDATE SET amount=%s",
              (user_id, coin, amount, amount))
    conn.commit()
    conn.close()

def get_price(symbol):
    try:
        r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}USDT")
        return float(r.json()["price"])
    except:
        return None

def get_change(symbol):
    try:
        r = requests.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}USDT")
        return float(r.json()["priceChangePercent"])
    except:
        return None

def get_fear_index():
    try:
        r = requests.get("https://api.alternative.me/fng/")
        d = r.json()["data"][0]
        return d["value"], d["value_classification"]
    except:
        return None, None

def get_ai_signal(symbol, price, change):
    try:
        prompt = f"""Ты опытный криптотрейдер. Дай торговый сигнал для {symbol}/USDT.
Цена: ${price:,.2f}, изменение за 24ч: {change:+.2f}%
Ответь в формате:
🎯 Сигнал: ПОКУПАТЬ/ПРОДАВАТЬ/ДЕРЖАТЬ
📊 Уверенность: X%
⚡ Причина: одно предложение
⏱ Горизонт: краткосрочный/среднесрочный"""
        return model.generate_content(prompt).text
    except Exception as e:
        return f"Ошибка ИИ: {e}"

async def send_auto_signals(app):
    subs = load_subscribers()
    if not subs:
        return
    btc = get_price("BTC")
    eth = get_price("ETH")
    btc_c = get_change("BTC")
    eth_c = get_change("ETH")
    fear_val, fear_label = get_fear_index()
    if not btc:
        return
    try:
        prompt = f"""Сделай краткий обзор крипторынка на русском.
BTC: ${btc:,.2f} ({btc_c:+.2f}%), ETH: ${eth:,.2f} ({eth_c:+.2f}%)
Индекс страха: {fear_val} ({fear_label})
Дай 2-3 торговых сигнала. Кратко."""
        analysis = model.generate_content(prompt).text
    except:
        analysis = "ИИ-анализ временно недоступен"
    message = (
        f"🤖 Авто-сигналы SignalBot\n"
        f"🕐 {datetime.now().strftime('%H:%M %d.%m.%Y')}\n\n"
        f"📊 BTC: ${btc:,.2f} {'📈' if btc_c > 0 else '📉'} {btc_c:+.2f}%\n"
        f"📊 ETH: ${eth:,.2f} {'📈' if eth_c > 0 else '📉'} {eth_c:+.2f}%\n"
        f"🧠 Страх/Жадность: {fear_val}/100\n\n{analysis}"
    )
    for user_id in subs:
        try:
            await app.bot.send_message(chat_id=user_id, text=message)
        except:
            save_subscriber(user_id, add=False)

async def predict_cmd(update, context):
    user_id = update.effective_user.id
    if not is_premium(user_id):
        await update.message.reply_text("Прогноз только в Premium! /premium")
        return
    if not context.args:
        await update.message.reply_text("Пример: /predict BTC")
        return
    symbol = context.args[0].upper()
    await update.message.reply_text(f"Анализирую {symbol} и строю прогноз...")
    try:
        p = get_price(symbol)
        c = get_change(symbol)
        r = requests.get(f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval=1d&limit=14", timeout=10)
        closes = [float(x[4]) for x in r.json()]
        period = 14
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i-1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        rsi = round(100 - (100 / (1 + avg_gain/avg_loss)), 2) if avg_loss != 0 else 100
        prompt = f"""Ты опытный криптоаналитик. Дай прогноз цены {symbol}/USDT на следующие 24 часа.
Текущая цена: ${p:,.2f}
Изменение за 24ч: {c:+.2f}%
RSI(14): {rsi}
Последние 7 дней закрытий: {[round(x) for x in closes[-7:]]}
Дай конкретный прогноз:
Направление: ВВЕРХ/ВНИЗ/БОКОВИК
Целевая цена: $X
Стоп-лосс: $X
Вероятность: X%
Обоснование: одно предложение"""
        prediction = model.generate_content(prompt).text
        await update.message.reply_text(
            f"🔮 Прогноз {symbol} на 24ч\n\n"
            f"💰 Цена: ${p:,.2f}\n"
            f"📉 RSI: {rsi}\n\n"
            f"{prediction}"
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def whales_cmd(update, context):
    user_id = update.effective_user.id
    if not can_use(user_id):
        await update.message.reply_text("Лимит исчерпан! /premium")
        return
    if not context.args:
        symbol = "BTC"
    else:
        symbol = context.args[0].upper()
    use_request(user_id)
    try:
        r = requests.get(f"https://api.binance.com/api/v3/trades?symbol={symbol}USDT&limit=1000", timeout=10)
        trades = r.json()
        big_trades = [t for t in trades if float(t["quoteQty"]) > 100000]
        buy_vol = sum(float(t["quoteQty"]) for t in big_trades if not t["isBuyerMaker"])
        sell_vol = sum(float(t["quoteQty"]) for t in big_trades if t["isBuyerMaker"])
        total = buy_vol + sell_vol
        if total == 0:
            await update.message.reply_text("Нет крупных сделок")
            return
        buy_pct = (buy_vol / total) * 100
        sell_pct = (sell_vol / total) * 100
        signal = "Киты покупают!" if buy_pct > 60 else "Киты продают!" if sell_pct > 60 else "Нейтрально"
        await update.message.reply_text(
            f"🐋 Активность китов {symbol}\n\n"
            f"Крупных сделок: {len(big_trades)}\n"
            f"Покупки: {buy_pct:.1f}% (${buy_vol/1e6:.1f}M)\n"
            f"Продажи: {sell_pct:.1f}% (${sell_vol/1e6:.1f}M)\n\n"
            f"Сигнал: {signal}"
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def macd_cmd(update, context):
    user_id = update.effective_user.id
    if not is_premium(user_id):
        await update.message.reply_text("MACD только в Premium! /premium")
        return
    if not context.args:
        await update.message.reply_text("Пример: /macd BTC")
        return
    symbol = context.args[0].upper()
    await update.message.reply_text(f"Считаю MACD для {symbol}...")
    try:
        r = requests.get(f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval=1d&limit=50", timeout=10)
        closes = [float(x[4]) for x in r.json()]
        def ema(data, period):
            k = 2 / (period + 1)
            result = [data[0]]
            for price in data[1:]:
                result.append(price * k + result[-1] * (1 - k))
            return result
        ema12 = ema(closes, 12)
        ema26 = ema(closes, 26)
        macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
        signal_line = ema(macd_line, 9)
        macd_val = macd_line[-1]
        signal_val = signal_line[-1]
        hist = macd_val - signal_val
        if hist > 0 and macd_val > 0:
            trend = "Бычий тренд — покупать"
        elif hist < 0 and macd_val < 0:
            trend = "Медвежий тренд — продавать"
        elif hist > 0:
            trend = "Разворот вверх"
        else:
            trend = "Разворот вниз"
        await update.message.reply_text(
            f"MACD {symbol}\n\n"
            f"MACD: {macd_val:.2f}\n"
            f"Сигнал: {signal_val:.2f}\n"
            f"Гистограмма: {hist:.2f}\n\n"
            f"Вывод: {trend}"
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def fib_cmd(update, context):
    user_id = update.effective_user.id
    if not can_use(user_id):
        await update.message.reply_text("Лимит исчерпан! /premium")
        return
    if not context.args:
        await update.message.reply_text("Пример: /fib BTC")
        return
    symbol = context.args[0].upper()
    use_request(user_id)
    try:
        r = requests.get(f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval=1d&limit=30", timeout=10)
        data = r.json()
        highs = [float(x[2]) for x in data]
        lows = [float(x[3]) for x in data]
        high = max(highs)
        low = min(lows)
        diff = high - low
        levels = {
            "0%": high,
            "23.6%": high - diff * 0.236,
            "38.2%": high - diff * 0.382,
            "50%": high - diff * 0.5,
            "61.8%": high - diff * 0.618,
            "100%": low
        }
        lines = [f"Фибоначчи {symbol} (30 дней):\n"]
        for level, price in levels.items():
            lines.append(f"{level}: ${price:,.2f}")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def ath_cmd(update, context):
    user_id = update.effective_user.id
    if not can_use(user_id):
        await update.message.reply_text("Лимит исчерпан! /premium")
        return
    if not context.args:
        await update.message.reply_text("Пример: /ath BTC")
        return
    symbol = context.args[0].upper()
    use_request(user_id)
    try:
        coin_map = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin", "XRP": "ripple", "ADA": "cardano", "DOGE": "dogecoin", "TON": "the-open-network", "AVAX": "avalanche-2", "DOT": "polkadot"}
        coin_id = coin_map.get(symbol, symbol.lower())
        r = requests.get(f"https://api.coingecko.com/api/v3/coins/{coin_id}?localization=false", timeout=10)
        data = r.json()
        ath = data["market_data"]["ath"]["usd"]
        current = data["market_data"]["current_price"]["usd"]
        ath_date = data["market_data"]["ath_date"]["usd"][:10]
        diff_pct = ((current - ath) / ath) * 100
        await update.message.reply_text(
            f"ATH {symbol}\n\n"
            f"Исторический максимум: ${ath:,.2f}\n"
            f"Дата ATH: {ath_date}\n"
            f"Текущая цена: ${current:,.2f}\n"
            f"До ATH: {diff_pct:.1f}%"
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def correlation_cmd(update, context):
    user_id = update.effective_user.id
    if not can_use(user_id):
        await update.message.reply_text("Лимит исчерпан! /premium")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Пример: /corr BTC ETH")
        return
    sym1 = context.args[0].upper()
    sym2 = context.args[1].upper()
    use_request(user_id)
    try:
        r1 = requests.get(f"https://api.binance.com/api/v3/klines?symbol={sym1}USDT&interval=1d&limit=30", timeout=10)
        r2 = requests.get(f"https://api.binance.com/api/v3/klines?symbol={sym2}USDT&interval=1d&limit=30", timeout=10)
        c1 = [float(x[4]) for x in r1.json()]
        c2 = [float(x[4]) for x in r2.json()]
        n = len(c1)
        mean1 = sum(c1) / n
        mean2 = sum(c2) / n
        num = sum((c1[i] - mean1) * (c2[i] - mean2) for i in range(n))
        den = (sum((x - mean1)**2 for x in c1) * sum((x - mean2)**2 for x in c2)) ** 0.5
        corr = num / den if den != 0 else 0
        if corr > 0.8:
            desc = "Очень высокая — двигаются вместе"
        elif corr > 0.5:
            desc = "Высокая корреляция"
        elif corr > 0.2:
            desc = "Умеренная корреляция"
        else:
            desc = "Низкая — двигаются независимо"
        await update.message.reply_text(
            f"Корреляция {sym1} и {sym2}\n\n"
            f"Коэффициент: {corr:.2f}\n"
            f"Вывод: {desc}"
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def weekly_top_cmd(update, context):
    user_id = update.effective_user.id
    if not can_use(user_id):
        await update.message.reply_text("Лимит исчерпан! /premium")
        return
    await update.message.reply_text("Загружаю топ по объёму...")
    use_request(user_id)
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr", timeout=10)
        data = r.json()
        usdt = [x for x in data if x["symbol"].endswith("USDT") and float(x["quoteVolume"]) > 10000000]
        top = sorted(usdt, key=lambda x: float(x["quoteVolume"]), reverse=True)[:10]
        lines = ["Топ 10 по объёму за 24ч:\n"]
        for i, coin in enumerate(top, 1):
            sym = coin["symbol"].replace("USDT", "")
            vol = float(coin["quoteVolume"]) / 1e6
            ch = float(coin["priceChangePercent"])
            arrow = "+" if ch > 0 else ""
            lines.append(f"{i}. {sym}: ${vol:.0f}M | {arrow}{ch:.1f}%")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def multialert_cmd(update, context):
    user_id = update.effective_user.id
    if not is_premium(user_id):
        await update.message.reply_text("Мультиалерт только в Premium! /premium")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Формат: /multialert BTC,ETH,SOL 5")
        return
    symbols = context.args[0].upper().split(",")
    try:
        threshold = float(context.args[1])
    except:
        await update.message.reply_text("Пример: /multialert BTC,ETH,SOL 5")
        return
    conn = get_conn()
    c = conn.cursor()
    for sym in symbols:
        c.execute("INSERT INTO alerts VALUES (%s,%s,%s) ON CONFLICT (user_id,symbol) DO UPDATE SET threshold=%s",
                  (user_id, sym.strip(), threshold, threshold))
    conn.commit()
    conn.close()
    syms = ", ".join(s.strip() for s in symbols)
    await update.message.reply_text(f"Алерты установлены для: {syms}\nПорог: {threshold}%")

async def cryptonews_cmd(update, context):
    user_id = update.effective_user.id
    if not can_use(user_id):
        await update.message.reply_text("Лимит исчерпан! /premium")
        return
    await update.message.reply_text("Загружаю крипто новости...")
    use_request(user_id)
    try:
        import xml.etree.ElementTree as ET
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get("https://bitcoinmagazine.com/feed", timeout=10, headers=headers)
        root = ET.fromstring(r.content)
        items = root.findall(".//item")[:5]
        titles = []
        for item in items:
            title = item.find("title").text[:100] if item.find("title") is not None else ""
            titles.append(title)
        prompt = "Переведи эти заголовки новостей на русский язык, каждый с новой строки:\n" + "\n".join(titles)
        translated = model.generate_content(prompt).text
        await update.message.reply_text(f"Крипто новости:\n\n{translated}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def check_rsi_signals(app):
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT user_id FROM subscribers")
        subs = [r[0] for r in c.fetchall()]
        conn.close()
        if not subs:
            return
        for symbol in ["BTC", "ETH", "SOL", "BNB"]:
            try:
                r = requests.get(f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval=1d&limit=100", timeout=10)
                closes = [float(x[4]) for x in r.json()]
                period = 14
                gains, losses = [], []
                for i in range(1, len(closes)):
                    diff = closes[i] - closes[i-1]
                    gains.append(max(diff, 0))
                    losses.append(max(-diff, 0))
                avg_gain = sum(gains[-period:]) / period
                avg_loss = sum(losses[-period:]) / period
                if avg_loss == 0:
                    rsi = 100
                else:
                    rs = avg_gain / avg_loss
                    rsi = round(100 - (100 / (1 + rs)), 2)
                p = get_price(symbol)
                if rsi < 30:
                    msg = f"RSI Сигнал {symbol}\n\nRSI: {rsi} — Перепродан!\nЦена: ${p:,.2f}\nВозможен отскок вверх"
                    for uid in subs:
                        try:
                            await app.bot.send_message(chat_id=uid, text=msg)
                        except:
                            pass
                elif rsi > 70:
                    msg = f"RSI Сигнал {symbol}\n\nRSI: {rsi} — Перекуплен!\nЦена: ${p:,.2f}\nВозможна коррекция"
                    for uid in subs:
                        try:
                            await app.bot.send_message(chat_id=uid, text=msg)
                        except:
                            pass
            except:
                pass
    except Exception as e:
        logger.error(f"RSI check error: {e}")

async def check_alerts(app):
    alerts = load_alerts()
    for user_id, watches in alerts.items():
        for symbol, threshold in watches.items():
            c = get_change(symbol)
            p = get_price(symbol)
            if c and p and abs(c) >= threshold:
                try:
                    arrow = "📈🚀" if c > 0 else "📉🔻"
                    await app.bot.send_message(
                        chat_id=user_id,
                        text=f"🚨 Alert! {symbol}/USDT\n\n{arrow} Изменение: {c:+.2f}%\n💰 Цена: ${p:,.2f}\n\nВаш порог: {threshold}%"
                    )
                except:
                    pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    name = update.effective_user.first_name or "трейдер"
    u = get_user(user_id)
    is_new = u["total_requests"] == 0
    if context.args:
        ref_id = int(context.args[0]) if context.args[0].isdigit() else None
        if ref_id and ref_id != user_id and not u["ref_by"]:
            ref = get_user(ref_id)
            if ref:
                conn = get_conn()
                c = conn.cursor()
                c.execute("UPDATE users SET ref_by=%s WHERE user_id=%s", (ref_id, user_id))
                c.execute("UPDATE users SET refs=refs+1 WHERE user_id=%s", (ref_id,))
                conn.commit()
                conn.close()
                ref = get_user(ref_id)
                if ref["refs"] >= 3 and ref["refs"] % 3 == 0:
                    activate_premium(ref_id, 7)
                    try:
                        await context.bot.send_message(chat_id=ref_id, text="🎉 3 реферала! Вам активированы 7 дней Premium!")
                    except:
                        pass
    if is_new and not u["trial_used"]:
        activate_trial(user_id)
        await update.message.reply_text(
            f"👋 Привет, {name}! Добро пожаловать в SignalBot!\n\n"
            f"🎁 Тебе активирован бесплатный пробный период на {TRIAL_DAYS} день!\n"
            f"Попробуй все функции Premium прямо сейчас 🚀",
            reply_markup=MAIN_KEYBOARD
        )
    else:
        premium = is_premium(user_id)
        u = get_user(user_id)
        status = "⭐ Premium" if premium else f"🆓 Бесплатный ({3 - u['requests_today']}/3 запросов)"
        await update.message.reply_text(
            f"👋 Привет, {name}!\n\n"
            f"🤖 Я SignalBot — ваш ИИ-трейдер 24/7\n\n"
            f"📊 Статус: {status}\n\n"
            f"Используй кнопки ниже 👇",
            reply_markup=MAIN_KEYBOARD
        )

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 Цены":
        keyboard = [[InlineKeyboardButton(coin, callback_data=f"price_{coin}")] for coin in COINS]
        await update.message.reply_text("Выбери монету:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif text == "🤖 Сигнал":
        keyboard = [[InlineKeyboardButton(coin, callback_data=f"signal_{coin}")] for coin in COINS]
        await update.message.reply_text("Выбери монету для сигнала:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif text == "📰 Новости":
        await news_cmd(update, context)
    elif text == "🗞 Крипто новости":
        await cryptonews_cmd(update, context)
    elif text == "😱 Индекс страха":
        await fear(update, context)
    elif text == "💼 Портфель":
        await portfolio_cmd(update, context)
    elif text == "⭐ Premium":
        await premium_cmd(update, context)
    elif text == "👤 Мой статус":
        await status_cmd(update, context)
    elif text == "👥 Реферал":
        await ref_cmd(update, context)
    elif text == "🚀 Топ растущих":
        await top_gainers(update, context)
    elif text == "📉 Топ падающих":
        await top_losers(update, context)
    elif text == "🧮 Калькулятор":
        await update.message.reply_text("🧮 Калькулятор\n\nФормат: /calc МОНЕТА ЦЕНА_ПОКУПКИ ЦЕНА_ПРОДАЖИ КОЛИЧЕСТВО\n\nПример: /calc BTC 50000 74000 0.5")
    elif text == "🔔 Авто-сигналы":
        await autosignal_cmd(update, context)
    elif text == "🔥 Тренды":
        await trending_cmd(update, context)
    elif text == "🌍 Доминация":
        await dominance_cmd(update, context)
    elif text == "⚡ Халвинг":
        await halving_cmd(update, context)
    elif text == "💱 Конвертер":
        await update.message.reply_text("💱 Конвертер\n\nФормат: /convert КОЛИЧЕСТВО ИЗ В\n\nПримеры:\n/convert 1 BTC USD\n/convert 0.5 ETH RUB")
    elif text == "🔮 Прогноз BTC":
        context.args = ["BTC"]
        await predict_cmd(update, context)
    elif text == "🐋 Киты BTC":
        context.args = ["BTC"]
        await whales_cmd(update, context)
    elif text == "📊 MACD":
        await update.message.reply_text("Формат: /macd BTC")
    elif text == "📐 Фибоначчи":
        await update.message.reply_text("Формат: /fib BTC")
    elif text == "🏆 ATH":
        await update.message.reply_text("Формат: /ath BTC")
    elif text == "🔗 Корреляция":
        await update.message.reply_text("Формат: /corr BTC ETH")
    elif text == "📊 Топ объёма":
        await weekly_top_cmd(update, context)
    elif text == "🔍 Скринер":
        await screener_cmd(update, context)
    elif text == "📖 Стакан BTC":
        context.args = ["BTC"]
        await orderbook_cmd(update, context)
    elif text == "💸 Funding BTC":
        context.args = ["BTC"]
        await funding_cmd(update, context)
    elif text == "⚔️ Сравнение":
        await update.message.reply_text("Формат: /compare BTC ETH")
    elif text == "🎯 Расш. сигнал":
        keyboard = [[InlineKeyboardButton(coin, callback_data=f"asignal_{coin}")] for coin in COINS]
        await update.message.reply_text("Выбери монету:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif text == "📅 Дневник":
        await diary_cmd(update, context)
    elif text == "🏆 Лидерборд":
        await leaderboard_cmd(update, context)
    elif text == "🌐 Язык":
        await lang_cmd(update, context)

async def calc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 4:
        await update.message.reply_text("Формат: /calc МОНЕТА ЦЕНА_ПОКУПКИ ЦЕНА_ПРОДАЖИ КОЛИЧЕСТВО\n\nПример: /calc BTC 50000 74000 0.5")
        return
    try:
        symbol = context.args[0].upper()
        buy = float(context.args[1])
        sell = float(context.args[2])
        amount = float(context.args[3])
        invested = buy * amount
        received = sell * amount
        profit = received - invested
        pct = (profit / invested) * 100
        emoji = "💰" if profit > 0 else "📉"
        await update.message.reply_text(
            f"🧮 Расчёт прибыли {symbol}\n\n"
            f"💵 Куплено: {amount} {symbol} по ${buy:,.2f}\n"
            f"💵 Продано: по ${sell:,.2f}\n\n"
            f"📊 Вложено: ${invested:,.2f}\n"
            f"📊 Получено: ${received:,.2f}\n"
            f"{emoji} Прибыль: ${profit:+,.2f} ({pct:+.2f}%)"
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def get_rsi(symbol, period=14):
    try:
        r = requests.get(f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval=1d&limit=100", timeout=10)
        closes = [float(x[4]) for x in r.json()]
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i-1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)
    except:
        return None

async def funding_cmd(update, context):
    user_id = update.effective_user.id
    if not is_premium(user_id):
        await update.message.reply_text("Funding rate только в Premium! /premium")
        return
    if context.args:
        symbol = context.args[0].upper()
    else:
        symbol = "BTC"
    try:
        r = requests.get(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}USDT&limit=1", timeout=5)
        data = r.json()
        if not data:
            await update.message.reply_text("Нет данных")
            return
        rate = float(data[0]["fundingRate"]) * 100
        if rate < 0:
            signal = "Шорты платят лонгам (бычий знак)"
            emoji = "Позитивно"
        elif rate > 0.05:
            signal = "Лонги платят шортам (медвежий знак)"
            emoji = "Негативно"
        else:
            signal = "Нейтрально"
            emoji = "Нейтрально"
        await update.message.reply_text(
            f"Funding Rate {symbol}\n\nСтавка: {rate:.4f}%\nСигнал: {signal}\nИтог: {emoji}\n\nОбновляется каждые 8 часов"
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def orderbook_cmd(update, context):
    user_id = update.effective_user.id
    if not can_use(user_id):
        await update.message.reply_text("Лимит исчерпан! /premium")
        return
    if context.args:
        symbol = context.args[0].upper()
    else:
        symbol = "BTC"
    use_request(user_id)
    try:
        r = requests.get(f"https://api.binance.com/api/v3/depth?symbol={symbol}USDT&limit=10", timeout=5)
        data = r.json()
        bids = sum(float(b[1]) for b in data["bids"])
        asks = sum(float(a[1]) for a in data["asks"])
        total = bids + asks
        bid_pct = (bids / total) * 100
        ask_pct = (asks / total) * 100
        pressure = "Давление покупателей" if bid_pct > 55 else "Давление продавцов" if ask_pct > 55 else "Баланс"
        bar_b = "=" * int(bid_pct / 10)
        bar_a = "=" * int(ask_pct / 10)
        await update.message.reply_text(
            f"Стакан {symbol}/USDT\n\nПокупки: {bid_pct:.1f}%\n[{bar_b}]\n\nПродажи: {ask_pct:.1f}%\n[{bar_a}]\n\nИтог: {pressure}"
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def screener_cmd(update, context):
    user_id = update.effective_user.id
    if not can_use(user_id):
        await update.message.reply_text("Лимит исчерпан! /premium")
        return
    await update.message.reply_text("Ищу монеты с аномальным объёмом...")
    use_request(user_id)
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr", timeout=10)
        data = r.json()
        usdt = [x for x in data if x["symbol"].endswith("USDT") and float(x["quoteVolume"]) > 5000000]
        avg_volume = sum(float(x["quoteVolume"]) for x in usdt) / len(usdt)
        pumps = [x for x in usdt if float(x["quoteVolume"]) > avg_volume * 3 and float(x["priceChangePercent"]) > 5]
        pumps = sorted(pumps, key=lambda x: float(x["quoteVolume"]), reverse=True)[:5]
        if not pumps:
            await update.message.reply_text("Аномалий не найдено — рынок спокойный")
            return
        lines = ["Скринер — монеты с аномальным объёмом:"]
        for coin in pumps:
            sym = coin["symbol"].replace("USDT", "")
            ch = float(coin["priceChangePercent"])
            vol = float(coin["quoteVolume"]) / 1e6
            lines.append(f"{sym}: +{ch:.1f}% | Объём: ${vol:.1f}M")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def dominance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=10)
        data = r.json()["data"]
        btc_dom = data["market_cap_percentage"]["btc"]
        eth_dom = data["market_cap_percentage"]["eth"]
        total = data["total_market_cap"]["usd"]
        await update.message.reply_text(
            f"🌍 Доминация крипторынка\n\n₿ BTC: {btc_dom:.1f}%\nΞ ETH: {eth_dom:.1f}%\n🏦 Капитализация: ${total/1e12:.2f}T"
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def convert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text("Формат: /convert 1 BTC USD\nПример: /convert 0.5 ETH RUB")
        return
    try:
        amount = float(context.args[0])
        from_sym = context.args[1].upper()
        to_sym = context.args[2].upper()
        if to_sym in ["USD", "USDT"]:
            price = get_price(from_sym)
            result = price * amount if price else None
            symbol = "$"
        elif to_sym == "RUB":
            price_usd = get_price(from_sym)
            r = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=5)
            rub_rate = r.json()["rates"]["RUB"]
            result = price_usd * amount * rub_rate if price_usd else None
            symbol = "₽"
        else:
            price1 = get_price(from_sym)
            price2 = get_price(to_sym)
            result = (price1 / price2) * amount if price1 and price2 else None
            symbol = to_sym
        if result:
            await update.message.reply_text(f"💱 {amount} {from_sym} = {result:,.4f} {symbol}")
        else:
            await update.message.reply_text("Не удалось конвертировать")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def halving_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5)
        btc_price = float(r.json()["price"])
        next_halving_block = 1050000
        r2 = requests.get("https://blockchain.info/q/getblockcount", timeout=5)
        current_block = int(r2.text)
        blocks_left = next_halving_block - current_block
        days_left = blocks_left * 10 / 60 / 24
        await update.message.reply_text(
            f"⚡ Следующий халвинг BTC\n\n📦 Блок: {current_block:,}\n🎯 Халвинг: {next_halving_block:,}\n⏳ Осталось: {blocks_left:,} блоков\n📅 Примерно: {int(days_left)} дней\n💰 BTC: ${btc_price:,.2f}"
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def trending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_use(user_id):
        await update.message.reply_text("Лимит исчерпан! /premium")
        return
    use_request(user_id)
    try:
        r = requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=10)
        coins = r.json()["coins"][:7]
        lines = ["🔥 Трендовые монеты сейчас:\n"]
        for i, item in enumerate(coins, 1):
            coin = item["item"]
            lines.append(f"{i}. {coin['name']} ({coin['symbol'].upper()})")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def compare_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_use(user_id):
        await update.message.reply_text("Лимит исчерпан! /premium")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Формат: /compare BTC ETH")
        return
    sym1 = context.args[0].upper()
    sym2 = context.args[1].upper()
    p1 = get_price(sym1)
    p2 = get_price(sym2)
    c1 = get_change(sym1)
    c2 = get_change(sym2)
    use_request(user_id)
    if not p1 or not p2:
        await update.message.reply_text("Не удалось получить данные")
        return
    winner = sym1 if (c1 or 0) > (c2 or 0) else sym2
    await update.message.reply_text(
        f"⚔️ Сравнение монет\n\n{'📈' if c1 > 0 else '📉'} {sym1}:\n  💰 ${p1:,.4f}\n  📊 {c1:+.2f}%\n\n{'📈' if c2 > 0 else '📉'} {sym2}:\n  💰 ${p2:,.4f}\n  📊 {c2:+.2f}%\n\n🏆 Лучше: {winner}"
    )

async def ta_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_premium(user_id):
        await update.message.reply_text("Тех. анализ только в Premium! /premium")
        return
    if not context.args:
        await update.message.reply_text("Пример: /ta BTC")
        return
    symbol = context.args[0].upper()
    await update.message.reply_text(f"Анализирую {symbol}...")
    p = get_price(symbol)
    c = get_change(symbol)
    rsi = await get_rsi(symbol)
    rsi_text = "Перепродан (покупка)" if rsi and rsi < 30 else "Перекуплен (продажа)" if rsi and rsi > 70 else "Нейтрально"
    await update.message.reply_text(
        f"📊 Тех. анализ {symbol}\n\n💰 Цена: ${p:,.2f}\n📈 24ч: {c:+.2f}%\n📉 RSI(14): {rsi} — {rsi_text}"
    )

async def daily_digest(app):
    subs = load_subscribers()
    if not subs:
        return
    btc = get_price("BTC")
    eth = get_price("ETH")
    btc_c = get_change("BTC")
    eth_c = get_change("ETH")
    fear_val, fear_label = get_fear_index()
    if not btc:
        return
    try:
        prompt = f"""Напиши утренний дайджест крипторынка на русском. Кратко.
BTC: ${btc:,.2f} ({btc_c:+.2f}%), ETH: ${eth:,.2f} ({eth_c:+.2f}%)
Индекс страха/жадности: {fear_val} ({fear_label})
1. Что важно знать сегодня
2. На что обратить внимание
3. Торговая идея дня"""
        analysis = model.generate_content(prompt).text
    except:
        analysis = "ИИ-анализ временно недоступен"
    message = (
        f"☀️ Доброе утро! Дайджест SignalBot\n"
        f"📅 {datetime.now().strftime('%d.%m.%Y')}\n\n"
        f"📊 BTC: ${btc:,.2f} {'📈' if btc_c > 0 else '📉'} {btc_c:+.2f}%\n"
        f"📊 ETH: ${eth:,.2f} {'📈' if eth_c > 0 else '📉'} {eth_c:+.2f}%\n"
        f"🧠 Индекс страха: {fear_val}/100 ({fear_label})\n\n{analysis}"
    )
    for user_id in subs:
        try:
            await app.bot.send_message(chat_id=user_id, text=message)
        except:
            save_subscriber(user_id, add=False)

async def top_gainers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_use(user_id):
        await update.message.reply_text("Лимит исчерпан! /premium")
        return
    await update.message.reply_text("⏳ Загружаю топ растущих...")
    use_request(user_id)
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr", timeout=10)
        data = r.json()
        usdt = [x for x in data if x["symbol"].endswith("USDT") and float(x["quoteVolume"]) > 1000000]
        gainers = sorted(usdt, key=lambda x: float(x["priceChangePercent"]), reverse=True)[:5]
        lines = ["🚀 Топ 5 растущих монет:\n"]
        for coin in gainers:
            sym = coin["symbol"].replace("USDT", "")
            ch = float(coin["priceChangePercent"])
            pr = float(coin["lastPrice"])
            lines.append(f"📈 {sym}: ${pr:,.4f} +{ch:.2f}%")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def top_losers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_use(user_id):
        await update.message.reply_text("Лимит исчерпан! /premium")
        return
    await update.message.reply_text("⏳ Загружаю топ падающих...")
    use_request(user_id)
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr", timeout=10)
        data = r.json()
        usdt = [x for x in data if x["symbol"].endswith("USDT") and float(x["quoteVolume"]) > 1000000]
        losers = sorted(usdt, key=lambda x: float(x["priceChangePercent"]))[:5]
        lines = ["📉 Топ 5 падающих монет:\n"]
        for coin in losers:
            sym = coin["symbol"].replace("USDT", "")
            ch = float(coin["priceChangePercent"])
            pr = float(coin["lastPrice"])
            lines.append(f"📉 {sym}: ${pr:,.4f} {ch:.2f}%")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    u = get_user(user_id)
    premium = is_premium(user_id)
    subs = load_subscribers()
    sub = "✅" if user_id in subs else "❌"
    if premium:
        until = u["premium_until"].strftime("%d.%m.%Y")
        await update.message.reply_text(
            f"⭐ Premium до {until}\n\n🔔 Авто-сигналы: {sub}\n👥 Рефералов: {u['refs']}\n📊 Всего запросов: {u['total_requests']}"
        )
    else:
        left = 3 - u["requests_today"]
        await update.message.reply_text(
            f"🆓 Бесплатный план\nОсталось запросов: {left}/3\n🔔 Авто-сигналы: {sub}\n👥 Рефералов: {u['refs']}\n📊 Всего запросов: {u['total_requests']}\n\n/premium — купить Premium"
        )

async def ref_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    u = get_user(user_id)
    link = f"https://t.me/tr4d3ai_bot?start={user_id}"
    await update.message.reply_text(
        f"👥 Реферальная программа\n\nТвоя ссылка:\n{link}\n\n👫 Приглашено: {u['refs']}/3\n\n🎁 Пригласи 3 друзей → получи 7 дней Premium бесплатно!"
    )

async def alert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_premium(user_id):
        await update.message.reply_text("⭐ Алерты только в Premium!\n/premium")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Формат: /alert BTC 5")
        return
    symbol = context.args[0].upper()
    try:
        threshold = float(context.args[1])
    except:
        await update.message.reply_text("Пример: /alert BTC 5")
        return
    save_alert(user_id, symbol, threshold)
    await update.message.reply_text(f"✅ Алерт установлен!\n{symbol} при изменении ±{threshold}%")

async def alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    alerts = load_alerts()
    if user_id not in alerts or not alerts[user_id]:
        await update.message.reply_text("У вас нет активных алертов.\n/alert BTC 5 — добавить")
        return
    lines = ["🔔 Ваши алерты:\n"]
    for symbol, threshold in alerts[user_id].items():
        lines.append(f"• {symbol}: ±{threshold}%")
    await update.message.reply_text("\n".join(lines))

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа")
        return
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE premium=1 AND premium_until > NOW()")
    premium_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE trial_used=1")
    trial_count = c.fetchone()[0]
    c.execute("SELECT SUM(total_requests) FROM users")
    total_req = c.fetchone()[0] or 0
    c.execute("SELECT COUNT(*) FROM subscribers")
    subs_count = c.fetchone()[0]
    conn.close()
    await update.message.reply_text(
        f"👑 Админ-панель SignalBot\n\n"
        f"👥 Пользователей: {total_users}\n"
        f"⭐ Premium: {premium_count}\n"
        f"🎁 Пробный: {trial_count}\n"
        f"📊 Запросов: {total_req}\n"
        f"🔔 Подписчиков: {subs_count}\n\n"
        f"/addpremium ID 30\n/broadcast текст\n/userinfo ID"
    )

async def addpremium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Формат: /addpremium ID дней")
        return
    try:
        target_id = int(context.args[0])
        days = int(context.args[1])
        activate_premium(target_id, days)
        await update.message.reply_text(f"✅ Premium активирован для {target_id} на {days} дней")
        await context.bot.send_message(chat_id=target_id, text=f"⭐ Вам активирован Premium на {days} дней!")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Формат: /broadcast текст")
        return
    text = " ".join(context.args)
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users_list = [r[0] for r in c.fetchall()]
    conn.close()
    sent = 0
    for uid in users_list:
        try:
            await context.bot.send_message(chat_id=uid, text=f"📢 Объявление SignalBot:\n\n{text}")
            sent += 1
        except:
            pass
    await update.message.reply_text(f"✅ Отправлено {sent} пользователям")

async def userinfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Формат: /userinfo ID")
        return
    target_id = int(context.args[0])
    u = get_user(target_id)
    premium = is_premium(target_id)
    until = u["premium_until"].strftime("%d.%m.%Y") if premium and u["premium_until"] else "нет"
    await update.message.reply_text(
        f"👤 Пользователь {target_id}\n\n⭐ Premium: {'да до ' + until if premium else 'нет'}\n🎁 Пробный: {'да' if u.get('trial_used') else 'нет'}\n📊 Запросов: {u['total_requests']}\n👥 Рефералов: {u['refs']}\n📅 Рег: {u['joined'].strftime('%d.%m.%Y')}"
    )

async def premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    u = get_user(user_id)
    trial_text = "🎁 Новым пользователям — 1 день бесплатно!\n\n" if not u.get("trial_used") else ""
    keyboard = [
        [InlineKeyboardButton(f"⭐ 30 дней — {PREMIUM_STARS_30} Stars", callback_data="buy_stars_30")],
        [InlineKeyboardButton(f"⭐ 90 дней — {PREMIUM_STARS_90} Stars", callback_data="buy_stars_90")],
    ]
    await update.message.reply_text(
        f"⭐ Premium подписка\n\n{trial_text}"
        f"✅ Безлимитные запросы\n✅ Авто-сигналы каждые 4 часа\n✅ Алерты\n✅ ИИ-анализ\n✅ MACD, Фибоначчи, Прогноз\n✅ Funding Rate\n\n"
        f"💰 30 дней — {PREMIUM_STARS_30} Stars\n💰 90 дней — {PREMIUM_STARS_90} Stars\n\nОплата через Telegram Stars — мгновенно! 🚀",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if query.data.startswith("price_"):
        symbol = query.data.replace("price_", "")
        if not can_use(user_id):
            await query.message.reply_text("Лимит исчерпан! /premium")
            return
        p = get_price(symbol)
        c = get_change(symbol)
        use_request(user_id)
        if p:
            arrow = "📈" if c and c > 0 else "📉"
            await query.message.reply_text(f"💰 {symbol}/USDT: ${p:,.2f}\n{arrow} {c:+.2f}%")
    elif query.data.startswith("signal_"):
        symbol = query.data.replace("signal_", "")
        if not can_use(user_id):
            await query.message.reply_text("Лимит исчерпан! /premium")
            return
        await query.message.reply_text(f"⏳ Генерирую сигнал для {symbol}...")
        p = get_price(symbol)
        c = get_change(symbol)
        use_request(user_id)
        sig = get_ai_signal(symbol, p, c or 0)
        await query.message.reply_text(f"💰 {symbol}: ${p:,.2f}\n\n{sig}")
    elif query.data.startswith("asignal_"):
        symbol = query.data.replace("asignal_", "")
        if not can_use(user_id):
            await query.message.reply_text("Лимит исчерпан! /premium")
            return
        await query.message.reply_text(f"⏳ Анализирую {symbol}...")
        klines = get_klines(symbol, interval="4h", limit=100)
        closes = [float(x[4]) for x in klines]
        price = closes[-1]
        change = get_change(symbol) or 0
        rsi = calc_rsi(closes) or 50
        upper, mid, lower = calc_bollinger(closes)
        use_request(user_id)
        lang = get_user_lang(user_id)
        sig = get_advanced_ai_signal(symbol, price, change, rsi, upper, mid, lower, "4h", lang)
        await query.message.reply_text(f"🎯 {symbol} | ${price:,.2f}\n\n{sig}")
    elif query.data == "buy_stars_30":
        await context.bot.send_invoice(
            chat_id=user_id, title="⭐ Premium 30 дней",
            description="Безлимитные сигналы, алерты, ИИ-анализ на 30 дней",
            payload="premium_30", currency="XTR",
            prices=[LabeledPrice("Premium 30 дней", PREMIUM_STARS_30)],
        )
    elif query.data == "buy_stars_90":
        await context.bot.send_invoice(
            chat_id=user_id, title="⭐ Premium 90 дней",
            description="Безлимитные сигналы, алерты, ИИ-анализ на 90 дней",
            payload="premium_90", currency="XTR",
            prices=[LabeledPrice("Premium 90 дней", PREMIUM_STARS_90)],
        )

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload
    days = 90 if payload == "premium_90" else 30
    activate_premium(user_id, days)
    u = get_user(user_id)
    until = u["premium_until"].strftime("%d.%m.%Y")
    await update.message.reply_text(f"🎉 Оплата прошла!\n\n⭐ Premium на {days} дней\n📅 До: {until}\n\nТеперь у тебя безлимитные запросы! 🚀")
    try:
        stars = update.message.successful_payment.total_amount
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"💰 Новая оплата!\nПользователь: {user_id}\nPremium: {days} дней\nСумма: {stars} Stars")
    except:
        pass

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_use(user_id):
        await update.message.reply_text("Лимит исчерпан! /premium")
        return
    if not context.args:
        await update.message.reply_text("Пример: /price BTC")
        return
    symbol = context.args[0].upper()
    p = get_price(symbol)
    c = get_change(symbol)
    use_request(user_id)
    if p:
        arrow = "📈" if c and c > 0 else "📉"
        await update.message.reply_text(f"💰 {symbol}/USDT: ${p:,.2f}\n{arrow} {c:+.2f}%")
    else:
        await update.message.reply_text(f"Ошибка получения цены {symbol}")

async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_use(user_id):
        await update.message.reply_text("Лимит исчерпан! /premium")
        return
    await update.message.reply_text("⏳ Загружаю топ 10 монет...")
    use_request(user_id)
    lines = ["🏆 Топ 10 монет:\n"]
    for i, coin in enumerate(COINS, 1):
        p = get_price(coin)
        c = get_change(coin)
        if p:
            arrow = "📈" if c and c > 0 else "📉"
            lines.append(f"{i}. {coin}: ${p:,.2f} {arrow} {c:+.2f}%")
    await update.message.reply_text("\n".join(lines))

async def fear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value, label = get_fear_index()
    if value:
        emoji = "😱" if int(value) < 25 else "😨" if int(value) < 50 else "😊" if int(value) < 75 else "🤑"
        await update.message.reply_text(f"🧠 Индекс страха и жадности\n\n{emoji} Значение: {value}/100\n📊 {label}")
    else:
        await update.message.reply_text("Не удалось получить индекс")

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_use(user_id):
        await update.message.reply_text("Лимит исчерпан! /premium")
        return
    if not context.args:
        await update.message.reply_text("Пример: /signal BTC")
        return
    symbol = context.args[0].upper()
    await update.message.reply_text(f"⏳ Генерирую сигнал для {symbol}...")
    p = get_price(symbol)
    c = get_change(symbol)
    use_request(user_id)
    sig = get_ai_signal(symbol, p, c or 0)
    await update.message.reply_text(f"💰 {symbol}: ${p:,.2f}\n\n{sig}")

async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_use(user_id):
        await update.message.reply_text("Лимит исчерпан! /premium")
        return
    if not context.args:
        await update.message.reply_text("Пример: /analyze BTC")
        return
    symbol = context.args[0].upper()
    await update.message.reply_text(f"⏳ Анализирую {symbol}...")
    p = get_price(symbol)
    c = get_change(symbol)
    use_request(user_id)
    try:
        prompt = f"Сделай полный анализ {symbol}/USDT. Цена: ${p:,.2f}, изменение 24ч: {c:+.2f}%. Тренд, поддержка/сопротивление, рекомендация, риски. На русском, кратко."
        response = model.generate_content(prompt)
        await update.message.reply_text(response.text)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def news_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_use(user_id):
        await update.message.reply_text("Лимит исчерпан! /premium")
        return
    await update.message.reply_text("⏳ Загружаю обзор рынка...")
    use_request(user_id)
    try:
        btc = get_price("BTC")
        eth = get_price("ETH")
        btc_c = get_change("BTC")
        eth_c = get_change("ETH")
        fear_val, fear_label = get_fear_index()
        prompt = f"""Напиши краткую сводку крипторынка на русском.
BTC: ${btc:,.2f} ({btc_c:+.2f}%), ETH: ${eth:,.2f} ({eth_c:+.2f}%)
Индекс страха/жадности: {fear_val} ({fear_label})
3-4 пункта что важно знать трейдеру сейчас."""
        response = model.generate_content(prompt).text
        await update.message.reply_text(f"📰 Обзор рынка\n{datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n{response}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def autosignal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    subs = load_subscribers()
    if user_id in subs:
        save_subscriber(user_id, add=False)
        await update.message.reply_text("🔕 Авто-сигналы отключены")
    else:
        save_subscriber(user_id, add=True)
        await update.message.reply_text("✅ Авто-сигналы включены!\nБуду присылать обзор каждые 4 часа 🤖")

async def portfolio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        portfolio = get_portfolio(user_id)
        if portfolio:
            lines = ["💼 Твой портфель:\n"]
            total = 0
            for coin, amount in portfolio.items():
                p = get_price(coin)
                if p:
                    value = p * amount
                    total += value
                    lines.append(f"• {coin}: {amount} = ${value:,.2f}")
            lines.append(f"\n💰 Итого: ${total:,.2f}")
            await update.message.reply_text("\n".join(lines))
        else:
            await update.message.reply_text("Портфель пустой!\n/portfolio BTC 0.5 — добавить монету")
        return
    if len(context.args) >= 2:
        coin = context.args[0].upper()
        try:
            amount = float(context.args[1])
            save_portfolio(user_id, coin, amount)
            p = get_price(coin)
            value = p * amount if p else 0
            await update.message.reply_text(f"✅ {amount} {coin} = ${value:,.2f}")
        except:
            await update.message.reply_text("Пример: /portfolio BTC 0.5")

TRANSLATIONS = {
    "ru": {
        "signal_title": "🎯 Торговый сигнал", "action": "Действие", "entry": "Вход",
        "stop": "Стоп-лосс", "take1": "Тейк-профит 1", "take2": "Тейк-профит 2",
        "rsi": "RSI(14)", "bb_pos": "Позиция в BB", "timeframe": "Таймфрейм",
        "confidence": "Уверенность", "reason": "Причина", "limit_msg": "Лимит исчерпан! /premium",
        "premium_only": "Только в Premium! /premium", "loading": "⏳ Анализирую",
        "backtest_correct": "Верных сигналов", "backtest_total": "Всего проверено",
        "backtest_accuracy": "Точность", "leaderboard_title": "🏆 Лидерборд трейдеров",
        "diary_added": "✅ Сделка записана!", "diary_empty": "Дневник пуст. Добавь: /diary BTC купил 50000 0.1",
        "diary_title": "📅 Дневник сделок",
    },
    "en": {
        "signal_title": "🎯 Trading Signal", "action": "Action", "entry": "Entry",
        "stop": "Stop-loss", "take1": "Take-profit 1", "take2": "Take-profit 2",
        "rsi": "RSI(14)", "bb_pos": "BB Position", "timeframe": "Timeframe",
        "confidence": "Confidence", "reason": "Reason", "limit_msg": "Limit reached! /premium",
        "premium_only": "Premium only! /premium", "loading": "⏳ Analyzing",
        "backtest_correct": "Correct signals", "backtest_total": "Total checked",
        "backtest_accuracy": "Accuracy", "leaderboard_title": "🏆 Trader Leaderboard",
        "diary_added": "✅ Trade saved!", "diary_empty": "Diary is empty. Add: /diary BTC buy 50000 0.1",
        "diary_title": "📅 Trade Diary",
    }
}

def t(user_id, key):
    lang = get_user_lang(user_id)
    return TRANSLATIONS.get(lang, TRANSLATIONS["ru"]).get(key, key)

def get_user_lang(user_id):
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT lang FROM user_settings WHERE user_id=%s", (user_id,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else "ru"
    except:
        return "ru"

def set_user_lang(user_id, lang):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""INSERT INTO user_settings (user_id, lang) VALUES (%s, %s)
                 ON CONFLICT (user_id) DO UPDATE SET lang=%s""", (user_id, lang, lang))
    conn.commit()
    conn.close()

def get_klines(symbol, interval="1d", limit=100):
    try:
        r = requests.get(
            f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval={interval}&limit={limit}",
            timeout=10)
        return r.json()
    except:
        return []

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)

def calc_bollinger(closes, period=20, k=2):
    if len(closes) < period:
        return None, None, None
    slice_ = closes[-period:]
    mid = sum(slice_) / period
    std = (sum((x - mid) ** 2 for x in slice_) / period) ** 0.5
    return round(mid + k * std, 2), round(mid, 2), round(mid - k * std, 2)

def calc_ema(data, period):
    k = 2 / (period + 1)
    result = [data[0]]
    for price in data[1:]:
        result.append(price * k + result[-1] * (1 - k))
    return result

def calc_macd(closes):
    if len(closes) < 35:
        return None, None, None
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
    signal_line = calc_ema(macd_line, 9)
    return round(macd_line[-1], 4), round(signal_line[-1], 4), round(macd_line[-1] - signal_line[-1], 4)

def bb_position_label(price, upper, mid, lower, lang="ru"):
    if price >= upper:
        return "выше верхней полосы 🔴" if lang == "ru" else "above upper band 🔴"
    elif price >= mid:
        return "выше средней линии 🟡" if lang == "ru" else "above midline 🟡"
    elif price >= lower:
        return "ниже средней линии 🟡" if lang == "ru" else "below midline 🟡"
    else:
        return "ниже нижней полосы 🟢" if lang == "ru" else "below lower band 🟢"

def get_advanced_ai_signal(symbol, price, change, rsi, upper, mid, lower, timeframe, lang="ru"):
    bb_pos = bb_position_label(price, upper, mid, lower, lang)
    if lang == "en":
        prompt = f"""You are an expert crypto trader. Give a trading signal for {symbol}/USDT on {timeframe} timeframe.
Price: ${price:,.2f}, 24h change: {change:+.2f}%
RSI(14): {rsi}
Bollinger Bands: Upper ${upper:,.2f} | Mid ${mid:,.2f} | Lower ${lower:,.2f}
Price position: {bb_pos}
Reply in this format:
🎯 Signal: BUY / SELL / HOLD
📊 Confidence: X%
💰 Entry: $X
🛑 Stop-loss: $X
🎯 Take-profit 1: $X
🎯 Take-profit 2: $X
⚡ Reason: one sentence
⏱ Horizon: short-term / mid-term"""
    else:
        prompt = f"""Ты опытный криптотрейдер. Дай торговый сигнал для {symbol}/USDT на таймфрейме {timeframe}.
Цена: ${price:,.2f}, изменение 24ч: {change:+.2f}%
RSI(14): {rsi}
Bollinger Bands: Верхняя ${upper:,.2f} | Средняя ${mid:,.2f} | Нижняя ${lower:,.2f}
Позиция цены: {bb_pos}
Ответь в формате:
🎯 Сигнал: ПОКУПАТЬ / ПРОДАВАТЬ / ДЕРЖАТЬ
📊 Уверенность: X%
💰 Вход: $X
🛑 Стоп-лосс: $X
🎯 Тейк-профит 1: $X
🎯 Тейк-профит 2: $X
⚡ Причина: одно предложение
⏱ Горизонт: краткосрочный / среднесрочный"""
    try:
        return model.generate_content(prompt).text
    except Exception as e:
        return f"Ошибка ИИ: {e}"

async def advanced_signal_cmd(update, context):
    user_id = update.effective_user.id
    if not can_use(user_id):
        await update.message.reply_text(t(user_id, "limit_msg"))
        return
    if not context.args:
        await update.message.reply_text("Формат: /asignal BTC [1h|4h|1d]")
        return
    symbol = context.args[0].upper()
    timeframe = context.args[1] if len(context.args) > 1 else "1d"
    if timeframe not in ["1h", "4h", "1d"]:
        timeframe = "1d"
    await update.message.reply_text(f"{t(user_id, 'loading')} {symbol} ({timeframe})...")
    klines = get_klines(symbol, interval=timeframe, limit=100)
    if not klines:
        await update.message.reply_text("Ошибка: нет данных")
        return
    closes = [float(x[4]) for x in klines]
    price = closes[-1]
    change = get_change(symbol) or 0
    rsi = calc_rsi(closes) or 50
    upper, mid, lower = calc_bollinger(closes)
    use_request(user_id)
    lang = get_user_lang(user_id)
    sig = get_advanced_ai_signal(symbol, price, change, rsi, upper, mid, lower, timeframe, lang)
    header = (f"🎯 {symbol}/USDT | ⏱ {timeframe} | 💰 ${price:,.2f}\n"
              f"📊 RSI: {rsi} | BB: {bb_position_label(price, upper, mid, lower, lang)}\n\n")
    await update.message.reply_text(header + sig)

async def backtest_cmd(update, context):
    user_id = update.effective_user.id
    if not is_premium(user_id):
        await update.message.reply_text(t(user_id, "premium_only"))
        return
    if not context.args:
        await update.message.reply_text("Формат: /backtest BTC [1h|4h|1d]")
        return
    symbol = context.args[0].upper()
    timeframe = context.args[1] if len(context.args) > 1 else "1d"
    await update.message.reply_text(f"⏳ Бэктест {symbol} ({timeframe})...")
    klines = get_klines(symbol, interval=timeframe, limit=60)
    if len(klines) < 20:
        await update.message.reply_text("Недостаточно данных")
        return
    closes = [float(x[4]) for x in klines]
    correct, total, results = 0, 0, []
    for i in range(15, len(closes) - 1):
        window = closes[:i + 1]
        rsi = calc_rsi(window)
        if rsi is None:
            continue
        price_now, price_next = closes[i], closes[i + 1]
        change_pct = (price_next - price_now) / price_now * 100
        if rsi < 35:
            signal, correct_if = "BUY", change_pct > 0
        elif rsi > 65:
            signal, correct_if = "SELL", change_pct < 0
        else:
            continue
        total += 1
        if correct_if:
            correct += 1
        results.append((signal, rsi, change_pct, correct_if))
    if total == 0:
        await update.message.reply_text("Нет сигналов для анализа")
        return
    accuracy = correct / total * 100
    emoji = "🟢" if accuracy >= 60 else "🟡" if accuracy >= 50 else "🔴"
    lines = [f"📉 Бэктест {symbol} ({timeframe})\n",
             f"Всего проверено: {total}",
             f"Верных: {correct}",
             f"Точность: {emoji} {accuracy:.1f}%\n",
             "Последние сигналы:"]
    for sig, rsi_val, chg, ok in results[-5:]:
        lines.append(f"{'✅' if ok else '❌'} {sig} | RSI {rsi_val} | {chg:+.2f}%")
    await update.message.reply_text("\n".join(lines))

async def indicator_alert_cmd(update, context):
    user_id = update.effective_user.id
    if not is_premium(user_id):
        await update.message.reply_text(t(user_id, "premium_only"))
        return
    if len(context.args) < 3:
        await update.message.reply_text(
            "Формат:\n/ialert BTC RSI 30\n/ialert BTC RSI 70 above\n/ialert ETH MACD cross")
        return
    symbol = context.args[0].upper()
    indicator = context.args[1].upper()
    direction = "cross" if indicator == "MACD" else "below"
    level = 0.0 if indicator == "MACD" else float(context.args[2])
    if len(context.args) >= 4 and context.args[3].lower() == "above":
        direction = "above"
    conn = get_conn()
    c = conn.cursor()
    c.execute("""INSERT INTO indicator_alerts (user_id, symbol, indicator, level, direction)
                 VALUES (%s,%s,%s,%s,%s)
                 ON CONFLICT (user_id, symbol, indicator) DO UPDATE SET level=%s, direction=%s""",
              (user_id, symbol, indicator, level, direction, level, direction))
    conn.commit()
    conn.close()
    desc = f"{indicator} {'≤' if direction == 'below' else '≥' if direction == 'above' else 'cross'} {level if indicator != 'MACD' else ''}"
    await update.message.reply_text(f"✅ Алерт установлен!\n{symbol}: {desc}")

async def check_indicator_alerts(app):
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT user_id, symbol, indicator, level, direction FROM indicator_alerts")
        rows = c.fetchall()
        conn.close()
        checked = {}
        for user_id, symbol, indicator, level, direction in rows:
            key = (symbol, indicator)
            if key not in checked:
                klines = get_klines(symbol, interval="1h", limit=50)
                checked[key] = [float(x[4]) for x in klines]
            closes = checked[key]
            if not closes:
                continue
            triggered, msg = False, ""
            if indicator == "RSI":
                rsi = calc_rsi(closes)
                if rsi is None:
                    continue
                if direction == "below" and rsi <= level:
                    triggered, msg = True, f"🔔 RSI Алерт!\n{symbol}: RSI = {rsi} ≤ {level}\nЦена: ${closes[-1]:,.2f}"
                elif direction == "above" and rsi >= level:
                    triggered, msg = True, f"🔔 RSI Алерт!\n{symbol}: RSI = {rsi} ≥ {level}\nЦена: ${closes[-1]:,.2f}"
            elif indicator == "MACD":
                _, _, hist = calc_macd(closes)
                _, _, hist_prev = calc_macd(closes[:-1])
                if hist is not None and hist_prev is not None:
                    if hist_prev < 0 < hist:
                        triggered, msg = True, f"🔔 MACD Алерт!\n{symbol}: MACD пересёк вверх ↑\nЦена: ${closes[-1]:,.2f}"
                    elif hist_prev > 0 > hist:
                        triggered, msg = True, f"🔔 MACD Алерт!\n{symbol}: MACD пересёк вниз ↓\nЦена: ${closes[-1]:,.2f}"
            if triggered:
                try:
                    await app.bot.send_message(chat_id=user_id, text=msg)
                except:
                    pass
    except Exception as e:
        logger.error(f"check_indicator_alerts error: {e}")

async def leaderboard_cmd(update, context):
    user_id = update.effective_user.id
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id, total_requests FROM users ORDER BY total_requests DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Пока нет данных")
        return
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines = ["🏆 Лидерборд трейдеров\n"]
    for i, (uid, reqs) in enumerate(rows):
        marker = " ← ты" if uid == user_id else ""
        lines.append(f"{medals[i]} #{i+1}  ID:{uid}  — {reqs} запросов{marker}")
    ids = [r[0] for r in rows]
    if user_id not in ids:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users WHERE total_requests > (SELECT total_requests FROM users WHERE user_id=%s)", (user_id,))
        rank = c.fetchone()[0] + 1
        c.execute("SELECT total_requests FROM users WHERE user_id=%s", (user_id,))
        my_reqs = c.fetchone()
        conn.close()
        if my_reqs:
            lines.append(f"\n📍 Твоя позиция: #{rank} ({my_reqs[0]} запросов)")
    await update.message.reply_text("\n".join(lines))

async def diary_cmd(update, context):
    user_id = update.effective_user.id
    if not context.args:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT symbol, action, price, amount, created_at FROM trades WHERE user_id=%s ORDER BY created_at DESC LIMIT 10", (user_id,))
        rows = c.fetchall()
        conn.close()
        if not rows:
            await update.message.reply_text("Дневник пуст. Добавь: /diary BTC купил 50000 0.1")
            return
        lines = ["📅 Дневник сделок\n"]
        total_pnl = 0
        for symbol, action, price, amount, created_at in rows:
            current = get_price(symbol)
            pnl_str = ""
            if current and action.lower() in ["купил", "buy", "bought"]:
                pnl = (current - price) * amount
                total_pnl += pnl
                pnl_str = f" | P&L: ${pnl:+,.2f}"
            date_str = created_at.strftime("%d.%m %H:%M") if created_at else ""
            lines.append(f"• {date_str} {action.upper()} {amount} {symbol} @ ${price:,.2f}{pnl_str}")
        if total_pnl != 0:
            lines.append(f"\n{'💰' if total_pnl > 0 else '📉'} Итого P&L: ${total_pnl:+,.2f}")
        await update.message.reply_text("\n".join(lines))
        return
    if len(context.args) < 4:
        await update.message.reply_text("Формат: /diary BTC купил 50000 0.1")
        return
    try:
        symbol, action, price, amount = context.args[0].upper(), context.args[1], float(context.args[2]), float(context.args[3])
    except:
        await update.message.reply_text("Проверь формат: /diary BTC купил 50000 0.1")
        return
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO trades (user_id, symbol, action, price, amount) VALUES (%s,%s,%s,%s,%s)",
              (user_id, symbol, action, price, amount))
    conn.commit()
    conn.close()
    current = get_price(symbol)
    pnl_str = f"\n📊 Текущий P&L: ${(current - price) * amount:+,.2f}" if current and action.lower() in ["купил", "buy"] else ""
    await update.message.reply_text(f"✅ Сделка записана!\n{action.upper()} {amount} {symbol} @ ${price:,.2f}{pnl_str}")

async def lang_cmd(update, context):
    user_id = update.effective_user.id
    new_lang = "en" if get_user_lang(user_id) == "ru" else "ru"
    set_user_lang(user_id, new_lang)
    await update.message.reply_text("🌐 Language changed to English 🇬🇧" if new_lang == "en" else "🌐 Язык изменён на Русский 🇷🇺")

def main():
    init_db()
    app = Application.builder().token(TOKEN).build()
    scheduler = BackgroundScheduler()
    scheduler.add_job(lambda: app.create_task(send_auto_signals(app)), "interval", hours=4)
    scheduler.add_job(lambda: app.create_task(check_alerts(app)), "interval", minutes=15)
    scheduler.add_job(lambda: app.create_task(check_rsi_signals(app)), "interval", hours=6)
    scheduler.add_job(lambda: app.create_task(daily_digest(app)), "cron", hour=9, minute=0)
    scheduler.add_job(lambda: app.create_task(check_indicator_alerts(app)), "interval", minutes=30)
    scheduler.start()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("premium", premium_cmd))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("fear", fear))
    app.add_handler(CommandHandler("signal", signal))
    app.add_handler(CommandHandler("analyze", analyze))
    app.add_handler(CommandHandler("autosignal", autosignal_cmd))
    app.add_handler(CommandHandler("news", news_cmd))
    app.add_handler(CommandHandler("portfolio", portfolio_cmd))
    app.add_handler(CommandHandler("ref", ref_cmd))
    app.add_handler(CommandHandler("alert", alert_cmd))
    app.add_handler(CommandHandler("alerts", alerts_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("addpremium", addpremium_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("userinfo", userinfo_cmd))
    app.add_handler(CommandHandler("calc", calc_cmd))
    app.add_handler(CommandHandler("ta", ta_cmd))
    app.add_handler(CommandHandler("compare", compare_cmd))
    app.add_handler(CommandHandler("dominance", dominance_cmd))
    app.add_handler(CommandHandler("convert", convert_cmd))
    app.add_handler(CommandHandler("halving", halving_cmd))
    app.add_handler(CommandHandler("trending", trending_cmd))
    app.add_handler(CommandHandler("screener", screener_cmd))
    app.add_handler(CommandHandler("ob", orderbook_cmd))
    app.add_handler(CommandHandler("funding", funding_cmd))
    app.add_handler(CommandHandler("cryptonews", cryptonews_cmd))
    app.add_handler(CommandHandler("weeklytop", weekly_top_cmd))
    app.add_handler(CommandHandler("multialert", multialert_cmd))
    app.add_handler(CommandHandler("macd", macd_cmd))
    app.add_handler(CommandHandler("fib", fib_cmd))
    app.add_handler(CommandHandler("ath", ath_cmd))
    app.add_handler(CommandHandler("corr", correlation_cmd))
    app.add_handler(CommandHandler("predict", predict_cmd))
    app.add_handler(CommandHandler("whales", whales_cmd))
    app.add_handler(CommandHandler("asignal", advanced_signal_cmd))
    app.add_handler(CommandHandler("backtest", backtest_cmd))
    app.add_handler(CommandHandler("ialert", indicator_alert_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(CommandHandler("diary", diary_cmd))
    app.add_handler(CommandHandler("lang", lang_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    print("SignalBot запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()