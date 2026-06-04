"""
hl7mw.cli — comandi CLI per gestione ordini, audit, operazioni di manutenzione.

Uso:
  python3 -m hl7mw.cli --db hl7mw.db orders --status READY
  python3 -m hl7mw.cli --db hl7mw.db retry --sample-key ABC123
  python3 -m hl7mw.cli --db hl7mw.db audit-log --sample-key ABC123
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .store import Store


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
