import socket, time, threading, requests, os, calendar
from flask import Flask, jsonify, render_template_string, request
from datetime import datetime, timedelta
import pytz
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

# --- 設定（環境変数から取得。Docker Composeで定義） ---
IP = os.environ.get("HEMS_IP", "192.168.0.142")
PORT = 3610
INFLUX_URL = os.environ.get("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN = os.environ.get("INFLUX_TOKEN", "my-super-secret-auth-token")
INFLUX_ORG = os.environ.get("INFLUX_ORG", "my-home")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "energy_bucket")

# ECHONET Lite EOJ: 太陽光[02,79,01], スマートメータ[02,A5,01]
SOLAR_EOJ, METER_EOJ = [0x02, 0x79, 0x01], [0x02, 0xA5, 0x01]
MAX_W, LAT, LON = 5900, 33.46, 130.54
jst = pytz.timezone('Asia/Tokyo')

app = Flask(__name__)
# InfluxDBクライアント初期化
client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write_api = client.write_api(write_options=SYNCHRONOUS)
query_api = client.query_api()

latest = {"solar": 0, "buy": 0, "sell": 0, "home": 0, "cloud": 0, "forecast": 0, "irradiance": 0, "cost_min": 0, "weather_code": 0}

def get_forecast():
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}&hourly=direct_radiation,diffuse_radiation,cloud_cover,weathercode&timezone=Asia%2FTokyo&forecast_days=3"
        res = requests.get(url, timeout=5).json()
        return {res['hourly']['time'][i]: {
            "w": int(MAX_W * ((res['hourly']['direct_radiation'][i] + res['hourly']['diffuse_radiation'][i]) / 1000) * 0.85),
            "cloud": res['hourly']['cloud_cover'][i],
            "irr": res['hourly']['direct_radiation'][i] + res['hourly']['diffuse_radiation'][i],
            "code": res['hourly']['weathercode'][i]
        } for i in range(len(res['hourly']['time']))}
    except: return {}

def fetch_echonet(eoj, epc):
    try:
        frame = bytes([0x10, 0x81, 0x00, 0x01, 0x05, 0xff, 0x01, eoj[0], eoj[1], eoj[2], 0x62, 0x01, epc, 0x00])
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1.0)
            s.sendto(frame, (IP, PORT))
            data, _ = s.recvfrom(1024)
            idx = data.find(bytes([epc]))
            return data[idx+2 : idx+2+data[idx+1]]
    except: return None

def get_unit_price(dt):
    if dt.hour >= 21 or dt.hour < 7: return 16.6
    return 33.8 if dt.month in [7, 8, 9] else 28.6

def collector():
    while True:
        f_map = get_forecast()
        now = datetime.now(jst)
        
        # 1. 実績取得
        res_s, res_m = fetch_echonet(SOLAR_EOJ, 0xE0), fetch_echonet(METER_EOJ, 0xF5)
        solar = int.from_bytes(res_s, "big", signed=True) if res_s else 0
        buy, sell = 0, 0
        if res_m and len(res_m) >= 8:
            val = int.from_bytes(res_m[0:4], "big", signed=True)
            sell, buy = (val, 0) if val >= 0 else (0, abs(val))
        
        home = max(0, solar + buy - sell)
        cost_min = (buy / 1000.0) * (get_unit_price(now) / 60.0)
        
        # 2. 現在時刻の予測値取得
        f_info = f_map.get(now.strftime("%Y-%m-%dT%H:00"), {"w": 0, "cloud": 0, "irr": 0, "code": 0})
        
        # 3. InfluxDBに実績を保存
        p = Point("energy").time(now, WritePrecision.NS) \
            .field("solar", float(solar)).field("buy", float(buy)).field("sell", float(sell)) \
            .field("home", float(home)).field("cloud", float(f_info["cloud"])) \
            .field("forecast", float(f_info["w"])).field("irradiance", float(f_info["irr"])) \
            .field("weather_code", int(f_info["code"]))
        write_api.write(bucket=INFLUX_BUCKET, record=p)

        # 4. 未来の予測枠も保存（点線表示用）
        for ft_str, fi in f_map.items():
            ft_dt = datetime.fromisoformat(ft_str).replace(tzinfo=jst)
            if ft_dt > now:
                pf = Point("energy").time(ft_dt, WritePrecision.NS) \
                    .field("forecast", float(fi["w"])).field("cloud", float(fi["cloud"])) \
                    .field("irradiance", float(fi["irr"])).field("weather_code", int(fi["code"]))
                write_api.write(bucket=INFLUX_BUCKET, record=pf)

        latest.update({"solar": solar, "buy": buy, "sell": sell, "home": home, "cost_min": cost_min})
        time.sleep(60)

@app.route("/api/live")
def api_live():
    solar_self = max(0, latest["solar"] - latest["sell"])
    scr = int((solar_self / max(1, latest["solar"])) * 100) if latest["solar"] > 0 else 0
    # 右上の時計を正確な日本時間で返す
    return jsonify({**latest, "scr": min(scr, 100), "dt": datetime.now(jst).strftime("%Y/%m/%d %H:%M:%S")})

@app.route("/api/stats/<mode>")
def api_stats(mode):
    target = request.args.get('date', datetime.now(jst).strftime("%Y-%m-%d"))
    
    # InfluxDB用クエリ範囲設定
    if mode == "hour":
        start, stop, window = "-60m", "now()", "1m"
        labels = [(datetime.now(jst) - timedelta(minutes=i)).strftime("%H:%M") for i in range(59, -1, -1)]
    elif mode == "day":
        start = f"{target}T00:00:00Z"; stop = f"{target}T23:59:59Z"; window = "30m"
        labels = [f"{h:02}:{m:02}" for h in range(24) for m in [0, 30]]
    elif mode == "month":
        y, m = map(int, target.split('-'))
        start = f"{target}-01T00:00:00Z"; stop = f"{target}-{calendar.monthrange(y,m)[1]}T23:59:59Z"; window = "1d"
        labels = [f"{m:02}/{d:02}" for d in range(1, calendar.monthrange(y, m)[1] + 1)]
    else:
        start = f"{target}-01-01T00:00:00Z"; stop = f"{target}-12-31T23:59:59Z"; window = "1mo"
        labels = [f"{m:02}月" for m in range(1, 13)]

    query = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {start}, stop: {stop})
      |> aggregateWindow(every: {window}, fn: mean, createEmpty: true)
      |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''
    result = query_api.query(query)
    
    db_data = {}
    total_nb, total_ds = 0, 0
    
    for table in result:
        for r in table.records:
            t = r.get_time().astimezone(jst)
            lbl = t.strftime("%H:%M") if mode in ["hour", "day"] else t.strftime("%m/%d") if mode=="month" else t.strftime("%m月")
            db_data[lbl] = r.values
            
            # 集計ロジック（SQLite版 SUM(...)/60.0 を再現）
            if mode == "day" and r.values.get("buy") is not None:
                interval_hours = 0.5 # 30分間隔
                if t.hour >= 21 or t.hour < 7: total_nb += (r.values.get("buy") or 0) * interval_hours
                if 7 <= t.hour < 21: total_ds += (r.values.get("sell") or 0) * interval_hours

    chart_res = []
    for lbl in labels:
        r = db_data.get(lbl, {})
        chart_res.append({
            "label": lbl, "solar": r.get("solar"), "forecast": r.get("forecast"),
            "sell": r.get("sell"), "buy": r.get("buy"), "home": r.get("home")
        })

    list_res = chart_res if mode != "day" else []
    if mode == "day":
        for h in range(24):
            lbl = f"{h:02}:00"
            r = db_data.get(lbl, {})
            list_res.append({"lbl": lbl, "s": r.get("solar"), "f": r.get("forecast"), "sl": r.get("sell"), 
                             "b": r.get("buy"), "h": r.get("home"), "c": r.get("cloud"), "i": r.get("irradiance"), "wc": r.get("weather_code")})

    return jsonify({
        "chart": chart_res, "list": list_res, 
        "total": {"nb": round(total_nb/1000.0, 2), "ds": round(total_ds/1000.0, 2), "bat": round(min(total_nb, total_ds)/900.0, 2)}
    })

@app.route("/")
def index():
    return render_template_string("""
    <!DOCTYPE html><html><head><meta charset="UTF-8"><script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: sans-serif; background: #f8fafc; margin: 0; padding: 20px; }
        .clock { position: absolute; top: 15px; right: 20px; font-weight: bold; color: #475569; }
        .live-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; margin-top: 30px; margin-bottom: 25px; }
        .live-card { background: #fff; padding: 25px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); }
        .live-card span { font-size: 1.1rem; font-weight: 800; display: block; margin-bottom: 5px; color: #94a3b8; }
        .live-val { font-size: 2.2rem; font-weight: 900; line-height: 1; }
        .main-box { background: #fff; border-radius: 16px; padding: 25px; }
        .nav { display: flex; justify-content: space-between; margin-bottom: 15px; flex-wrap: wrap; gap: 10px; }
        .btn-group { display: flex; gap: 5px; background: #f1f5f9; padding: 5px; border-radius: 10px; }
        button { border: none; padding: 8px 15px; border-radius: 6px; cursor: pointer; font-weight: bold; }
        button.active { background: #fff; box-shadow: 0 2px 4px rgba(0,0,0,0.1); color: #2563eb; }
        .hidden { display: none; }
        table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
        th { position: sticky; top: 0; background: #f8fafc; padding: 12px; border-bottom: 2px solid #e2e8f0; }
        td { padding: 12px; border-bottom: 1px solid #f1f5f9; text-align: center; }
        .weather-ico { font-size: 2rem; }
        .stat-bar { margin-top: 15px; font-weight: bold; color: #1e40af; background: #eff6ff; padding: 12px; border-radius: 8px; }
    </style></head>
    <body>
        <div class="clock" id="clock">0000/00/00 00:00:00</div>
        <div class="live-grid">
            <div class="live-card"><span>☀️ 発電</span><div class="live-val" id="ls" style="color:#f59e0b">0W</div></div>
            <div class="live-card"><span>📤 売電</span><div class="live-val" id="lsl" style="color:#10b981">0W</div></div>
            <div class="live-card"><span>🔌 買電</span><div class="live-val" id="lb" style="color:#ef4444">0W</div></div>
            <div class="live-card"><span>🏠 自己消費</span><div class="live-val" id="lh" style="color:#8b5cf6">0W</div></div>
            <div class="live-card"><span>💴 電気代(分)</span><div class="live-val" id="lc" style="color:#475569">0.0円</div></div>
            <div class="live-card"><span>📈 自己消費率</span><div class="live-val" id="lscr" style="color:#06b6d4">0%</div></div>
        </div>
        <div class="main-box">
            <div class="nav">
                <div class="btn-group"><button onclick="setM('hour',this)" class="m-btn active">1時間</button><button onclick="setM('day',this)" class="m-btn">日</button><button onclick="setM('month',this)" class="m-btn">月</button><button onclick="setM('year',this)" class="m-btn">年</button></div>
                <div><input type="date" id="dp" onchange="update()"><input type="month" id="mp" onchange="update()" class="hidden"><select id="yp" onchange="update()" class="hidden"></select></div>
                <div class="btn-group"><button onclick="setV('chart',this)" class="v-btn active">グラフ</button><button onclick="setV('list',this)" class="v-btn">リスト</button></div>
            </div>
            <div id="cp" style="height:50vh;"><canvas id="mainChart"></canvas></div>
            <div id="lp" class="hidden">
                <div style="max-height:50vh; overflow-y:auto; border:1px solid #e2e8f0; border-radius:12px;">
                    <table><thead><tr><th>時間軸</th><th>発電</th><th>予測</th><th>売電</th><th>買電</th><th>自己消費</th><th>自己消費率</th><th>天気</th><th>雲量</th><th>日射</th></tr></thead><tbody id="tb"></tbody></table>
                </div>
                <div id="sb" class="stat-bar"></div>
            </div>
        </div>
        <script>
            let chart; let mode='hour'; let view='chart';
            const wEmoji = {0:"☀️", 1:"☀️", 2:"⛅", 3:"☁️", 45:"🌫️", 51:"🌦️", 61:"☔", 71:"❄️", 95:"⚡"};
            const yp = document.getElementById('yp'); const now = new Date();
            for(let y=2024; y<=now.getFullYear()+1; y++){ let o=document.createElement('option'); o.value=y; o.text=y+'年'; yp.add(o); }
            yp.value = now.getFullYear(); document.getElementById('dp').valueAsDate = now; document.getElementById('mp').value = now.toISOString().slice(0,7);

            async function update() {
                const live = await (await fetch('/api/live')).json();
                document.getElementById('clock').innerText = live.dt;
                document.getElementById('ls').innerText = live.solar + 'W'; document.getElementById('lsl').innerText = live.sell + 'W';
                document.getElementById('lb').innerText = live.buy + 'W'; document.getElementById('lh').innerText = live.home + 'W';
                document.getElementById('lc').innerText = live.cost_min.toFixed(1) + '円'; document.getElementById('lscr').innerText = live.scr + '%';
                
                let dv = (mode==='day')?document.getElementById('dp').value:(mode==='month')?document.getElementById('mp').value:yp.value;
                const res = await (await fetch(`/api/stats/${mode}?date=${dv}`)).json();
                
                if(view==='chart') {
                    const ctx = document.getElementById('mainChart').getContext('2d');
                    if(chart) chart.destroy();
                    chart = new Chart(ctx, { type: 'line', data: {
                        labels: res.chart.map(d => d.label),
                        datasets: [
                            { label:'発電', data:res.chart.map(d=>d.solar), borderColor:'#f59e0b', tension:0.4, pointRadius: ctx => ctx.raw === null ? 0 : 3, fill:true, backgroundColor:'rgba(245,158,11,0.05)', spanGaps:true },
                            { label:'自家消費', data:res.chart.map(d=>d.home), borderColor:'#8b5cf6', tension:0.4, pointRadius: ctx => ctx.raw === null ? 0 : 3, spanGaps:true },
                            { label:'売電', data:res.chart.map(d=>d.sell), borderColor:'#10b981', tension:0.4, pointRadius: ctx => ctx.raw === null ? 0 : 3, spanGaps:true },
                            { label:'買電', data:res.chart.map(d=>d.buy), borderColor:'#ef4444', tension:0.4, pointRadius: ctx => ctx.raw === null ? 0 : 3, spanGaps:true },
                            { label:'予測', data:res.chart.map(d=>d.forecast), borderColor:'#94a3b8', borderDash:[5,5], pointRadius: ctx => ctx.raw === null ? 0 : 3, spanGaps:true }
                        ]}, options: { maintainAspectRatio:false, scales:{x:{ticks:{callback:function(v,i){ const l=this.getLabelForValue(v); return (mode==='day'&&!l.endsWith(':00'))?'':(mode==='hour'&&i%5!==0)?'':l; }}}}}});
                }
                let h = ''; const unit = mode==='day'?'kWh':'W';
                res.list.forEach(d => {
                    const s = d.s || 0; const sl = d.sl || 0;
                    const scr = s > 0 ? Math.min(100, Math.round(((s - sl)/s)*100)) : 0;
                    h += `<tr><td>${d.lbl}</td><td>${Math.round(s)}${unit}</td><td>${d.f !== null ? Math.round(d.f) + 'W' : '-'}</td><td>${Math.round(sl)}${unit}</td><td>${Math.round(d.b || 0)}${unit}</td><td>${Math.round(d.h || 0)}${unit}</td><td>${scr}%</td><td class="weather-ico">${wEmoji[d.wc]||'-'}</td><td>${d.c!==null?d.c+'%':'-'}</td><td>${d.i!==null?Math.round(d.i):'-'}</td></tr>`;
                });
                document.getElementById('tb').innerHTML = h;
                document.getElementById('sb').innerText = `夜間買電: ${res.total.nb}kWh / 昼間売電: ${res.total.ds}kWh / 推奨電池容量: ${res.total.bat}kWh`;
            }
            function setM(m,b){ mode=m; document.querySelectorAll('.m-btn').forEach(x=>x.classList.remove('active')); b.classList.add('active'); document.getElementById('dp').classList.toggle('hidden',m!=='day'); document.getElementById('mp').classList.toggle('hidden',m!=='month'); yp.classList.toggle('hidden',m!=='year'); update(); }
            function setV(v,b){ view=v; document.getElementById('cp').classList.toggle('hidden',v!=='chart'); document.getElementById('lp').classList.toggle('hidden',v!=='list'); document.querySelectorAll('.v-btn').forEach(x=>x.classList.remove('active')); b.classList.add('active'); update(); }
            setInterval(update, 60000); update();
        </script>
    </body></html>
    """)

if __name__ == "__main__":
    threading.Thread(target=collector, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)