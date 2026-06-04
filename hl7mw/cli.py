"""
hl7mw.cli — comandi CLI per gestione ordini, audit, operazioni di manutenzione.

Uso:
  python3 -m hl7mw.cli --db hl7mw.db orders --status READY
  python3 -m hl7mw.cli --db hl7mw.db retry --sample-key ABC123
  python3 -m hl7mw.cli --db hl7mw.db audit-log --sample-key ABC123
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from . import auth
from .store import Store
from .adapters import hemoscreen_config


def cmd_orders(args, store: Store):
    """Elenco ordini con filtri."""
    orders = store.orders_by_status(args.status) if args.status else []
    if not args.status:
        with store._conn() as c:
            orders = [dict(r) for r in c.execute("SELECT * FROM orders ORDER BY updated_at DESC").fetchall()]

    print(f"Ordini totali: {len(orders)}")
    for o in orders:
        print(f"  {o['sample_key']:30} {o['status']:12} {o['created_at']:19} {o.get('last_error', '-')[:40]}")


def cmd_order_detail(args, store: Store):
    """Dettaglio ordine completo."""
    order = store.get_order(args.sample_key)
    if not order:
        print(f"Ordine non trovato: {args.sample_key}")
        return 1

    print(f"Sample Key: {order['sample_key']}")
    print(f"Status: {order['status']}")
    print(f"Created: {order['created_at']}")
    print(f"Updated: {order['updated_at']}")
    if order['last_error']:
        print(f"Error: {order['last_error']}")

    results = store.results_for(args.sample_key)
    print(f"\nRisultati ({len(results)}):")
    for r in results:
        print(f"  {json.dumps(r, ensure_ascii=False)[:80]}")

    timing = store.get_timing(args.sample_key)
    if timing:
        print(f"\nTiming:")
        for k, v in dict(timing).items():
            if k != "sample_key" and v:
                print(f"  {k}: {v}")

    return 0


def cmd_retry(args, store: Store):
    """Riporta un ordine ERROR a READY."""
    order = store.get_order(args.sample_key)
    if not order:
        print(f"Ordine non trovato: {args.sample_key}")
        return 1

    if order["status"] not in ("ERROR", "READY"):
        print(f"Non posso riprovare un ordine in status {order['status']}")
        return 1

    store.set_status(args.sample_key, "READY")
    store.audit_log(
        "cli_retry",
        sample_key=args.sample_key,
        details=f"Manual CLI retry from {order['status']}",
    )
    print(f"Ordine {args.sample_key} rimesso a READY per retry")
    return 0


def cmd_cancel(args, store: Store):
    """Cancella un ordine."""
    order = store.get_order(args.sample_key)
    if not order:
        print(f"Ordine non trovato: {args.sample_key}")
        return 1

    if order["status"] == "SENT":
        print(f"Non posso cancellare un ordine già inoltrato")
        return 1

    store.set_status(args.sample_key, "ERROR", "CANCELLED")
    store.audit_log(
        "cli_cancel",
        sample_key=args.sample_key,
        severity="WARNING",
    )
    print(f"Ordine {args.sample_key} cancellato")
    return 0


def cmd_audit_log(args, store: Store):
    """Visualizza audit log."""
    logs = store.get_audit_log(args.limit)

    if args.sample_key:
        logs = [l for l in logs if l["sample_key"] == args.sample_key]

    if args.event_type:
        logs = [l for l in logs if l["event_type"] == args.event_type]

    print(f"Audit log entries: {len(logs)}")
    for l in logs:
        print(f"  {l['timestamp']:19} {l['event_type']:20} {l.get('sample_key', '-'):30} {l.get('severity', 'INFO'):8} {l.get('details', '-')[:50]}")

    return 0


def cmd_instruments(args, store: Store):
    """Elenco strumenti."""
    instruments = store.get_instruments()
    print(f"Strumenti totali: {len(instruments)}")
    for i in instruments:
        print(f"  {i['name']:20} {i['host']:15}:{str(i['port']):5} {i['status']:8} last_msg={i.get('last_message_at', 'never'):19}")

    return 0


def cmd_stats(args, store: Store):
    """Statistiche globali."""
    stats = store.get_dashboard_stats()
    print("Dashboard Statistics:")
    print(f"  Total Orders: {stats['total_orders']}")
    print(f"  Status Counts:")
    for status, count in stats.get("status_counts", {}).items():
        print(f"    {status}: {count}")
    print(f"  Unmatched Results: {stats['unmatched_results']}")
    print(f"  Instruments: {stats['instruments']['online']}/{stats['instruments']['total']} online")
    print(f"  Total Results: {stats['total_results']}")
    if stats.get("avg_time_to_ready_seconds"):
        print(f"  Avg Time RECEIVED→READY: {stats['avg_time_to_ready_seconds']:.1f}s")
    if stats.get("avg_time_ready_to_sent_seconds"):
        print(f"  Avg Time READY→SENT: {stats['avg_time_ready_to_sent_seconds']:.1f}s")

    return 0


def cmd_unmatched(args, store: Store):
    """Visualizza risultati orfani."""
    unmatched = store.unmatched()
    print(f"Risultati orfani: {len(unmatched)}")
    for u in unmatched:
        result = json.loads(u["result_json"])
        print(f"  ID {u['id']}: sample={u.get('sample_key', 'unknown'):20} {u['received_at']}")

    return 0


def cmd_operators(args, store: Store):
    """Elenco operatori."""
    operators = store.list_operators(active_only=args.active_only)
    print(f"Operatori totali: {len(operators)}")
    for o in operators:
        state = "BLOCCATO" if o["locked"] else ("attivo" if o["active"] else "inattivo")
        print(f"  {o['operator_id']:20} {o['full_name']:25} {o['role']:12} "
              f"poct={o.get('poct_permission', '-'):10} {state}")
    return 0


def cmd_operator_add(args, store: Store):
    """Crea o aggiorna un operatore."""
    if not auth.is_valid_role(args.role):
        print(f"Ruolo non valido: {args.role}. Ammessi: {', '.join(auth.ROLES)}")
        return 1
    password = args.password
    if args.ask_password:
        password = getpass.getpass("Password / PIN: ")
        if password != getpass.getpass("Conferma: "):
            print("Le password non coincidono.")
            return 1
    store.upsert_operator(
        args.operator_id, args.full_name, role=args.role, password=password or None,
        poct_permission=args.poct_permission,
        certifications=args.certification or None,
        valid_from=args.valid_from, valid_until=args.valid_until,
        active=not args.inactive,
    )
    store.audit_log("cli_operator_upsert",
                    details=f"operator={args.operator_id} role={args.role}")
    print(f"Operatore {args.operator_id} salvato (ruolo {args.role}).")
    return 0


def cmd_operator_passwd(args, store: Store):
    """Imposta/cambia la password di un operatore."""
    if not store.get_operator(args.operator_id):
        print(f"Operatore non trovato: {args.operator_id}")
        return 1
    password = args.password
    if not password:
        password = getpass.getpass("Nuova password / PIN: ")
        if password != getpass.getpass("Conferma: "):
            print("Le password non coincidono.")
            return 1
    store.set_operator_password(args.operator_id, password)
    store.audit_log("cli_operator_password_change", details=f"operator={args.operator_id}")
    print(f"Password aggiornata per {args.operator_id}.")
    return 0


def cmd_operator_remove(args, store: Store):
    """Elimina un operatore."""
    if store.delete_operator(args.operator_id):
        store.audit_log("cli_operator_delete", details=f"operator={args.operator_id}",
                        severity="WARNING")
        print(f"Operatore {args.operator_id} eliminato.")
        return 0
    print(f"Operatore non trovato: {args.operator_id}")
    return 1


def cmd_operator_unlock(args, store: Store):
    """Sblocca un operatore bloccato per troppi tentativi falliti."""
    if store.set_operator_locked(args.operator_id, False):
        store.audit_log("cli_operator_unlock", details=f"operator={args.operator_id}")
        print(f"Operatore {args.operator_id} sbloccato.")
        return 0
    print(f"Operatore non trovato: {args.operator_id}")
    return 1


def cmd_device_config(args, store: Store):
    """Mostra o aggiorna la configurazione remota di uno strumento."""
    if args.set:
        params = {}
        for pair in args.set:
            if "=" not in pair:
                print(f"Formato atteso chiave=valore: {pair!r}")
                return 1
            k, v = pair.split("=", 1)
            params[k.strip()] = v.strip()
        try:
            canonical = hemoscreen_config.validate_config(params)
        except hemoscreen_config.ConfigError as e:
            print(f"Configurazione non valida: {e}")
            return 1
        store.set_device_config(args.instrument, canonical, updated_by="cli")
        store.audit_log("cli_device_config_change", instrument=args.instrument,
                        details=f"params={','.join(canonical)}")
        print(f"Configurazione aggiornata per {args.instrument}.")

    saved = store.get_device_config(args.instrument)
    merged = hemoscreen_config.default_config()
    merged.update(saved)
    print(f"Configurazione {args.instrument} (default + override):")
    for key, spec in hemoscreen_config.CONFIG_CATALOG.items():
        marker = "*" if key in saved else " "
        print(f" {marker} {key:30} = {merged[key]:15} ({spec['desc']})")
    print("  (* = valore impostato, gli altri sono default)")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="HL7 Middleware CLI — gestione ordini, statistiche, audit"
    )
    parser.add_argument(
        "--db", default="hl7mw.db", help="Path al database SQLite (default: hl7mw.db)"
    )

    subparsers = parser.add_subparsers(dest="command", help="Comando da eseguire")

    # orders
    p_orders = subparsers.add_parser("orders", help="Elenco ordini")
    p_orders.add_argument("--status", help="Filtra per status (RECEIVED, READY, SENT, ERROR)")
    p_orders.set_defaults(func=cmd_orders)

    # order
    p_order = subparsers.add_parser("order", help="Dettaglio ordine")
    p_order.add_argument("sample_key", help="Sample key dell'ordine")
    p_order.set_defaults(func=cmd_order_detail)

    # retry
    p_retry = subparsers.add_parser("retry", help="Riprovare inoltro ordine")
    p_retry.add_argument("sample_key", help="Sample key dell'ordine")
    p_retry.set_defaults(func=cmd_retry)

    # cancel
    p_cancel = subparsers.add_parser("cancel", help="Cancellare ordine")
    p_cancel.add_argument("sample_key", help="Sample key dell'ordine")
    p_cancel.set_defaults(func=cmd_cancel)

    # audit-log
    p_audit = subparsers.add_parser("audit-log", help="Visualizza audit log")
    p_audit.add_argument("--sample-key", help="Filtra per sample key")
    p_audit.add_argument("--event-type", help="Filtra per event type")
    p_audit.add_argument("--limit", type=int, default=100, help="Limite risultati")
    p_audit.set_defaults(func=cmd_audit_log)

    # instruments
    p_instr = subparsers.add_parser("instruments", help="Elenco strumenti")
    p_instr.set_defaults(func=cmd_instruments)

    # stats
    p_stats = subparsers.add_parser("stats", help="Statistiche globali")
    p_stats.set_defaults(func=cmd_stats)

    # unmatched
    p_unmatched = subparsers.add_parser("unmatched", help="Risultati orfani")
    p_unmatched.set_defaults(func=cmd_unmatched)

    # operators
    p_ops = subparsers.add_parser("operators", help="Elenco operatori")
    p_ops.add_argument("--active-only", action="store_true", help="Solo operatori attivi")
    p_ops.set_defaults(func=cmd_operators)

    # operator-add
    p_opadd = subparsers.add_parser("operator-add", help="Crea/aggiorna operatore")
    p_opadd.add_argument("operator_id", help="ID operatore (badge/login)")
    p_opadd.add_argument("full_name", help="Nome completo")
    p_opadd.add_argument("--role", default="OPERATOR",
                         help=f"Ruolo RBAC ({', '.join(auth.ROLES)})")
    p_opadd.add_argument("--password", help="Password/PIN (sconsigliato: usa --ask-password)")
    p_opadd.add_argument("--ask-password", action="store_true",
                         help="Chiedi la password in modo interattivo")
    p_opadd.add_argument("--poct-permission", default="OPERATOR",
                         help=f"Permesso POCT device ({', '.join(auth.POCT_PERMISSION_LEVELS)})")
    p_opadd.add_argument("--certification", action="append",
                         help="Certificazione (ripetibile)")
    p_opadd.add_argument("--valid-from", help="Validità dal (YYYY-MM-DD)")
    p_opadd.add_argument("--valid-until", help="Validità fino (YYYY-MM-DD)")
    p_opadd.add_argument("--inactive", action="store_true", help="Crea l'operatore inattivo")
    p_opadd.set_defaults(func=cmd_operator_add)

    # operator-passwd
    p_oppwd = subparsers.add_parser("operator-passwd", help="Imposta password operatore")
    p_oppwd.add_argument("operator_id", help="ID operatore")
    p_oppwd.add_argument("--password", help="Password/PIN (altrimenti chiesta interattivamente)")
    p_oppwd.set_defaults(func=cmd_operator_passwd)

    # operator-remove
    p_oprm = subparsers.add_parser("operator-remove", help="Elimina operatore")
    p_oprm.add_argument("operator_id", help="ID operatore")
    p_oprm.set_defaults(func=cmd_operator_remove)

    # operator-unlock
    p_opunlock = subparsers.add_parser("operator-unlock", help="Sblocca operatore")
    p_opunlock.add_argument("operator_id", help="ID operatore")
    p_opunlock.set_defaults(func=cmd_operator_unlock)

    # device-config
    p_cfg = subparsers.add_parser("device-config", help="Configurazione remota strumento")
    p_cfg.add_argument("instrument", help="Nome strumento")
    p_cfg.add_argument("--set", action="append", metavar="KEY=VALUE",
                       help="Imposta un parametro (ripetibile)")
    p_cfg.set_defaults(func=cmd_device_config)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Carica database
    if not Path(args.db).exists():
        print(f"Database non trovato: {args.db}")
        return 1

    store = Store(args.db)
    return args.func(args, store) or 0


if __name__ == "__main__":
    sys.exit(main())
