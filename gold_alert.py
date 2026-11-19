"""
黃金（XAU/USD）警示小幫手 — 雲端版
=====================================================================
每次執行會檢查四件事，符合就推播到 Discord：
  🔺 賣價（Bid）創今日新高
  🔻 買價（Ask）創今日新低
  🟢 30 分鐘 RSI(14) 跌破 30
  🔴 30 分鐘 RSI(14) 漲破 70

資料來源：
  - Bid／Ask：Swissquote 公開報價（不用註冊、不用金鑰）
  - RSI：Twelve Data 的 30 分鐘 K 線（免費帳號，一天 800 次額度）

由 GitHub Actions 每 5 分鐘在雲端執行一次，電腦關機也照跑。
上一次的紀錄（今天最高／最低、上次什麼時候叫過）存在 state.json。
只用 Python 內建套件，不用另外安裝任何東西。

⚠️ 新高／新低是「每次檢查時的價格」互相比較，
   兩次檢查之間一閃而過的尖刺可能會漏掉。
=====================================================================
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------- 可以自己調整的設定 ----------
RSI_LEN = 14                # RSI 期數
RSI_INTERVAL = "30min"      # RSI 用的 K 線週期
RSI_UPPER = 70              # 超買線
RSI_LOWER = 30              # 超賣線
HIGH_LOW_COOLDOWN_MIN = 60  # 新高／新低：同一種提醒幾分鐘內只叫一次
SQ_PROFILE = "prime"        # Swissquote 報價等級（premium／prime／elite，價差略有不同）

# ---------- 金鑰：從環境變數讀（GitHub 加密欄位），不寫死在程式裡 ----------
TWELVE_API_KEY = os.environ.get("TWELVE_API_KEY", "")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

SQ_URL = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD"
TD_URL = ("https://api.twelvedata.com/time_series?symbol=XAU/USD"
          f"&interval={RSI_INTERVAL}&outputsize=200&timezone=UTC&apikey=")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
TAIPEI = ZoneInfo("Asia/Taipei")
NEW_YORK = ZoneInfo("America/New_York")


# =====================================================================
# 小工具
# =====================================================================
def http_json(url, headers=None, data=None):
    """送出網路請求，回傳 JSON"""
    h = {"User-Agent": "gold-alert-bot"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h, data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}


def trading_day_start(now_utc):
    """
    今天的起點 = 最近一次紐約時間 17:00（國際黃金市場的換日時間）
    換算台北時間：冬令約早上 06:00、夏令約早上 05:00，跟 MT4 的日線一致
    """
    ny_now = now_utc.astimezone(NEW_YORK)
    start = ny_now.replace(hour=17, minute=0, second=0, microsecond=0)
    if ny_now < start:
        start -= timedelta(days=1)
    return start.astimezone(timezone.utc)


def fetch_bid_ask():
    """跟 Swissquote 抓黃金現在的 Bid 和 Ask，回傳 (bid, ask, 報價時間)"""
    data = http_json(SQ_URL)
    # 優先用 Swissquote 自家平台的報價
    feeds = sorted(data, key=lambda d: d.get("topo", {}).get("platform") != "SwissquoteLtd")
    for feed in feeds:
        for p in feed.get("spreadProfilePrices", []):
            if p.get("spreadProfile") == SQ_PROFILE:
                ts = datetime.fromtimestamp(feed["ts"] / 1000, tz=timezone.utc)
                return float(p["bid"]), float(p["ask"]), ts
    # 找不到指定等級就用第一筆
    feed = feeds[0]
    p = feed["spreadProfilePrices"][0]
    return float(p["bid"]), float(p["ask"]), datetime.fromtimestamp(feed["ts"] / 1000, tz=timezone.utc)


def fetch_rsi_candles():
    """跟 Twelve Data 抓 30 分鐘 K 線，回傳由舊到新的 [(K線時間, 收盤價), ...]"""
    data = http_json(TD_URL + TWELVE_API_KEY)
    if data.get("status") != "ok":
        raise RuntimeError(f"Twelve Data 錯誤：{data.get('message')}")
    values = list(reversed(data["values"]))   # 原本是新到舊，翻成舊到新
    return [(v["datetime"], float(v["close"])) for v in values]


def calc_rsi(closes, length):
    """
    標準 RSI（Wilder 平滑法，跟 MT4／TradingView 同一種算法）
    回傳每根 K 線對應的 RSI 清單，前面資料不足的位置是 None
    """
    rsi = [None] * len(closes)
    if len(closes) <= length:
        return rsi
    gains, losses = 0.0, 0.0
    for i in range(1, length + 1):
        diff = closes[i] - closes[i - 1]
        gains += max(diff, 0)
        losses += max(-diff, 0)
    avg_gain, avg_loss = gains / length, losses / length

    def to_rsi(g, l):
        return 100.0 if l == 0 else 100 - 100 / (1 + g / l)

    rsi[length] = to_rsi(avg_gain, avg_loss)
    for i in range(length + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (length - 1) + max(diff, 0)) / length
        avg_loss = (avg_loss * (length - 1) + max(-diff, 0)) / length
        rsi[i] = to_rsi(avg_gain, avg_loss)
    return rsi


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def minutes_since(iso_text, now_utc):
    if not iso_text:
        return 10**9
    return (now_utc - datetime.fromisoformat(iso_text)).total_seconds() / 60


def send_discord(messages, now_utc):
    """把這次所有提醒合成一則訊息送到 Discord"""
    tpe = now_utc.astimezone(TAIPEI).strftime("%m/%d %H:%M")
    text = "\n".join(messages) + f"\n-# 台北時間 {tpe}"
    payload = json.dumps({"username": "黃金警示", "content": text}).encode("utf-8")
    http_json(DISCORD_WEBHOOK, data=payload, headers={"Content-Type": "application/json"})


def fmt(x):
    return f"{x:,.2f}"


# =====================================================================
# 判斷邏輯（不碰網路，方便測試）
# =====================================================================
def check_high_low(now_utc, state, bid, ask, quote_time):
    """Bid 新高、Ask 新低"""
    messages = []

    # 報價超過 15 分鐘沒更新 = 休市中（週末、每日休市時段），不判斷
    if (now_utc - quote_time).total_seconds() > 15 * 60:
        return messages

    day_key = trading_day_start(now_utc).isoformat()
    if state.get("day") != day_key:
        # 新的一天（或第一次執行）：只記錄起點，不發提醒，避免一開盤就叫
        state.update(day=day_key, bid_high=bid, ask_low=ask)
        return messages

    if bid > state["bid_high"]:
        if minutes_since(state.get("last_high_alert"), now_utc) >= HIGH_LOW_COOLDOWN_MIN:
            messages.append(f"🔺 **黃金 Sell 價創今日新高：{fmt(bid)}**（前高 {fmt(state['bid_high'])}）")
            state["last_high_alert"] = now_utc.isoformat()
        state["bid_high"] = bid

    if ask < state["ask_low"]:
        if minutes_since(state.get("last_low_alert"), now_utc) >= HIGH_LOW_COOLDOWN_MIN:
            messages.append(f"🔻 **黃金 Buy 價創今日新低：{fmt(ask)}**（前低 {fmt(state['ask_low'])}）")
            state["last_low_alert"] = now_utc.isoformat()
        state["ask_low"] = ask

    return messages


def check_rsi(state, candles):
    """30 分鐘 RSI 穿越 30／70；最後一根是還沒收完的 K 線，跟看盤軟體一樣即時變動"""
    messages = []
    closes = [c for _, c in candles]
    rsi = calc_rsi(closes, RSI_LEN)
    if len(rsi) < 2 or rsi[-1] is None or rsi[-2] is None:
        return messages

    bar_time = candles[-1][0]              # 目前這根 30 分鐘 K 線的時間
    now_rsi, prev_rsi = rsi[-1], rsi[-2]   # prev = 上一根已收盤 K 線的 RSI
    price = closes[-1]

    if prev_rsi >= RSI_LOWER > now_rsi and state.get("rsi_low_bar") != bar_time:
        messages.append(f"🟢 **黃金 30 分 RSI 跌破 {RSI_LOWER}（超賣）**：RSI {now_rsi:.1f}，價格 {fmt(price)}")
        state["rsi_low_bar"] = bar_time
    if prev_rsi <= RSI_UPPER < now_rsi and state.get("rsi_high_bar") != bar_time:
        messages.append(f"🔴 **黃金 30 分 RSI 漲破 {RSI_UPPER}（超買）**：RSI {now_rsi:.1f}，價格 {fmt(price)}")
        state["rsi_high_bar"] = bar_time

    state["last_rsi"] = round(now_rsi, 2)
    return messages


def error_notice(state, source, err, now_utc):
    """資料來源出錯時，一天最多提醒一次，避免洗版"""
    key = f"err_{source}"
    print(f"{source} 出錯：{err}")
    if minutes_since(state.get(key), now_utc) >= 24 * 60:
        state[key] = now_utc.isoformat()
        return [f"⚠️ 黃金警示的 {source} 資料抓取失敗（{str(err)[:120]}），這部分提醒暫時停擺，請告訴 Claude 檢查"]
    return []


# =====================================================================
# 主流程
# =====================================================================
def main():
    if not TWELVE_API_KEY or not DISCORD_WEBHOOK:
        sys.exit("缺少 TWELVE_API_KEY 或 DISCORD_WEBHOOK，請到 GitHub Secrets 設定")

    now_utc = datetime.now(timezone.utc)

    # 測試模式：手動執行時勾選，直接送一則測試訊息確認 Discord 收得到
    if os.environ.get("TEST_MODE") == "true":
        bid, ask, _ = fetch_bid_ask()
        candles = fetch_rsi_candles()
        rsi = calc_rsi([c for _, c in candles], RSI_LEN)[-1]
        send_discord([f"✅ 黃金警示測試成功！目前 Sell {fmt(bid)}／Buy {fmt(ask)}／30 分 RSI {rsi:.1f}"], now_utc)
        print("測試訊息已送出")
        return

    state = load_state()
    messages = []

    # 兩個資料來源分開處理：一個壞掉，另一個照常運作
    try:
        bid, ask, qt = fetch_bid_ask()
        messages += check_high_low(now_utc, state, bid, ask, qt)
        state.pop("err_Swissquote", None)
    except Exception as e:
        messages += error_notice(state, "Swissquote", e, now_utc)

    try:
        messages += check_rsi(state, fetch_rsi_candles())
        state.pop("err_TwelveData", None)
    except Exception as e:
        messages += error_notice(state, "TwelveData", e, now_utc)

    if messages:
        send_discord(messages, now_utc)
        print("已推播：\n" + "\n".join(messages))
    else:
        print(f"沒有觸發。今日 Bid 高 {state.get('bid_high')}／Ask 低 {state.get('ask_low')}／RSI {state.get('last_rsi')}")

    save_state(state)


if __name__ == "__main__":
    main()
