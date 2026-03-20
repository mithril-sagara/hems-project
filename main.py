import socket, time, threading, requests, os, calendar
from flask import Flask, jsonify, render_template_string, request
from datetime import datetime, timedelta
import pytz
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

# --- 設定 ---
IP = os.environ.get("HEMS_IP", "192.168.0.146")
PORT = 3610
INFLUX_URL = os.environ.get("INFLUX_URL", "http://influxdb:8086")
INFLUX_TOKEN = os.environ.get("INFLUX_TOKEN", "AcXpZW0fAIBaNQTYR5J11RDz0oVKpUTmcwr5SGYgmbq1VUnCtMCD3NDTRcbir_9Z7EQoiS28p5vedwGZuqDz0w==")
INFLUX_ORG = os.environ.get("INFLUX_ORG", "my-home")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "energy_bucket")

SOLAR_EOJ, METER_EOJ = [0x02, 0x79, 0x01], [0x02, 0xA5, 0x01]
MAX_W, LAT, LON = 5900, 33.46, 130.54
jst = pytz.timezone('Asia/Tokyo')

app = Flask(__name__)
client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write_api = client.write_api(write_options=SYNCHRONOUS)

# 累計保持用
totals = {"buy_kwh": 0.0, "sell_kwh": 0.0, "buy_yen": 0.0, "sell_yen": 0.0, "day": ""}
latest = {"solar": 0, "buy": 0, "sell": 0, "home": 0, "online": True}

def get_unit_price(dt):
    if dt.hour >= 21 or dt.hour < 7: return 16.60
    return 33.80 if dt.month in [7, 8, 9] else 28.60


def fetch_echonet(eoj, epc):
    try:
        frame = bytes([0x10, 0x81, 0x00, 0x01, 0x05, 0xff, 0x01, eoj[0], eoj[1], eoj[2], 0x62, 0x01, epc, 0x00])
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            # 送信元ポートを3610に固定（これがないとパワコンが返信をくれない場合がある）
            s.bind(("", 3610)) 
            s.settimeout(2.0)
            s.sendto(frame, (IP, PORT))
            data, _ = s.recvfrom(1024)
            idx = data.find(bytes([epc]))
            return data[idx+2 : idx+2+data[idx+1]]
    except Exception as e:
        # print(f"Error: {e}") # デバッグ時に有効にすると原因がわかります
        return None

def collector():
    global totals
    while True:
        now = datetime.now(jst)
        today = now.strftime("%Y-%m-%d")
        
        if totals["day"] != today:
            totals = {"buy_kwh": 0.0, "sell_kwh": 0.0, "buy_yen": 0.0, "sell_yen": 0.0, "day": today}

        res_s, res_m = fetch_echonet(SOLAR_EOJ, 0xE0), fetch_echonet(METER_EOJ, 0xF5)
        is_online = (res_s is not None and res_m is not None)
        
        solar = int.from_bytes(res_s, "big", signed=True) if res_s else 0
        buy, sell = 0, 0
        if res_m and len(res_m) >= 8:
            val = int.from_bytes(res_m[0:4], "big", signed=True)
            sell, buy = (val, 0) if val >= 0 else (0, abs(val))
        
        m_buy_kwh = (buy / 1000.0) / 60.0
        m_sell_kwh = (sell / 1000.0) / 60.0
        totals["buy_kwh"] += m_buy_kwh
        totals["sell_kwh"] += m_sell_kwh
        totals["buy_yen"] += m_buy_kwh * get_unit_price(now)
        totals["sell_yen"] += m_sell_kwh * 7.0 

        home = max(0, solar + buy - sell)
        latest.update({
            "solar": solar, "buy": buy, "sell": sell, "home": home, "online": is_online,
            "d_buy_k": round(totals["buy_kwh"], 2), 
            "d_buy_y": round(totals["buy_yen"], 1), # 小数点第1位まで保持
            "d_sell_k": round(totals["sell_kwh"], 2), 
            "d_sell_y": round(totals["sell_yen"], 1)
        })
        
        try:
            p = Point("energy").time(now, WritePrecision.NS).field("solar", float(solar)).field("buy", float(buy)).field("sell", float(sell)).field("home", float(home))
            write_api.write(bucket=INFLUX_BUCKET, record=p)
        except: pass
        time.sleep(60)

@app.route("/api/live")
def api_live():
    return jsonify({**latest, "dt": datetime.now(jst).strftime("%Y/%m/%d %H:%M:%S")})

@app.route("/")
def index():
    return render_template_string("""
    <!DOCTYPE html><html><head><meta charset="UTF-8">
    <style>
        body { font-family: sans-serif; background: #f1f5f9; margin: 0; padding: 20px; color: #1e293b; }
        .clock { position: absolute; top: 15px; right: 20px; font-weight: bold; color: #64748b; font-family: monospace; }
        .energy-container { position: relative; width: 100%; max-width: 800px; height: 380px; background: #ffffff; border-radius: 20px; margin: 30px auto; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.1); overflow: hidden; }
        .node { position: absolute; width: 100px; height: 100px; border-radius: 50%; background: #fff; border: 4px solid #e2e8f0; display: flex; flex-direction: column; justify-content: center; align-items: center; z-index: 2; font-size: 11px; text-align: center; }
        .node.solar { top: 30px; left: calc(50% - 50px); border-color: #f59e0b; color: #b45309; }
        .node.grid { top: 200px; left: 10%; border-color: #3b82f6; color: #1d4ed8; }
        .node.home { top: 200px; right: 10%; border-color: #10b981; color: #065f46; }
        .val { font-weight: 900; font-size: 18px; }
        .grid-info { position: absolute; top: 310px; left: 5%; width: 220px; font-size: 11px; line-height: 1.6; color: #475569; font-weight: bold; }
        svg.flow-lines { position: absolute; top: 0; left: 0; width: 100%; height: 100%; z-index: 1; pointer-events: none; }
        path { fill: none; stroke: #f1f5f9; stroke-width: 8; stroke-linecap: round; }
        .dot { opacity: 0; pointer-events: none; }
        .animating { opacity: 1; }
    </style></head>
    <body>
        <div class="clock" id="clock">--:--:--</div>
        <div class="energy-container">
            <svg id="energy-svg" class="flow-lines" viewBox="0 0 800 380">
                <path id="path-s2h" d="M 400 130 V 250 H 600" />
                <path id="path-s2g" d="M 400 130 V 250 H 200" />
                <path id="path-g2h" d="M 200 250 H 600" />
                <circle id="dot-s2h" class="dot" r="6" fill="#f59e0b" />
                <circle id="dot-s2g" class="dot" r="6" fill="#f59e0b" />
                <circle id="dot-g2h" class="dot" r="6" fill="#3b82f6" />
            </svg>
            <div class="node solar"><i>☀️</i>発電<br><span class="val" id="v-solar">0</span>W</div>
            <div class="node grid"><i>🌐</i>電力網<br><span class="val" id="v-grid">0</span>W</div>
            <div class="grid-info">
                本日買電: <span id="v-bk">0</span> kWh / <span id="v-by">0</span> 円<br>
                本日売電: <span id="v-sk">0</span> kWh / <span id="v-sy">0</span> 円
            </div>
            <div class="node home"><i>🏠</i>家庭内<br><span class="val" id="v-home">0</span>W</div>
        </div>
        <script>
            // アニメーション管理用オブジェクト
            const rafs = {};

            function updateDotAnimation(dotId, pathId, value) {
                const dot = document.getElementById(dotId);
                const path = document.getElementById(pathId);
                
                if (value <= 10) { // 10W以下は停止
                    dot.classList.remove('animating');
                    if (rafs[dotId]) cancelAnimationFrame(rafs[dotId]);
                    return;
                }

                dot.classList.add('animating');
                const length = path.getTotalLength();
                // 速度調整: 負荷が高いほど速く（2秒〜5秒の間で可変）
                const duration = Math.max(1500, 5000 - (value / 2)); 

                let start = null;
                function step(timestamp) {
                    if (!start) start = timestamp;
                    const progress = (timestamp - start) % duration;
                    const point = path.getPointAtLength((progress / duration) * length);
                    dot.setAttribute('cx', point.x);
                    dot.setAttribute('cy', point.y);
                    rafs[dotId] = requestAnimationFrame(step);
                }

                if (rafs[dotId]) cancelAnimationFrame(rafs[dotId]);
                rafs[dotId] = requestAnimationFrame(step);
            }

            async function update() {
                try {
                    const res = await fetch('/api/live');
                    const d = await res.json();
                    
                    document.getElementById('clock').innerText = d.dt;
                    document.getElementById('v-solar').innerText = d.solar + 'W';
                    document.getElementById('v-grid').innerText = (d.sell > 0 ? d.sell : d.buy) + 'W';
                    document.getElementById('v-home').innerText = d.home + 'W';
                    document.getElementById('v-bk').innerText = d.d_buy_k.toFixed(2);
                    document.getElementById('v-by').innerText = d.d_buy_y.toFixed(1);
                    document.getElementById('v-sk').innerText = d.d_sell_k.toFixed(2);
                    document.getElementById('v-sy').innerText = d.d_sell_y.toFixed(1);

                    // リアルタイムデータでアニメーション更新
                    updateDotAnimation('dot-s2h', 'path-s2h', Math.min(d.solar, d.home)); 
                    updateDotAnimation('dot-s2g', 'path-s2g', d.sell);
                    updateDotAnimation('dot-g2h', 'path-g2h', d.buy);

                } catch(e) { console.error("Update failed", e); }
            }

            setInterval(update, 5000); 
            update();
        </script>
    </body></html>
    """)

if __name__ == "__main__":
    threading.Thread(target=collector, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)