"""
hl7mw.ack — politica di riscontro (acknowledgement) HL7 v2.

Un solo posto in cui vive la semantica del capitolo 2.9 dello standard, usata sia
dai receiver (che rispondono) sia dal forwarder (che interpreta le risposte):

  * original mode   — il mittente non valorizza MSH-15/MSH-16: il ricevente
                      risponde con un unico ACK applicativo (AA/AE/AR).
  * enhanced mode   — il mittente valorizza MSH-15 (accept ack) e/o MSH-16
                      (application ack) con AL/NE/ER/SU: il ricevente manda un
                      commit ACK (CA/CE/CR) e/o un ACK applicativo, secondo
                      quanto richiesto. Sono due messaggi distinti sulla stessa
                      connessione.

Nota implementativa: il commit ACK significa "messaggio ricevuto e messo al
sicuro", l'ACK applicativo "messaggio elaborato". Qui l'elaborazione e'
sincrona, quindi i due riscontri partono in sequenza subito dopo il
trattamento; la distinzione resta significativa per il mittente, che in
enhanced mode si aspetta due risposte e non una.

Solo stdlib.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import hl7

LOG = logging.getLogger("hl7mw")

# MSH-15 / MSH-16 (tabella HL7 0155)
ALWAYS, NEVER, ERROR_ONLY, SUCCESS_ONLY = "AL", "NE", "ER", "SU"
ACK_TYPES = (ALWAYS, NEVER, ERROR_ONLY, SUCCESS_ONLY)

# MSA-1 (tabella HL7 0008)
APPLICATION_POSITIVE = {"AA"}
APPLICATION_NEGATIVE = {"AE", "AR"}
COMMIT_POSITIVE = {"CA"}
COMMIT_NEGATIVE = {"CE", "CR"}
POSITIVE = APPLICATION_POSITIVE | COMMIT_POSITIVE
NEGATIVE = APPLICATION_NEGATIVE | COMMIT_NEGATIVE
COMMIT_CODES = {"AA": "CA", "AE": "CE", "AR": "CR"}

MODE_ORIGINAL, MODE_ENHANCED, MODE_AUTO = "original", "enhanced", "auto"


def is_commit_code(code: str) -> bool:
    """True per i codici di livello commit (CA/CE/CR): confermano la presa in
    carico, NON l'esito applicativo. Chi invia non puo' considerare concluso lo
    scambio se ha richiesto anche l'ACK applicativo."""
    return code.upper() in (COMMIT_POSITIVE | COMMIT_NEGATIVE)


def wants_ack(ack_type: str, ok: bool, default: bool = False) -> bool:
    """Interpreta un valore di MSH-15/MSH-16 rispetto all'esito dell'elaborazione."""
    t = (ack_type or "").upper()
    if t == ALWAYS:
        return True
    if t == NEVER:
        return False
    if t == ERROR_ONLY:
        return not ok
    if t == SUCCESS_ONLY:
        return ok
    return default


@dataclass
class AckOutcome:
    """Esito dell'elaborazione di un messaggio, tradotto poi in ACK.

    `body` permette di sostituire l'ACK applicativo con una risposta di dominio
    gia' costruita (ORR^O02 / ORL^O22) mantenendo invariata la logica di
    riscontro.
    """
    code: str = "AA"
    text: str = ""
    error_code: str = ""
    severity: str = "E"
    body: str | None = None
    duplicate: bool = False
    # Chiave campione ricavata dal messaggio: serve al tracciamento (message_log),
    # non alla costruzione dell'ACK.
    sample_key: str = ""

    @property
    def ok(self) -> bool:
        return self.code.upper() in APPLICATION_POSITIVE

    @classmethod
    def from_hl7_error(cls, exc: hl7.Hl7Error) -> "AckOutcome":
        return cls(code=getattr(exc, "ack_code", "AR"), text=str(exc),
                   error_code=getattr(exc, "error_code", "207"))


@dataclass
class AckPlan:
    """Quali riscontri vanno emessi per un messaggio."""
    commit: bool = False
    application: bool = True

    @property
    def silent(self) -> bool:
        return not (self.commit or self.application)


@dataclass
class AckPolicy:
    """Costruisce le risposte per un messaggio in ingresso.

    mode:
      - "auto"     (default) enhanced se il mittente valorizza MSH-15/MSH-16,
                   altrimenti original. E' il comportamento sicuro: non cambia
                   nulla per i mittenti esistenti e onora la richiesta di chi
                   usa l'enhanced mode.
      - "original" un solo ACK applicativo, MSH-15/16 ignorati.
      - "enhanced" MSH-15/16 sempre onorati (se assenti si ricade su original).
    """
    sending_app: str = "HL7MW"
    sending_facility: str = "MIDDLEWARE"
    mode: str = MODE_AUTO
    include_err: bool = True
    version: str = ""          # "" = eco della versione del messaggio in ingresso
    _warned_modes: set = field(default_factory=set, repr=False)

    # ----- pianificazione -----
    def plan(self, header: hl7.MessageHeader, ok: bool = True) -> AckPlan:
        declared = bool(header.accept_ack_type or header.application_ack_type)
        if self.mode == MODE_ORIGINAL or (self.mode == MODE_AUTO and not declared):
            return AckPlan(commit=False, application=True)
        if not declared:
            return AckPlan(commit=False, application=True)
        for value in (header.accept_ack_type, header.application_ack_type):
            if value and value not in ACK_TYPES and value not in self._warned_modes:
                self._warned_modes.add(value)
                LOG.warning("ACK: valore MSH-15/16 non previsto dalla tabella 0155: %r "
                            "(trattato come 'nessun riscontro richiesto')", value)
        return AckPlan(
            # MSH-15 assente: nessun commit ACK (non e' stato richiesto).
            commit=wants_ack(header.accept_ack_type, ok, default=False),
            # MSH-16 assente: valgono le regole dell'original mode, cioe' l'ACK
            # applicativo va comunque mandato. Si tace solo su un "NE" esplicito.
            application=wants_ack(header.application_ack_type, ok, default=True),
        )

    # ----- costruzione -----
    def commit_ack(self, message: str, header: hl7.MessageHeader,
                   outcome: AckOutcome) -> str:
        code = COMMIT_CODES.get(outcome.code.upper(), "CA")
        return hl7.build_ack(message, code, outcome.text, self.sending_app,
                             self.sending_facility, header=header,
                             version=self.version or header.version or "2.5")

    def application_ack(self, message: str, header: hl7.MessageHeader,
                        outcome: AckOutcome) -> str:
        if outcome.body is not None:
            return outcome.body
        return hl7.build_ack(
            message, outcome.code, outcome.text, self.sending_app, self.sending_facility,
            error_code=outcome.error_code if self.include_err else "",
            severity=outcome.severity, header=header,
            version=self.version or header.version or "2.5",
        )

    def responses(self, message: str, header: hl7.MessageHeader,
                  outcome: AckOutcome) -> list[str]:
        """Le risposte da rimandare sulla connessione, in ordine di invio."""
        plan = self.plan(header, outcome.ok)
        out: list[str] = []
        if plan.commit:
            out.append(self.commit_ack(message, header, outcome))
        if plan.application:
            out.append(self.application_ack(message, header, outcome))
        if plan.silent:
            LOG.debug("ACK: il mittente %s non richiede riscontro (MSH-15=%s MSH-16=%s)",
                      header.sending_app or "?", header.accept_ack_type or "-",
                      header.application_ack_type or "-")
        return out

    def malformed(self, message: str, reason: str) -> list[str]:
        """Risposta a un payload che non e' nemmeno un messaggio HL7 leggibile."""
        header = hl7.parse_header(message)
        return self.responses(message, header,
                              AckOutcome(code="AR", text=reason, error_code="100"))
