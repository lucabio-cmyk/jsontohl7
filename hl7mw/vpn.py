"""
hl7mw.vpn — gestione opzionale del tunnel VPN site-to-site richiesto per
raggiungere endpoint esterni (es. Citizen Care Connect Cloud Ingest Server,
che la spec del fornitore richiede sia raggiunto "through a site-to-site VPN
tunnel originating from the host location del LIS/EMR", §5.1).

Il middleware NON reimplementa IPsec/WireGuard/OpenVPN: delega al tool di sistema
già installato sull'host (wg-quick, systemd unit OpenVPN, o un comando custom) e
verifica la raggiungibilità dell'endpoint remoto prima che il traffico HL7 con dati
clinici venga instradato. Se il tunnel è gestito esternamente (systemd al boot,
appliance di rete dedicata) — la modalità consigliata in produzione — impostare
"vpn_manage_lifecycle": false: il middleware farà solo l'health-check.

Solo stdlib (subprocess, socket, shlex). Coerente con la proprietà "zero
dipendenze esterne nel core" (vedi CLAUDE.md).
"""
from __future__ import annotations

import logging
import os
import shlex
import signal
import socket
import subprocess
import time
from pathlib import Path

LOG = logging.getLogger("hl7mw")


class VpnError(Exception):
    """Errore nell'avvio/arresto del tunnel VPN."""


class VpnManager:
    """Avvia/arresta (opzionalmente) e verifica un tunnel VPN site-to-site.

    Provider supportati out-of-the-box:
      - "wireguard": wg-quick up/down <interface o config_path>
      - "openvpn":   systemctl start/stop openvpn-client@<interface>
                     (oppure openvpn --config <config_path> --daemon se non
                     e' definita una interface/unit systemd)
      - "external":  nessuna azione di avvio/arresto, solo health-check
                     (tunnel gestito fuori dal middleware: systemd al boot,
                     appliance di rete, ecc. — modalità consigliata)
      - comando custom: up_command/down_command hanno sempre la precedenza
    """

    def __init__(self, provider: str = "external", interface: str = "",
                 config_path: str = "", up_command: str = "", down_command: str = "",
                 manage_lifecycle: bool = False,
                 health_check_host: str = "", health_check_port: int = 0,
                 health_check_timeout: float = 5.0,
                 wait_seconds: float = 20.0, poll_interval: float = 1.0):
        self.provider = provider
        self.interface = interface
        self.config_path = config_path
        self.up_command = up_command
        self.down_command = down_command
        self.manage_lifecycle = manage_lifecycle
        self.health_check_host = health_check_host
        self.health_check_port = health_check_port
        self.health_check_timeout = health_check_timeout
        self.wait_seconds = wait_seconds
        self.poll_interval = poll_interval

    # ------------------------------------------------------------- comandi
    def _openvpn_pid_file(self) -> str:
        """Pidfile del processo openvpn --daemon avviato da vpn_config_path
        (nessuna unit systemd da poter fermare: serve tracciare il PID reale,
        scritto dal processo stesso via --writepid, per poterlo fermare in down())."""
        return f"{self.config_path}.pid"

    def _default_up_command(self) -> list[str] | None:
        if self.provider == "wireguard":
            target = self.interface or self.config_path
            if not target:
                raise VpnError("vpn_interface o vpn_config_path richiesti per provider=wireguard")
            return ["wg-quick", "up", target]
        if self.provider == "openvpn":
            if self.interface:
                return ["systemctl", "start", f"openvpn-client@{self.interface}"]
            if self.config_path:
                return ["openvpn", "--config", self.config_path, "--daemon",
                        "--writepid", self._openvpn_pid_file()]
            raise VpnError("vpn_interface o vpn_config_path richiesti per provider=openvpn")
        return None

    def _default_down_command(self) -> list[str] | None:
        if self.provider == "wireguard":
            target = self.interface or self.config_path
            return ["wg-quick", "down", target] if target else None
        if self.provider == "openvpn" and self.interface:
            return ["systemctl", "stop", f"openvpn-client@{self.interface}"]
        return None

    def _stop_openvpn_daemon(self) -> None:
        """Ferma il processo openvpn --daemon avviato da vpn_config_path (senza
        interface/unit systemd, _default_down_command() non ha nulla da
        restituire): legge il PID scritto da --writepid e lo termina."""
        pid_file = Path(self._openvpn_pid_file())
        if not pid_file.exists():
            raise VpnError(
                f"Impossibile fermare openvpn: pidfile {pid_file} non trovato "
                f"(processo gia' terminato, o mai avviato da questo middleware?)"
            )
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError) as e:
            raise VpnError(f"Impossibile fermare openvpn (pid file {pid_file}): {e}") from e
        finally:
            pid_file.unlink(missing_ok=True)

    def up(self) -> None:
        """Avvia il tunnel (solo se manage_lifecycle=True); altrimenti no-op."""
        if not self.manage_lifecycle:
            LOG.info("VPN: lifecycle gestito esternamente (vpn_manage_lifecycle=false), skip avvio.")
            return
        cmd = shlex.split(self.up_command) if self.up_command else self._default_up_command()
        if not cmd:
            LOG.info("VPN: provider=%s, nessun comando di avvio da eseguire.", self.provider)
            return
        LOG.info("VPN: avvio tunnel (%s)...", " ".join(cmd))
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
        except subprocess.CalledProcessError as e:
            raise VpnError(f"Comando VPN fallito ({e.returncode}): {e.stderr or e.stdout}") from e
        except (OSError, subprocess.TimeoutExpired) as e:
            raise VpnError(f"Impossibile eseguire il comando VPN: {e}") from e

    def down(self) -> None:
        """Arresta il tunnel (solo se manage_lifecycle=True). Solleva VpnError in
        caso di fallimento (simmetrico a up()): sta al chiamante decidere se
        tollerarlo (es. shutdown, dove non deve bloccare l'arresto pulito) o
        segnalarlo (es. azione on-demand dalla GUI, dove l'operatore deve
        sapere se il comando e' davvero fallito)."""
        if not self.manage_lifecycle:
            return
        cmd = shlex.split(self.down_command) if self.down_command else self._default_down_command()
        if not cmd:
            if not self.down_command and self.provider == "openvpn" and not self.interface and self.config_path:
                # Avviato con "openvpn --config ... --daemon" (nessuna interface/unit
                # systemd da fermare): l'unico modo per fermarlo e' il PID scritto
                # da --writepid in up(), altrimenti il processo sopravvive al
                # riavvio del middleware.
                LOG.info("VPN: arresto tunnel (openvpn --daemon, da pidfile)...")
                self._stop_openvpn_daemon()
            return
        LOG.info("VPN: arresto tunnel (%s)...", " ".join(cmd))
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
        except subprocess.CalledProcessError as e:
            raise VpnError(f"Comando VPN fallito ({e.returncode}): {e.stderr or e.stdout}") from e
        except (OSError, subprocess.TimeoutExpired) as e:
            raise VpnError(f"Impossibile eseguire il comando VPN: {e}") from e

    # ------------------------------------------------------------- health check
    def is_reachable(self) -> bool:
        """Verifica raggiungibilità TCP dell'endpoint remoto attraverso il tunnel."""
        if not self.health_check_host or not self.health_check_port:
            return True  # nessun health-check configurato: assume ok
        try:
            with socket.create_connection(
                (self.health_check_host, self.health_check_port),
                timeout=self.health_check_timeout,
            ):
                return True
        except OSError:
            return False

    def wait_until_reachable(self) -> bool:
        """Attende fino a wait_seconds che l'endpoint diventi raggiungibile."""
        if not self.health_check_host or not self.health_check_port:
            return True
        deadline = time.monotonic() + self.wait_seconds
        while True:
            if self.is_reachable():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.poll_interval)

    def ensure_up(self) -> bool:
        """Avvia (se richiesto) e verifica il tunnel. Ritorna True se l'endpoint
        e' raggiungibile. Non solleva per errori di raggiungibilità: logga e
        ritorna False, così il middleware resta comunque in piedi (l'inoltro
        al LIS fallirà con errore transitorio e verrà ritentato automaticamente
        dal loop principale, vedi pipeline.Forwarder)."""
        try:
            self.up()
        except VpnError as e:
            LOG.error("VPN: avvio tunnel fallito: %s", e)
            return False
        ok = self.wait_until_reachable()
        if ok:
            LOG.info("VPN: endpoint %s:%s raggiungibile.", self.health_check_host, self.health_check_port)
        else:
            LOG.error(
                "VPN: endpoint %s:%s NON raggiungibile dopo %.0fs. Verificare il tunnel "
                "prima di inviare dati clinici — il middleware resta comunque attivo e ritenterà.",
                self.health_check_host, self.health_check_port, self.wait_seconds,
            )
        return ok


def from_config(cfg: dict) -> "VpnManager | None":
    """Costruisce un VpnManager dalla configurazione, o None se vpn_enabled=false."""
    if not cfg.get("vpn_enabled"):
        return None
    return VpnManager(
        provider=cfg.get("vpn_provider", "external"),
        interface=cfg.get("vpn_interface", ""),
        config_path=cfg.get("vpn_config_path", ""),
        up_command=cfg.get("vpn_up_command", ""),
        down_command=cfg.get("vpn_down_command", ""),
        manage_lifecycle=cfg.get("vpn_manage_lifecycle", False),
        health_check_host=cfg.get("vpn_health_check_host", ""),
        health_check_port=int(cfg.get("vpn_health_check_port", 0) or 0),
        health_check_timeout=cfg.get("vpn_health_check_timeout", 5.0),
        wait_seconds=cfg.get("vpn_wait_seconds", 20.0),
        poll_interval=cfg.get("vpn_poll_interval", 1.0),
    )
