"""
hl7mw.webstatus — server di stato di sola lettura (stdlib http.server).

Espone:
  GET /api/status      -> conteggi per stato + risultati orfani (JSON)
  GET /api/orders?status=READY  -> elenco ordini in uno stato
  GET /                 -> pagina HTML minimale che fa polling di /api/status

È volutamente minimale: è il punto di aggancio per la UI vera e propria, che
conviene sviluppare a parte (vedi ARCHITECTURE.md, sezione Interfaccia).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .store import Store

PAGE = """<!doctype html><html lang="it"><head><meta charset="utf-8">
<title>HL7 Middleware — stato</title>
<style>body{font:14px system-ui;margin:2rem;color:#1a1a2e}
h1{font-size:1.2rem}.card{display:inline-block;min-width:120px;margin:.4rem;
padding:1rem;border:1px solid #ddd;border-radius:8px;text-align:center}
.n{font-size:1.8rem;font-weight:700}.k{color:#666;text-transform:uppercase;font-size:.7rem}</style>
</head><body><h1>HL7 Middleware — stato ordini</h1><div id="cards"></div>
<p style="color:#999">aggiornamento automatico ogni 3s · sola lettura</p>
<script>
async function tick(){
 const r = await fetch('/api/status'); const d = await r.json();
 document.getElementById('cards').innerHTML = Object.entries(d).map(
   ([k,v])=>`<div class="card"><div class="n">${v}</div><div class="k">${k}</div></div>`).join('');
}
tick(); setInterval(tick, 3000);
</script></body></html>"""


class StatusServer:
    def __init__(self, store: Store, host: str, port: int):
        self.store, self.host, self.port = store, host, port
        self._srv = None
        self._thread = None

    def start(self):
        store = self.store

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urlparse(self.path)
                if u.path == "/":
                    return self._send(200, PAGE, "text/html; charset=utf-8")
                if u.path == "/api/status":
                    return self._send(200, json.dumps(store.dashboard_counts()))
                if u.path == "/api/orders":
                    q = parse_qs(u.query)
                    st = (q.get("status") or ["READY"])[0]
                    return self._send(200, json.dumps(store.orders_by_status(st)))
                if u.path == "/api/unmatched":
                    return self._send(200, json.dumps(store.unmatched()))
                return self._send(404, json.dumps({"error": "not found"}))

        self._srv = ThreadingHTTPServer((self.host, self.port), H)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
