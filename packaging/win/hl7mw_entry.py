"""Entry point dell'eseguibile Windows (PyInstaller) del middleware.

Avvia l'applicazione desktop (hl7mw.desktop): servizio + finestra con la
dashboard, cartella dati scrivibile, istanza singola, errori in un dialogo.

    hl7mw-middleware.exe                    # applicazione con finestra
    hl7mw-middleware.exe --browser          # interfaccia nel browser di sistema
    hl7mw-middleware.exe --headless         # solo servizio (uso come servizio di sistema)
    hl7mw-middleware.exe --selftest         # avvia, verifica, esce (diagnostica)
    hl7mw-middleware.exe -c C:\\percorso\\config.json

Il servizio "puro" da riga di comando resta `python -m hl7mw.run`.
"""
import sys

from hl7mw.desktop import main

if __name__ == "__main__":
    sys.exit(main())
