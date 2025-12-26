import argparse
import shutil
import sys
from pathlib import Path

from ergon import manager


# ---------------------------------------------------------------------
# Utility: copy the bootstrap template and rename the internal src package
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

        if item.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
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

    # -----------------------------------------------------------------
    # ergon list
    # -----------------------------------------------------------------
    subparsers.add_parser("list", help="List all registered tasks")

    # -----------------------------------------------------------------
    # ergon run <task>
    # -----------------------------------------------------------------
    run_parser = subparsers.add_parser("run", help="Run a registered task")
    run_parser.add_argument("task_name", type=str)

    # -----------------------------------------------------------------
    # ergon bootstrap <project_name> <target_dir>
    # -----------------------------------------------------------------
    bootstrap_parser = subparsers.add_parser(
        "bootstrap",
        help="Create a new project using the Ergon template",
        description="Example: ergon bootstrap jsl_manifesto .",
    )

    bootstrap_parser.add_argument(
        "project_name",
        type=str,
        help="Name of the python package that will go under src/",
    )

    bootstrap_parser.add_argument(
        "target_dir",
        type=str,
        nargs="?",
        default=".",
        help="Directory where project will be created (default: current directory)",
    )

    return parser


# ---------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------
def ergon():
    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # ---- LIST TASKS ----
    if args.command == "list":
        tasks = manager.list_tasks()
        if not tasks:
            print("No tasks registered. Did you import your config files?")
        else:
            print("Registered tasks:")
            for t in tasks:
                print(f"  - {t}")
        return

    # ---- RUN TASK ----
    if args.command == "run":
        try:
            manager.run(args.task_name)
        except Exception as exc:
            print(f"[ERROR] Failed to run task '{args.task_name}': {exc}")
            sys.exit(1)
        return

    # ---- BOOTSTRAP PROJECT ----
    if args.command == "bootstrap":
        try:
            bootstrap_project(args.project_name, args.target_dir)
        except Exception as exc:
            print(f"[ERROR] Could not bootstrap project: {exc}")
            sys.exit(1)
        return
