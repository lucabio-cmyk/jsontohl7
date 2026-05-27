# CLAUDE.md

Contesto per Claude Code. Leggi anche `ARCHITECTURE.md` per il dettaglio.

## WHY
Middleware sanitario **order-driven** tra un LIS e gli strumenti di laboratorio
(emocromo/CBC). Deve essere affidabile e semplice: è il pezzo che resta in piedi quando
il resto non funziona. Per questo il **core non ha dipendenze esterne** (solo stdlib;
stato su SQLite). Mantieni questa proprietà salvo decisione esplicita.

## WHAT
Tre flussi (in `hl7mw/pipeline.py`):
1. `OrderReceiver` — server MLLP, riceve ORM/OML dal LIS, salva l'ordine, risponde ACK.
2. `ResultReceiver` — server MLLP, riceve ORU dagli strumenti, **associa** all'ordine.
3. `Forwarder` — ordini `READY` → ORU^R01 → LIS, gestisce l'ACK.

Associazione per `sample_key` (specimen/barcode → filler → placer). Risultati senza
ordine → tabella `unmatched_results`. Ciclo di vita ordine: RECEIVED → READY → SENT
(o ERROR). Vedi `store.py`.

## HOW
- Eseguire i test: `python3 tests/test_e2e.py` (deve stampare "TUTTI I TEST OK").
- Avviare il servizio: `python3 -m hl7mw.run -c config.json`.
- HL7: separatore segmento `\r`, framing MLLP `0x0B … 0x1C 0x0D`. Non introdurre `\n`.
- Convenzioni: commenti/log in italiano; nomi e docstring chiari; niente librerie esterne
  nel package `hl7mw/` senza prima discuterne (UI e tooling di sviluppo possono usarle).
- Prima di modificare il parsing HL7, aggiungi un test in `tests/` che riproduce il caso.

## Da fare (priorità)
1. Adapter **ASTM E1381/E1394** per strumenti non-HL7 → produrre lo stesso dict di
   `hl7.parse_result`.
2. Regola di **completezza** reale in `pipeline.try_complete` (test richiesti vs ricevuti).
3. **Retry/backoff persistente** nel `Forwarder`.
4. **UI**: API REST (es. FastAPI) sopra `store.py` + frontend; sostituire `webstatus.py`.

## Attenzione (dominio sanitario)
Dati clinici reali: niente dati paziente nei log/commit, attenzione a sicurezza del
trasporto (TLS/stunnel sul MLLP) e tracciabilità (audit). In dubbio, chiedi.
