"""
hl7mw.auth — gestione operatori, ruoli e autorizzazioni (RBAC).

Nucleo di sicurezza del middleware, **solo stdlib** (hashlib/hmac/secrets):
non introduce dipendenze esterne nel package `hl7mw/`.

Due livelli di identità, deliberatamente uniti nel concetto di "operatore":
  - **RBAC middleware**: chi può usare dashboard/API/CLI e con quali permessi
    (ruoli ADMIN / SUPERVISOR / OPERATOR / VIEWER).
  - **Operatore POCT**: chi è autorizzato a eseguire test sullo strumento
    (lista operatori inviata al device HemoScreen, con livello e validità).

Le password/PIN sono salvate con PBKDF2-HMAC-SHA256 (salt per-utente). Nessuna
password in chiaro né nei log né nel DB. Vedi `store.py` per la persistenza.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

# ---------------------------------------------------------------------------
# Permessi
# ---------------------------------------------------------------------------
# Ogni permesso è una capability atomica verificata dagli endpoint/CLI.
VIEW_DASHBOARD        = "view_dashboard"        # leggere statistiche e stato
VIEW_ORDERS           = "view_orders"           # leggere ordini e risultati
MANAGE_ORDERS         = "manage_orders"         # retry / cancel / match unmatched
VIEW_INSTRUMENTS      = "view_instruments"      # leggere stato strumenti
CONFIGURE_DEVICES     = "configure_devices"     # configurazione remota strumenti
MANAGE_OPERATOR_LIST  = "manage_operator_list"  # inviare la lista operatori al device
VIEW_AUDIT            = "view_audit"            # leggere audit log
MANAGE_OPERATORS      = "manage_operators"      # creare/modificare/eliminare operatori

ALL_PERMISSIONS: frozenset[str] = frozenset({
    VIEW_DASHBOARD, VIEW_ORDERS, MANAGE_ORDERS, VIEW_INSTRUMENTS,
    CONFIGURE_DEVICES, MANAGE_OPERATOR_LIST, VIEW_AUDIT, MANAGE_OPERATORS,
})

# ---------------------------------------------------------------------------
# Ruoli → set di permessi
# ---------------------------------------------------------------------------
ROLES: dict[str, frozenset[str]] = {
    # Amministratore: ogni capability, inclusa la gestione operatori.
    "ADMIN": ALL_PERMISSIONS,
    # Supervisore: gestisce ordini, configura i device, gestisce la lista
    # operatori e ne crea di nuovi; non ha privilegi esclusivi di ADMIN
    # (vedi can_manage_operator) ma copre l'operatività quotidiana.
    "SUPERVISOR": frozenset({
        VIEW_DASHBOARD, VIEW_ORDERS, MANAGE_ORDERS, VIEW_INSTRUMENTS,
        CONFIGURE_DEVICES, MANAGE_OPERATOR_LIST, VIEW_AUDIT, MANAGE_OPERATORS,
    }),
    # Operatore: opera sugli ordini ed esegue i test, ma non configura i device
    # né gestisce gli altri operatori.
    "OPERATOR": frozenset({
        VIEW_DASHBOARD, VIEW_ORDERS, MANAGE_ORDERS, VIEW_INSTRUMENTS, VIEW_AUDIT,
    }),
    # Sola lettura.
    "VIEWER": frozenset({
        VIEW_DASHBOARD, VIEW_ORDERS, VIEW_INSTRUMENTS, VIEW_AUDIT,
    }),
}

# Gerarchia per la regola "chi può gestire chi": un operatore può creare/modificare
# solo operatori di rango pari o inferiore al proprio (un SUPERVISOR non tocca un ADMIN).
ROLE_RANK: dict[str, int] = {"VIEWER": 0, "OPERATOR": 1, "SUPERVISOR": 2, "ADMIN": 3}

# Livelli di permesso POCT (lista operatori del device HemoScreen).
POCT_PERMISSION_LEVELS = ("OPERATOR", "SUPERVISOR", "TRAINER")


def is_valid_role(role: str) -> bool:
    return role in ROLES


def role_permissions(role: str) -> frozenset[str]:
    """Permessi associati a un ruolo (set vuoto se ruolo sconosciuto)."""
    return ROLES.get(role, frozenset())


def has_permission(role: str, permission: str) -> bool:
    """True se il ruolo include il permesso richiesto."""
    return permission in ROLES.get(role, frozenset())


def can_manage_operator(actor_role: str, target_role: str) -> bool:
    """True se un operatore con `actor_role` può gestire un operatore `target_role`.

    Serve il permesso MANAGE_OPERATORS e un rango ≥ a quello del target, così un
    SUPERVISOR non può creare/modificare/eliminare un ADMIN.
    """
    if not has_permission(actor_role, MANAGE_OPERATORS):
        return False
    return ROLE_RANK.get(actor_role, -1) >= ROLE_RANK.get(target_role, 99)


# ---------------------------------------------------------------------------
# Password / PIN: hashing PBKDF2-HMAC-SHA256
# ---------------------------------------------------------------------------
_PBKDF2_ITERATIONS = 200_000
_ALGO = "pbkdf2_sha256"


def hash_password(password: str, *, iterations: int = _PBKDF2_ITERATIONS,
                  salt: bytes | None = None) -> str:
    """Cifra una password/PIN. Ritorna 'pbkdf2_sha256$iter$salt_hex$hash_hex'."""
    if not password:
        raise ValueError("Password/PIN vuoto non ammesso.")
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Verifica una password/PIN contro l'hash salvato (confronto a tempo costante)."""
    if not stored or not password:
        return False
    try:
        algo, iter_s, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        iterations = int(iter_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(dk, expected)


# ---------------------------------------------------------------------------
# Token di sessione
# ---------------------------------------------------------------------------

def generate_token() -> str:
    """Token di sessione opaco, crittograficamente sicuro (URL-safe)."""
    return secrets.token_urlsafe(32)
