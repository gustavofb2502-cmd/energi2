import os
import time
from contextlib import asynccontextmanager
from typing import Any
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .db import get_conn, init_db

INGEST_TOKEN = os.environ.get("ENERGI2_INGEST_TOKEN", "cambiar-este-token")
AUTHORIZED_DEVICES = {
    d.strip() for d in os.environ.get("ENERGI2_DEVICES", "dev-prueba").split(",") if d.strip()
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="ENERGI 2", lifespan=lifespan)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

class Telemetry(BaseModel):
    schema_version: int = 1
    device_id: str
    source_timestamp: int
    values: dict[str, Any] = Field(default_factory=dict)
    updated_at: dict[str, Any] = Field(default_factory=dict)

def require_token(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Falta token")
    if authorization.removeprefix("Bearer ").strip() != INGEST_TOKEN:
        raise HTTPException(status_code=403, detail="Token invalido")

@app.get("/health")
def health(): return {"status": "ok"}

@app.post("/api/ingest", dependencies=[Depends(require_token)])
def ingest(payload: Telemetry) -> dict[str, Any]:
    if payload.device_id not in AUTHORIZED_DEVICES:
        raise HTTPException(status_code=403, detail="Dispositivo no autorizado")

    now_ms = int(time.time() * 1000)
    values = payload.values
    updated = payload.updated_at
    energy = values.get("total_energy_kwh")
    energy_ts = updated.get("total_energy_kwh")

    with get_conn() as conn:
        cur = conn.cursor() if hasattr(conn, 'cursor') else conn
        is_pg = hasattr(conn, 'cursor')
        
        placeholder = "%s" if is_pg else "?"
        
        # Insertar dispositivo
        cur.execute(f"INSERT INTO devices(device_id, user_id, created_at) VALUES ({placeholder},{placeholder},{placeholder}) ON CONFLICT(device_id) DO NOTHING", 
                    (payload.device_id, "usuario-prueba", now_ms))
        
        # Actualizar latest
        cur.execute(f"""
            INSERT INTO latest_telemetry(
                device_id, schema_version, source_timestamp, received_at,
                voltage, voltage_updated_at, current, current_updated_at,
                power, power_updated_at, energy_kwh, energy_updated_at
            ) VALUES ({placeholder},{placeholder},{placeholder},{placeholder},{placeholder},{placeholder},{placeholder},{placeholder},{placeholder},{placeholder},{placeholder},{placeholder})
            ON CONFLICT(device_id) DO UPDATE SET
                schema_version=EXCLUDED.schema_version, source_timestamp=EXCLUDED.source_timestamp,
                received_at=EXCLUDED.received_at, voltage=EXCLUDED.voltage, voltage_updated_at=EXCLUDED.voltage_updated_at,
                current=EXCLUDED.current, current_updated_at=EXCLUDED.current_updated_at,
                power=EXCLUDED.power, power_updated_at=EXCLUDED.power_updated_at,
                energy_kwh=EXCLUDED.energy_kwh, energy_updated_at=EXCLUDED.energy_updated_at
            """,
            (payload.device_id, payload.schema_version, payload.source_timestamp, now_ms,
             values.get("voltage_v"), updated.get("voltage_v"), values.get("current_a"), updated.get("current_a"),
             values.get("power_w"), updated.get("power_w"), energy, energy_ts))
        
        inserted = 0
        if isinstance(energy, (int, float)) and isinstance(energy_ts, int):
            if is_pg:
                cur.execute(f"INSERT INTO energy_readings(device_id, sampled_at, energy_kwh) VALUES ({placeholder},{placeholder},{placeholder}) ON CONFLICT DO NOTHING",
                            (payload.device_id, energy_ts, float(energy)))
            else:
                cur.execute(f"INSERT OR IGNORE INTO energy_readings(device_id, sampled_at, energy_kwh) VALUES ({placeholder},{placeholder},{placeholder})",
                            (payload.device_id, energy_ts, float(energy)))
            inserted = cur.rowcount
        if not is_pg: conn.commit()

    return {"ok": True, "historical_inserted": inserted}

@app.get("/api/chart_data")
def chart_data(device_id: str):
    with get_conn() as conn:
        cur = conn.cursor() if hasattr(conn, 'cursor') else conn
        is_pg = hasattr(conn, 'cursor')
        placeholder = "%s" if is_pg else "?"
        
        cur.execute(f"SELECT sampled_at, energy_kwh FROM energy_readings WHERE device_id={placeholder} ORDER BY sampled_at ASC", (device_id,))
        rows = cur.fetchall()
        
        if not rows: return {"labels": [], "data": []}

        daily = {}
        for r in rows:
            ts = r['sampled_at'] if isinstance(r, dict) else r[0]
            kwh = r['energy_kwh'] if isinstance(r, dict) else r[1]
            
            dt = datetime.fromtimestamp(ts / 1000)
            day_str = dt.strftime("%d/%m")
            if day_str not in daily: daily[day_str] = {"min": kwh, "max": kwh}
            else: daily[day_str]["max"] = kwh
        
        labels = []
        data = []
        for day, vals in daily.items():
            labels.append(day)
            data.append(round(vals["max"] - vals["min"], 2))

        return {"labels": labels, "data": data}

@app.get("/", response_class=HTMLResponse)
def panel() -> str:
    with get_conn() as conn:
        cur = conn.cursor() if hasattr(conn, 'cursor') else conn
        is_pg = hasattr(conn, 'cursor')
        placeholder = "%s" if is_pg else "?"
        
        cur.execute(f"SELECT device_id FROM latest_telemetry WHERE device_id != 'dev-prueba' LIMIT 1")
        dev = cur.fetchone()
        if not dev: 
            cur.execute("SELECT device_id FROM latest_telemetry LIMIT 1")
            dev = cur.fetchone()
        if not dev: return "<html><body><h2>Esperando datos de la llave...</h2></body></html>"
            
        device_id = dev['device_id'] if isinstance(dev, dict) else dev[0]
        
        cur.execute(f"SELECT * FROM latest_telemetry WHERE device_id={placeholder}", (device_id,))
        latest = cur.fetchone()
        
        now = datetime.now()
        start_of_day = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        start_of_month = int(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        
        def calc_consumo(start_ts):
            cur.execute(f"SELECT energy_kwh FROM energy_readings WHERE device_id={placeholder} AND sampled_at >= {placeholder} ORDER BY sampled_at ASC", (device_id, start_ts))
            rows = cur.fetchall()
            if len(rows) < 2: return 0.0
            last = rows[-1]['energy_kwh'] if isinstance(rows[-1], dict) else rows[-1][0]
            first = rows[0]['energy_kwh'] if isinstance(rows[0], dict) else rows[0][0]
            return round(last - first, 2)
            
        cons_dia = calc_consumo(start_of_day)
        cons_mes = calc_consumo(start_of_month)

        v = latest['voltage'] if (latest['voltage'] if isinstance(latest, dict) else latest[4]) else 0.0
        i = latest['current'] if (latest['current'] if isinstance(latest, dict) else latest[6]) else 0.0
        w = latest['power'] if (latest['power'] if isinstance(latest, dict) else latest[8]) else 0.0
        e_kwh = latest['energy_kwh'] if (latest['energy_kwh'] if isinstance(latest, dict) else latest[10]) else 0.0
        e_ts = latest['energy_updated_at'] if (latest['energy_updated_at'] if isinstance(latest, dict) else latest[11]) else None
        
        alarm_div = ""
        status_color = "#10203a" 
        status_text = "Sistema Normal"
        
        if i > 22.0:
            status_color = "#9e3327"; status_text = "Corte Preventivo Activado"
            alarm_div = '<div style="background:#fff0ee;color:#9e3327;padding:15px;border-radius:12px;margin-top:15px;font-weight:bold;text-align:center;font-size:18px;">¡ALARMA: SOBRECORRIENTE!</div>'
        elif v < 198.0 and v > 0:
            status_color = "#e66f1d"; status_text = "Tensión Peligrosa"
            alarm_div = '<div style="background:#fff7df;color:#7b5a00;padding:15px;border-radius:12px;margin-top:15px;font-weight:bold;text-align:center;font-size:18px;">¡ADVERTENCIA: BAJA TENSIÓN!</div>'

        upd_time = '---'
        if e_ts: upd_time = datetime.fromtimestamp(e_ts/1000).strftime('%d/%m/%Y %H:%M:%S')

    return f"""
    <!DOCTYPE html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
        <meta http-equiv="refresh" content="10"><title>ENERGI 2 - Panel</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body {{ font-family: 'Segoe UI', sans-serif; background-color: #f4f7fb; color: #10203a; margin: 0; padding: 20px; }}
            .container {{ max-width: 800px; margin: 0 auto; }}
            .header {{ background: linear-gradient(135deg, #07101f, #0e1d35); color: white; padding: 20px; border-radius: 16px; display: flex; justify-content: space-between; align-items: center; }}
            .header h1 {{ margin: 0; font-size: 22px; }} .status {{ background: {status_color}; padding: 8px 16px; border-radius: 20px; font-weight: bold; font-size: 14px; }}
            .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 20px; }}
            .card {{ background: white; padding: 24px; border-radius: 16px; border: 1px solid #e6ebf2; }}
            .card p {{ margin: 0; color: #71809a; font-size: 14px; font-weight: 600; }} .card h2 {{ margin: 10px 0 0 0; font-size: 36px; color: #2f7cf6; }}
            .card h2 span {{ font-size: 18px; color: #71809a; }} .main-card {{ grid-column: span 2; text-align: center; padding: 40px 20px; }}
            .main-card h2 {{ font-size: 56px; margin: 0; color: #10203a; }} .main-card .sub {{ color: #71809a; margin-top: 10px; }}
            .metrics-grid {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 15px; margin-top: 20px; }}
            .metric {{ background: white; padding: 20px; border-radius: 16px; text-align: center; border: 1px solid #e6ebf2; }}
            .metric p {{ margin: 0; color: #71809a; font-size: 13px; }} .metric h3 {{ margin: 10px 0 0 0; font-size: 28px; }}
            .chart-container {{ background: white; padding: 20px; border-radius: 16px; margin-top: 20px; border: 1px solid #e6ebf2; }}
            @media (max-width: 600px) {{ .grid {{ grid-template-columns: 1fr; }} .main-card {{ grid-column: span 1; }} .metrics-grid {{ grid-template-columns: 1fr; }} }}
        </style>
    </head><body><div class="container">
        <div class="header"><div><h1>ENERGI 2</h1><p>{device_id}</p></div><div class="status">{status_text}</div></div>
        {alarm_div}
        <div class="metrics-grid">
            <div class="metric"><p>Tensión</p><h3>{v:.1f} <span style="font-size:14px;color:#71809a">V</span></h3></div>
            <div class="metric"><p>Corriente</p><h3 style="color: {'#9e3327' if i > 22 else '#10203a'};">{i:.2f} <span style="font-size:14px;color:#71809a">A</span></h3></div>
            <div class="metric"><p>Potencia</p><h3>{w:.0f} <span style="font-size:14px;color:#71809a">W</span></h3></div>
        </div>
        <div class="grid">
            <div class="card main-card"><p>Consumo Total Acumulado</p><h2>{e_kwh} <span>kWh</span></h2><p class="sub">Actualizado: {upd_time}</p></div>
            <div class="card"><p>Consumo Hoy</p><h2>{cons_dia} <span>kWh</span></h2></div>
            <div class="card"><p>Consumo Este Mes</p><h2>{cons_mes} <span>kWh</span></h2></div>
        </div>
        <div class="chart-container"><h3>Consumo Diario (kWh)</h3><canvas id="myChart" height="100"></canvas></div>
    </div>
    <script>
        fetch('/api/chart_data?device_id={device_id}').then(r => r.json()).then(data => {{
            new Chart(document.getElementById('myChart').getContext('2d'), {{
                type: 'bar', data: {{ labels: data.labels, datasets: [{{ label: 'kWh', data: data.data, backgroundColor: '#2f7cf6', borderRadius: 6 }}] }},
                options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }}, scales: {{ y: {{ beginAtZero: true }} }} }}
            }});
        }});
    </script></body></html>
    """
