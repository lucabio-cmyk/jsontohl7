"""
hl7mw.desktop — applicazione desktop del middleware.

Perche' esiste: l'eseguibile Windows finora avviava il servizio e basta. Chi lo
apriva vedeva una finestra di console con dei log e doveva sapere che
l'interfaccia stava su un indirizzo da digitare nel browser; se qualcosa andava
storto all'avvio (porta occupata, cartella non scrivibile, config illeggibile)
la console spariva insieme all'errore e il programma sembrava "non funzionare".

Questo modulo trasforma lo stesso servizio in un'applicazione con finestra
propria:

  - **cartella dati scrivibile**: db, log e configurazione vanno in
    %LOCALAPPDATA%\\hl7mw (Windows) o ~/.local/share/hl7mw, non nella cartella
    da cui e' stato lanciato l'eseguibile — che puo' essere di sola lettura
    (Program Files) o essere la cartella Download, dove i file si perdono;
  - **istanza singola**: un secondo avvio non muore con "address already in
    use" ma avvisa e riporta l'utente all'istanza gia' attiva;
  - **finestra nativa**: la dashboard viene mostrata in una finestra
    dell'applicazione (WebView2/Edge su Windows tramite pywebview), non in una
    scheda del browser. Se il componente grafico non e' disponibile si ricade
    sul browser di sistema, aprendolo da soli: l'utente non deve digitare nulla;
  - **errori visibili**: un errore fatale all'avvio finisce in una finestra di
    dialogo (oltre che nel log), non in una console che si chiude.

Il servizio vero e proprio resta `hl7mw.run.MiddlewareService`: qui non c'e'
logica clinica ne' HL7, solo il guscio dell'applicazione.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from . import run as runmod
from .logging_setup import configure_logging

LOG = logging.getLogger("hl7mw.desktop")

APP_NAME = "hl7mw"
WINDOW_TITLE = "HL7 Middleware"
# Porta di controllo dell'istanza singola: un socket in ascolto sul loopback e'
# il modo piu' affidabile di sapere se un'altra copia e' viva (un lock file
# resterebbe orfano dopo un crash o uno spegnimento brutale).
SINGLE_INSTANCE_PORT = 47615


# --------------------------------------------------------------------------- ambiente
def is_frozen() -> bool:
    """True se stiamo girando dentro l'eseguibile impacchettato (PyInstaller)."""
    return bool(getattr(sys, "frozen", False))


def default_data_dir() -> Path:
    """Cartella dove tenere database, log e configurazione."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / APP_NAME


def prepare_data_dir(explicit: str | None = None) -> Path:
    """Crea (se serve) la cartella dati e verifica che sia scrivibile."""
    path = Path(explicit).expanduser() if explicit else default_data_dir()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(
            f"Impossibile creare la cartella dati {path}: {e}\n"
            "Indicare una cartella diversa con --data-dir.") from e
    probe = path / ".scrittura"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        raise RuntimeError(
            f"La cartella dati {path} non e' scrivibile: {e}\n"
            "Indicare una cartella diversa con --data-dir.") from e
    return path


def resolve_paths(cfg: dict, config_path: str | Path) -> None:
    """Rende assoluti i percorsi relativi di configurazione (database, log).

    Delega a `run.resolve_config_paths`, che li ancora al file di
    configurazione: cosi' servizio, dashboard (`GET /api/logs`) e CLI vedono
    esattamente gli stessi file, indipendentemente dalla directory da cui e'
    stata avviata l'applicazione.
    """
    runmod.resolve_config_paths(cfg, str(config_path))


# --------------------------------------------------------------------------- istanza singola
class SingleInstance:
    """Guardia di istanza singola basata su un socket di loopback.

    Chi tiene il posto risponde a ogni connessione con un banner che lo
    identifica: senza, un qualsiasi altro programma che occupasse la stessa
    porta ci convincerebbe che il middleware e' gia' in esecuzione, mandando
    l'utente su un'interfaccia inesistente senza alcun modo di rimediare.
    """

    BANNER = b"HL7MW-INSTANCE\n"

    def __init__(self, port: int = SINGLE_INSTANCE_PORT):
        self.port = port
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closing = threading.Event()

    def acquire(self) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name != "nt":
                # Su POSIX SO_REUSEADDR non permette due listener vivi sulla
                # stessa porta (servirebbe SO_REUSEPORT): evita solo che il
                # TIME_WAIT lasciato dall'handshake blocchi il riavvio subito
                # dopo una chiusura. Su Windows la stessa opzione permetterebbe
                # invece di scavalcare un socket vivo, quindi li' non si tocca.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", self.port))
            sock.listen(5)
        except OSError:
            sock.close()
            return False
        self._sock = sock
        self._thread = threading.Thread(target=self._serve_banner, daemon=True,
                                        name="single-instance")
        self._thread.start()
        return True

    def _serve_banner(self) -> None:
        # accept() con timeout invece che bloccante: chiudere il socket da un
        # altro thread non sveglia una accept() gia' in corso (il socket
        # resterebbe in LISTEN e la porta occupata anche dopo release()).
        while not self._closing.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                sock.settimeout(0.3)
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return                      # socket chiuso: si esce
            with conn:
                try:
                    conn.settimeout(1.0)
                    conn.sendall(self.BANNER)
                except OSError:
                    pass

    def held_by_this_app(self, timeout: float = 1.0) -> bool:
        """True se la porta e' tenuta davvero da una nostra istanza."""
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as c:
                c.settimeout(timeout)
                return c.recv(len(self.BANNER)) == self.BANNER
        except OSError:
            return False

    def release(self) -> None:
        self._closing.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)        # lascia uscire l'accept in corso
        if self._sock:
            self._sock.close()
            self._sock = None


# --------------------------------------------------------------------------- porte
def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host if host not in ("", "::") else "0.0.0.0", port))
            return True
        except OSError:
            return False


def busy_ports(cfg: dict) -> list[tuple[str, str, int]]:
    """Porte gia' occupate fra quelle che il servizio deve aprire.

    Controllarle prima significa poter dire "la porta 6661 e' occupata" invece
    di morire con una traceback a meta' avvio, con alcuni listener gia' aperti.
    """
    wanted = [("Ordini dal LIS", cfg.get("order_listen_host", "0.0.0.0"), cfg.get("order_listen_port"))]
    if cfg.get("adt_listen_port"):
        wanted.append(("Canale ADT", cfg.get("adt_listen_host") or cfg.get("order_listen_host"),
                       cfg.get("adt_listen_port")))
    wanted.append(("Risultati dagli strumenti", cfg.get("result_listen_host", "0.0.0.0"),
                   cfg.get("result_listen_port")))
    # Solo le porte che verranno davvero aperte: se "api_enabled" e' true ma
    # FastAPI non e' installato la dashboard non parte, e pretendere quella
    # porta bloccherebbe l'avvio per una porta che non useremmo.
    if cfg.get("api_enabled") and runmod.FASTAPI_AVAILABLE:
        wanted.append(("Dashboard", cfg.get("api_host", "127.0.0.1"), cfg.get("api_port")))
    if cfg.get("status_enabled"):
        wanted.append(("Pagina di stato", cfg.get("status_host", "127.0.0.1"), cfg.get("status_port")))
    if cfg.get("hemoscreen_hl7_enabled"):
        wanted.append(("HemoScreen HL7", cfg.get("hemoscreen_hl7_host"), cfg.get("hemoscreen_hl7_port")))
    if cfg.get("hemoscreen_poct1a2_enabled"):
        wanted.append(("HemoScreen POCT1-A2", cfg.get("hemoscreen_poct1a2_host"),
                       cfg.get("hemoscreen_poct1a2_port")))
    return [(label, host, port) for label, host, port in wanted
            if port and not port_is_free(host, int(port))]


# --------------------------------------------------------------------------- dialoghi
def show_message(title: str, text: str, error: bool = False) -> None:
    """Messaggio all'utente con i mezzi disponibili sulla piattaforma."""
    if os.name == "nt":
        try:
            import ctypes
            MB_ICONERROR, MB_ICONINFORMATION = 0x10, 0x40
            ctypes.windll.user32.MessageBoxW(
                None, text, title, MB_ICONERROR if error else MB_ICONINFORMATION)
            return
        except Exception:      # nessuna user32 raggiungibile: si ripiega sotto
            LOG.debug("MessageBoxW non disponibile, uso lo standard error.", exc_info=True)
    stream = sys.stderr if error else sys.stdout
    try:
        print(f"{title}: {text}", file=stream)
    except Exception:          # eseguibile senza console agganciata
        pass


# --------------------------------------------------------------------------- finestra
def open_window(url: str, on_close=None) -> bool:
    """Apre la dashboard in una finestra dell'applicazione.

    Ritorna False se non c'e' un motore grafico utilizzabile (in quel caso il
    chiamante ricade sul browser di sistema). Blocca finche' la finestra resta
    aperta: i toolkit grafici pretendono il thread principale.
    """
    try:
        import webview                                  # pywebview
    except ImportError:
        LOG.info("pywebview non disponibile: uso il browser di sistema.")
        return False
    try:
        window = webview.create_window(WINDOW_TITLE, url, width=1280, height=860,
                                       min_size=(900, 600), text_select=True)
        if on_close is not None:
            window.events.closed += on_close
        webview.start()
        return True
    except Exception as e:
        # Es. WebView2 assente su Windows, nessun GTK/Qt WebKit su Linux,
        # sessione senza display. Non e' fatale: c'e' il browser.
        LOG.warning("Finestra applicazione non disponibile (%s): uso il browser di sistema.", e)
        return False


def _app_mode_browsers() -> list[str]:
    """Browser Chromium in grado di aprire una finestra "applicazione".

    Su Windows Edge c'e' sempre (Windows 10/11), quindi questa strada da'
    comunque una finestra dedicata — senza barra degli indirizzi e senza
    schede — anche quando il motore embedded non e' utilizzabile.
    """
    if os.name == "nt":
        program_files = [os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                         os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                         os.environ.get("LOCALAPPDATA", "")]
        relative = [r"Microsoft\Edge\Application\msedge.exe",
                    r"Google\Chrome\Application\chrome.exe"]
        found = [os.path.join(base, rel) for base in program_files if base for rel in relative]
        return [p for p in found if os.path.exists(p)]
    candidates = ["google-chrome", "chromium", "chromium-browser", "microsoft-edge"]
    return [path for path in (shutil.which(c) for c in candidates) if path]


def open_app_window(url: str, data_dir: Path) -> "subprocess.Popen | None":
    """Apre l'interfaccia in una finestra applicazione di Edge/Chrome.

    Profilo dedicato (--user-data-dir): non tocca il profilo dell'utente e
    rende prevedibile il comportamento di --app.
    """
    for browser in _app_mode_browsers():
        try:
            proc = subprocess.Popen([
                browser, f"--app={url}", "--window-size=1280,860",
                f"--user-data-dir={data_dir / 'ui-profile'}",
                "--no-first-run", "--no-default-browser-check",
            ])
            LOG.info("Interfaccia aperta in finestra applicazione con %s", os.path.basename(browser))
            return proc
        except OSError as e:
            LOG.debug("Avvio di %s in modalita' applicazione fallito: %s", browser, e)
    return None


def open_browser(url: str) -> bool:
    try:
        return webbrowser.open(url)
    except Exception:
        LOG.exception("Apertura del browser fallita per %s", url)
        return False


# --------------------------------------------------------------------------- avvio
def build_config(args) -> tuple[dict, str, Path]:
    """Configurazione dell'app desktop: cartella dati, config.json, percorsi."""
    data_dir = prepare_data_dir(args.data_dir)
    config_path = Path(args.config).expanduser() if args.config else data_dir / "config.json"
    cfg = dict(runmod.DEFAULTS)
    if config_path.exists():
        cfg.update(json.loads(config_path.read_text(encoding="utf-8")))
    else:
        # Prima esecuzione: si scrive la configurazione di default accanto ai
        # dati, cosi' la pagina Impostazioni della dashboard ha un file su cui
        # salvare e l'utente sa dove metterci le mani.
        config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    resolve_paths(cfg, config_path)
    return cfg, str(config_path), data_dir


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="hl7mw-middleware",
        description="Middleware HL7v2 order-driven — applicazione desktop.")
    ap.add_argument("-c", "--config", help="File di configurazione (default: config.json nella cartella dati)")
    ap.add_argument("--data-dir", help="Cartella per database, log e configurazione")
    ap.add_argument("--loglevel", help="DEBUG/INFO/WARNING/ERROR")
    ap.add_argument("--headless", action="store_true",
                    help="Avvia solo il servizio, senza interfaccia (uso come servizio di sistema)")
    ap.add_argument("--browser", action="store_true",
                    help="Mostra l'interfaccia nel browser di sistema invece che nella finestra dell'app")
    ap.add_argument("--selftest", action="store_true",
                    help="Avvia, verifica che l'interfaccia risponda ed esce (diagnostica)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    guard = SingleInstance()
    service = None
    try:
        cfg, config_path, data_dir = build_config(args)
        configure_logging(
            level=args.loglevel or cfg.get("log_level", "INFO"),
            log_file=cfg.get("log_file", ""),
            max_bytes=cfg.get("log_max_bytes", 10 * 1024 * 1024),
            backup_count=cfg.get("log_backup_count", 5),
            # Senza console agganciata (eseguibile windowed) lo stream di
            # console non esiste: si logga solo su file.
            console=cfg.get("log_console", True) and sys.stderr is not None,
        )
        LOG.info("Cartella dati: %s (configurazione: %s)", data_dir, config_path)

        if not args.selftest and not guard.acquire():
            if guard.held_by_this_app():
                show_message(WINDOW_TITLE,
                             "Il middleware è già in esecuzione su questo computer.\n\n"
                             f"L'interfaccia è disponibile su {_ui_url(cfg)}.")
                LOG.warning("Avvio annullato: un'altra istanza e' gia' attiva.")
                return 0
            # La porta e' occupata da un programma estraneo: non e' una nostra
            # istanza, quindi si prosegue (al massimo si perde il controllo di
            # istanza singola, che e' meglio di un avvio impossibile).
            LOG.warning("La porta di controllo %d e' occupata da un altro programma: "
                        "controllo di istanza singola disattivato per questo avvio.",
                        guard.port)

        occupied = busy_ports(cfg)
        if occupied:
            dettaglio = "\n".join(f"  • {label}: porta {port} ({host})" for label, host, port in occupied)
            raise RuntimeError(
                "Alcune porte necessarie sono già occupate da un altro programma:\n\n"
                f"{dettaglio}\n\nChiudere il programma che le occupa oppure cambiare le porte "
                f"nel file di configurazione:\n{config_path}")

        service = runmod.MiddlewareService(cfg, config_path).start()
        service.run_in_background()
        url = service.ui_url

        if args.selftest:
            if not url:
                # Nessuna interfaccia configurata: il servizio e' comunque
                # avviato, il selftest verifica quello e lo dice.
                esito = "selftest: nessuna interfaccia configurata (api_enabled/status_enabled disattivi); servizio avviato -> OK"
                LOG.info(esito)
                try:
                    print(esito)
                except Exception:
                    pass
                return 0
            ok = service.wait_until_ready(timeout=20.0)
            esito = f"selftest: interfaccia su {url} -> {'OK' if ok else 'NON RAGGIUNGIBILE'}"
            LOG.info(esito)
            try:
                print(esito)
            except Exception:    # eseguibile senza console (build windowed)
                pass
            return 0 if ok else 1

        if args.headless:
            LOG.info("Modalita' headless: %s. Ctrl-C per fermare.",
                     f"interfaccia su {url}" if url else "nessuna interfaccia attiva")
            _wait_forever(service)
            return 0

        if not url:
            # Nessuna UI abilitata: dirlo e restare in servizio, invece di
            # aprire un indirizzo che non risponde a nessuno.
            LOG.warning("Nessuna interfaccia abilitata in configurazione: il servizio resta attivo.")
            show_message(WINDOW_TITLE,
                         "Il middleware è attivo, ma nessuna interfaccia è abilitata.\n\n"
                         f"Attiva \"api_enabled\" o \"status_enabled\" in:\n{config_path}")
            _wait_forever(service)
            return 0

        if not service.wait_until_ready():
            # L'interfaccia non risponde: aprire comunque una finestra su quel
            # indirizzo mostrerebbe una pagina di errore che non si aggiorna.
            LOG.error("Interfaccia non raggiungibile su %s: il servizio resta attivo.", url)
            show_message(WINDOW_TITLE,
                         f"Il middleware è attivo, ma l'interfaccia non risponde su:\n{url}\n\n"
                         f"Controlla il log:\n{cfg.get('log_file') or '(solo console)'}",
                         error=True)
            _wait_forever(service)
            return 1
        LOG.info("Interfaccia pronta su %s", url)
        # Tre livelli, dal piu' integrato al piu' generico: finestra embedded,
        # finestra applicazione del browser Chromium di sistema, browser
        # normale. L'utente non deve mai digitare un indirizzo a mano.
        if not args.browser and open_window(url, on_close=service.request_stop):
            return 0
        proc = None if args.browser else open_app_window(url, data_dir)
        if proc is not None:
            proc.wait()                      # la finestra e' l'applicazione: si chiude con lei
            return 0
        if not open_browser(url):
            show_message(WINDOW_TITLE,
                         f"Il middleware è attivo.\n\nApri l'interfaccia da questo indirizzo:\n{url}")
        _wait_forever(service)
        return 0
    except Exception as e:
        LOG.exception("Avvio dell'applicazione fallito.")
        show_message(f"{WINDOW_TITLE} — avvio fallito", str(e), error=True)
        return 1
    finally:
        if service is not None:
            service.stop()
        guard.release()


def _ui_url(cfg: dict) -> str:
    """URL dell'interfaccia secondo la configurazione (senza avviare nulla)."""
    return runmod.MiddlewareService(cfg).ui_url or "(nessuna interfaccia abilitata)"


def _wait_forever(service) -> None:
    """Tiene vivo il processo finche' il servizio non viene fermato."""
    stop = threading.Event()

    def _sig(_s, _f):
        service.request_stop()
        stop.set()

    import signal
    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if sig is not None:
            try:
                signal.signal(sig, _sig)
            except ValueError:      # non nel thread principale
                pass
    while not stop.is_set() and not service.stopped:
        stop.wait(0.5)


if __name__ == "__main__":
    sys.exit(main())
