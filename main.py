import os
import logging
import threading
import time
import json
import requests
import websocket
from datetime import datetime, timedelta
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ========== CONFIG ==========
INITIAL_BALANCE = 100.0
POSITION_SIZE = 5.0
MAX_CONCURRENT_POSITIONS = 2
ENTRY_PRICE = 0.85
ENTRY_TOLERANCE = 0.002
STOP_LOSS = 0.75
TAKE_PROFIT = 0.95
MIN_SECONDS_TO_RESOLUTION = 60

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
ALLOWED_USER_IDS = [int(id) for id in os.environ.get('ALLOWED_USERS', '').split(',')]

# ========== GLOBAL STATE ==========
balance = INITIAL_BALANCE
positions = []
trades = []
current_market_id = None
market_end_time = None
active = True

# ========== FLASK SERVER (keeps Railway happy) ==========
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

# ========== HELPER FUNCTIONS ==========
def get_current_market():
    try:
        url = "https://gamma-api.polymarket.com/markets"
        params = {"slug": "btc-updown-5m", "active": True, "limit": 1}
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        if data:
            return data[0]['id']
    except:
        return None

def get_market_end_time():
    now = datetime.utcnow()
    seconds = (now.second + now.minute * 60) % 300
    if seconds == 0:
        end = now + timedelta(seconds=300)
    else:
        end = now + timedelta(seconds=300 - seconds)
    return end

def seconds_left():
    if market_end_time:
        return max(0, (market_end_time - datetime.utcnow()).total_seconds())
    return 300

# ========== TRADING ENGINE ==========
def enter_trade(side, price, secs):
    global balance, positions
    if balance < POSITION_SIZE:
        return None
    pos = {
        'market_id': current_market_id,
        'side': side,
        'entry_price': price,
        'size': POSITION_SIZE,
        'shares': POSITION_SIZE / price,
        'entry_time': datetime.utcnow(),
        'stop_loss': STOP_LOSS,
        'take_profit': TAKE_PROFIT,
        'current_price': price
    }
    positions.append(pos)
    balance -= POSITION_SIZE
    return pos

def close_trade(pos, exit_price, reason):
    global balance, trades
    if pos['side'] == 'YES':
        pnl = (exit_price - pos['entry_price']) * pos['shares']
    else:
        pnl = (pos['entry_price'] - exit_price) * pos['shares']
    balance += pos['size'] + pnl
    record = {**pos, 'exit_price': exit_price, 'pnl': pnl, 'reason': reason}
    trades.append(record)
    positions.remove(pos)
    return record

def on_message(ws, message):
    global current_market_id, market_end_time
    data = json.loads(message)
    if 'market' in data and data['market'] != current_market_id:
        current_market_id = data['market']
        market_end_time = get_market_end_time()
    if 'bids' in data and 'asks' in data:
        bids = data['bids']
        asks = data['asks']
        secs = seconds_left()
        if secs <= MIN_SECONDS_TO_RESOLUTION and len(positions) < MAX_CONCURRENT_POSITIONS:
            if asks and abs(float(asks[0]['price']) - ENTRY_PRICE) <= ENTRY_TOLERANCE:
                enter_trade('YES', float(asks[0]['price']), secs)
            if bids and abs(float(bids[0]['price']) - ENTRY_PRICE) <= ENTRY_TOLERANCE:
                enter_trade('NO', float(bids[0]['price']), secs)
        # Update positions (simplified)
        for pos in positions[:]:
            # For simplicity, we'll just keep them; exit logic omitted for brevity
            pass

def on_error(ws, error):
    pass

def on_close(ws, *args):
    time.sleep(5)
    connect_ws()

def on_open(ws):
    if current_market_id:
        ws.send(json.dumps({"method": "subscribe", "params": [current_market_id]}))

def connect_ws():
    ws = websocket.WebSocketApp("wss://ws-subscriptions-clob.polymarket.com/ws/market",
                                on_open=on_open, on_message=on_message,
                                on_error=on_error, on_close=on_close)
    ws.run_forever()

def start_ws():
    while True:
        try:
            connect_ws()
        except:
            time.sleep(5)

threading.Thread(target=start_ws, daemon=True).start()

# ========== TELEGRAM HANDLERS ==========
def restricted(func):
    async def wrapper(update, context):
        if update.effective_user.id not in ALLOWED_USER_IDS:
            await update.message.reply_text("⛔ No access")
            return
        return await func(update, context)
    return wrapper

@restricted
async def start(update, context):
    msg = "🤖 Paper Trading Bot\n/status – Balance\n/history – Trades\n/reset – Restart"
    await update.message.reply_text(msg)

@restricted
async def status(update, context):
    global balance, positions, trades
    total_pnl = balance - INITIAL_BALANCE
    win_rate = 0
    if trades:
        wins = sum(1 for t in trades if t['pnl'] > 0)
        win_rate = wins/len(trades)*100
    text = f"💰 Balance: ${balance:.2f}\n📊 P&L: {'+'if total_pnl>0 else ''}{total_pnl:.2f}\n📈 Open: {len(positions)}\n🎯 Win rate: {win_rate:.1f}%"
    await update.message.reply_text(text)

@restricted
async def history(update, context):
    if not trades:
        await update.message.reply_text("No trades yet.")
        return
    text = "Recent trades:\n"
    for t in trades[-5:]:
        text += f"{'✅' if t['pnl']>0 else '❌'} ${t['pnl']:.2f}\n"
    await update.message.reply_text(text)

@restricted
async def reset(update, context):
    global balance, positions, trades
    balance = INITIAL_BALANCE
    positions = []
    trades = []
    await update.message.reply_text("Reset to $100.")

# ========== MAIN ==========
def run_telegram():
    app_tg = Application.builder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("start", start))
    app_tg.add_handler(CommandHandler("status", status))
    app_tg.add_handler(CommandHandler("history", history))
    app_tg.add_handler(CommandHandler("reset", reset))
    app_tg.run_polling()

threading.Thread(target=run_telegram, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
