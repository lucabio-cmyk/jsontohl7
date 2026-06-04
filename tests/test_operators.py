"""
Test della gestione operatori e autorizzazioni (RBAC).

Eseguibile senza pytest: `python3 tests/test_operators.py`
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hl7mw import auth
from hl7mw.store import Store


def test_password_hashing():
    """Hashing PBKDF2: verifica positiva/negativa e niente password in chiaro."""
    h = auth.hash_password("S3gr3t0!")
    assert h.startswith("pbkdf2_sha256$")
    assert "S3gr3t0!" not in h
    assert auth.verify_password("S3gr3t0!", h)
    assert not auth.verify_password("sbagliata", h)
    assert not auth.verify_password("", h)
    assert not auth.verify_password("x", None)
    # salt casuale: due hash della stessa password differiscono
    assert auth.hash_password("S3gr3t0!") != h


def test_roles_and_permissions():
    assert auth.has_permission("ADMIN", auth.MANAGE_OPERATORS)
    assert auth.has_permission("SUPERVISOR", auth.CONFIGURE_DEVICES)
    assert not auth.has_permission("OPERATOR", auth.CONFIGURE_DEVICES)
    assert not auth.has_permission("VIEWER", auth.MANAGE_ORDERS)
    assert auth.has_permission("VIEWER", auth.VIEW_DASHBOARD)
    # ruolo sconosciuto: nessun permesso
    assert not auth.has_permission("BOGUS", auth.VIEW_DASHBOARD)


def test_role_hierarchy_management():
    # ADMIN gestisce tutti, SUPERVISOR non gestisce ADMIN
    assert auth.can_manage_operator("ADMIN", "ADMIN")
    assert auth.can_manage_operator("ADMIN", "SUPERVISOR")
    assert auth.can_manage_operator("SUPERVISOR", "OPERATOR")
    assert not auth.can_manage_operator("SUPERVISOR", "ADMIN")
    # OPERATOR non ha MANAGE_OPERATORS
    assert not auth.can_manage_operator("OPERATOR", "VIEWER")


def test_operator_crud():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Store(Path(tmpdir) / "test.db")
        db.upsert_operator("op1", "Mario Rossi", role="OPERATOR", password="pin1234",
                           poct_permission="OPERATOR")
        op = db.get_operator("op1")
        assert op is not None
        assert op["full_name"] == "Mario Rossi"
        assert op["role"] == "OPERATOR"
        assert op["active"] is True
        # la vista pubblica non espone l'hash
        assert "password_hash" not in op

        # aggiornamento preserva la password se non fornita
        db.upsert_operator("op1", "Mario Rossi", role="SUPERVISOR")
        assert db.get_operator("op1")["role"] == "SUPERVISOR"
        assert db.authenticate_operator("op1", "pin1234") is not None

        assert db.count_operators() == 1
        assert db.delete_operator("op1") is True
        assert db.get_operator("op1") is None


def test_invalid_role_rejected():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Store(Path(tmpdir) / "test.db")
        try:
            db.upsert_operator("x", "X", role="GODMODE", password="p")
            assert False, "atteso ValueError per ruolo non valido"
        except ValueError:
            pass


def test_authentication_and_lockout():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Store(Path(tmpdir) / "test.db")
        db.upsert_operator("op1", "Mario", role="OPERATOR", password="pin1234")

        assert db.authenticate_operator("op1", "pin1234") is not None
        assert db.authenticate_operator("op1", "wrong") is None

        # 5 tentativi falliti -> lockout
        for _ in range(5):
            db.authenticate_operator("op1", "wrong")
        op = db.get_operator("op1")
        assert op["locked"] is True
        # bloccato: anche la password giusta non passa
        assert db.authenticate_operator("op1", "pin1234") is None

        # sblocco
        db.set_operator_locked("op1", False)
        assert db.authenticate_operator("op1", "pin1234") is not None


def test_inactive_operator_cannot_auth():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Store(Path(tmpdir) / "test.db")
        db.upsert_operator("op1", "Mario", role="OPERATOR", password="pin1234", active=False)
        assert db.authenticate_operator("op1", "pin1234") is None


def test_sessions():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Store(Path(tmpdir) / "test.db")
        db.upsert_operator("op1", "Mario", role="ADMIN", password="pin1234")
        token = db.create_session("op1")
        assert token

        op = db.get_session_operator(token)
        assert op is not None and op["operator_id"] == "op1"

        # token inventato -> None
        assert db.get_session_operator("nope") is None

        # logout invalida la sessione
        db.delete_session(token)
        assert db.get_session_operator(token) is None


def test_expired_session_rejected():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Store(Path(tmpdir) / "test.db")
        db.upsert_operator("op1", "Mario", role="ADMIN", password="pin1234")
        token = db.create_session("op1", ttl_seconds=-1)  # già scaduto
        assert db.get_session_operator(token) is None


def test_session_invalid_if_operator_deactivated():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Store(Path(tmpdir) / "test.db")
        db.upsert_operator("op1", "Mario", role="ADMIN", password="pin1234")
        token = db.create_session("op1")
        db.set_operator_active("op1", False)
        assert db.get_session_operator(token) is None


if __name__ == "__main__":
    test_password_hashing()
    test_roles_and_permissions()
    test_role_hierarchy_management()
    test_operator_crud()
    test_invalid_role_rejected()
    test_authentication_and_lockout()
    test_inactive_operator_cannot_auth()
    test_sessions()
    test_expired_session_rejected()
    test_session_invalid_if_operator_deactivated()
    print("TUTTI I TEST OK")
