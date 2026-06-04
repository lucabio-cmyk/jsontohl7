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
from typing import Optional
from datetime import datetime, timedelta

from fastapi import FastAPI, Query, HTTPException, Header, Depends, Body
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from .store import Store
from . import auth
from .adapters import hemoscreen_config

app = FastAPI(title="HL7 Middleware API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_store: Store | None = None

def init_api(store: Store) -> FastAPI:
    """Inizializza API con referenza al database."""
    global _store
    _store = store
    return app


# ---------------------------------------------------------------------------
# Autenticazione e autorizzazione (RBAC)
# ---------------------------------------------------------------------------
# Il token di sessione viaggia nell'header Authorization: Bearer <token>
# (oppure X-Auth-Token). In "bootstrap mode" — nessun operatore ancora creato —
# l'API resta aperta per consentire la prima configurazione; appena esiste
# almeno un operatore, l'autenticazione diventa obbligatoria.

def _extract_token(authorization: Optional[str], x_auth_token: Optional[str]) -> Optional[str]:
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return authorization.strip()
    return x_auth_token.strip() if x_auth_token else None


def current_operator(
    authorization: Optional[str] = Header(None),
    x_auth_token: Optional[str] = Header(None),
) -> Optional[dict]:
    """Operatore autenticato dal token, o None se assente/non valido."""
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")
    token = _extract_token(authorization, x_auth_token)
    return _store.get_session_operator(token) if token else None


def require(permission: str):
    """Dependency factory: richiede un permesso specifico.

    In bootstrap mode (nessun operatore) lascia passare. Altrimenti pretende un
    operatore autenticato con il permesso richiesto.
    """
    def _dep(operator: Optional[dict] = Depends(current_operator)) -> Optional[dict]:
        if _store and _store.count_operators() == 0:
            return operator  # bootstrap: API aperta finché non si crea il primo operatore
        if not operator:
            raise HTTPException(status_code=401, detail="Autenticazione richiesta")
        if not auth.has_permission(operator.get("role", ""), permission):
            raise HTTPException(status_code=403, detail="Permesso negato")
        return operator
    return _dep


@app.post("/api/auth/login")
async def login(credentials: dict = Body(...)):
    """Login operatore: {operator_id, password} → {token, operator}."""
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")
    operator_id = (credentials or {}).get("operator_id", "")
    password = (credentials or {}).get("password", "")
    op = _store.authenticate_operator(operator_id, password)
    if not op:
        _store.audit_log("login_failed", details=f"operator={operator_id}", severity="WARNING")
        raise HTTPException(status_code=401, detail="Credenziali non valide o operatore bloccato")
    token = _store.create_session(operator_id)
    _store.audit_log("login", details=f"operator={operator_id}", severity="INFO")
    return {"token": token, "operator": op}


@app.post("/api/auth/logout")
async def logout(
    authorization: Optional[str] = Header(None),
    x_auth_token: Optional[str] = Header(None),
):
    """Invalida la sessione corrente."""
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")
    token = _extract_token(authorization, x_auth_token)
    if token:
        _store.delete_session(token)
    return {"status": "ok"}


@app.get("/api/auth/me")
async def whoami(operator: Optional[dict] = Depends(current_operator)):
    """Operatore corrente e suoi permessi (per la UI)."""
    if not operator:
        bootstrap = bool(_store and _store.count_operators() == 0)
        return {"authenticated": False, "bootstrap": bootstrap}
    return {
        "authenticated": True,
        "operator": operator,
        "permissions": sorted(auth.role_permissions(operator.get("role", ""))),
    }


@app.get("/health")
async def health():
    """Health check."""
    return {"status": "ok"}


@app.get("/api/dashboard")
async def get_dashboard(_op=Depends(require(auth.VIEW_DASHBOARD))):
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
    _op=Depends(require(auth.VIEW_ORDERS)),
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
async def get_order_detail(sample_key: str, _op=Depends(require(auth.VIEW_ORDERS))):
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
async def retry_order(sample_key: str, _op=Depends(require(auth.MANAGE_ORDERS))):
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
async def cancel_order(sample_key: str, _op=Depends(require(auth.MANAGE_ORDERS))):
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
async def get_unmatched(limit: int = Query(100, ge=1, le=1000),
                        _op=Depends(require(auth.VIEW_ORDERS))):
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
async def match_unmatched(result_id: int, sample_key: str,
                          _op=Depends(require(auth.MANAGE_ORDERS))):
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
async def list_instruments(_op=Depends(require(auth.VIEW_INSTRUMENTS))):
    """Status di tutti gli strumenti collegati."""
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")
    instruments = _store.get_instruments()
    return instruments


@app.get("/api/instruments/{name}")
async def get_instrument_detail(name: str, _op=Depends(require(auth.VIEW_INSTRUMENTS))):
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
    _op=Depends(require(auth.VIEW_AUDIT)),
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


# ---------------------------------------------------------------------------
# Operatori (RBAC)
# ---------------------------------------------------------------------------

@app.get("/api/roles")
async def list_roles(_op=Depends(require(auth.MANAGE_OPERATORS))):
    """Ruoli disponibili con i relativi permessi (per i form della UI)."""
    return {
        "roles": {r: sorted(p) for r, p in auth.ROLES.items()},
        "poct_permission_levels": list(auth.POCT_PERMISSION_LEVELS),
    }


@app.get("/api/operators")
async def list_operators(active_only: bool = Query(False),
                         _op=Depends(require(auth.MANAGE_OPERATORS))):
    """Elenco operatori (senza hash password)."""
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")
    return _store.list_operators(active_only=active_only)


@app.get("/api/operators/{operator_id}")
async def get_operator(operator_id: str, _op=Depends(require(auth.MANAGE_OPERATORS))):
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")
    op = _store.get_operator(operator_id)
    if not op:
        raise HTTPException(status_code=404, detail="Operatore non trovato")
    return op


@app.post("/api/operators")
async def create_operator(payload: dict = Body(...),
                          actor: Optional[dict] = Depends(require(auth.MANAGE_OPERATORS))):
    """Crea o aggiorna un operatore.

    Body: {operator_id, full_name, role, password?, poct_permission?,
           certifications?, valid_from?, valid_until?, active?}
    """
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")
    operator_id = (payload or {}).get("operator_id", "").strip()
    full_name = (payload or {}).get("full_name", "").strip()
    role = (payload or {}).get("role", "OPERATOR")
    if not operator_id or not full_name:
        raise HTTPException(status_code=400, detail="operator_id e full_name obbligatori")
    if not auth.is_valid_role(role):
        raise HTTPException(status_code=400, detail=f"Ruolo non valido: {role}")
    # Un attore non-bootstrap non può creare/modificare un operatore di rango superiore.
    if actor and not auth.can_manage_operator(actor.get("role", ""), role):
        raise HTTPException(status_code=403, detail="Non puoi gestire operatori di rango superiore")
    existing = _store.get_operator(operator_id)
    if existing and actor and not auth.can_manage_operator(actor.get("role", ""), existing.get("role", "")):
        raise HTTPException(status_code=403, detail="Non puoi modificare questo operatore")
    try:
        _store.upsert_operator(
            operator_id, full_name, role=role,
            password=(payload.get("password") or None),
            poct_permission=payload.get("poct_permission", "OPERATOR"),
            certifications=payload.get("certifications"),
            valid_from=payload.get("valid_from"),
            valid_until=payload.get("valid_until"),
            active=bool(payload.get("active", True)),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _store.audit_log("operator_upsert", details=f"operator={operator_id} role={role}",
                     severity="INFO")
    return _store.get_operator(operator_id)


@app.post("/api/operators/{operator_id}/password")
async def set_operator_password(operator_id: str, payload: dict = Body(...),
                                actor: Optional[dict] = Depends(require(auth.MANAGE_OPERATORS))):
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")
    password = (payload or {}).get("password", "")
    if not password:
        raise HTTPException(status_code=400, detail="password obbligatoria")
    target = _store.get_operator(operator_id)
    if not target:
        raise HTTPException(status_code=404, detail="Operatore non trovato")
    if actor and not auth.can_manage_operator(actor.get("role", ""), target.get("role", "")):
        raise HTTPException(status_code=403, detail="Non puoi gestire questo operatore")
    _store.set_operator_password(operator_id, password)
    _store.audit_log("operator_password_change", details=f"operator={operator_id}",
                     severity="INFO")
    return {"status": "ok"}


@app.post("/api/operators/{operator_id}/active")
async def set_operator_active(operator_id: str, payload: dict = Body(...),
                              actor: Optional[dict] = Depends(require(auth.MANAGE_OPERATORS))):
    """Attiva/disattiva un operatore. Body: {active: bool}."""
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")
    target = _store.get_operator(operator_id)
    if not target:
        raise HTTPException(status_code=404, detail="Operatore non trovato")
    if actor and not auth.can_manage_operator(actor.get("role", ""), target.get("role", "")):
        raise HTTPException(status_code=403, detail="Non puoi gestire questo operatore")
    active = bool((payload or {}).get("active", True))
    _store.set_operator_active(operator_id, active)
    _store.audit_log("operator_active_change",
                     details=f"operator={operator_id} active={active}", severity="INFO")
    return {"status": "ok", "active": active}


@app.post("/api/operators/{operator_id}/unlock")
async def unlock_operator(operator_id: str,
                          actor: Optional[dict] = Depends(require(auth.MANAGE_OPERATORS))):
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")
    target = _store.get_operator(operator_id)
    if not target:
        raise HTTPException(status_code=404, detail="Operatore non trovato")
    if actor and not auth.can_manage_operator(actor.get("role", ""), target.get("role", "")):
        raise HTTPException(status_code=403, detail="Non puoi gestire questo operatore")
    _store.set_operator_locked(operator_id, False)
    _store.audit_log("operator_unlock", details=f"operator={operator_id}", severity="INFO")
    return {"status": "ok"}


@app.delete("/api/operators/{operator_id}")
async def delete_operator(operator_id: str,
                          actor: Optional[dict] = Depends(require(auth.MANAGE_OPERATORS))):
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")
    target = _store.get_operator(operator_id)
    if not target:
        raise HTTPException(status_code=404, detail="Operatore non trovato")
    if actor and not auth.can_manage_operator(actor.get("role", ""), target.get("role", "")):
        raise HTTPException(status_code=403, detail="Non puoi eliminare questo operatore")
    if actor and actor.get("operator_id") == operator_id:
        raise HTTPException(status_code=400, detail="Non puoi eliminare te stesso")
    _store.delete_operator(operator_id)
    _store.audit_log("operator_delete", details=f"operator={operator_id}", severity="WARNING")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Configurazione remota strumenti (HemoScreen)
# ---------------------------------------------------------------------------

@app.get("/api/config/catalog")
async def config_catalog(_op=Depends(require(auth.VIEW_INSTRUMENTS))):
    """Catalogo dei parametri configurabili del device (tipi, default, valori)."""
    return {"catalog": hemoscreen_config.CONFIG_CATALOG,
            "defaults": hemoscreen_config.default_config()}


@app.get("/api/instruments/{name}/config")
async def get_instrument_config(name: str, _op=Depends(require(auth.VIEW_INSTRUMENTS))):
    """Configurazione corrente dello strumento (merge default + valori salvati)."""
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")
    saved = _store.get_device_config(name)
    merged = hemoscreen_config.default_config()
    merged.update(saved)
    return {"instrument": name, "config": merged, "overrides": saved}


@app.put("/api/instruments/{name}/config")
async def set_instrument_config(name: str, payload: dict = Body(...),
                                actor: Optional[dict] = Depends(require(auth.CONFIGURE_DEVICES))):
    """Aggiorna la configurazione remota dello strumento. Body: {params: {...}}.

    I valori sono validati contro il catalogo; il device la riceverà alla prossima
    connessione (o su sua richiesta RDCF).
    """
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")
    params = (payload or {}).get("params", payload)
    if not isinstance(params, dict) or not params:
        raise HTTPException(status_code=400, detail="Nessun parametro fornito")
    try:
        canonical = hemoscreen_config.validate_config(params)
    except hemoscreen_config.ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    updated_by = actor.get("operator_id") if actor else None
    _store.set_device_config(name, canonical, updated_by=updated_by)
    _store.audit_log("device_config_change", instrument=name,
                     details=f"params={','.join(canonical)}", severity="INFO")
    return {"status": "ok", "config": _store.get_device_config(name)}


@app.get("/api/instruments/{name}/config/history")
async def get_instrument_config_history(name: str, limit: int = Query(100, ge=1, le=1000),
                                        _op=Depends(require(auth.VIEW_INSTRUMENTS))):
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")
    return _store.get_device_config_history(name, limit=limit)


@app.get("/api/instruments/{name}/config/preview")
async def preview_config_directive(name: str, _op=Depends(require(auth.CONFIGURE_DEVICES))):
    """Anteprima del messaggio POCT1-A2 (DTV.R01 + OPL.R01) che verrà inviato al device."""
    if not _store:
        raise HTTPException(status_code=500, detail="Store not initialized")
    saved = _store.get_device_config(name)
    dtv = hemoscreen_config.build_config_directive(saved) if saved else None
    opl = hemoscreen_config.build_operator_list(_store.list_operators())
    return {"config_directive": dtv, "operator_list": opl}


@app.get("/")
async def get_ui():
    """Serve la dashboard HTML."""
    return HTMLResponse(get_dashboard_html())


def get_dashboard_html() -> str:
    """HTML della dashboard con statsatistiche e gestione ordini."""
    return """<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>HL7 Middleware Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
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
        .modal-content .close {
            float: right;
            font-size: 24px;
            font-weight: bold;
            cursor: pointer;
            color: #999;
        }
        .modal-content .close:hover { color: #333; }

        pre { background: #f5f5f5; padding: 10px; border-radius: 4px; overflow-x: auto; font-size: 11px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>HL7 Middleware — Dashboard</h1>
            <div class="subtitle">Monitoraggio ordini, strumenti, statistiche</div>
            <div id="userBar" style="margin-top:10px; font-size:13px; color:#555;"></div>
            <div id="tabs" style="margin-top:12px; display:none;">
                <button class="action-button" onclick="showTab('monitor')">Monitoraggio</button>
                <button class="action-button" id="tabOperators" onclick="showTab('operators')" style="display:none;">Operatori</button>
                <button class="action-button" id="tabConfig" onclick="showTab('config')" style="display:none;">Config Device</button>
            </div>
        </div>

        <!-- Sezione Monitoraggio -->
        <div id="view-monitor">
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
        </div><!-- /view-monitor -->

        <!-- Sezione Operatori -->
        <div id="view-operators" style="display:none;">
            <div class="table-container">
                <h3>Gestione Operatori</h3>
                <button class="action-button" onclick="openOperatorForm()">+ Nuovo operatore</button>
                <table id="operatorsTable" style="margin-top:15px;">
                    <thead>
                        <tr><th>ID</th><th>Nome</th><th>Ruolo</th><th>Permesso POCT</th><th>Stato</th><th>Validità</th><th>Azioni</th></tr>
                    </thead>
                    <tbody></tbody>
                </table>
            </div>
        </div>

        <!-- Sezione Config Device -->
        <div id="view-config" style="display:none;">
            <div class="table-container">
                <h3>Configurazione Remota Strumento</h3>
                <div style="margin-bottom:15px;">
                    Strumento: <input id="cfgInstrument" value="HEMOSCREEN-POCT" style="padding:6px; border:1px solid #ccc; border-radius:4px;">
                    <button class="action-button" onclick="loadConfig()">Carica</button>
                    <button class="action-button" onclick="saveConfig()">Salva &amp; invia al device</button>
                </div>
                <div id="configForm"></div>
            </div>
        </div>
    </div>

    <!-- Operator Form Modal -->
    <div class="modal" id="operatorModal">
        <div class="modal-content">
            <span class="close" onclick="closeOperatorForm()">&times;</span>
            <h2>Operatore</h2>
            <div id="operatorFormBody"></div>
        </div>
    </div>

    <!-- Login Modal -->
    <div class="modal" id="loginModal">
        <div class="modal-content" style="max-width:380px;">
            <h2>Accesso operatore</h2>
            <div style="margin:15px 0;">
                <input id="loginId" placeholder="ID operatore" style="width:100%; padding:10px; margin-bottom:10px; border:1px solid #ccc; border-radius:4px;">
                <input id="loginPwd" type="password" placeholder="Password / PIN" style="width:100%; padding:10px; border:1px solid #ccc; border-radius:4px;"
                       onkeydown="if(event.key==='Enter') doLogin()">
            </div>
            <div id="loginError" style="color:#e74c3c; font-size:13px; margin-bottom:10px;"></div>
            <button class="action-button" onclick="doLogin()" style="width:100%; padding:10px;">Accedi</button>
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

    <script>
        let statusChart = null;
        let instrumentChart = null;
        let currentOperator = null;
        let currentPermissions = [];
        let bootstrapMode = false;
        let availableRoles = {};
        let poctLevels = [];

        function getToken() { return localStorage.getItem('hl7mw_token') || ''; }
        function setToken(t) { if (t) localStorage.setItem('hl7mw_token', t); else localStorage.removeItem('hl7mw_token'); }

        // fetch con token + gestione 401 (mostra login)
        async function apiFetch(url, options) {
            options = options || {};
            options.headers = Object.assign({}, options.headers, { 'Authorization': 'Bearer ' + getToken() });
            const resp = await fetch(url, options);
            if (resp.status === 401) {
                showLogin();
                throw new Error('unauthorized');
            }
            return resp;
        }

        function can(perm) { return bootstrapMode || currentPermissions.includes(perm); }

        async function refreshAuth() {
            const resp = await fetch('/api/auth/me', { headers: { 'Authorization': 'Bearer ' + getToken() } });
            const me = await resp.json();
            bootstrapMode = !!me.bootstrap;
            if (me.authenticated) {
                currentOperator = me.operator;
                currentPermissions = me.permissions || [];
            } else {
                currentOperator = null;
                currentPermissions = [];
            }
            renderUserBar();
            // tabs visibili in base ai permessi
            document.getElementById('tabs').style.display = 'block';
            document.getElementById('tabOperators').style.display = can('manage_operators') ? 'inline-block' : 'none';
            document.getElementById('tabConfig').style.display = (can('configure_devices') || can('view_instruments')) ? 'inline-block' : 'none';
            if (!me.authenticated && !bootstrapMode) { showLogin(); }
            else { hideLogin(); }
        }

        function renderUserBar() {
            const bar = document.getElementById('userBar');
            if (currentOperator) {
                bar.innerHTML = `Operatore: <strong>${currentOperator.full_name}</strong> (${currentOperator.role}) ` +
                    `&nbsp;<button class="action-button danger" onclick="doLogout()">Logout</button>`;
            } else if (bootstrapMode) {
                bar.innerHTML = `<span style="color:#f39c12;">Bootstrap mode — nessun operatore configurato. Crea il primo operatore amministratore.</span>`;
            } else {
                bar.innerHTML = `<button class="action-button" onclick="showLogin()">Accedi</button>`;
            }
        }

        function showLogin() { document.getElementById('loginModal').classList.add('show'); }
        function hideLogin() { document.getElementById('loginModal').classList.remove('show'); }

        async function doLogin() {
            const operator_id = document.getElementById('loginId').value;
            const password = document.getElementById('loginPwd').value;
            const resp = await fetch('/api/auth/login', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ operator_id, password }),
            });
            if (resp.ok) {
                const data = await resp.json();
                setToken(data.token);
                document.getElementById('loginPwd').value = '';
                document.getElementById('loginError').textContent = '';
                await refreshAuth();
                updateDashboard();
            } else {
                document.getElementById('loginError').textContent = 'Credenziali non valide o operatore bloccato';
            }
        }

        async function doLogout() {
            await apiFetch('/api/auth/logout', { method: 'POST' }).catch(() => {});
            setToken('');
            await refreshAuth();
        }

        function showTab(tab) {
            document.getElementById('view-monitor').style.display = tab === 'monitor' ? 'block' : 'none';
            document.getElementById('view-operators').style.display = tab === 'operators' ? 'block' : 'none';
            document.getElementById('view-config').style.display = tab === 'config' ? 'block' : 'none';
            if (tab === 'operators') loadOperators();
            if (tab === 'config') loadConfig();
        }

        async function updateDashboard() {
            if (!bootstrapMode && !currentOperator) return;
            try {
                // Dashboard stats
                const statsResp = await apiFetch('/api/dashboard');
                const stats = await statsResp.json();
                renderStats(stats);
                renderStatusChart(stats);

                // Instruments
                const instrResp = await apiFetch('/api/instruments');
                const instruments = await instrResp.json();
                renderInstruments(instruments);

                // Orders
                const ordersResp = await apiFetch('/api/orders?limit=20');
                const orders = await ordersResp.json();
                renderOrders(orders);
            } catch (e) {
                console.error('Dashboard update failed:', e);
            }
        }

        // ---- Operatori ----
        async function loadOperators() {
            try {
                const rolesResp = await apiFetch('/api/roles');
                const rdata = await rolesResp.json();
                availableRoles = rdata.roles; poctLevels = rdata.poct_permission_levels;
                const resp = await apiFetch('/api/operators');
                const ops = await resp.json();
                const tbody = document.querySelector('#operatorsTable tbody');
                tbody.innerHTML = ops.map(o => `
                    <tr>
                        <td><strong>${o.operator_id}</strong></td>
                        <td>${o.full_name}</td>
                        <td>${o.role}</td>
                        <td>${o.poct_permission || '-'}</td>
                        <td>${o.locked ? '<span class="badge error">BLOCCATO</span>' : (o.active ? '<span class="badge sent">ATTIVO</span>' : '<span class="badge unknown">INATTIVO</span>')}</td>
                        <td>${(o.valid_from || '-') + ' → ' + (o.valid_until || '-')}</td>
                        <td>
                            <button class="action-button" onclick='openOperatorForm(${JSON.stringify(o)})'>Modifica</button>
                            ${o.locked ? `<button class="action-button" onclick="unlockOperator('${o.operator_id}')">Sblocca</button>` : ''}
                            <button class="action-button danger" onclick="deleteOperator('${o.operator_id}')">Elimina</button>
                        </td>
                    </tr>`).join('');
            } catch (e) { console.error(e); }
        }

        function openOperatorForm(op) {
            op = op || {};
            const roleOptions = Object.keys(availableRoles).map(r =>
                `<option value="${r}" ${op.role === r ? 'selected' : ''}>${r}</option>`).join('');
            const poctOptions = poctLevels.map(p =>
                `<option value="${p}" ${op.poct_permission === p ? 'selected' : ''}>${p}</option>`).join('');
            document.getElementById('operatorFormBody').innerHTML = `
                <label>ID operatore</label>
                <input id="opId" value="${op.operator_id || ''}" ${op.operator_id ? 'readonly' : ''} style="width:100%;padding:8px;margin-bottom:8px;border:1px solid #ccc;border-radius:4px;">
                <label>Nome completo</label>
                <input id="opName" value="${op.full_name || ''}" style="width:100%;padding:8px;margin-bottom:8px;border:1px solid #ccc;border-radius:4px;">
                <label>Ruolo (RBAC)</label>
                <select id="opRole" style="width:100%;padding:8px;margin-bottom:8px;border:1px solid #ccc;border-radius:4px;">${roleOptions}</select>
                <label>Permesso POCT (device)</label>
                <select id="opPoct" style="width:100%;padding:8px;margin-bottom:8px;border:1px solid #ccc;border-radius:4px;">${poctOptions}</select>
                <label>Password / PIN ${op.operator_id ? '(lascia vuoto per non cambiare)' : ''}</label>
                <input id="opPwd" type="password" style="width:100%;padding:8px;margin-bottom:8px;border:1px solid #ccc;border-radius:4px;">
                <label>Valido dal (opz.)</label>
                <input id="opFrom" value="${op.valid_from || ''}" placeholder="YYYY-MM-DD" style="width:100%;padding:8px;margin-bottom:8px;border:1px solid #ccc;border-radius:4px;">
                <label>Valido fino (opz.)</label>
                <input id="opUntil" value="${op.valid_until || ''}" placeholder="YYYY-MM-DD" style="width:100%;padding:8px;margin-bottom:8px;border:1px solid #ccc;border-radius:4px;">
                <label><input id="opActive" type="checkbox" ${op.active === false ? '' : 'checked'}> Attivo</label>
                <div style="margin-top:15px;"><button class="action-button" onclick="saveOperator()">Salva</button></div>
                <div id="opError" style="color:#e74c3c;font-size:13px;margin-top:8px;"></div>`;
            document.getElementById('operatorModal').classList.add('show');
        }
        function closeOperatorForm() { document.getElementById('operatorModal').classList.remove('show'); }

        async function saveOperator() {
            const payload = {
                operator_id: document.getElementById('opId').value.trim(),
                full_name: document.getElementById('opName').value.trim(),
                role: document.getElementById('opRole').value,
                poct_permission: document.getElementById('opPoct').value,
                valid_from: document.getElementById('opFrom').value.trim() || null,
                valid_until: document.getElementById('opUntil').value.trim() || null,
                active: document.getElementById('opActive').checked,
            };
            const pwd = document.getElementById('opPwd').value;
            if (pwd) payload.password = pwd;
            const resp = await apiFetch('/api/operators', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            if (resp.ok) { closeOperatorForm(); loadOperators(); await refreshAuth(); }
            else { const e = await resp.json(); document.getElementById('opError').textContent = e.detail || 'Errore'; }
        }

        async function unlockOperator(id) {
            await apiFetch(`/api/operators/${id}/unlock`, { method: 'POST' });
            loadOperators();
        }
        async function deleteOperator(id) {
            if (!confirm('Eliminare operatore ' + id + '?')) return;
            const resp = await apiFetch(`/api/operators/${id}`, { method: 'DELETE' });
            if (!resp.ok) { const e = await resp.json(); alert(e.detail || 'Errore'); }
            loadOperators();
        }

        // ---- Config Device ----
        async function loadConfig() {
            const name = document.getElementById('cfgInstrument').value.trim();
            try {
                const catResp = await apiFetch('/api/config/catalog');
                const cat = await catResp.json();
                const cfgResp = await apiFetch(`/api/instruments/${name}/config`);
                const cfg = await cfgResp.json();
                const editable = can('configure_devices');
                let html = '<table><thead><tr><th>Parametro</th><th>Valore</th><th>Descrizione</th></tr></thead><tbody>';
                for (const [key, spec] of Object.entries(cat.catalog)) {
                    const val = cfg.config[key];
                    let input;
                    const dis = editable ? '' : 'disabled';
                    if (spec.type === 'bool') {
                        input = `<select id="cfg_${key}" ${dis}><option value="true" ${val==='true'?'selected':''}>true</option><option value="false" ${val==='false'?'selected':''}>false</option></select>`;
                    } else if (spec.type === 'enum') {
                        input = `<select id="cfg_${key}" ${dis}>` + spec.values.map(v => `<option ${val===v?'selected':''}>${v}</option>`).join('') + '</select>';
                    } else {
                        input = `<input id="cfg_${key}" value="${val != null ? val : ''}" ${dis} style="padding:6px;border:1px solid #ccc;border-radius:4px;">`;
                    }
                    html += `<tr><td><strong>${key}</strong></td><td>${input}</td><td style="color:#666;">${spec.desc}</td></tr>`;
                }
                html += '</tbody></table>';
                document.getElementById('configForm').innerHTML = html;
                document.getElementById('configForm').dataset.keys = JSON.stringify(Object.keys(cat.catalog));
            } catch (e) { console.error(e); }
        }

        async function saveConfig() {
            const name = document.getElementById('cfgInstrument').value.trim();
            const keys = JSON.parse(document.getElementById('configForm').dataset.keys || '[]');
            const params = {};
            keys.forEach(k => { const el = document.getElementById('cfg_' + k); if (el) params[k] = el.value; });
            const resp = await apiFetch(`/api/instruments/${name}/config`, {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ params }),
            });
            if (resp.ok) { alert('Configurazione salvata. Sarà inviata al device alla prossima connessione.'); loadConfig(); }
            else { const e = await resp.json(); alert(e.detail || 'Errore'); }
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

        function renderInstruments(instruments) {
            document.getElementById('instruments').innerHTML = instruments.map(i =>
                `<div class="instrument-card">
                    <div class="name">${i.name}</div>
                    <div class="info"><strong>Host:</strong> ${i.host}:${i.port}</div>
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
            if (!confirm('Riprovare l\'inoltro di questo ordine?')) return;
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

        // Initial load and auto-refresh
        updateDashboard();
        setInterval(updateDashboard, 5000);
    </script>
</body>
</html>"""
