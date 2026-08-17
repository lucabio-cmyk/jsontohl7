"""Entry point per l'eseguibile Windows (PyInstaller) del middleware.

Uso identico a `python -m hl7mw.run`:
    hl7mw-middleware.exe                 # usa i default (vedi hl7mw/run.py -> DEFAULTS)
    hl7mw-middleware.exe -c config.json  # usa una configurazione custom
"""
import sys

from hl7mw.run import main

if __name__ == "__main__":
    sys.exit(main())
