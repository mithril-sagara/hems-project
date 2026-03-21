import os, socket, time, threading, requests, json, hmac, hashlib, base64, uuid, calendar, math
from flask import Flask, jsonify, render_template_string, request
from datetime import datetime, timedelta
import pytz
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

# --- 基本設定 ---
IP = os.environ.get("ECHONET_IP", "192.168.0.146")
INFLUX_URL = os.environ.get("INFLUX_URL", "http://influxdb:8086")
INFLUX_TOKEN = os.environ.get("INFLUX_TOKEN")
INFLUX_ORG = os.environ.get("INFLUX_ORG")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET")
SB_TOKEN = os.environ.get("SB_TOKEN")
SB_SECRET = os.environ.get("SB_SECRET")

jst = pytz.timezone('Asia/Tokyo')
app = Flask(__name__)

latest = {"solar": 0, "buy": 0, "sell": 0, "home": 0, "d_buy_k": 0, "d_buy_y": 0, "d_sell_k": 0, "d_sell_y": 0, "d_solar_t": 0}
totals = {"buy_kwh": 0.0, "sell_kwh": 0.0, "buy_yen": 0.0, "sell_yen": 0.0, "solar_kwh": 0.0, "day": ""}

def get_unit_price(dt):
    """電化でナイト・セレクト21"""
    if dt.hour >= 21 or dt.hour < 7: return 16.60
    return 33.80 if dt.month in [7, 8, 9] else 28.60

def fetch_echonet(eoj, epc):
    try:
        frame = bytes([0x10, 0x81, 0x00, 0x01, 0x05, 0xff, 0x01, eoj[0], eoj[1], eoj[2], 0x62, 0x01, epc, 0x00])
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(("", 3610)); s.settimeout(1.0)
            s.sendto(frame, (IP, 3610))
            data, _ = s.recvfrom(1024)
            idx = data.find(bytes([epc]))
            return data[idx+2 : idx+2+data[idx+1]]
    except: return None

def collector():
    global totals
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)
    while True:
        try:
            now = datetime.now(jst)
            if totals["day"] != now.strftime("%Y-%m-%d"):
                totals = {"buy_kwh": 0.0, "sell_kwh": 0.0, "buy_yen": 0.0, "sell_yen": 0.0, "solar_kwh": 0.0, "day": now.strftime("%Y-%m-%d")}
            
            res_s = fetch_echonet([0x02, 0x79, 0x01], 0xE0)
            res_m = fetch_echonet([0x02, 0xA5, 0x01], 0xF5)
            solar = int.from_bytes(res_s, "big", signed=True) if res_s else 0
            buy, sell = 0, 0
            if res_m and len(res_m) >= 8:
                val = int.from_bytes(res_m[0:4], "big", signed=True)
                if val >= 0: sell = val; buy = 0
                else: sell = 0; buy = abs(val)
            
            step = (5/3600.0)
            totals["buy_kwh"] += (buy/1000.0)*step
            totals["buy_yen"] += (buy/1000.0)*step * get_unit_price(now)
            totals["solar_kwh"] += (solar/1000.0)*step
            totals["sell_kwh"] += (sell/1000.0)*step
            totals["sell_yen"] += (sell/1000.0)*step * 7.0 
            
            latest.update({"solar": solar, "buy": buy, "sell": sell, "home": max(0, solar+buy-sell),
                           "d_buy_k": round(totals["buy_kwh"], 2), "d_buy_y": int(totals["buy_yen"]),
                           "d_sell_k": round(totals["sell_kwh"], 2), "d_sell_y": int(totals["sell_yen"]),
                           "d_solar_t": round(totals["solar_kwh"], 2)})
            write_api.write(bucket=INFLUX_BUCKET, record=Point("energy").field("solar", float(solar)).field("buy", float(buy)).field("sell", float(sell)).field("home", float(latest["home"])))
        except: pass
        time.sleep(5)

@app.route("/api/live")
def api_live(): return jsonify(latest)

@app.route("/api/history")
def api_history():
    u, d_str = request.args.get("unit", "day"), request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    if u == "day":
        start = jst.localize(datetime.strptime(d_str, "%Y-%m-%d")); stop = start + timedelta(days=1); window = "1h"
        labels = [f"{i:02d}:00" for i in range(24)]
        fc = [round(5.9 * math.sin(math.pi * (i-6)/12), 2) if 6 <= i <= 18 else 0 for i in range(24)]
    elif u == "month":
        y, m, _ = map(int, d_str.split("-")); start = jst.localize(datetime(y, m, 1)); ld = calendar.monthrange(y, m)[1]
        stop = start + timedelta(days=ld); window = "1d"; labels = [f"{i}日" for i in range(1, ld + 1)]; fc = []
    else:
        y = int(d_str.split("-")[0]); start = jst.localize(datetime(y, 1, 1)); stop = jst.localize(datetime(y+1, 1, 1)); window = "1mo"
        labels = [f"{i}月" for i in range(1, 13)]; fc = []

    res_d = {f: [None]*len(labels) for f in ["buy", "sell", "solar", "home"]}
    query = f'from(bucket:"{INFLUX_BUCKET}") |> range(start: {start.isoformat()}, stop: {stop.isoformat()}) |> aggregateWindow(every: {window}, fn: mean, createEmpty: true)'
    try:
        tables = client.query_api().query(query)
        for t in tables:
            fld = t.records[0].get_field()
            if fld in res_d:
                for i, r in enumerate(t.records):
                    if i < len(labels) and r.get_value() is not None: res_d[fld][i] = round(r.get_value()/1000.0, 2)
    except: pass
    by = [int(v * get_unit_price(start + timedelta(hours=i))) if v is not None else None for i, v in enumerate(res_d["buy"])]
    return jsonify({"labels": labels, "buy": res_d["buy"], "sell": res_d["sell"], "solar": res_d["solar"], "home": res_d["home"], "buy_yen": by, "sell_yen": [int(v*7) if v else None for v in res_d["sell"]], "forecast": fc})

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
    <!DOCTYPE html><html lang="ja-jp"><head><meta charset="UTF-8"><title>HEMS Ultimate</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
        :root { --bg: #0b1120; --card: #1e293b; --text: #f8fafc; --buy: #f97316; --sell: #22c55e; --solar: #3b82f6; --home: #a855f7; }
        body { margin: 0; font-family: sans-serif; display: flex; height: 100vh; background: var(--bg); color: var(--text); overflow: hidden; }
        #left, #right { width: 50%; height: 100%; display: flex; flex-direction: column; border-right: 1px solid #334155; }
        
        /* グラフの高さを確保するため、ライブ表示部を少し縮小(380px -> 35vh程度) */
        .area-live { height: 35vh; min-height: 300px; position: relative; background: #000; flex-shrink: 0; border-bottom: 2px solid #334155; }
        .area-data { flex-grow: 1; display: flex; flex-direction: column; min-height: 0; background: #0f172a; }
        
        .weather-card { height: 130px; margin: 10px; padding: 12px; border-radius: 12px; background: linear-gradient(135deg, #1e40af, #0f172a); border: 1px solid #3b82f6; flex-shrink: 0; }
	/* 雨雲レーダー：親の幅を半分にし、中央寄せ */
        .radar-box { 
            height: 280px; 
            width: 100%;
            margin: 0 auto; 
            border-radius: 12px; 
            overflow: hidden; 
            border: 2px solid #334155; 
            position: relative; 
            flex-shrink: 0; 
            background: #000; 
        }

        /* iframe：200%で描画したものを0.5倍にして、隙間なくフィットさせる */
        .radar-box iframe {
            width: 200% !important; 
            height: 200% !important;
            transform: scale(0.5); 
            transform-origin: 0 0; /* 左上を起点に縮小 */
            border: none;
            display: block;
        }



        #sb-wrap { flex-grow: 1; overflow-y: auto; padding: 10px; background: #0b1120; }
        #clock { position: absolute; top: 10px; left: 15px; color: var(--solar); font-weight: bold; font-family: monospace; z-index: 20; }
        .node { position: absolute; width: 80px; height: 80px; border-radius: 50%; border: 4px solid #475569; background: #0f172a; display: flex; flex-direction: column; align-items: center; justify-content: center; z-index: 10; font-size: 11px; }
        .node.solar { top: 20px; left: 50%; transform: translateX(-50%); border-color: var(--solar); }
        .node.grid { top: 120px; left: 40px; border-color: var(--buy); }
        .node.home { top: 120px; right: 40px; border-color: var(--home); }
        .solar-total { position: absolute; top: 50px; left: calc(50% + 65px); font-size: 13px; font-weight: bold; color: #94a3b8; }
        .acc-info { position: absolute; bottom: 8px; left: 50%; transform: translateX(-50%); width: 94%; display:flex; justify-content:space-between; font-size:13px; background:rgba(30,41,59,0.9); padding:8px; border-radius:8px; border:1px solid #475569; }
        .data-content { flex-grow: 1; position: relative; min-height: 0; }
/* グラフとリストを包むコンテナ */
        .data-content { 
            flex-grow: 1; 
            position: relative; 
            min-height: 0; /* flex内のスクロールに必須 */
            overflow: hidden; 
        }

        /* グラフとリストそれぞれのラップ要素 */
        #wrap-chart, #wrap-list { 
            width: 100%; 
            height: 100%; 
            position: absolute; 
            top: 0; 
            left: 0; 
            overflow-y: auto; /* ここで垂直スクロールを許可 */
            -webkit-overflow-scrolling: touch; /* タブレットでの操作感を滑らかに */
        }

        /* テーブルが親の幅を超えないように固定 */
        table { 
            width: 100%; 
            border-collapse: collapse; 
            font-size: 11px; 
            table-layout: fixed; /* 列幅を固定してはみ出しを防止 */
        }
        th { background: #1e293b; padding: 8px; border-bottom: 2px solid #334155; position: sticky; top: 0; z-index: 20; }
        td { padding: 8px; border-bottom: 1px solid #1e293b; text-align: center; }
        .btn { padding: 5px 10px; font-size: 11px; border: 1px solid #475569; border-radius: 4px; cursor: pointer; background: #1e293b; color: #fff; }
        .btn.active { background: var(--solar); border-color: #fff; }
        .sb-card { background: var(--card); padding: 12px; border-radius: 10px; margin-bottom: 10px; border: 1px solid #334155; }
        svg { position: absolute; width: 100%; height: 100%; pointer-events: none; }
    </style>
    </head>
    <body>
        <div id="left">
            <div class="area-live">
                <div id="clock"></div>
                <svg viewBox="0 0 600 380"><path id="p-s2h" d="M 300 120 V 190 H 510" stroke="#1e293b" stroke-width="8" /><path id="p-s2g" d="M 300 120 V 190 H 90" stroke="#1e293b" stroke-width="8" /><path id="p-g2h" d="M 90 190 H 510" stroke="#1e293b" stroke-width="8" /><circle id="d-s2h" r="5" fill="#fbbf24" style="opacity:0;" /><circle id="d-s2g" r="5" fill="#fbbf24" style="opacity:0;" /><circle id="d-g2h" r="5" fill="#f97316" style="opacity:0;" /></svg>
                <div class="node solar"><small>発電</small><b id="v-solar" style="font-size:18px;">0</b>W</div>
                <div class="solar-total">本日総発電<br><span id="v-s-t" style="color:var(--solar)">0.0</span> kWh</div>
                <div class="node grid"><small>電力網</small><b id="v-grid" style="font-size:18px;">0</b>W</div>
                <div class="node home"><small>家消費</small><b id="v-home" style="font-size:18px;">0</b>W</div>
                <div class="acc-info">
                    <div>買電: <span id="v-bk" style="color:var(--buy)">0</span>kWh (<span id="v-by">0</span>円)</div>
                    <div>売電: <span id="v-sk" style="color:var(--sell)">0</span>kWh (<span id="v-sy">0</span>円)</div>
                </div>
            </div>
            <div class="area-data">
                <div style="padding:8px; display:flex; gap:8px; background:#1e293b;">
                    <button class="btn active" id="b-chart" onclick="setView('chart')">グラフ</button><button class="btn" id="b-list" onclick="setView('list')">リスト</button>
                    <select id="sel-u" class="btn" style="margin-left:auto;" onchange="loadData()"><option value="day">日</option><option value="month">月</option><option value="year">年</option></select>
                    <input type="date" id="sel-d" class="btn" onchange="loadData()">
                </div>
                <div class="data-content"><div id="wrap-chart" style="padding:10px;"><canvas id="mainChart"></canvas></div><div id="wrap-list" style="display:none;"><table><thead><tr id="table-head"></tr></thead><tbody id="list-body"></tbody></table></div></div>
            </div>
        </div>
        <div id="right">
            <div class="weather-card">
                <div style="display:flex; justify-content:space-between;">
                    <div><b style="font-size:16px;">筑紫野市 筑紫</b><div id="w-txt" style="font-size:12px; margin-top:4px;"></div></div>
                    <div style="text-align:right"><span id="w-max" style="font-size:28px; font-weight:bold; color:var(--buy);">--</span>° / <span id="w-min" style="font-size:18px; opacity:0.8;">--</span>°</div>
                </div>
                <div id="w-weekly" style="display:flex; justify-content:space-between; margin-top:12px; font-size:10px; text-align:center;"></div>
            </div>
            <div class="radar-box">
                <iframe src="https://webapp.ydits.net/" width=100% height=100% scrolling=NO frameborder=0 border=0 marginwidth=0 marginheight=0></iframe>
            </div>
            <div id="sb-wrap"></div>
        </div>

        <script>
            let chart; const rafs = {};
// 現在のJST（日本時間）の日付を YYYY-MM-DD 形式で取得してセット
const now = new Date();
const jstDate = new Date(now.getTime() + (9 * 60 * 60 * 1000)); // 9時間加算
const dateStr = jstDate.toISOString().split('T')[0];
document.getElementById('sel-d').value = dateStr;

            function anim(dotId, pathId, val) {
                const dot = document.getElementById(dotId), path = document.getElementById(pathId);
                if (val <= 50) { dot.style.opacity = 0; cancelAnimationFrame(rafs[dotId]); return; }
                dot.style.opacity = 1; const len = path.getTotalLength(), dur = Math.max(1000, 8000 - (val/1.2));
                let start = null; function step(ts) { if(!start) start = ts; const p = path.getPointAtLength(((ts-start)%dur/dur)*len);
                dot.setAttribute('cx', p.x); dot.setAttribute('cy', p.y); rafs[dotId] = requestAnimationFrame(step); }
                cancelAnimationFrame(rafs[dotId]); rafs[dotId] = requestAnimationFrame(step);
            }

            async function updateLive() {
                const d = await (await fetch('/api/live')).json();
                document.getElementById('clock').innerText = new Date().toLocaleString('ja-JP',{month:'short',day:'numeric',weekday:'short',hour:'2-digit',minute:'2-digit',second:'2-digit'});
                document.getElementById('v-solar').innerText = d.solar; document.getElementById('v-grid').innerText = d.sell > 0 ? d.sell : d.buy;
                document.getElementById('v-home').innerText = d.home; document.getElementById('v-s-t').innerText = d.d_solar_t;
                document.getElementById('v-bk').innerText = d.d_buy_k; document.getElementById('v-by').innerText = d.d_buy_y;
                document.getElementById('v-sk').innerText = d.d_sell_k; document.getElementById('v-sy').innerText = d.d_sell_y;
                anim('d-s2h', 'p-s2h', Math.min(d.solar, d.home)); anim('d-s2g', 'p-s2g', d.sell); anim('d-g2h', 'p-g2h', d.buy);
            }

async function loadData() {
                const unitEl = document.getElementById('sel-u');
                const dateEl = document.getElementById('sel-d');

                // 1. 日付が未設定（初期表示時）の場合、JSTの今日をセット
                if (!dateEl.value) {
                    const now = new Date();
                    const jstDate = new Date(now.getTime() + (9 * 60 * 60 * 1000));
                    dateEl.value = jstDate.toISOString().split('T')[0];
                }

                const u = unitEl.value;
                const d = dateEl.value;

                try {
                    // 2. APIからデータ取得
                    const response = await fetch(`/api/history?unit=${u}&date=${d}`);
                    if (!response.ok) throw new Error('Network response was not ok');
                    const data = await response.json();

                    // 3. グラフの更新
                    chart.data.labels = data.labels;
                    chart.data.datasets = [
                        {label:'買電', type:'bar', backgroundColor:'#f97316', data:data.buy, order: 2},
                        {label:'売電', type:'bar', backgroundColor:'#22c55e', data:data.sell, order: 2},
                        {label:'発電', type:'line', borderColor:'#3b82f6', data:data.solar, pointRadius:4, pointBackgroundColor:'#3b82f6', tension:0.2, order: 1},
                        {label:'消費', type:'line', borderColor:'#a855f7', data:data.home, pointRadius:4, pointBackgroundColor:'#a855f7', tension:0.2, order: 1}
                    ];

                    // 日別表示の時だけ「予測」ラインを追加
                    if (u === 'day' && data.forecast) {
                        chart.data.datasets.push({
                            label:'予測', type:'line', borderColor:'#475569', borderDash:[5,5], 
                            data:data.forecast, pointRadius:0, order: 3
                        });
                    }
                    chart.update();

                    // 4. リスト（テーブル）の更新
                    const f = (v) => (v === null || v === undefined) ? '-' : v;
                    
                    // ヘッダーの生成
                    let h = `<th>期間</th><th>買</th><th>売</th><th>発</th><th>消</th><th>買(¥)</th><th>売(¥)</th><th>天気</th><th>日射</th>`;
                    if (u === 'day') h += `<th>予測</th>`;
                    document.getElementById('table-head').innerHTML = h;

                    // ボディ（行）の生成
                    document.getElementById('list-body').innerHTML = data.labels.map((l, i) => {
                        let row = `<tr>
                            <td>${l}</td>
                            <td>${f(data.buy[i])}</td>
                            <td>${f(data.sell[i])}</td>
                            <td>${f(data.solar[i])}</td>
                            <td>${f(data.home[i])}</td>
                            <td>${f(data.buy_yen[i])}</td>
                            <td>${f(data.sell_yen[i])}</td>
                            <td>-</td>
                            <td>-</td>`;
                        if (u === 'day') row += `<td>${f(data.forecast[i])}</td>`;
                        row += `</tr>`;
                        return row;
                    }).join('');

                    // 5. スクロール位置をリセット（リスト切り替え時の操作性向上）
                    document.getElementById('wrap-list').scrollTop = 0;
                    document.getElementById('wrap-chart').scrollTop = 0;

                } catch (error) {
                    console.error('Failed to load data:', error);
                }
            }

            function setView(v) {
                document.getElementById('wrap-chart').style.display = v==='chart'?'block':'none';
                document.getElementById('wrap-list').style.display = v==='list'?'block':'none';
                document.getElementById('b-chart').classList.toggle('active', v==='chart');
                document.getElementById('b-list').classList.toggle('active', v==='list');
            }


async function loadWeather() {
    const res = await fetch(
        "https://api.open-meteo.com/v1/forecast?latitude=33.45&longitude=130.53&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=Asia%2FTokyo"
    );
    const d = await res.json();

    // 今日
    document.getElementById('w-max').innerText = Math.round(d.daily.temperature_2m_max[0]);
    document.getElementById('w-min').innerText = Math.round(d.daily.temperature_2m_min[0]);
    document.getElementById('w-txt').innerText =
        `降水確率: ${d.daily.precipitation_probability_max[0]}%`;

    // 週間
    document.getElementById('w-weekly').innerHTML =
        d.daily.time.map((t, i) => {
            const rain = d.daily.precipitation_probability_max[i];
            const icon = rain > 50 ? '☔' : '☀️';

            return `
                <div>
                    ${t.slice(8,10)}日<br>
                    <span style="font-size:16px;">${icon}</span><br>
                    ${Math.round(d.daily.temperature_2m_max[i])}° /
                    ${Math.round(d.daily.temperature_2m_min[i])}°<br>
                    <span style="font-size:10px;">${rain}%</span>
                </div>
            `;
        }).join('');
}

            async function loadSB() {
                const d = await (await fetch('/api/devices')).json();
                const wrap = document.getElementById('sb-wrap'); wrap.innerHTML = '';
                (d.deviceList || []).concat(d.infraredRemoteList || []).forEach(v => {
                    const isAC = (v.deviceType || v.remoteType || "").toLowerCase().includes("air conditioner");
                    const card = document.createElement('div'); card.className = "sb-card";
                    card.innerHTML = `<div style="display:flex; justify-content:space-between; align-items:center;"><strong>${v.deviceName || v.remoteName}</strong><div><button class="btn" id="on-${v.deviceId}" onclick="ctrl('${v.deviceId}','turnOn')">ON</button><button class="btn" id="off-${v.deviceId}" onclick="ctrl('${v.deviceId}','turnOff')">OFF</button></div></div>`;
                    if(isAC) card.innerHTML += `<div style="display:grid; grid-template-columns:repeat(3,1fr); gap:4px; margin-top:8px;"><button class="btn" onclick="ac('${v.deviceId}','cool')">冷房</button><button class="btn" onclick="ac('${v.deviceId}','dry')">除湿</button><button class="btn" onclick="ac('${v.deviceId}','heat')">暖房</button></div><div style="margin-top:8px;">設定: <input type="number" id="t-${v.deviceId}" value="26" style="width:40px; background:#000; color:#fff; border:1px solid #444;"> ℃ <button class="btn" onclick="ac('${v.deviceId}','set')">送信</button></div>`;
                    wrap.appendChild(card);
                });
            }

            function ctrl(id, cmd) {
                document.getElementById(`on-${id}`).classList.toggle('active', cmd==='turnOn');
                document.getElementById(`off-${id}`).classList.toggle('active', cmd==='turnOff');
                fetch('/api/control', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({deviceId:id, payload:{command:cmd, parameter:'default', commandType:'command'}})});
            }
            function ac(id, m) {
                const t = document.getElementById(`t-${id}`).value;
                const p = m==='set' ? `${t},1,1` : `26,${m},1,1`;
                fetch('/api/control', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({deviceId:id, payload:{command:'setAll', parameter:p, commandType:'command'}})});
            }

            chart = new Chart(document.getElementById('mainChart').getContext('2d'), { type:'bar', options:{ responsive:true, maintainAspectRatio:false, plugins:{legend:{position:'bottom', labels:{color:'#94a3b8', boxWidth:12}}}, scales:{y:{beginAtZero:true, grid:{color:'#1e293b'}}, x:{grid:{display:false}}} } });
            updateLive(); loadData(); loadWeather(); loadSB(); setInterval(updateLive, 5000);
        </script>
    </body></html>
    """)
if __name__ == "__main__":
    threading.Thread(target=collector, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)
