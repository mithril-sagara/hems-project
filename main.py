import os, socket, time, threading, requests, json, hmac, hashlib, base64, uuid, calendar, math
from flask import Flask, jsonify, render_template_string, request
from datetime import datetime, timedelta
import pytz
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

# --- 設定 ---
IP = os.environ.get("ECHONET_IP", "192.168.0.146")
INFLUX_URL = os.environ.get("INFLUX_URL", "http://influxdb:8086")
INFLUX_TOKEN = os.environ.get("INFLUX_TOKEN")
INFLUX_ORG = os.environ.get("INFLUX_ORG")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET")
SB_TOKEN = os.environ.get("SB_TOKEN")
SB_SECRET = os.environ.get("SB_SECRET")

jst = pytz.timezone('Asia/Tokyo')

COEFF_MAP = {1: 1.72, 2: 1.72, 3: 1.62, 4: 1.56, 5: 1.50, 6: 1.40, 7: 1.30, 8: 1.30, 9: 1.40, 10: 1.56, 11: 1.65, 12: 1.72}
PANEL_CAPACITY = 5.9

ai_ratio = 1.0
last_learned_date = ""

def get_ai_adjustment():
    global ai_ratio, last_learned_date
    yesterday = (datetime.now(jst) - timedelta(days=1)).strftime("%Y-%m-%d")
    if last_learned_date == yesterday: return ai_ratio
    try:
        client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
        start = jst.localize(datetime.strptime(yesterday, "%Y-%m-%d"))
        stop = start + timedelta(days=1)
        query = f'from(bucket:"{INFLUX_BUCKET}") |> range(start: {start.isoformat()}, stop: {stop.isoformat()}) |> filter(fn: (r) => r._field == "solar") |> aggregateWindow(every: 1d, fn: mean, createEmpty: false)'
        result = client.query_api().query(query)
        measured_kwh = (result[0].records[0].get_value() / 1000.0) * 24 if result else 0
        w_url = f"https://api.open-meteo.com/v1/forecast?latitude=33.45&longitude=130.53&hourly=shortwave_radiation&timezone=Asia%2FTokyo&start_date={yesterday}&end_date={yesterday}"
        w_res = requests.get(w_url).json()
        m = int(yesterday.split("-")[1])
        base_c = COEFF_MAP.get(m, 1.1)
        theo_kwh = sum([min((w / 1000.0) * PANEL_CAPACITY * base_c, PANEL_CAPACITY) for w in w_res['hourly']['shortwave_radiation']])
        if theo_kwh > 0.5:
            daily_ratio = measured_kwh / theo_kwh
            ai_ratio = (ai_ratio * 0.8) + (daily_ratio * 0.2)
            ai_ratio = max(0.5, min(1.5, ai_ratio))
        last_learned_date = yesterday
    except: pass
    return ai_ratio

app = Flask(__name__)

def get_unit_price(dt):
    # 電化でナイト・セレクト21 (夜間 21-7時)
    if dt.hour >= 21 or dt.hour < 7: return 16.60
    # 昼間 (夏季 7,8,9月は高い)
    return 33.80 if dt.month in [7, 8, 9] else 28.60

def fetch_echonet(eoj, epc):
    try:
        frame = bytes([0x10, 0x81, 0x00, 0x01, 0x05, 0xff, 0x01, eoj[0], eoj[1], eoj[2], 0x62, 0x01, epc, 0x00])
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(("", 3610)); s.settimeout(0.8)
            s.sendto(frame, (IP, 3610))
            data, _ = s.recvfrom(1024)
            idx = data.find(bytes([epc]))
            return data[idx+2 : idx+2+data[idx+1]]
    except: return None

latest_instant = {"solar": 0, "buy": 0, "sell": 0, "home": 0}

def collector():
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)
    while True:
        try:
            res_s = fetch_echonet([0x02, 0x79, 0x01], 0xE0)
            res_m = fetch_echonet([0x02, 0xA5, 0x01], 0xF5)
            solar = int.from_bytes(res_s, "big", signed=True) if res_s else 0
            buy, sell = 0, 0
            if res_m and len(res_m) >= 8:
                val = int.from_bytes(res_m[0:4], "big", signed=True)
                if val >= 0: sell = val; buy = 0
                else: sell = 0; buy = abs(val)
            home = max(0, solar + buy - sell)
            latest_instant.update({"solar": solar, "buy": buy, "sell": sell, "home": home})
            p = Point("energy").field("solar", float(solar)).field("buy", float(buy)).field("sell", float(sell)).field("home", float(home))
            write_api.write(bucket=INFLUX_BUCKET, record=p)
        except: pass
        time.sleep(5)

@app.route("/api/live")
def api_live():
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    now = datetime.now(jst)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    query = f'from(bucket:"{INFLUX_BUCKET}") |> range(start: {start.isoformat()}) |> aggregateWindow(every: 1h, fn: mean, createEmpty: true)'
    sums = {"buy_k": 0.0, "sell_k": 0.0, "solar_k": 0.0, "buy_y": 0, "sell_y": 0}
    try:
        tables = client.query_api().query(query)
        for t in tables:
            fld = t.records[0].get_field()
            for i, r in enumerate(t.records):
                val = r.get_value()
                if val is not None:
                    kwh = val / 1000.0
                    if fld == "buy":
                        sums["buy_k"] += kwh
                        sums["buy_y"] += int(kwh * get_unit_price(start + timedelta(hours=i)))
                    elif fld == "sell":
                        sums["sell_k"] += kwh
                        sums["sell_y"] += int(kwh * 7.0)
                    elif fld == "solar":
                        sums["solar_k"] += kwh
    except: pass
    res = latest_instant.copy()
    res.update({"d_buy_k": round(sums["buy_k"], 2), "d_buy_y": sums["buy_y"], "d_sell_k": round(sums["sell_k"], 2), "d_sell_y": sums["sell_y"], "d_solar_t": round(sums["solar_k"], 2), "ai_ratio": round(get_ai_adjustment(), 3)})
    return jsonify(res)

@app.route("/api/history")
def api_history():
    u = request.args.get("unit", "day")
    d_str = request.args.get("date", datetime.now(jst).strftime("%Y-%m-%d"))
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    current_ratio = get_ai_adjustment()
    now = datetime.now(jst)
    
    # 範囲設定
    if u == "day":
        start = jst.localize(datetime.strptime(d_str, "%Y-%m-%d"))
        stop = start + timedelta(days=1)
        window = "1h"
        labels = [f"{i:02d}:00" for i in range(24)]
    elif u == "month":
        y, m, _ = map(int, d_str.split("-"))
        start = jst.localize(datetime(y, m, 1))
        ld = calendar.monthrange(y, m)[1]
        stop = start + timedelta(days=ld)
        window = "1d"
        labels = [f"{i}日" for i in range(1, ld + 1)]
    else:
        y = int(d_str.split("-")[0])
        start = jst.localize(datetime(y, 1, 1))
        stop = jst.localize(datetime(y+1, 1, 1))
        window = "1mo"
        labels = [f"{i}月" for i in range(1, 13)]
    
    res_d = {f: [None]*len(labels) for f in ["buy", "sell", "solar", "home"]}
    query = f'from(bucket:"{INFLUX_BUCKET}") |> range(start: {start.isoformat()}, stop: {stop.isoformat()}) |> aggregateWindow(every: {window}, fn: mean, createEmpty: true)'
    
    try:
        tables = client.query_api().query(query)
        for t in tables:
            fld = t.records[0].get_field()
            if fld in res_d:
                for i, r in enumerate(t.records):
                    if i >= len(labels): break
                    val = r.get_value()
                    if val is None: continue
                    
                    # --- ここで計算ロジックを修正 ---
                    # 1. 共通: WからkWへ
                    kw = val / 1000.0
                    
                    if u == "day":
                        # 日次(1h単位): 平均Wがそのままその1時間のkWhとみなせる
                        res_d[fld][i] = round(kw, 2)
                        
                    elif u == "month":
                        # 月次(1d単位): その日の「平均W × 24h」が1日のkWh
                        # ただし「今日」の場合は、経過時間分だけ掛ける
                        target_date = (start + timedelta(days=i)).date()
                        if target_date == now.date():
                            hours_passed = now.hour + (now.minute / 60.0)
                            res_d[fld][i] = round(kw * hours_passed, 2)
                        elif target_date < now.date():
                            res_d[fld][i] = round(kw * 24, 2)
                        else:
                            res_d[fld][i] = 0.0 # 未来は0
                            
                    elif u == "year":
                        # 年次(1ヶ月単位): その月の「平均W × 24h × 日数」が1ヶ月のkWh
                        target_month_start = jst.localize(datetime(start.year, i+1, 1))
                        _, days_in_month = calendar.monthrange(start.year, i+1)
                        
                        if target_month_start.year == now.year and target_month_start.month == now.month:
                            # 今月の場合：月初から今までの総時間数を掛ける
                            days_passed = now.day - 1 + (now.hour / 24.0)
                            res_d[fld][i] = round(kw * 24 * days_passed, 2)
                        elif target_month_start < now.replace(day=1, hour=0, minute=0, second=0, microsecond=0):
                            res_d[fld][i] = round(kw * 24 * days_in_month, 2)
                        else:
                            res_d[fld][i] = 0.0
    except Exception as e:
        print(f"Query Error: {e}")
    
    forecast, w_codes, irradiances = [None]*len(labels), ["-"]*len(labels), ["-"]*len(labels)
    # 天気情報の取得（Open-Meteo）
    try:
        if u == "day":
            w_url = f"https://api.open-meteo.com/v1/forecast?latitude=33.45&longitude=130.53&hourly=weather_code,shortwave_radiation&timezone=Asia%2FTokyo&start_date={d_str}&end_date={d_str}"
            w_res = requests.get(w_url).json()
            m_num = int(d_str.split("-")[1])
            dynamic_coeff = COEFF_MAP.get(m_num, 2.5) * current_ratio
            forecast = [round(min((w / 1000.0) * PANEL_CAPACITY * dynamic_coeff, PANEL_CAPACITY), 2) for w in w_res['hourly']['shortwave_radiation']]
            w_codes = w_res['hourly']['weather_code']
            irradiances = [round(w, 0) for w in w_res['hourly']['shortwave_radiation']]
        elif u == "month":
            w_url = f"https://api.open-meteo.com/v1/forecast?latitude=33.45&longitude=130.53&daily=weather_code,shortwave_radiation_sum&timezone=Asia%2FTokyo&start_date={start.strftime('%Y-%m-%d')}&end_date={(stop-timedelta(days=1)).strftime('%Y-%m-%d')}"
            w_res = requests.get(w_url).json()
            w_codes = w_res['daily']['weather_code']
            irradiances = [round(w, 1) for w in w_res['daily']['shortwave_radiation_sum']]
        elif u == "year":
            # 年間は日射量のみ（天気実績の羅列は避ける）
            w_url = f"https://api.open-meteo.com/v1/forecast?latitude=33.45&longitude=130.53&daily=shortwave_radiation_sum&timezone=Asia%2FTokyo&start_date={start.strftime('%Y-%m-%d')}&end_date={(stop-timedelta(days=1)).strftime('%Y-%m-%d')}"
            w_res = requests.get(w_url).json()
            # 月ごとに集計
            irradiances = [0.0]*12
            for i, d_str_w in enumerate(w_res['daily']['time']):
                m_idx = int(d_str_w.split("-")[1]) - 1
                irradiances[m_idx] += w_res['daily']['shortwave_radiation_sum'][i]
            irradiances = [round(v, 1) for v in irradiances]
    except: pass
    
    # 金額計算
    by = []
    if u == "day":
        by = [int(v * get_unit_price(start + timedelta(hours=i))) if v is not None else None for i, v in enumerate(res_d["buy"])]
    else:
        # 月・年単位は簡易平均単価(28円)で算出
        by = [int(v * 28) if v is not None else None for v in res_d["buy"]]
    
    sy = [int(v * 16) if v is not None else None for v in res_d["sell"]]
    
    return jsonify({"labels": labels, "buy": res_d["buy"], "sell": res_d["sell"], "solar": res_d["solar"], "home": res_d["home"], "buy_yen": by, "sell_yen": sy, "forecast": forecast, "weather": w_codes, "irradiance": irradiances, "ai_ratio": round(current_ratio, 3)})

def sb_headers():
    t, nonce = str(int(time.time()*1000)), str(uuid.uuid4())
    sign = base64.b64encode(hmac.new(bytes(SB_SECRET, 'utf-8'), msg=bytes(f"{SB_TOKEN}{t}{nonce}", 'utf-8'), digestmod=hashlib.sha256).digest())
    return {"Authorization": SB_TOKEN, "t": t, "sign": str(sign, 'utf-8'), "nonce": nonce, "Content-Type": "application/json"}

@app.route("/api/devices")
def get_devices(): return jsonify(requests.get("https://api.switch-bot.com/v1.1/devices", headers=sb_headers()).json().get("body", {}))

@app.route("/api/control", methods=["POST"])
def control(): return jsonify(requests.post(f"https://api.switch-bot.com/v1.1/devices/{request.json['deviceId']}/commands", json=request.json['payload'], headers=sb_headers()).json())

@app.route("/")
def index():
    return render_template_string("""
    <!DOCTYPE html><html lang="ja-jp"><head><meta charset="UTF-8"><title>HEMS Professional</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root { --bg: #0b1120; --card: #1e293b; --text: #f8fafc; --buy: #f97316; --sell: #22c55e; --solar: #3b82f6; --home: #a855f7; }
        body { margin: 0; font-family: 'Segoe UI', sans-serif; display: flex; height: 100vh; background: var(--bg); color: var(--text); overflow: hidden; }
        #left, #right { width: 50%; height: 100%; display: flex; flex-direction: column; border-right: 1px solid #334155; }
        .area-live { height: 280px; position: relative; background: #000; flex-shrink: 0; border-bottom: 2px solid #334155; }
        .area-data { flex-grow: 1; display: flex; flex-direction: column; min-height: 0; background: #0f172a; }
        .data-content { flex-grow: 1; position: relative; overflow: hidden; }
        #wrap-chart, #wrap-list { width: 100%; height: 100%; position: absolute; }
        .weather-card { height: 120px; padding: 15px; margin: 0 10px 10px 10px; border-radius: 12px; background: linear-gradient(135deg, #1e40af, #0f172a); border: 1px solid #3b82f6; flex-shrink: 0; }
        .radar-box { height: 280px; margin: 0 10px 10px 10px; border-radius: 12px; overflow: hidden; border: 1px solid #334155; background: #000; flex-shrink: 0; }
        #sb-wrap { flex-grow: 1; overflow-y: auto; padding: 10px; }
        #clock { position: absolute; top: 10px; left: 15px; color: var(--solar); font-weight: bold; font-family: monospace; z-index: 20; cursor: pointer; }
        .node { position: absolute; width: 85px; height: 85px; border-radius: 50%; border: 4px solid #475569; background: #0f172a; display: flex; flex-direction: column; align-items: center; justify-content: center; z-index: 10; font-size: 12px; }
        .node.solar { top: 20px; left: 50%; transform: translateX(-50%); border-color: var(--solar); }
        .node.grid { top: 125px; left: 50px; border-color: var(--buy); }
        .node.home { top: 125px; right: 50px; border-color: var(--home); }
        .solar-total { position: absolute; top: 55px; left: calc(50% + 65px); font-size: 15px; font-weight: bold; color: #cbd5e1; white-space: nowrap; }
        .acc-info { position: absolute; bottom: 10px; left: 50%; transform: translateX(-50%); width: 92%; display:flex; justify-content:space-between; font-size:15px; background:rgba(30,41,59,0.9); padding:10px; border-radius:10px; border:1px solid #475569; }
        table { width: 100%; border-collapse: collapse; font-size: 11px; table-layout: fixed; }
        th { background: #1e293b; padding: 6px; border-bottom: 2px solid #334155; position: sticky; top: 0; }
        td { padding: 6px; border-bottom: 1px solid #1e293b; text-align: center; color: #cbd5e1; }
        .btn { padding: 6px 12px; font-size: 12px; border: 1px solid #475569; border-radius: 6px; cursor: pointer; background: #1e293b; color: #fff; }
        .btn.active { background: var(--solar); border-color: #fff; }
        .sb-card { background: var(--card); padding: 12px; border-radius: 10px; margin-bottom: 10px; border: 1px solid #334155; }
        .ac-controls { display: grid; grid-template-columns: repeat(3, 1fr); gap: 5px; margin-top: 8px; }
        .ac-btn { font-size: 10px; padding: 4px; border-radius: 4px; border: 1px solid #475569; background: #0f172a; color: #fff; cursor: pointer; }
        .ac-btn.on { background: #ef4444; } .ac-btn.off { background: #475569; }
        svg { position: absolute; width: 100%; height: 100%; pointer-events: none; }
    </style>
    </head>
    <body>
        <div id="left">
            <div class="area-live">
                <div id="clock" onclick="toggleFullScreen()"></div>
                <svg viewBox="0 0 600 320">
                    <path id="p-s2h" d="M 300 110 V 170 H 465" stroke="#1e293b" stroke-width="8" fill="none" />
                    <path id="p-s2g" d="M 300 110 V 170 H 135" stroke="#1e293b" stroke-width="8" fill="none" />
                    <path id="p-g2h" d="M 135 170 H 465" stroke="#1e293b" stroke-width="8" fill="none" />
                    <circle id="d-s2h" r="5" fill="#fbbf24" style="opacity:0;" />
                    <circle id="d-s2g" r="5" fill="#fbbf24" style="opacity:0;" />
                    <circle id="d-g2h" r="5" fill="#f97316" style="opacity:0;" />
                </svg>
                <div class="node solar"><small>発電</small><b id="v-solar" style="font-size:20px;">0</b>W</div>
                <div class="solar-total">本日総発電<br><span id="v-s-t" style="color:var(--solar)">0.0</span> kWh</div>
                <div class="node grid"><small>電力網</small><b id="v-grid" style="font-size:20px;">0</b>W</div>
                <div class="node home"><small>家消費</small><b id="v-home" style="font-size:20px;">0</b>W</div>
                <div class="acc-info">
                    <div>買電: <span id="v-bk" style="color:var(--buy)">0</span>kWh (<span id="v-by">0</span>円)</div>
                    <div>売電: <span id="v-sk" style="color:var(--sell)">0</span>kWh (<span id="v-sy">0</span>円)</div>
                </div>
            </div>
            <div class="area-data">
                <div style="padding:10px; display:flex; gap:10px; background:#1e293b; align-items:center;">
                    <button class="btn active" id="b-chart" onclick="setView('chart')">グラフ</button>
                    <button class="btn" id="b-list" onclick="setView('list')">リスト</button>
                    <select id="sel-u" class="btn" style="margin-left:auto;" onchange="loadData()">
                        <option value="day">日</option><option value="month">月</option><option value="year">年</option>
                    </select>
                    <input type="date" id="sel-d" class="btn" onchange="loadData()">
                </div>
                <div class="data-content">
                    <div id="wrap-chart"><canvas id="mainChart"></canvas></div>
                    <div id="wrap-list" style="display:none; overflow-y:auto; height:100%;">
                        <table><thead><tr id="table-head"></tr></thead><tbody id="list-body"></tbody></table>
                    </div>
                </div>
            </div>
        </div>
        <div id="right">
            <div class="weather-card">
                <div style="display:flex; justify-content:space-between;">
                    <div><b style="font-size:18px;">筑紫野市 筑紫</b><div id="w-txt" style="font-size:13px; margin-top:4px;"></div></div>
                    <div style="text-align:right"><span id="w-max" style="font-size:32px; font-weight:bold; color:var(--buy);">--</span>° / <span id="w-min" style="font-size:20px; opacity:0.8;">--</span>°</div>
                </div>
                <div id="w-weekly" style="display:flex; justify-content:space-between; margin-top:15px; font-size:11px; text-align:center;"></div>
            </div>
            <div class="radar-box">
                <iframe src="https://webapp.ydits.net/" width="100%" height="100%" scrolling="NO" frameborder="0"></iframe>
            </div>
            <div id="sb-wrap"></div>
        </div>

        <script>
            let mainChart; const rafs = {};
const getWeatherDetails = (code) => {
    const map = {
        0:  { icon: '☀️', text: '快晴', bg: 'linear-gradient(135deg, #0ea5e9, #3b82f6)' },
        1:  { icon: '🌤️', text: '晴れ', bg: 'linear-gradient(135deg, #3b82f6, #6366f1)' },
        2:  { icon: '⛅', text: '時々曇り', bg: 'linear-gradient(135deg, #64748b, #3b82f6)' },
        3:  { icon: '☁️', text: '曇り', bg: 'linear-gradient(135deg, #475569, #1e293b)' },
        45: { icon: '🌫️', text: '霧',   bg: 'linear-gradient(135deg, #94a3b8, #475569)' },
        61: { icon: '☔', text: '小雨', bg: 'linear-gradient(135deg, #1e3a8a, #1e293b)' },
        63: { icon: '☔', text: '雨',   bg: 'linear-gradient(135deg, #0f172a, #1e3a8a)' },
        65: { icon: '🌊', text: '大雨', bg: 'linear-gradient(135deg, #000000, #1e3a8a)' },
        71: { icon: '❄️', text: '小雪', bg: 'linear-gradient(135deg, #e2e8f0, #94a3b8)' },
        80: { icon: '🚿', text: 'にわか雨', bg: 'linear-gradient(135deg, #334155, #1e40af)' },
        95: { icon: '⚡', text: '雷雨', bg: 'linear-gradient(135deg, #4c1d95, #0f172a)' }
    };
    // 範囲外のコードも代表的な色にマッピング
    if (!map[code]) {
        if (code >= 51 && code <= 57) return { ...map[61], text: '霧雨' };
        if (code >= 71 && code <= 77) return { ...map[71], text: '雪' };
        if (code >= 81 && code <= 82) return { ...map[80], text: '強いにわか雨' };
    }
    return map[code] || { icon: '❓', text: '不明', bg: 'linear-gradient(135deg, #1e293b, #0f172a)' };
};

            function toggleFullScreen() { if (!document.fullscreenElement) document.documentElement.requestFullscreen(); else document.exitFullscreen(); }

            function anim(dotId, pathId, val) {
                const dot = document.getElementById(dotId), path = document.getElementById(pathId);
                if (val <= 50) { dot.style.opacity = 0; cancelAnimationFrame(rafs[dotId]); return; }
                dot.style.opacity = 1; const len = path.getTotalLength(), dur = Math.max(1000, 8000 - (val/1.5));
                let start = null; function step(ts) { if(!start) start = ts; const p = path.getPointAtLength(((ts-start)%dur/dur)*len);
                dot.setAttribute('cx', p.x); dot.setAttribute('cy', p.y); rafs[dotId] = requestAnimationFrame(step); }
                cancelAnimationFrame(rafs[dotId]); rafs[dotId] = requestAnimationFrame(step);
            }

            async function updateLive() {
                const d = await (await fetch('/api/live')).json();
                document.getElementById('clock').innerText = new Date().toLocaleString('ja-JP',{month:'short',day:'numeric',weekday:'short',hour:'2-digit',minute:'2-digit',second:'2-digit'});
                document.getElementById('v-solar').innerText = d.solar; 
                document.getElementById('v-grid').innerText = d.sell > 0 ? d.sell : d.buy;
                document.getElementById('v-home').innerText = d.home; document.getElementById('v-s-t').innerText = d.d_solar_t;
                document.getElementById('v-bk').innerText = d.d_buy_k; document.getElementById('v-by').innerText = d.d_buy_y;
                document.getElementById('v-sk').innerText = d.d_sell_k; document.getElementById('v-sy').innerText = d.d_sell_y;
                anim('d-s2h', 'p-s2h', Math.min(d.solar, d.home)); anim('d-s2g', 'p-s2g', d.sell); anim('d-g2h', 'p-g2h', d.buy);
            }

            async function loadData() {
                const u = document.getElementById('sel-u').value, d = document.getElementById('sel-d').value;
                const res = await (await fetch(`/api/history?unit=${u}&date=${d}`)).json();
                mainChart.data.labels = res.labels;
                mainChart.data.datasets = [
                    {label:'買電', type:'bar', backgroundColor:'#f97316', data:res.buy},
                    {label:'売電', type:'bar', backgroundColor:'#22c55e', data:res.sell},
                    {label:'発電', type:'line', borderColor:'#3b82f6', backgroundColor:'#3b82f6', data:res.solar, pointRadius:4, tension:0.2},
                    {label:'消費', type:'line', borderColor:'#a855f7', backgroundColor:'#a855f7', data:res.home, pointRadius:4, tension:0.2}
                ];
                if(u==='day') mainChart.data.datasets.push({label:'AI予測', type:'line', borderColor:'#94a3b8', borderDash:[5,5], data:res.forecast, pointRadius:0});
                mainChart.update('none');

                const f = (v) => (v === null || v === undefined || v === "-") ? '-' : v;
                let h = `<th>期間</th><th>買(kW)</th><th>売(kW)</th><th>発(kW)</th><th>消(kW)</th><th>買(¥)</th><th>売(¥)</th>`;
                if(u==='day' || u==='month') h += `<th>天気</th><th>日射</th>`;
                if(u==='day') h += `<th>予測</th>`;
                document.getElementById('table-head').innerHTML = h;
                document.getElementById('list-body').innerHTML = res.labels.map((l, i) => {
                    let r = `<tr><td>${l}</td><td>${f(res.buy[i])}</td><td>${f(res.sell[i])}</td><td>${f(res.solar[i])}</td><td>${f(res.home[i])}</td><td>${f(res.buy_yen[i])}</td><td>${f(res.sell_yen[i])}</td>`;
                    if(u==='day' || u==='month') r += `<td>${res.weather[i]!=='-'?getWeatherDetails(res.weather[i]).icon:'-'}</td><td>${f(res.irradiance[i])}</td>`;
                    if(u==='day') r += `<td>${f(res.forecast[i])}</td>`;
                    return r + `</tr>`;
                }).join('');
            }

            function setView(v) {
                document.getElementById('wrap-chart').style.display = v==='chart'?'block':'none';
                document.getElementById('wrap-list').style.display = v==='list'?'block':'none';
                document.getElementById('b-chart').classList.toggle('active', v==='chart');
                document.getElementById('b-list').classList.toggle('active', v==='list');
            }

/* --- loadWeather をアップデート --- */
async function loadWeather() {
    const res = await (await fetch("https://api.open-meteo.com/v1/forecast?latitude=33.45&longitude=130.53&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=Asia%2FTokyo")).json();
    
    // 今日の天気詳細を取得
    const todayCode = res.daily.weather_code[0];
    const details = getWeatherDetails(todayCode);
    
    // 背景色とアイコンの更新
    const card = document.querySelector('.weather-card');
    card.style.background = details.bg;
    
    document.getElementById('w-max').innerText = Math.round(res.daily.temperature_2m_max[0]);
    document.getElementById('w-min').innerText = Math.round(res.daily.temperature_2m_min[0]);
    document.getElementById('w-txt').innerHTML = `<span class="weather-icon-anim">${details.icon}</span> ${details.text} (降水: ${res.daily.precipitation_probability_max[0]}%)`;
    
    // 週報部分も同様に
    document.getElementById('w-weekly').innerHTML = res.daily.time.map((t, i) => {
        const d = getWeatherDetails(res.daily.weather_code[i]);
        return `<div>${t.slice(8,10)}日<br><span style="font-size:18px;">${d.icon}</span><br>${Math.round(res.daily.temperature_2m_max[i])}°</div>`;
    }).join('');
}

            async function loadSB() {
                const d = await (await fetch('/api/devices')).json();
                const wrap = document.getElementById('sb-wrap'); wrap.innerHTML = '';
                (d.deviceList || []).concat(d.infraredRemoteList || []).forEach(v => {
                    const card = document.createElement('div'); card.className = "sb-card";
                    const isAC = v.deviceType === "Air Conditioner";
                    let html = `<div style="display:flex; justify-content:space-between; align-items:center;"><strong>${v.deviceName || v.remoteName}</strong>`;
                    if(isAC) {
                        html += `<div><button class="ac-btn on" onclick="ctrlAC('${v.deviceId}','on')">ON</button><button class="ac-btn off" onclick="ctrlAC('${v.deviceId}','off')">OFF</button></div></div>`;
                        html += `<div class="ac-controls">
                            <select onchange="ctrlAC('${v.deviceId}','mode',this.value)" class="ac-btn"><option>冷房</option><option>暖房</option><option>除湿</option></select>
                            <input type="number" value="26" min="18" max="30" onchange="ctrlAC('${v.deviceId}','temp',this.value)" class="ac-btn">
                        </div>`;
                    } else {
                        html += `<div><button class="btn" onclick="ctrl('${v.deviceId}','turnOn')">ON</button><button class="btn" onclick="ctrl('${v.deviceId}','turnOff')">OFF</button></div></div>`;
                    }
                    card.innerHTML = html; wrap.appendChild(card);
                });
            }

            function ctrl(id, cmd) { fetch('/api/control', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({deviceId:id, payload:{command:cmd, parameter:'default', commandType:'command'}})}); }
            function ctrlAC(id, type, val) {
                let cmd = "setAll"; 
                let param = type === 'on' ? "26,2,1,on" : type === 'off' ? "26,2,1,off" : `26,2,1,on`; // 簡易パラメータ化
                fetch('/api/control', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({deviceId:id, payload:{command:cmd, parameter:param, commandType:'command'}})});
            }

            document.getElementById('sel-d').value = new Date(Date.now() + 9*3600000).toISOString().split('T')[0];
            mainChart = new Chart(document.getElementById('mainChart').getContext('2d'), { type:'bar', options:{ responsive:true, maintainAspectRatio:false, animation: {duration: 0}, plugins:{legend:{position:'bottom', labels:{color:'#94a3b8', boxWidth:12}}}, scales:{y:{beginAtZero:true, grid:{color:'#1e293b'}, ticks:{color:'#64748b'}}, x:{grid:{display:false}, ticks:{color:'#64748b'}}} } });
            
            updateLive(); loadData(); loadWeather(); loadSB(); 
            setInterval(updateLive, 5000); 
            setInterval(() => { if(document.getElementById('sel-d').value === new Date(Date.now() + 9*3600000).toISOString().split('T')[0]) loadData(); }, 60000);
        </script>
    </body></html>
    """)

if __name__ == "__main__":
    threading.Thread(target=collector, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)
