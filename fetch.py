"""每日抓取氣象署鄉鎮預報與中央社頭條，輸出 data/brief.json。僅用標準庫。"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

TZ = timezone(timedelta(hours=8))
DISTRICTS = [
    ("新莊", "新莊區", "F-D0047-069"),
    ("大安", "大安區", "F-D0047-061"),
]
NEWS_FEEDS = [
    ("國際", "https://feeds.feedburner.com/rsscna/intworld", 8),
    ("兩岸", "https://feeds.feedburner.com/rsscna/mainland", 4),
    ("財經", "https://feeds.feedburner.com/rsscna/finance", 10),
]
NEWS_MAX_AGE_H = 30
NEWS_SKIP = ("539", "樂透", "威力彩", "賓果", "刮刮樂", "彩券", "開獎")
SUMMARY_MAX = 110
RAIN_POP = 30
UMBRELLA_POP = 40
DAY_START, DAY_END = 6, 24


def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 taiwan-daily-brief"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def to_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def series(location, key):
    """依 ElementValue 的英文鍵取出時間序列，不依賴中文欄位名稱。"""
    out = []
    for el in location["WeatherElement"]:
        for t in el["Time"]:
            ev = t["ElementValue"][0]
            if key in ev:
                start = t.get("StartTime") or t.get("DataTime")
                out.append((start, t.get("EndTime"), ev[key]))
        if out:
            return out
    return out


def parse_dt(s):
    return datetime.fromisoformat(s).astimezone(TZ)


def district_report(label, name, dataset, key, today):
    url = (
        f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/{dataset}?"
        + urllib.parse.urlencode({"Authorization": key, "format": "JSON"})
    )
    data = json.loads(http_get(url).decode("utf-8"))
    locs = data["records"]["Locations"][0]["Location"]
    loc = next(x for x in locs if x["LocationName"] == name)

    day0 = datetime(today.year, today.month, today.day, tzinfo=TZ)
    win_start = day0 + timedelta(hours=DAY_START)
    win_end = day0 + timedelta(hours=DAY_END)

    temps = []
    for s, _, v in series(loc, "Temperature"):
        t = parse_dt(s)
        n = to_num(v)
        if n is not None:
            temps.append((t, n))
    pops = series(loc, "ProbabilityOfPrecipitation")
    wx = {s: v for s, _, v in series(loc, "Weather")}

    slots = []
    for s, e, v in pops:
        a, b = parse_dt(s), parse_dt(e)
        if b <= win_start or a >= win_end:
            continue
        pop = to_num(v)
        weather = wx.get(s, "")
        in_slot = [n for t, n in temps if a <= t < b]
        slots.append(
            {
                "from": a.strftime("%H"),
                "to": "24" if b.hour == 0 and b.date() > a.date() else b.strftime("%H"),
                "pop": pop,
                "weather": weather,
                "tmin": min(in_slot) if in_slot else None,
                "tmax": max(in_slot) if in_slot else None,
            }
        )

    def is_rain(sl):
        return (sl["pop"] is not None and sl["pop"] >= RAIN_POP) or "雨" in sl["weather"]

    rain_ranges = []
    for sl in slots:
        if is_rain(sl):
            if rain_ranges and rain_ranges[-1]["to"] == sl["from"]:
                rain_ranges[-1]["to"] = sl["to"]
                rain_ranges[-1]["pop"] = max(rain_ranges[-1]["pop"] or 0, sl["pop"] or 0)
            else:
                rain_ranges.append({"from": sl["from"], "to": sl["to"], "pop": sl["pop"] or 0})

    pop_vals = [sl["pop"] for sl in slots if sl["pop"] is not None]
    lows = [sl["tmin"] for sl in slots if sl["tmin"] is not None]
    highs = [sl["tmax"] for sl in slots if sl["tmax"] is not None]
    pop_max = max(pop_vals) if pop_vals else None
    return {
        "label": label,
        "location": name,
        "dataset": dataset,
        "slots": slots,
        "rain_ranges": rain_ranges,
        "pop_max": pop_max,
        "tmin": min(lows) if lows else None,
        "tmax": max(highs) if highs else None,
        "umbrella": bool(
            (pop_max is not None and pop_max >= UMBRELLA_POP) or any("雨" in sl["weather"] for sl in slots)
        ),
    }


def fetch_news():
    """中央社官方分類 RSS；只收 NEWS_MAX_AGE_H 小時內的新聞，避免舊源混入。"""
    now = datetime.now(TZ)
    items, errors = [], []
    for category, url, limit in NEWS_FEEDS:
        try:
            root = ET.fromstring(http_get(url))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{category}: {exc}")
            continue
        taken = 0
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            pub = it.findtext("pubDate")
            if not title or not pub or any(k in title for k in NEWS_SKIP):
                continue
            published = parsedate_to_datetime(pub).astimezone(TZ)
            if now - published > timedelta(hours=NEWS_MAX_AGE_H):
                continue
            summary = re.sub(r"<[^>]+>", "", it.findtext("description") or "")
            summary = re.sub(r"^（[^）]*）", "", summary.strip()).strip()
            items.append(
                {
                    "category": category,
                    "title": title,
                    "summary": summary[:SUMMARY_MAX],
                    "published": published.isoformat(timespec="minutes"),
                    "link": (it.findtext("link") or "").strip(),
                }
            )
            taken += 1
            if taken >= limit:
                break
    items.sort(key=lambda x: x["published"], reverse=True)
    out = {"source": "中央社 RSS (feeds.feedburner.com/rsscna)", "items": items}
    if errors:
        out["errors"] = errors
    return out


MARKET_SYMBOLS = {
    "TAIEX": "^TWII",
    "SPX": "^GSPC",
    "VIX": "^VIX",
    "US10Y": "^TNX",
    "USDTWD": "TWD=X",
}


def yahoo_closes(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range=1y&interval=1d"
    r = json.loads(http_get(url, timeout=30))["chart"]["result"][0]
    pairs = [
        (ts, c)
        for ts, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"])
        if c is not None
    ]
    as_of = datetime.fromtimestamp(pairs[-1][0], TZ).strftime("%Y-%m-%d")
    return [c for _, c in pairs], as_of


def sma(vals, n):
    return sum(vals[-n:]) / n


def index_stats(closes):
    last = closes[-1]
    ma20, ma60, ma200 = sma(closes, 20), sma(closes, 60), sma(closes, 200)
    ret_1m = last / closes[-22] - 1
    ret_3m = last / closes[-64] - 1
    drawdown = last / max(closes[-252:]) - 1
    signals = {
        "above_ma200": 1 if last > ma200 else -1,
        "above_ma60": 1 if last > ma60 else -1,
        "ma20_above_ma60": 1 if ma20 > ma60 else -1,
        "ret_1m_positive": 1 if ret_1m > 0 else -1,
        "drawdown": 1 if drawdown > -0.05 else (-1 if drawdown < -0.10 else 0),
    }
    return {
        "last": round(last, 2),
        "vs_ma60_pct": round((last / ma60 - 1) * 100, 1),
        "vs_ma200_pct": round((last / ma200 - 1) * 100, 1),
        "ret_1m_pct": round(ret_1m * 100, 1),
        "ret_3m_pct": round(ret_3m * 100, 1),
        "drawdown_from_52w_high_pct": round(drawdown * 100, 1),
        "score": sum(signals.values()),
        "signals": signals,
    }


def market_outlook():
    """規則式趨勢評分：台股加權(權重2)+標普500(權重1)+VIX。非預測。"""
    data, as_of = {}, {}
    for name, sym in MARKET_SYMBOLS.items():
        data[name], as_of[name] = yahoo_closes(sym)
    taiex, spx = index_stats(data["TAIEX"]), index_stats(data["SPX"])
    vix_last = data["VIX"][-1]
    vix_score = 1 if vix_last < 18 else (-1 if vix_last > 25 else 0)
    total = 2 * taiex["score"] + spx["score"] + vix_score
    ratio = total / 16
    if ratio >= 0.55:
        label = "偏多"
    elif ratio >= 0.2:
        label = "中性偏多"
    elif ratio > -0.2:
        label = "中性"
    elif ratio > -0.55:
        label = "中性偏空"
    else:
        label = "偏空"
    y10, y10_prev = data["US10Y"][-1], data["US10Y"][-22]
    fx, fx_prev = data["USDTWD"][-1], data["USDTWD"][-22]
    y10_chg_bp = round((y10 - y10_prev) * 100)
    fx_chg_pct = round((fx / fx_prev - 1) * 100, 1)
    risk_flags = []
    if taiex["vs_ma200_pct"] > 20:
        risk_flags.append(f"台股高於年線 {taiex['vs_ma200_pct']}%，漲幅過熱")
    if y10_chg_bp >= 25:
        risk_flags.append(f"美10年期殖利率 {y10:.2f}%，一個月升 {y10_chg_bp}bp")
    if fx_chg_pct >= 1.5:
        risk_flags.append(f"台幣一個月貶值 {fx_chg_pct}%，外資匯損壓力")
    if label == "偏多" and len(risk_flags) >= 2:
        label = "中性偏多"
    return {
        "outlook": label,
        "score": total,
        "score_range": [-16, 16],
        "risk_flags": risk_flags,
        "as_of": as_of,
        "taiex": taiex,
        "spx": spx,
        "vix": {"last": round(vix_last, 2), "score": vix_score},
        "us10y": {"last_pct": round(y10, 2), "chg_1m_bp": y10_chg_bp},
        "usdtwd": {"last": round(fx, 3), "chg_1m_pct": fx_chg_pct},
        "method": "規則式趨勢評分(站上季線/年線、均線多排、近一月漲跌、距52週高點、VIX)，台股權重2、美股權重1；風險旗標(過熱、殖利率上升、台幣轉弱)達2項以上則把偏多降為中性偏多。門檻為經驗設定、未經回測驗證，為趨勢判讀而非預測，非投資建議",
    }


def build_text(districts, news):
    parts = []
    for d in districts:
        if d["rain_ranges"]:
            rr = "、".join(f"{r['from']}-{r['to']}時" for r in d["rain_ranges"])
            parts.append(f"{d['label']}{rr}雨({int(d['pop_max'])}%)")
        else:
            mp = f"{int(d['pop_max'])}%" if d["pop_max"] is not None else "?"
            parts.append(f"{d['label']}無雨(最高{mp})")
    lows = [d["tmin"] for d in districts if d["tmin"] is not None]
    highs = [d["tmax"] for d in districts if d["tmax"] is not None]
    temp = f"氣溫{int(min(lows))}-{int(max(highs))}°C" if lows and highs else "氣溫不明"
    umb = "建議帶傘" if any(d["umbrella"] for d in districts) else "不用帶傘"
    text = "；".join(["、".join(parts), temp, umb]) + "。"
    budget = 100 - len(text) - len("頭條：。")
    heads = []
    order = [c for c, _, _ in NEWS_FEEDS]
    ranked = sorted(news["items"], key=lambda x: order.index(x["category"]))
    for item in ranked[:2]:
        h = item["title"][: min(16, max(budget, 0))].rstrip(" ，、；：-")
        if len(h) >= 6:
            heads.append(h)
            budget -= len(h) + 1
    if heads:
        text += "頭條：" + "；".join(heads) + "。"
    return text


def main():
    key = os.environ.get("CWA_API_KEY")
    if not key:
        sys.exit("missing CWA_API_KEY")
    now = datetime.now(TZ)
    districts = [district_report(l, n, ds, key, now) for l, n, ds in DISTRICTS]
    news = fetch_news()
    try:
        market = market_outlook()
    except Exception as exc:  # noqa: BLE001
        market = {"error": str(exc)}
    out = {
        "generated_at": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "window": f"{DAY_START:02d}-{DAY_END:02d}",
        "brief": build_text(districts, news),
        "districts": districts,
        "news": news,
        "market": market,
    }
    os.makedirs("data", exist_ok=True)
    with open("data/brief.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(out["brief"])


if __name__ == "__main__":
    main()
