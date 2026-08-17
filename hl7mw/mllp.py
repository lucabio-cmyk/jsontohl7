"""
hl7mw.mllp — trasporto MLLP (TCP) per HL7v2.

Client:  MllpClient / send_message()  per inoltrare al LIS e leggere l'ACK.
Server:  MllpServer                   per ricevere ordini dal LIS e risultati dagli strumenti.

Framing: <0x0B> messaggio <0x1C><0x0D>.  Solo stdlib.

Due proprieta' importanti per l'interoperabilita' reale:

  1. connessione persistente — un mittente puo' inviare N messaggi sulla stessa
     connessione TCP senza riaprirla (e' il comportamento tipico dei LIS
     ospedalieri). Il server resta in lettura finche' il peer chiude o scade
     l'inattivita'; FrameReader conserva i byte gia' arrivati e non perde il
     messaggio successivo eventualmente presente nello stesso segmento TCP.

  2. enhanced acknowledgement — un singolo messaggio inviato puo' ricevere due
     risposte (commit ACK CA/CE/CR e poi ACK applicativo AA/AE/AR). Il client
     attende la seconda quando l'ha richiesta o quando il peer risponde con un
     codice di commit: considerare "CA" un successo definitivo significherebbe
     dichiarare inoltrato un ordine che il LIS potrebbe ancora rifiutare.
"""
from __future__ import annotations

import logging
import socket
import socketserver
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable

from . import hl7
from .ack import (COMMIT_NEGATIVE, COMMIT_POSITIVE, NEGATIVE, POSITIVE,  # noqa: F401 (API pubblica)
                  is_commit_code)

LOG = logging.getLogger("hl7mw")

SB, EB, CR = b"\x0b", b"\x1c", b"\x0d"


class MllpError(Exception):
    """Errore di trasporto (rete/timeout): condizione transitoria, ritentabile."""


class MllpTimeout(MllpError):
    """Nessun dato entro il timeout: usato per distinguere l'inattivita' normale
    di una connessione persistente da un errore vero."""


class AckError(Exception):
    def __init__(self, code: str, text: str):
        self.code, self.text = code, text
        super().__init__(f"ACK {code}: {text}")


def frame(message: str) -> bytes:
    return SB + message.encode("utf-8") + EB + CR


# Indirizzo del corrispondente della connessione in corso, per thread: permette
# ai gestori (pipeline/adapter) di tracciare "da chi" arriva un messaggio senza
# cambiare la firma del callback.
_LOCAL = threading.local()


def current_peer() -> str:
    return getattr(_LOCAL, "peer", "")


class FrameReader:
    """Lettore MLLP con buffer persistente su una connessione.

    Conserva fra una chiamata e l'altra i byte gia' ricevuti: senza questo, un
    mittente che accoda due messaggi nello stesso segmento TCP vedrebbe il
    secondo scartato insieme al buffer.
    """

    def __init__(self, sock: socket.socket, max_message_bytes: int = 16 * 1024 * 1024):
        self.sock = sock
        self.buf = bytearray()
        self.eof = False
        self.max_message_bytes = max_message_bytes

    def _take_frame(self) -> bytes | None:
        """Estrae dal buffer un messaggio completo, se presente."""
        start = self.buf.find(SB)
        if start < 0:
            # Nessun blocco iniziato: scarta eventuale rumore (keep-alive, CR di coda).
            if self.buf:
                del self.buf[:]
            return None
        if start > 0:
            LOG.debug("MLLP: scartati %d byte prima del blocco iniziale.", start)
            del self.buf[:start]
        end = self.buf.find(EB)
        if end < 0:
            return None
        payload = bytes(self.buf[1:end])
        # Consuma <EB> e l'eventuale <CR> di chiusura.
        drop = end + 1
        if len(self.buf) > drop and self.buf[drop] == CR[0]:
            drop += 1
        del self.buf[:drop]
        return payload

    def read(self, timeout: float) -> bytes | None:
        """Prossimo messaggio (senza framing). None = il peer ha chiuso.
        Solleva MllpTimeout se non arriva nulla entro `timeout`."""
        ready = self._take_frame()
        if ready is not None:
            return ready
        self.sock.settimeout(timeout)
        while True:
            if self.eof:
                if self.buf:
                    raise MllpError("Connessione chiusa prima della fine del messaggio.")
                return None
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout as e:
                raise MllpTimeout("Timeout in lettura MLLP.") from e
            except OSError as e:
                raise MllpError(f"Errore di lettura MLLP: {e}") from e
            if not chunk:
                self.eof = True
                continue
            self.buf.extend(chunk)
            if len(self.buf) > self.max_message_bytes:
                raise MllpError(
                    f"Messaggio oltre il limite di {self.max_message_bytes} byte: "
                    "connessione interrotta.")
            ready = self._take_frame()
            if ready is not None:
                return ready


def read_frame(sock: socket.socket, timeout: float) -> bytes:
    """Legge un singolo messaggio (compatibilita': ritorna b'' se il peer chiude)."""
    data = FrameReader(sock).read(timeout)
    return data if data is not None else b""


# --------------------------------------------------------------------------- ACK in ingresso
@dataclass
class AckResult:
    """Esito completo di uno scambio: tiene traccia anche del commit ACK, che in
    enhanced mode precede quello applicativo."""
    code: str = ""
    control_id: str = ""
    text: str = ""
    commit_code: str = ""
    error_code: str = ""
    raw: str = ""

    @property
    def positive(self) -> bool:
        return self.code.upper() in POSITIVE


def parse_ack(raw: str) -> AckResult:
    """Estrae MSA (e ERR, se presente) rispettando i delimitatori dichiarati."""
    delims = hl7.Delimiters.from_message(raw)
    segs = hl7.split_segments(raw, delims)
    msa = hl7.find(segs, "MSA")
    if not msa:
        raise MllpError("Risposta priva di segmento MSA.")
    err = hl7.find(segs, "ERR")
    error_code = ""
    if err:
        # ERR-3 (2.4+) oppure ERR-1 (<=2.3.1, forma ^^^<codice>&<testo>).
        error_code = hl7.comp(hl7.get(err, 3), 0, delims) or \
            hl7.comp(hl7.get(err, 1), 3, delims).split(delims.sub)[0]
    return AckResult(
        code=hl7.get(msa, 1).strip(),
        control_id=hl7.get(msa, 2).strip(),
        text=hl7.get(msa, 3).strip(),
        error_code=error_code.strip(),
        raw=raw,
    )


def parse_ack_code(raw: str) -> tuple[str, str, str]:
    """Compatibilita': (codice, control id, testo)."""
    a = parse_ack(raw)
    return a.code, a.control_id, a.text


# --------------------------------------------------------------------------- client
class MllpClient:
    """Connessione MLLP riusabile: apre una volta e invia N messaggi.

    Usarla come context manager. `send()` gestisce l'enhanced mode: se la prima
    risposta e' un commit ACK e ci si aspetta anche il riscontro applicativo,
    resta in ascolto della seconda risposta sulla stessa connessione.
    """

    def __init__(self, host: str, port: int, connect_timeout: float = 10.0,
                 read_timeout: float = 30.0, application_ack_timeout: float | None = None):
        self.host, self.port = host, port
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        # Attesa dell'ACK applicativo dopo un commit ACK: di norma piu' corta,
        # cosi' un peer che manda solo il commit non blocca l'inoltro.
        self.application_ack_timeout = (
            read_timeout if application_ack_timeout is None else application_ack_timeout)
        self._sock: socket.socket | None = None
        self._reader: FrameReader | None = None

    # ----- ciclo di vita -----
    def connect(self) -> "MllpClient":
        if self._sock is not None:
            return self
        try:
            self._sock = socket.create_connection((self.host, self.port),
                                                  timeout=self.connect_timeout)
        except OSError as e:
            raise MllpError(f"Connessione a {self.host}:{self.port} fallita: {e}") from e
        self._sock.settimeout(self.read_timeout)
        self._reader = FrameReader(self._sock)
        return self

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock, self._reader = None, None

    def __enter__(self) -> "MllpClient":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # ----- invio -----
    def send(self, message: str, expected_control_id: str | None = None,
             expect_application_ack: bool = True) -> AckResult:
        self.connect()
        assert self._sock is not None and self._reader is not None
        LOG.debug("MLLP: invio a %s:%s (%d byte)", self.host, self.port, len(message))
        try:
            self._sock.sendall(frame(message))
        except OSError as e:
            self.close()
            raise MllpError(f"Invio a {self.host}:{self.port} fallito: {e}") from e

        result = self._read_ack(self.read_timeout)
        if is_commit_code(result.code):
            result.commit_code = result.code
            if result.code.upper() in COMMIT_NEGATIVE:
                # Commit negativo: il messaggio non e' stato nemmeno preso in
                # carico, inutile attendere il riscontro applicativo.
                return result
            if expect_application_ack:
                try:
                    app = self._read_ack(self.application_ack_timeout)
                except MllpTimeout:
                    LOG.warning("MLLP %s:%s: ricevuto solo il commit ACK %s, nessun ACK "
                                "applicativo entro %.1fs.", self.host, self.port,
                                result.code, self.application_ack_timeout)
                    return result
                app.commit_code = result.code
                result = app
        if expected_control_id and result.control_id and result.control_id != expected_control_id:
            raise MllpError(
                f"Control ID ACK {result.control_id} != inviato {expected_control_id}.")
        return result

    def _read_ack(self, timeout: float) -> AckResult:
        assert self._reader is not None
        data = self._reader.read(timeout)
        if data is None:
            raise MllpError("Connessione chiusa dal peer senza risposta.")
        return parse_ack(data.decode("utf-8", errors="replace"))


def exchange(host: str, port: int, message: str,
             connect_timeout: float = 10.0, read_timeout: float = 30.0) -> str:
    """Invia un messaggio e ritorna la risposta grezza (una sola connessione)."""
    try:
        with MllpClient(host, port, connect_timeout, read_timeout) as c:
            return c.send(message, expect_application_ack=False).raw
    except MllpError:
        raise


def send_message_ex(host: str, port: int, message: str, expected_control_id: str | None = None,
                    connect_timeout: float = 10.0, read_timeout: float = 30.0,
                    expect_application_ack: bool = True,
                    application_ack_timeout: float | None = None) -> AckResult:
    """Invia, verifica il riscontro e ritorna l'esito completo.

    Solleva AckError su NACK applicativo/commit, MllpError su problemi di
    trasporto o codici non interpretabili.
    """
    with MllpClient(host, port, connect_timeout, read_timeout,
                    application_ack_timeout) as client:
        result = client.send(message, expected_control_id, expect_application_ack)
    code = result.code.upper()
    if expect_application_ack and is_commit_code(code):
        # Abbiamo chiesto il riscontro applicativo (MSH-16=AL) e il peer ha
        # mandato solo il commit: la presa in carico non e' un esito. Condizione
        # transitoria: il chiamante ritenta (stesso MSH-10, quindi il peer puo'
        # deduplicare).
        raise MllpError(
            f"Ricevuto solo il commit ACK {result.code}: nessun riscontro applicativo dal peer.")
    if code in POSITIVE:
        return result
    if code in NEGATIVE:
        raise AckError(result.code, result.text or hl7.ERROR_MESSAGES.get(result.error_code, ""))
    raise MllpError(f"Codice ACK non riconosciuto: {result.code!r}")


def send_message(host: str, port: int, message: str, expected_control_id: str | None = None,
                 connect_timeout: float = 10.0, read_timeout: float = 30.0) -> str:
    """Invia e verifica un ACK positivo. Ritorna il codice ACK; solleva AckError/MllpError."""
    return send_message_ex(host, port, message, expected_control_id,
                           connect_timeout, read_timeout).code


def check_endpoint(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- server
# Il callback riceve il messaggio (str) e ritorna la risposta MLLP: una stringa,
# una lista di stringhe (enhanced mode: commit ACK + ACK applicativo) oppure None
# per non rispondere (il mittente ha dichiarato MSH-15/16 = NE).
Handler = Callable[[str], "str | Iterable[str] | None"]


@dataclass
class MllpServer:
    host: str
    port: int
    handler: Handler
    read_timeout: float = 60.0
    # Attesa massima fra due messaggi della stessa connessione: un LIS tiene la
    # connessione aperta anche per ore, quindi e' piu' generosa del timeout di
    # lettura del primo messaggio.
    idle_timeout: float = 300.0
    max_messages_per_connection: int = 0   # 0 = illimitato
    _srv: "socketserver.ThreadingTCPServer | None" = None
    _thread: "threading.Thread | None" = None
    _connections: int = field(default=0, repr=False)

    @property
    def bound_port(self) -> int:
        """Porta effettiva (utile con port=0 nei test)."""
        return self._srv.server_address[1] if self._srv else self.port

    def start(self) -> "MllpServer":
        outer = self

        class _H(socketserver.BaseRequestHandler):
            def handle(self):
                reader = FrameReader(self.request)
                handled = 0
                try:
                    _LOCAL.peer = f"{self.client_address[0]}:{self.client_address[1]}"
                except (TypeError, IndexError):   # socket unix o indirizzo inatteso
                    _LOCAL.peer = str(self.client_address)
                while True:
                    timeout = outer.read_timeout if handled == 0 else outer.idle_timeout
                    try:
                        raw = reader.read(timeout)
                    except MllpTimeout:
                        LOG.debug("MLLP %s:%s: connessione inattiva da %.0fs, chiudo "
                                  "(client=%s, messaggi=%d)",
                                  outer.host, outer.port, timeout, self.client_address, handled)
                        return
                    except MllpError as e:
                        LOG.warning("MLLP %s:%s: %s (client=%s)",
                                    outer.host, outer.port, e, self.client_address)
                        return
                    if raw is None:
                        LOG.debug("MLLP %s:%s: connessione chiusa dal client %s dopo %d messaggi.",
                                  outer.host, outer.port, self.client_address, handled)
                        return
                    if not raw:
                        continue
                    if not self._process(raw):
                        return
                    handled += 1
                    if (outer.max_messages_per_connection
                            and handled >= outer.max_messages_per_connection):
                        LOG.debug("MLLP %s:%s: raggiunto il massimo di %d messaggi per "
                                  "connessione, chiudo (client=%s)", outer.host, outer.port,
                                  outer.max_messages_per_connection, self.client_address)
                        return

            def _process(self, raw: bytes) -> bool:
                """Elabora un messaggio e invia le risposte. False = chiudere."""
                message = raw.decode("utf-8", errors="replace")
                try:
                    reply = outer.handler(message)
                except Exception:
                    # Il server non deve cadere per un bug nel gestore: risponde AE,
                    # ma l'eccezione (stack trace incluso) va sempre in log — altrimenti
                    # il mittente vede solo un ACK rifiutato, senza nessuna traccia di
                    # cosa sia realmente andato storto lato middleware.
                    LOG.exception(
                        "MLLP %s:%s: errore nel gestore del messaggio (client=%s)",
                        outer.host, outer.port, self.client_address,
                    )
                    reply = hl7.build_ack(message, "AE", "Errore interno del middleware "
                                          "(vedi log applicativo per il dettaglio)",
                                          error_code="207")
                replies = [] if reply is None else ([reply] if isinstance(reply, str) else list(reply))
                for r in replies:
                    try:
                        self.request.sendall(frame(r))
                    except OSError as e:
                        LOG.warning("MLLP %s:%s: invio risposta fallito (client=%s): %s",
                                    outer.host, outer.port, self.client_address, e)
                        return False
                return True

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
            self._srv = None
