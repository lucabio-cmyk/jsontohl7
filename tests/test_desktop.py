"""
Applicazione desktop (hl7mw/desktop.py): guscio dell'eseguibile.

Eseguibile senza pytest: `python3 tests/test_desktop.py`.

Copre i modi in cui l'eseguibile "non funzionava" senza dirlo:
  1  cartella dati scrivibile fuori dalla directory di lancio
  2  percorsi relativi di db/log risolti dentro la cartella dati
  3  configurazione creata alla prima esecuzione e riletta alla seconda
  4  istanza singola: il secondo avvio non uccide il primo
  5  porte occupate riconosciute PRIMA di aprire mezzo servizio
  6  catena dell'interfaccia: finestra embedded -> finestra applicazione -> browser
  7  avvio completo verificato end-to-end (--selftest)
  8  errore di avvio riportato all'utente, non solo in una console che sparisce
"""
import json
import os
import socket
import stat
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hl7mw import desktop
from hl7mw import run as runmod

ok = True


def check(label: str, condition: bool, detail: str = ""):
    global ok
    if condition:
        print(f"[OK]     {label}")
    else:
        ok = False
        print(f"[FALLITO] {label} {detail}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_data_dir(tmp: Path):
    data_dir = desktop.prepare_data_dir(str(tmp / "dati"))
    check("1 Cartella dati creata e verificata scrivibile", data_dir.is_dir())
    check("1 La sonda di scrittura non lascia residui",
          list(data_dir.iterdir()) == [], str(list(data_dir.iterdir())))

    # Percorso non creabile (un file al posto di una cartella): errore parlante
    blocco = tmp / "file-non-cartella"
    blocco.write_text("x", encoding="utf-8")
    try:
        desktop.prepare_data_dir(str(blocco / "sotto"))
        check("1 Cartella dati non creabile -> errore esplicito", False, "nessuna eccezione")
    except RuntimeError as e:
        check("1 Cartella dati non creabile -> errore esplicito con rimedio",
              "--data-dir" in str(e), str(e)[:80])

    cfg = {"db_path": "hl7mw.db", "log_file": "hl7mw.log", "altro": "invariato"}
    desktop.resolve_paths(cfg, data_dir)
    check("2 db_path e log_file risolti dentro la cartella dati",
          cfg["db_path"] == str(data_dir / "hl7mw.db") and cfg["log_file"] == str(data_dir / "hl7mw.log"),
          str(cfg))
    cfg_abs = {"db_path": str(tmp / "altrove.db")}
    desktop.resolve_paths(cfg_abs, data_dir)
    check("2 Un percorso gia' assoluto non viene toccato",
          cfg_abs["db_path"] == str(tmp / "altrove.db"))


def test_config_bootstrap(tmp: Path):
    class Args:
        config = None
        data_dir = str(tmp / "cfgtest")

    cfg, config_path, data_dir = desktop.build_config(Args())
    check("3 Prima esecuzione: config.json creato nella cartella dati",
          Path(config_path).exists() and Path(config_path).parent == data_dir, config_path)
    check("3 La configurazione creata contiene i default del servizio",
          cfg["order_listen_port"] == runmod.DEFAULTS["order_listen_port"])

    # L'utente modifica il file: la seconda esecuzione lo rispetta
    salvata = json.loads(Path(config_path).read_text(encoding="utf-8"))
    salvata["order_listen_port"] = 7777
    Path(config_path).write_text(json.dumps(salvata), encoding="utf-8")
    cfg2, _, _ = desktop.build_config(Args())
    check("3 Seconda esecuzione: la configurazione salvata viene riletta",
          cfg2["order_listen_port"] == 7777, str(cfg2["order_listen_port"]))


def test_single_instance():
    first = desktop.SingleInstance(port=free_port())
    check("4 La prima istanza prende il posto", first.acquire())
    second = desktop.SingleInstance(port=first.port)
    check("4 La seconda istanza riconosce che il posto e' occupato", not second.acquire())
    first.release()
    third = desktop.SingleInstance(port=first.port)
    check("4 Rilasciato il posto, una nuova istanza puo' partire", third.acquire())
    third.release()


def test_busy_ports():
    port = free_port()
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        s.listen(1)
        cfg = dict(runmod.DEFAULTS)
        cfg.update({"order_listen_port": port, "result_listen_port": free_port(),
                    "api_enabled": False, "status_enabled": False})
        busy = desktop.busy_ports(cfg)
        check("5 La porta occupata viene rilevata prima dell'avvio",
              len(busy) == 1 and busy[0][2] == port, str(busy))
        check("5 Il rilievo dice a cosa serviva quella porta",
              busy and busy[0][0] == "Ordini dal LIS", str(busy))
    cfg["order_listen_port"] = free_port()
    check("5 Nessun falso positivo con porte libere", desktop.busy_ports(cfg) == [])


def test_ui_chain(tmp: Path):
    # 1° livello: finestra embedded. Qui non c'e' toolkit grafico: deve
    # rispondere "non disponibile" senza sollevare eccezioni.
    check("6 Finestra embedded assente -> fallback annunciato, nessuna eccezione",
          desktop.open_window("http://127.0.0.1:9/") is False)

    # 2° livello: finestra applicazione di un browser Chromium.
    fakebin = tmp / "bin"
    fakebin.mkdir(parents=True, exist_ok=True)
    fake = fakebin / "chromium"
    tracciato = tmp / "argomenti.txt"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {tracciato}\nsleep 5\n', encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    vecchio_path = os.environ["PATH"]
    os.environ["PATH"] = f"{fakebin}:{vecchio_path}"
    try:
        if os.name == "nt":       # su Windows si cercano Edge/Chrome installati
            print("[OK]     6 Finestra applicazione: verifica specifica POSIX saltata su Windows")
            return
        proc = desktop.open_app_window("http://127.0.0.1:8000", tmp)
        check("6 Senza finestra embedded si apre una finestra applicazione del browser",
              proc is not None)
        if proc:
            time.sleep(0.5)          # lascia partire il processo prima di chiuderlo
            proc.terminate()
            proc.wait(timeout=5)
        args = tracciato.read_text(encoding="utf-8") if tracciato.exists() else ""
        check("6 La finestra applicazione punta all'interfaccia, senza barra indirizzi",
              "--app=http://127.0.0.1:8000" in args, args.strip())
        check("6 Usa un profilo dedicato, non quello dell'utente",
              "--user-data-dir=" in args, args.strip())
    finally:
        os.environ["PATH"] = vecchio_path


def test_selftest_end_to_end(tmp: Path):
    data_dir = tmp / "selftest"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_dir / "config.json"
    cfg = dict(runmod.DEFAULTS)
    cfg.update({
        "order_listen_port": free_port(), "result_listen_port": free_port(),
        "api_port": free_port(), "status_port": free_port(),
        "log_console": False, "log_file": "hl7mw.log", "db_path": "hl7mw.db",
    })
    config_path.write_text(json.dumps(cfg), encoding="utf-8")
    rc = desktop.main(["--selftest", "--data-dir", str(data_dir), "-c", str(config_path)])
    check("7 --selftest: avvia il servizio, verifica l'interfaccia ed esce con 0", rc == 0, str(rc))
    check("7 --selftest: database e log finiscono nella cartella dati",
          (data_dir / "hl7mw.db").exists() and (data_dir / "hl7mw.log").exists())


def test_startup_error_is_reported(tmp: Path):
    """Un avvio impossibile deve tornare 1 e passare dal canale di avviso,
    non morire con una traccia in una console che si chiude."""
    messaggi = []
    originale = desktop.show_message
    desktop.show_message = lambda titolo, testo, error=False: messaggi.append((titolo, testo, error))
    try:
        data_dir = tmp / "errore"
        data_dir.mkdir(parents=True, exist_ok=True)
        config_path = data_dir / "config.json"
        occupata = free_port()
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", occupata))
            s.listen(1)
            cfg = dict(runmod.DEFAULTS)
            cfg.update({"order_listen_port": occupata, "result_listen_port": free_port(),
                        "api_port": free_port(), "status_port": free_port(),
                        "log_console": False})
            config_path.write_text(json.dumps(cfg), encoding="utf-8")
            rc = desktop.main(["--data-dir", str(data_dir), "-c", str(config_path)])
        check("8 Porta occupata: uscita con errore (1), non traccia non gestita", rc == 1, str(rc))
        check("8 L'utente riceve un avviso con la porta e il file da correggere",
              any(str(occupata) in testo and "config" in testo for _, testo, _ in messaggi),
              str(messaggi)[:160])
        check("8 L'avviso e' marcato come errore", any(err for _, _, err in messaggi))
    finally:
        desktop.show_message = originale


def test_service_urls():
    cfg = dict(runmod.DEFAULTS)
    cfg.update({"api_enabled": False, "status_enabled": True,
                "status_host": "0.0.0.0", "status_port": 8080})
    service = runmod.MiddlewareService(cfg)
    check("8 ui_url non propone 0.0.0.0 come indirizzo da aprire",
          service.ui_url == "http://127.0.0.1:8080", service.ui_url)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="hl7mw-desktop-") as tmpdir:
        tmp = Path(tmpdir)
        test_data_dir(tmp)
        test_config_bootstrap(tmp)
        test_single_instance()
        test_busy_ports()
        test_ui_chain(tmp)
        test_selftest_end_to_end(tmp)
        test_startup_error_is_reported(tmp)
        test_service_urls()
    if not ok:
        print("\nTEST DESKTOP FALLITI")
        return 1
    print("\nTUTTI I TEST DESKTOP OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
