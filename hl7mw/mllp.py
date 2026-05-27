"""
hl7mw.mllp — trasporto MLLP (TCP) per HL7v2.

Client:  exchange()/send_message()  per inoltrare al LIS e leggere l'ACK.
Server:  serve()                    per ricevere ordini dal LIS e risultati dagli strumenti.

Framing: <0x0B> messaggio <0x1C><0x0D>.  Solo stdlib.
"""
from __future__ import annotations

import socket
import socketserver
import threading
from dataclasses import dataclass
from typing import Callable

SB, EB, CR = b"\x0b", b"\x1c", b"\x0d"


class MllpError(Exception):
    """Errore di trasporto (rete/timeout): condizione transitoria, ritentabile."""


class AckError(Exception):
    def __init__(self, code: str, text: str):
        self.code, self.text = code, text
        super().__init__(f"ACK {code}: {text}")


POSITIVE = {"AA", "CA"}
NEGATIVE = {"AE", "AR", "CE", "CR"}


def frame(message: str) -> bytes:
    return SB + message.encode("utf-8") + EB + CR


def read_frame(sock: socket.socket, timeout: float) -> bytes:
    sock.settimeout(timeout)
    buf = bytearray()
    started = False
    while True:
        try:
            chunk = sock.recv(4096)
        except socket.timeout as e:
            raise MllpError("Timeout in lettura MLLP.") from e
        if not chunk:
            if started:
                raise MllpError("Connessione chiusa prima della fine del messaggio.")
            return b""
        buf.extend(chunk)
        if not started and SB[0] in buf:
            started = True
            del buf[: buf.index(SB[0]) + 1]
        if started and EB[0] in buf:
            return bytes(buf[: buf.index(EB[0])])


# --------------------------------------------------------------------------- client
def exchange(host: str, port: int, message: str,
             connect_timeout: float = 10.0, read_timeout: float = 30.0) -> str:
    try:
        with socket.create_connection((host, port), timeout=connect_timeout) as s:
            s.settimeout(read_timeout)
            s.sendall(frame(message))
            return read_frame(s, read_timeout).decode("utf-8", errors="replace")
    except (OSError, socket.timeout) as e:
        raise MllpError(f"Connessione a {host}:{port} fallita: {e}") from e


def parse_ack_code(raw: str) -> tuple[str, str, str]:
    for line in raw.replace("\n", "\r").split("\r"):
        if line.startswith("MSA"):
            f = line.split("|")
            return (f[1].strip() if len(f) > 1 else "",
                    f[2].strip() if len(f) > 2 else "",
                    f[3].strip() if len(f) > 3 else "")
    raise MllpError("Risposta priva di segmento MSA.")


def send_message(host: str, port: int, message: str, expected_control_id: str | None = None,
                 connect_timeout: float = 10.0, read_timeout: float = 30.0) -> str:
    """Invia e verifica un ACK positivo. Ritorna il codice ACK; solleva AckError/MllpError."""
    raw = exchange(host, port, message, connect_timeout, read_timeout)
    code, ctrl, text = parse_ack_code(raw)
    if expected_control_id and ctrl and ctrl != expected_control_id:
        raise MllpError(f"Control ID ACK {ctrl} != inviato {expected_control_id}.")
    if code in POSITIVE:
        return code
    if code in NEGATIVE:
        raise AckError(code, text)
    raise MllpError(f"Codice ACK non riconosciuto: {code!r}")


def check_endpoint(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- server
# Il callback riceve il messaggio (str) e ritorna la risposta MLLP (str), tipicamente un ACK.
Handler = Callable[[str], str]


@dataclass
class MllpServer:
    host: str
    port: int
    handler: Handler
    read_timeout: float = 60.0
    _srv: "socketserver.ThreadingTCPServer | None" = None
    _thread: "threading.Thread | None" = None

    def start(self) -> "MllpServer":
        outer = self

        class _H(socketserver.BaseRequestHandler):
            def handle(self):
                raw = read_frame(self.request, outer.read_timeout)
                if not raw:
                    return
                message = raw.decode("utf-8", errors="replace")
                try:
                    reply = outer.handler(message)
                except Exception as e:  # il server non deve cadere: rispondi AE
                    from . import hl7
                    reply = hl7.build_ack(message, "AE", f"handler error: {e}")
                self.request.sendall(frame(reply))

        class _Srv(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._srv = _Srv((self.host, self.port), _H)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
