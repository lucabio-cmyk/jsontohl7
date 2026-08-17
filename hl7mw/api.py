"""
hl7mw.api — REST API per gestione ordini, statistiche, strumenti, audit.

Rimpiazza webstatus.py con operazioni complete:
- Dashboard: statistiche globali, throughput, timing medio
- Ordini: lista, dettaglio, retry, cancel
- Strumenti: status heartbeat, performance
- Audit: log tracciabilità clinica
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from .store import Store
from . import vpn as vpnmod

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="HL7 Middleware API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_store: Store | None = None
_config_path: str = "config.json"
_config_defaults: dict = {}

def init_api(store: Store, config_path: str = "config.json", defaults: dict | None = None) -> FastAPI:
    """Inizializza API con referenza al database e al file di configurazione
    (per la pagina Impostazioni: GET/PUT /api/config)."""
    global _store, _config_path, _config_defaults
    _store = store
    _config_path = config_path
    _config_defaults = dict(defaults or {})
    return app


@app.get("/health")
async def health():
    """Health check."""
    return {"status": "ok"}


def _read_config() -> dict:
    cfg = dict(_config_defaults)
    p = Path(_config_path)
    if p.exists():
        cfg.update(json.loads(p.read_text(encoding="utf-8")))
    return cfg


def _validate_config_update(payload: dict) -> dict:
    """Valida il payload della GUI Impostazioni contro le chiavi/i tipi noti
    (hl7mw.run.DEFAULTS): rifiuta chiavi sconosciute, coerce i tipi. Non c'e'
    validazione applicativa più fine (es. range porte): resta responsabilita'
    dell'operatore, coerente con l'assenza di autenticazione sull'API (vedi
    CLAUDE.md 'Da fare' — TLS/auth)."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload non valido: atteso un oggetto JSON")
    unknown = sorted(set(payload) - set(_config_defaults))
    if unknown:
        raise HTTPException(status_code=400, detail=f"Chiavi di configurazione sconosciute: {unknown}")
    validated: dict = {}
    for key, value in payload.items():
        expected = type(_config_defaults[key])
        try:
            if expected is bool:
                if not isinstance(value, bool):
                    raise ValueError
                validated[key] = value
            elif expected is int:
                validated[key] = int(value)
            elif expected is float:
                validated[key] = float(value)
            else:
                validated[key] = "" if value is None else str(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{key}: valore non valido per il tipo atteso ({expected.__name__})")
    return validated


@app.get("/api/config")
async def get_config():
    """Configurazione corrente (default + config.json), per la pagina Impostazioni."""
    p = Path(_config_path)
    return {"config": _read_config(), "config_path": str(p), "file_exists": p.exists()}


@app.put("/api/config")
async def update_config(payload: dict):
    """Salva la configurazione su file. Non applicata a runtime: i componenti
    (LIS, VPN, adapter strumenti) sono inizializzati una sola volta all'avvio —
    serve un riavvio del servizio perché le modifiche abbiano effetto."""
    validated = _validate_config_update(payload)
    cfg = _read_config()
    cfg.update(validated)
    p = Path(_config_path)
    try:
        p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Scrittura di {p} fallita: {e}")
    if _store:
        _store.audit_log("config_updated", details=f"{sorted(validated)}", severity="INFO")
    return {"status": "ok", "config_path": str(p), "restart_required": True}


@app.get("/api/vpn/check")
async def check_vpn(host: str = Query(...), port: int = Query(..., ge=1, le=65535),
                    timeout: float = Query(5.0, ge=0.1, le=30.0)):
    """Verifica on-demand la raggiungibilita' TCP di host:port (tipicamente
    attraverso il tunnel VPN esistente) — non avvia/ferma nessun tunnel."""
    mgr = vpnmod.VpnManager(health_check_host=host, health_check_port=port, health_check_timeout=timeout)
    return {"host": host, "port": port, "reachable": mgr.is_reachable()}


@app.get("/api/dashboard")
async def get_dashboard():
    """Statistiche globali: conteggi, timing medio, instrument status."""
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")
    stats = _store.get_dashboard_stats()
    return stats


@app.get("/api/orders")
def list_orders(
    status: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """Elenco ordini con filtri."""
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")

    with _store._conn() as c:
        if status:
            rows = c.execute(
                "SELECT * FROM orders WHERE status=? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM orders ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/orders/{sample_key}")
async def get_order_detail(sample_key: str):
    """Dettaglio ordine con risultati e timing."""
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")

    order = _store.get_order(sample_key)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    results = _store.results_for(sample_key)
    timing = _store.get_timing(sample_key)

    return {
        "order": dict(order),
        "results": results,
        "timing": dict(timing) if timing else None,
    }


@app.post("/api/orders/{sample_key}/retry")
async def retry_order(sample_key: str):
    """Riporta un ordine ERROR a READY per il retry."""
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")

    order = _store.get_order(sample_key)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order["status"] not in ("ERROR", "READY"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot retry order in status {order['status']}",
        )

    _store.set_status(sample_key, "READY")
    _store.audit_log(
        "order_retry",
        sample_key=sample_key,
        details=f"Manual retry from {order['status']}",
        severity="INFO",
    )
    return {"status": "ok", "order_status": "READY"}


@app.post("/api/orders/{sample_key}/cancel")
async def cancel_order(sample_key: str):
    """Cancella un ordine (lo mette in ERROR con motivo 'CANCELLED')."""
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")

    order = _store.get_order(sample_key)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order["status"] == "SENT":
        raise HTTPException(
            status_code=400,
            detail="Cannot cancel order already sent",
        )

    _store.set_status(sample_key, "ERROR", "CANCELLED by user")
    _store.audit_log(
        "order_cancelled",
        sample_key=sample_key,
        severity="WARNING",
    )
    return {"status": "ok", "order_status": "ERROR"}


@app.get("/api/unmatched")
async def get_unmatched(limit: int = Query(100, ge=1, le=1000)):
    """Risultati orfani (senza ordine corrispondente)."""
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")

    with _store._conn() as c:
        rows = c.execute(
            "SELECT * FROM unmatched_results ORDER BY received_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/unmatched/{result_id}/match")
async def match_unmatched(result_id: int, sample_key: str):
    """Associa manualmente un risultato orfano a un ordine."""
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")

    success = _store.match_unmatched(result_id, sample_key)
    if not success:
        raise HTTPException(status_code=404, detail="Unmatched result not found or order doesn't exist")

    _store.audit_log(
        "unmatched_matched",
        sample_key=sample_key,
        details=f"Manually matched result {result_id}",
        severity="INFO",
    )
    return {"status": "ok"}


@app.get("/api/instruments")
async def list_instruments():
    """Status di tutti gli strumenti collegati."""
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")
    instruments = _store.get_instruments()
    return instruments


@app.get("/api/instruments/{name}")
async def get_instrument_detail(name: str):
    """Dettaglio strumento con statistiche di messaggi."""
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")

    instr = _store.get_instrument(name)
    if not instr:
        raise HTTPException(status_code=404, detail="Instrument not found")

    with _store._conn() as c:
        msg_count = c.execute(
            "SELECT COUNT(*) n FROM results WHERE source_instrument=?", (name,)
        ).fetchone()["n"]

    instr["results_sent"] = msg_count
    return dict(instr)


@app.get("/api/audit-log")
async def get_audit_log(
    sample_key: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=2000),
):
    """Log tracciabilità clinica."""
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")

    with _store._conn() as c:
        query = "SELECT * FROM audit_log WHERE 1=1"
        params = []
        if sample_key:
            query += " AND sample_key=?"
            params.append(sample_key)
        if event_type:
            query += " AND event_type=?"
            params.append(event_type)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = c.execute(query, params).fetchall()
    return [dict(r) for r in rows]


@app.get("/")
async def get_ui():
    """Serve la dashboard HTML."""
    return HTMLResponse(get_dashboard_html())


@app.get("/static/chart.min.js")
async def get_chart_js():
    """Chart.js vendorizzato localmente (vedi hl7mw/static/): la dashboard non
    deve dipendere da un CDN esterno raggiungibile — reti cliniche/di laboratorio
    spesso bloccano l'accesso a internet per policy."""
    return FileResponse(STATIC_DIR / "chart.umd.min.js", media_type="application/javascript")


def get_dashboard_html() -> str:
    """HTML della dashboard con statsatistiche e gestione ordini."""
    return """<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>HL7 Middleware Dashboard</title>
    <script src="/static/chart.min.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
            color: #333;
        }
        .container { max-width: 1400px; margin: 0 auto; }
        .header {
            background: white;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        .header h1 { font-size: 28px; margin-bottom: 10px; }
        .header .subtitle { color: #666; font-size: 14px; }

        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin-bottom: 20px; }
        .card {
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            border-left: 4px solid #667eea;
        }
        .card h3 { font-size: 12px; color: #999; text-transform: uppercase; margin-bottom: 10px; }
        .card .value { font-size: 32px; font-weight: bold; color: #667eea; }
        .card .detail { font-size: 12px; color: #999; margin-top: 10px; }

        .card.error { border-left-color: #e74c3c; }
        .card.error .value { color: #e74c3c; }

        .card.success { border-left-color: #27ae60; }
        .card.success .value { color: #27ae60; }

        .card.warning { border-left-color: #f39c12; }
        .card.warning .value { color: #f39c12; }

        .card.info { border-left-color: #3498db; }
        .card.info .value { color: #3498db; }

        .charts-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); gap: 20px; margin-bottom: 20px; }
        .chart-container {
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            position: relative;
            height: 300px;
        }
        .chart-container h3 { font-size: 14px; margin-bottom: 15px; font-weight: 600; }

        .table-container {
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            overflow-x: auto;
        }
        .table-container h3 { font-size: 14px; margin-bottom: 15px; font-weight: 600; }

        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 12px;
        }
        th {
            background: #f8f9fa;
            padding: 10px;
            text-align: left;
            font-weight: 600;
            border-bottom: 2px solid #ddd;
        }
        td {
            padding: 10px;
            border-bottom: 1px solid #eee;
        }
        tr:hover { background: #f8f9fa; }

        .badge {
            display: inline-block;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
        }
        .badge.online { background: #d4edda; color: #155724; }
        .badge.offline { background: #f8d7da; color: #721c24; }
        .badge.unknown { background: #e2e3e5; color: #383d41; }
        .badge.received { background: #cfe2ff; color: #084298; }
        .badge.ready { background: #cff4fc; color: #055160; }
        .badge.forwarding { background: #e2d9f3; color: #432874; }
        .badge.sent { background: #d4edda; color: #155724; }
        .badge.error { background: #f8d7da; color: #721c24; }

        .action-button {
            background: #667eea;
            color: white;
            border: none;
            padding: 6px 12px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 11px;
            margin-right: 5px;
            transition: background 0.2s;
        }
        .action-button:hover { background: #5568d3; }
        .action-button.danger {
            background: #e74c3c;
        }
        .action-button.danger:hover {
            background: #c0392b;
        }

        .last-update {
            text-align: right;
            color: #999;
            font-size: 12px;
            margin-top: 10px;
        }

        .instruments-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }
        .instrument-card {
            background: white;
            padding: 15px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        .instrument-card .name { font-weight: 600; margin-bottom: 10px; }
        .instrument-card .info { font-size: 12px; color: #666; margin: 5px 0; }

        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.5);
            z-index: 1000;
        }
        .modal.show { display: flex; align-items: center; justify-content: center; }
        .modal-content {
            background: white;
            padding: 30px;
            border-radius: 8px;
            max-width: 600px;
            max-height: 80vh;
            overflow-y: auto;
            box-shadow: 0 4px 20px rgba(0,0,0,0.3);
        }
        .modal-content h2 { margin-bottom: 15px; }
        .modal-content.wide { max-width: 900px; }
        .modal-content .close {
            float: right;
            font-size: 24px;
            font-weight: bold;
            cursor: pointer;
            color: #999;
        }
        .modal-content .close:hover { color: #333; }

        .header-actions { float: right; }
        .header { overflow: hidden; }

        .settings-note {
            background: #fff3cd;
            color: #664d03;
            padding: 10px 14px;
            border-radius: 6px;
            font-size: 12px;
            margin-bottom: 15px;
        }
        .settings-section {
            font-size: 13px;
            font-weight: 600;
            color: #667eea;
            text-transform: uppercase;
            margin: 20px 0 10px;
            border-bottom: 1px solid #eee;
            padding-bottom: 6px;
        }
        .settings-section:first-of-type { margin-top: 0; }
        .settings-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 12px 20px;
        }
        .settings-field { display: flex; flex-direction: column; font-size: 12px; color: #444; gap: 4px; }
        .settings-field.checkbox { flex-direction: row; align-items: center; gap: 8px; font-size: 13px; }
        .settings-field input[type=text], .settings-field input[type=number], .settings-field select {
            padding: 6px 8px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 13px;
        }
        .settings-actions {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-top: 20px;
            padding-top: 15px;
            border-top: 1px solid #eee;
        }
        #vpnCheckResult { font-size: 12px; font-weight: 600; }

        pre { background: #f5f5f5; padding: 10px; border-radius: 4px; overflow-x: auto; font-size: 11px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="header-actions">
                <button class="action-button" onclick="openSettings()">⚙ Impostazioni</button>
            </div>
            <h1>HL7 Middleware — Dashboard</h1>
            <div class="subtitle">Monitoraggio ordini, strumenti, statistiche</div>
        </div>

        <!-- KPIs -->
        <div class="grid" id="stats"></div>

        <!-- Charts -->
        <div class="charts-grid">
            <div class="chart-container">
                <h3>Distribuzione Stato Ordini</h3>
                <canvas id="statusChart"></canvas>
            </div>
            <div class="chart-container">
                <h3>Strumenti Collegati</h3>
                <canvas id="instrumentChart"></canvas>
            </div>
        </div>

        <!-- Strumenti -->
        <div class="instruments-grid" id="instruments"></div>

        <!-- Ordini -->
        <div class="table-container">
            <h3>Ultimi Ordini (status: <span id="filterStatus">TUTTI</span>)</h3>
            <table id="ordersTable">
                <thead>
                    <tr>
                        <th>Sample Key</th>
                        <th>Stato</th>
                        <th>Creato</th>
                        <th>Aggiornato</th>
                        <th>Azioni</th>
                    </tr>
                </thead>
                <tbody></tbody>
            </table>
            <div class="last-update">Aggiornamento automatico ogni 5s</div>
        </div>
    </div>

    <!-- Order Detail Modal -->
    <div class="modal" id="orderModal">
        <div class="modal-content">
            <span class="close" onclick="closeModal()">&times;</span>
            <h2 id="orderModalTitle">Dettaglio Ordine</h2>
            <div id="orderModalBody"></div>
        </div>
    </div>

    <!-- Settings Modal -->
    <div class="modal" id="settingsModal">
        <div class="modal-content wide">
            <span class="close" onclick="closeSettings()">&times;</span>
            <h2>Impostazioni</h2>
            <div class="settings-note">
                Le modifiche vengono salvate su <code id="settingsPath"></code> ma non applicate a
                runtime: LIS, VPN e adapter strumenti sono inizializzati all'avvio — riavvia il
                servizio dopo il salvataggio perché abbiano effetto.
            </div>
            <div id="settingsForm"></div>
            <div class="settings-actions">
                <button id="saveSettingsBtn" class="action-button" onclick="saveSettings()">Salva</button>
                <button class="action-button danger" onclick="closeSettings()">Annulla</button>
            </div>
        </div>
    </div>

    <script>
        let statusChart = null;
        let instrumentChart = null;

        async function updateDashboard() {
            try {
                // Dashboard stats
                const statsResp = await fetch('/api/dashboard');
                const stats = await statsResp.json();
                renderStats(stats);
                // Il grafico non deve poter bloccare il resto della dashboard
                // (es. Chart.js non caricato): errori qui restano isolati.
                try { renderStatusChart(stats); } catch (e) { console.error('Grafico non disponibile:', e); }

                // Instruments
                const instrResp = await fetch('/api/instruments');
                const instruments = await instrResp.json();
                renderInstruments(instruments);
                try { renderInstrumentChart(instruments); } catch (e) { console.error('Grafico strumenti non disponibile:', e); }

                // Orders
                const ordersResp = await fetch('/api/orders?limit=20');
                const orders = await ordersResp.json();
                renderOrders(orders);
            } catch (e) {
                console.error('Dashboard update failed:', e);
            }
        }

        function renderStats(stats) {
            const statCards = [
                { title: 'ORDINI TOTALI', value: stats.total_orders, class: 'info' },
                { title: 'RICEVUTI', value: stats.status_counts.RECEIVED || 0, class: 'warning' },
                { title: 'PRONTI', value: stats.status_counts.READY || 0, class: 'info' },
                { title: 'INOLTRATI', value: stats.status_counts.SENT || 0, class: 'success' },
                { title: 'ERRORI', value: stats.status_counts.ERROR || 0, class: 'error' },
                { title: 'RISULTATI ORFANI', value: stats.unmatched_results, class: 'warning' },
                { title: 'STRUMENTI ONLINE', value: stats.instruments.online + '/' + stats.instruments.total, class: 'info' },
                { title: 'TEMPO MEDIO (RECEIVED→READY)', value: stats.avg_time_to_ready_seconds ? stats.avg_time_to_ready_seconds.toFixed(1) + 's' : '-', class: 'info' },
            ];

            document.getElementById('stats').innerHTML = statCards.map(s =>
                `<div class="card ${s.class}"><h3>${s.title}</h3><div class="value">${s.value}</div></div>`
            ).join('');
        }

        function renderStatusChart(stats) {
            const ctx = document.getElementById('statusChart').getContext('2d');
            if (statusChart) statusChart.destroy();

            statusChart = new Chart(ctx, {
                type: 'doughnut',
                data: {
                    labels: Object.keys(stats.status_counts),
                    datasets: [{
                        data: Object.values(stats.status_counts),
                        backgroundColor: ['#3498db', '#f39c12', '#27ae60', '#e74c3c', '#95a5a6'],
                        borderColor: 'white',
                        borderWidth: 2,
                    }],
                },
                options: { responsive: true, maintainAspectRatio: false },
            });
        }

        function renderInstrumentChart(instruments) {
            const ctx = document.getElementById('instrumentChart').getContext('2d');
            if (instrumentChart) instrumentChart.destroy();

            const colors = { ONLINE: '#27ae60', OFFLINE: '#e74c3c', UNKNOWN: '#95a5a6' };
            const counts = {};
            instruments.forEach(i => {
                const s = (i.status || 'UNKNOWN').toUpperCase();
                counts[s] = (counts[s] || 0) + 1;
            });
            const labels = Object.keys(counts);

            instrumentChart = new Chart(ctx, {
                type: 'doughnut',
                data: {
                    labels: labels,
                    datasets: [{
                        data: labels.map(l => counts[l]),
                        backgroundColor: labels.map(l => colors[l] || '#3498db'),
                        borderColor: 'white',
                        borderWidth: 2,
                    }],
                },
                options: { responsive: true, maintainAspectRatio: false },
            });
        }

        function renderInstruments(instruments) {
            document.getElementById('instruments').innerHTML = instruments.map(i =>
                `<div class="instrument-card">
                    <div class="name">${i.name}</div>
                    ${i.host ? `<div class="info"><strong>Host:</strong> ${i.host}${i.port ? ':' + i.port : ''}</div>` : ''}
                    <div class="info"><strong>Tipo:</strong> ${i.type}</div>
                    <div class="info"><strong>Status:</strong> <span class="badge ${i.status.toLowerCase()}">${i.status}</span></div>
                    <div class="info"><strong>Ultimi messaggi:</strong> ${i.last_message_at || 'mai'}</div>
                    <div class="info"><strong>Totale messaggi:</strong> ${i.messages_received}</div>
                </div>`
            ).join('');
        }

        function renderOrders(orders) {
            const tbody = document.querySelector('#ordersTable tbody');
            tbody.innerHTML = orders.map(o =>
                `<tr>
                    <td><strong>${o.sample_key}</strong></td>
                    <td><span class="badge ${o.status.toLowerCase()}">${o.status}</span></td>
                    <td>${o.created_at.substring(0, 19)}</td>
                    <td>${o.updated_at.substring(0, 19)}</td>
                    <td>
                        <button class="action-button" onclick="viewOrder('${o.sample_key}')">Dettagli</button>
                        ${o.status === 'ERROR' ? `<button class="action-button" onclick="retryOrder('${o.sample_key}')">Retry</button>` : ''}
                        ${o.status !== 'SENT' ? `<button class="action-button danger" onclick="cancelOrder('${o.sample_key}')">Cancella</button>` : ''}
                    </td>
                </tr>`
            ).join('');
        }

        async function viewOrder(sampleKey) {
            const resp = await fetch(`/api/orders/${sampleKey}`);
            const data = await resp.json();
            const modal = document.getElementById('orderModal');
            const body = document.getElementById('orderModalBody');

            let html = `
                <div><strong>Sample Key:</strong> ${sampleKey}</div>
                <div><strong>Status:</strong> <span class="badge ${data.order.status.toLowerCase()}">${data.order.status}</span></div>
                <div><strong>Creato:</strong> ${data.order.created_at}</div>
                <div><strong>Aggiornato:</strong> ${data.order.updated_at}</div>
                <hr style="margin: 15px 0; border: none; border-bottom: 1px solid #ddd;">
                <h3 style="margin-top: 20px;">Ordine (JSON)</h3>
                <pre>${JSON.stringify(JSON.parse(data.order.order_json), null, 2)}</pre>
                <h3 style="margin-top: 20px;">Risultati</h3>
                ${data.results.length > 0 ? data.results.map((r, i) =>
                    `<div style="margin-bottom: 10px;"><strong>Risultato ${i+1}:</strong><pre>${JSON.stringify(r, null, 2)}</pre></div>`
                ).join('') : '<p>Nessun risultato</p>'}
                <h3 style="margin-top: 20px;">Timing</h3>
                ${data.timing ? `<pre>${JSON.stringify(data.timing, null, 2)}</pre>` : '<p>-</p>'}
            `;

            body.innerHTML = html;
            modal.classList.add('show');
        }

        function closeModal() {
            document.getElementById('orderModal').classList.remove('show');
        }

        async function retryOrder(sampleKey) {
            if (!confirm("Riprovare l'inoltro di questo ordine?")) return;
            const resp = await fetch(`/api/orders/${sampleKey}/retry`, { method: 'POST' });
            if (resp.ok) {
                alert('Ordine rimesso in coda per retry');
                updateDashboard();
            } else {
                alert('Errore nel retry');
            }
        }

        async function cancelOrder(sampleKey) {
            if (!confirm('Cancellare definitivamente questo ordine?')) return;
            const resp = await fetch(`/api/orders/${sampleKey}/cancel`, { method: 'POST' });
            if (resp.ok) {
                alert('Ordine cancellato');
                updateDashboard();
            } else {
                alert('Errore nella cancellazione');
            }
        }

        // ---------------------------------------------------------------- Impostazioni
        const CONFIG_FIELDS = [
            // LIS
            { key: 'lis_host', label: 'LIS — host', type: 'text', section: 'LIS' },
            { key: 'lis_port', label: 'LIS — porta', type: 'number', section: 'LIS' },
            { key: 'order_listen_host', label: 'Ordini in ingresso — host', type: 'text', section: 'LIS' },
            { key: 'order_listen_port', label: 'Ordini in ingresso — porta', type: 'number', section: 'LIS' },
            { key: 'sending_app', label: 'Sending App (nostro)', type: 'text', section: 'LIS' },
            { key: 'sending_facility', label: 'Sending Facility (nostro)', type: 'text', section: 'LIS' },
            { key: 'receiving_app', label: 'Receiving App (LIS)', type: 'text', section: 'LIS' },
            { key: 'receiving_facility', label: 'Receiving Facility (LIS)', type: 'text', section: 'LIS' },
            { key: 'forward_interval_seconds', label: 'Intervallo inoltro (s)', type: 'number', step: '0.5', section: 'LIS' },
            { key: 'ack_retry_attempts', label: 'Tentativi retry ACK', type: 'number', section: 'LIS' },
            { key: 'ack_retry_backoff_seconds', label: 'Backoff retry (s)', type: 'number', step: '0.1', section: 'LIS' },

            // Strumenti
            { key: 'result_listen_host', label: 'Risultati in ingresso — host', type: 'text', section: 'Strumenti' },
            { key: 'result_listen_port', label: 'Risultati in ingresso — porta', type: 'number', section: 'Strumenti' },
            { key: 'device_offline_timeout_seconds', label: 'Timeout offline strumenti (s)', type: 'number', section: 'Strumenti' },
            { key: 'hemoscreen_hl7_enabled', label: 'HemoScreen HL7 abilitato', type: 'checkbox', section: 'Strumenti' },
            { key: 'hemoscreen_hl7_host', label: 'HemoScreen HL7 — host', type: 'text', section: 'Strumenti' },
            { key: 'hemoscreen_hl7_port', label: 'HemoScreen HL7 — porta', type: 'number', section: 'Strumenti' },
            { key: 'hemoscreen_poct1a2_enabled', label: 'HemoScreen POCT1-A2 abilitato', type: 'checkbox', section: 'Strumenti' },
            { key: 'hemoscreen_poct1a2_host', label: 'HemoScreen POCT1-A2 — host', type: 'text', section: 'Strumenti' },
            { key: 'hemoscreen_poct1a2_port', label: 'HemoScreen POCT1-A2 — porta', type: 'number', section: 'Strumenti' },
            { key: 'hemoscreen_poct1a2_continuous_mode', label: 'HemoScreen modalità continua', type: 'checkbox', section: 'Strumenti' },
            { key: 'hemoscreen_poct1a2_timeout', label: 'HemoScreen timeout (s)', type: 'number', step: '0.5', section: 'Strumenti' },

            // VPN
            { key: 'vpn_enabled', label: 'VPN abilitata', type: 'checkbox', section: 'VPN' },
            { key: 'vpn_provider', label: 'Provider', type: 'select', options: ['external', 'wireguard', 'openvpn'], section: 'VPN' },
            { key: 'vpn_manage_lifecycle', label: 'Il middleware gestisce avvio/arresto tunnel', type: 'checkbox', section: 'VPN' },
            { key: 'vpn_interface', label: 'Interfaccia (wg-quick / systemd unit)', type: 'text', section: 'VPN' },
            { key: 'vpn_config_path', label: 'File config tunnel (WireGuard)', type: 'text', section: 'VPN' },
            { key: 'vpn_up_command', label: 'Comando custom di avvio', type: 'text', section: 'VPN' },
            { key: 'vpn_down_command', label: 'Comando custom di arresto', type: 'text', section: 'VPN' },
            { key: 'vpn_health_check_host', label: 'Health-check — host (vuoto = LIS host)', type: 'text', section: 'VPN' },
            { key: 'vpn_health_check_port', label: 'Health-check — porta (vuoto = LIS porta)', type: 'number', section: 'VPN' },
            { key: 'vpn_health_check_timeout', label: 'Health-check — timeout (s)', type: 'number', step: '0.5', section: 'VPN' },
            { key: 'vpn_wait_seconds', label: 'Attesa raggiungibilità all’avvio (s)', type: 'number', section: 'VPN' },
            { key: 'vpn_poll_interval', label: 'Intervallo polling (s)', type: 'number', step: '0.1', section: 'VPN' },

            // Servizio
            { key: 'db_path', label: 'Percorso database', type: 'text', section: 'Servizio' },
            { key: 'status_enabled', label: 'Status UI abilitata', type: 'checkbox', section: 'Servizio' },
            { key: 'status_host', label: 'Status UI — host', type: 'text', section: 'Servizio' },
            { key: 'status_port', label: 'Status UI — porta', type: 'number', section: 'Servizio' },
            { key: 'api_enabled', label: 'Dashboard REST abilitata', type: 'checkbox', section: 'Servizio' },
            { key: 'api_host', label: 'Dashboard — host', type: 'text', section: 'Servizio' },
            { key: 'api_port', label: 'Dashboard — porta', type: 'number', section: 'Servizio' },
        ];

        async function openSettings() {
            const resp = await fetch('/api/config');
            const data = await resp.json();
            document.getElementById('settingsPath').textContent = data.config_path;
            renderSettingsForm(data.config);
            document.getElementById('settingsModal').classList.add('show');
        }

        function closeSettings() {
            document.getElementById('settingsModal').classList.remove('show');
        }

        function renderSettingsForm(cfg) {
            const bySection = {};
            CONFIG_FIELDS.forEach(f => {
                (bySection[f.section] = bySection[f.section] || []).push(f);
            });
            let html = '';
            for (const section of Object.keys(bySection)) {
                html += `<h3 class="settings-section">${section}</h3><div class="settings-grid">`;
                html += bySection[section].map(f => renderSettingsFieldHtml(f, cfg[f.key])).join('');
                html += '</div>';
                if (section === 'VPN') {
                    html += `<div class="settings-actions" style="border-top:none;padding-top:8px;margin-top:8px;">
                        <button type="button" class="action-button" onclick="checkVpn()">Verifica tunnel</button>
                        <span id="vpnCheckResult"></span>
                    </div>`;
                }
            }
            document.getElementById('settingsForm').innerHTML = html;
        }

        function renderSettingsFieldHtml(f, value) {
            if (f.type === 'checkbox') {
                return `<label class="settings-field checkbox">
                    <input type="checkbox" data-key="${f.key}" ${value ? 'checked' : ''}> ${f.label}
                </label>`;
            }
            if (f.type === 'select') {
                const opts = f.options.map(o => `<option value="${o}" ${o === value ? 'selected' : ''}>${o}</option>`).join('');
                return `<label class="settings-field"><span>${f.label}</span><select data-key="${f.key}">${opts}</select></label>`;
            }
            const step = f.step ? ` step="${f.step}"` : '';
            const v = value === null || value === undefined ? '' : value;
            return `<label class="settings-field"><span>${f.label}</span><input type="${f.type}" data-key="${f.key}" value="${v}"${step}></label>`;
        }

        function collectSettingsPayload() {
            const payload = {};
            document.querySelectorAll('#settingsForm [data-key]').forEach(el => {
                const field = CONFIG_FIELDS.find(f => f.key === el.dataset.key);
                if (field.type === 'checkbox') payload[field.key] = el.checked;
                else if (field.type === 'number') payload[field.key] = el.value === '' ? 0 : Number(el.value);
                else payload[field.key] = el.value;
            });
            return payload;
        }

        async function saveSettings() {
            const payload = collectSettingsPayload();
            const resp = await fetch('/api/config', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            if (resp.ok) {
                alert('Configurazione salvata. Riavvia il servizio per applicare le modifiche.');
                closeSettings();
            } else {
                const err = await resp.json().catch(() => ({}));
                alert('Errore: ' + (err.detail || resp.status));
            }
        }

        async function checkVpn() {
            const val = key => (document.querySelector(`[data-key="${key}"]`) || {}).value || '';
            // "0" (default numerico di vpn_health_check_port) e' una stringa
            // non vuota quindi truthy in JS: va trattato come "non impostato",
            // coerente con la stessa convenzione lato server (run.py).
            const numOrEmpty = v => (v && v !== '0') ? v : '';
            const host = val('vpn_health_check_host') || val('lis_host');
            const port = numOrEmpty(val('vpn_health_check_port')) || val('lis_port');
            const resultEl = document.getElementById('vpnCheckResult');
            if (!host || !port) {
                resultEl.textContent = 'Imposta almeno LIS host/porta o health-check host/porta.';
                resultEl.style.color = '#f39c12';
                return;
            }
            resultEl.textContent = 'Verifica in corso…';
            resultEl.style.color = '#666';
            try {
                const resp = await fetch(`/api/vpn/check?host=${encodeURIComponent(host)}&port=${encodeURIComponent(port)}`);
                const data = await resp.json();
                resultEl.textContent = data.reachable
                    ? `✓ raggiungibile (${host}:${port})`
                    : `✗ non raggiungibile (${host}:${port})`;
                resultEl.style.color = data.reachable ? '#27ae60' : '#e74c3c';
            } catch (e) {
                resultEl.textContent = 'Errore nella verifica.';
                resultEl.style.color = '#e74c3c';
            }
        }

        // Initial load and auto-refresh
        updateDashboard();
        setInterval(updateDashboard, 5000);
    </script>
</body>
</html>"""
