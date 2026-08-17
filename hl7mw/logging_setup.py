"""
hl7mw.logging_setup — configurazione centralizzata del logging applicativo.

Obiettivo: poter sempre ricostruire "cosa e' successo" senza dover riprodurre
il problema — un log persistente su file (con rotazione, per non riempire il
disco), oltre alla console, e' il primo strumento diagnostico in un middleware
che deve restare in piedi 24/7 in un laboratorio clinico.

Configura il logger radice (non solo "hl7mw"): cosi' finiscono nello stesso
file anche i log di uvicorn/FastAPI (vedi run.py: uvicorn.run(log_config=None)
li fa propagare qui invece di usare la propria configurazione separata), utile
per correlare un errore applicativo con la richiesta HTTP che l'ha causato.

Solo stdlib (logging.handlers.RotatingFileHandler). Nessuna dipendenza esterna.
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging(
    level: str = "INFO",
    log_file: str = "",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    console: bool = True,
) -> None:
    """Configura il logger radice con (opzionalmente) file rotante + console.

    level        : "DEBUG"/"INFO"/"WARNING"/"ERROR" (case-insensitive)
    log_file     : path del file di log; "" = nessun file, solo console
    max_bytes    : dimensione massima di ciascun file prima della rotazione
    backup_count : quanti file ruotati mantenere (hl7mw.log.1, .2, ...)
    console      : se True, logga anche su stderr (utile con systemd/journalctl
                   o quando il servizio gira in un terminale interattivo)

    Rimpiazza qualunque configurazione precedente del logger radice (safe da
    richiamare una sola volta, tipicamente in main()): rimuove gli handler
    esistenti prima di aggiungere i propri, per evitare log duplicati se
    richiamata più di una volta (es. nei test).
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(FORMAT)

    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(formatter)
        root.addHandler(sh)

    if log_file:
        path = Path(log_file)
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
        )
        fh.setFormatter(formatter)
        root.addHandler(fh)
        logging.getLogger("hl7mw").info(
            "Logging su file: %s (max %d byte x %d rotazioni)", log_file, max_bytes, backup_count,
        )
