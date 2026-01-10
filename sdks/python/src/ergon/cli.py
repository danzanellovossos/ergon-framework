import argparse
import json
import shutil
import sys
from pathlib import Path

from .task.manager import manager
from .connector import Transaction
from .task.base import TaskConfig
from typing import List, Optional


# ---------------------------------------------------------------------
# Utility: bootstrap project
# ---------------------------------------------------------------------
def bootstrap_project(project_name: str, target_dir: str):
    root = Path(__file__).resolve().parent
    template_src = root / "bootstrap" / "src" / "__project__"

    if not template_src.exists():
        raise RuntimeError(f"Template missing: {template_src}")

    target = Path(target_dir).resolve()
    target_src = target / "src" / project_name
    target_src.mkdir(parents=True, exist_ok=True)

    for item in template_src.rglob("*"):
        rel = item.relative_to(template_src)
        dest = target_src / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if item.is_file():
            shutil.copy2(item, dest)

    print(f"✔ Bootstrapped successfully at src/{project_name}")


# ---------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------
def create_parser():
    parser = argparse.ArgumentParser(
        prog="ergon",
        description="Ergon Task Framework CLI",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # -------------------------------------------------------------
    # ergon list
    # -------------------------------------------------------------
    subparsers.add_parser("list", help="List all registered tasks")

    # -------------------------------------------------------------
    # ergon run <task>
    # -------------------------------------------------------------
    run_parser = subparsers.add_parser("run", help="Run a registered task")
    run_parser.add_argument("task_name", type=str)

    # -------------------------------------------------------------
    # ergon process-transaction
    # -------------------------------------------------------------
    tx_parser = subparsers.add_parser(
        "process-transaction",
        help="Process a single transaction (inline payload)",
    )
    tx_parser.add_argument("task", type=str)
    tx_parser.add_argument("policy", type=str)
    tx_parser.add_argument(
        "--payload-json",
        required=True,
        help="Transaction payload as JSON string",
    )

    # -------------------------------------------------------------
    # ergon process-transaction-by-id
    # -------------------------------------------------------------
    tx_id_parser = subparsers.add_parser(
        "process-transaction-by-id",
        help="Process a transaction by ID via connector fetch",
    )
    tx_id_parser.add_argument("task", type=str)
    tx_id_parser.add_argument("policy", type=str)
    tx_id_parser.add_argument("transaction_id", type=str)

    # -------------------------------------------------------------
    # ergon bootstrap
    # -------------------------------------------------------------
    bootstrap_parser = subparsers.add_parser(
        "bootstrap",
        help="Create a new project using the Ergon template",
    )
    bootstrap_parser.add_argument("project_name", type=str)
    bootstrap_parser.add_argument(
        "target_dir",
        nargs="?",
        default=".",
        help="Target directory (default: current directory)",
    )

    return parser


# ---------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------
def ergon(tasks: Optional[List[TaskConfig]] = None):
    if tasks:
        for cfg in tasks:
            if manager.get(cfg.name) is None:
                manager.register(cfg)

    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # -------------------------------------------------------------
    # LIST TASKS
    # -------------------------------------------------------------
    if args.command == "list":
        tasks = manager.list_tasks()
        if not tasks:
            print("No tasks registered. Did you import your config files?")
        else:
            print("Registered tasks:")
            for t in tasks:
                print(f"  - {t}")
        return

    # -------------------------------------------------------------
    # RUN TASK
    # -------------------------------------------------------------
    if args.command == "run":
        manager.run(args.task_name)
        return

    # -------------------------------------------------------------
    # PROCESS TRANSACTION (INLINE)
    # -------------------------------------------------------------
    if args.command == "process-transaction":
        try:
            payload = json.loads(args.payload_json)
        except json.JSONDecodeError as exc:
            print(f"[ERROR] Invalid JSON payload: {exc}")
            sys.exit(2)

        transaction = Transaction(payload=payload)
        manager.process_transaction(
            task=args.task,
            policy=args.policy,
            transaction=transaction,
        )
        return

    # -------------------------------------------------------------
    # PROCESS TRANSACTION BY ID
    # -------------------------------------------------------------
    if args.command == "process-transaction-by-id":
        manager.process_transaction_by_id(
            task=args.task,
            policy=args.policy,
            transaction_id=args.transaction_id,
        )
        return

    # -------------------------------------------------------------
    # BOOTSTRAP
    # -------------------------------------------------------------
    if args.command == "bootstrap":
        bootstrap_project(args.project_name, args.target_dir)
        return
