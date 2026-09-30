from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .identity import IdentityMemory
from .paths import utf8_stdio
from .paths import profile_search_dirs
from .recipe import resolve


def _list(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def list_profiles() -> list[dict[str, str]]:
    seen: dict[str, dict[str, str]] = {}
    for folder in profile_search_dirs():
        if not folder.is_dir():
            continue
        for child in sorted(folder.iterdir()):
            manifest = child / "profile.json"
            if child.name.startswith("_") or not manifest.is_file():
                continue
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            name = str(data.get("name", child.name))
            seen.setdefault(name, {
                "name": name,
                "agent": str(data.get("agent", name)),
                "summary": str((data.get("core") or {}).get("summary", ""))[:160],
                "path": str(child),
            })
    return list(seen.values())


def setup_config(profile: str, db: Path | None, server_name: str | None) -> dict:
    args = ["-m", "trajecta_identity.mcp_server", "--profile", profile]
    if db:
        args += ["--db", str(Path(db).expanduser().resolve())]
    return {
        "mcpServers": {
            server_name or f"trajecta-identity-{profile}": {
                "command": sys.executable,
                "args": args,
            }
        }
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="trajecta-identity", description="Trajecta Identity Memory")
    root.add_argument(
        "-p", "--profile",
        help="profile name, folder, .json file or http(s) URL (default: last used, then 'example')",
    )
    root.add_argument("--db", type=Path, help="override the database path")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="bootstrap anchors, VHO and the core")
    setup = commands.add_parser("setup", help="bootstrap and print the MCP config for your agent client")
    setup.add_argument("--name", help="MCP server name (default: trajecta-identity-<profile>)")
    commands.add_parser("profiles", help="list profiles that can be used by name")
    plugin = commands.add_parser("plugin", help="build a Claude .plugin file: install once, no config to paste")
    plugin.add_argument("--out", type=Path, default=Path("dist"), help="output folder or .plugin file (default: dist/)")
    plugin.add_argument("--name", help="plugin and MCP server name (default: trajecta-identity-<profile>)")
    plugin.add_argument("--house", help="folder holding house-data/ and private-profiles/ (sets the three paths below)")
    plugin.add_argument("--data-dir", help="identity data folder the server uses")
    plugin.add_argument("--profiles-dir", help="folder with private profiles")
    plugin.add_argument("--work-root", help="trajecta-work-memory store to link")
    plugin.add_argument("--target", choices=["posix", "windows"], help="launcher type (default: this machine)")
    commands.add_parser("work", help="list work items in the linked trajecta-work-memory store")
    view = commands.add_parser("view", help="open a read-only web view of the memory")
    view.add_argument("--port", type=int, default=8767)
    view.add_argument("--no-browser", action="store_true")
    commands.add_parser("status")
    retrieve = commands.add_parser("retrieve")
    retrieve.add_argument("cue")
    retrieve.add_argument("--limit", type=int, default=10)
    retrieve.add_argument("--budget", type=int, default=2400)
    retrieve.add_argument("--history", action="store_true")
    retrieve.add_argument("--readonly", action="store_true", help="do not change activation")
    retrieve.add_argument("--packet", action="store_true", help="print only the text packet")
    phase = commands.add_parser("log-phase")
    phase.add_argument("event_id")
    phase.add_argument("--title", required=True)
    phase.add_argument("--summary", required=True)
    phase.add_argument("--content", default="")
    phase.add_argument("--follows", help="comma-separated earlier record ids")
    phase.add_argument("--caused-by")
    phase.add_argument("--depends-on")
    phase.add_argument("--open-loop", action="store_true")
    phase.add_argument("--source", default="")
    fact = commands.add_parser("log-fact")
    fact.add_argument("fact_id")
    fact.add_argument("--title", required=True)
    fact.add_argument("--summary", required=True)
    fact.add_argument("--content", default="")
    fact.add_argument("--source", default="")
    timeline = commands.add_parser("timeline")
    timeline.add_argument("--limit", type=int, default=20)
    commands.add_parser("decay", help="apply time decay (run daily, e.g. from a scheduler)")
    commands.add_parser("core-proposals", help="list open core proposals and stale state")
    approve = commands.add_parser("approve-core", help="owner: issue and consume a core decision receipt")
    approve.add_argument("proposal_id")
    outcome = approve.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--apply", action="store_true")
    outcome.add_argument("--reject", action="store_true")
    approve.add_argument("--note", default="")
    approve.add_argument("--issue-only", action="store_true")
    approve_retract = commands.add_parser("approve-retract", help="owner: issue and consume a retract receipt")
    approve_retract.add_argument("record_id")
    approve_retract.add_argument("--reason", required=True)
    approve_retract.add_argument("--issue-only", action="store_true")
    close = commands.add_parser("close-legacy-discussion", help="owner: close a migrated v4 discussion")
    close.add_argument("--note", required=True)
    close.add_argument("--issue-only", action="store_true")
    apply_receipt = commands.add_parser("apply-receipt", help="consume an existing owner receipt")
    apply_receipt.add_argument("receipt_id")
    loop = commands.add_parser("close-loop")
    loop.add_argument("record_id")
    loop.add_argument("--note", required=True)
    migrate = commands.add_parser("migrate-to", help="migrate only a copy of a legacy store")
    migrate.add_argument("target", type=Path)
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument("--backup", type=Path)
    aml = commands.add_parser("import-aml", help="import an AML vault (read-only)")
    aml.add_argument("path", type=Path)
    return root


def _settle(receipt: dict, issue_only: bool, consume) -> dict:
    """R2a §4.2: after a receipt exists, --issue-only returns it; otherwise consume
    inline once. On any consume failure the durable receipt_id goes to stderr and
    the exact typed failure is the exit text (no retry, no second receipt)."""
    if issue_only:
        return {"receipt_id": receipt["receipt_id"], "status": "issued"}
    try:
        return consume(receipt["receipt_id"])
    except Exception as exc:
        print(receipt["receipt_id"], file=sys.stderr)
        raise SystemExit(f"{type(exc).__name__}: {exc}") from exc


def main(argv: list[str] | None = None) -> None:
    utf8_stdio()
    args = parser().parse_args(argv)
    command = args.command
    if command == "profiles":
        print(json.dumps({"profiles": list_profiles()}, ensure_ascii=False, indent=2))
        return
    if command == "plugin":
        from .plugin import build_plugin

        if not args.profile:
            raise SystemExit("plugin needs a profile: trajecta-identity -p NAME plugin")
        result = build_plugin(
            args.out, args.profile, name=args.name, target=args.target, house=args.house,
            data_dir=args.data_dir, profiles_dir=args.profiles_dir, work_root=args.work_root,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    profile = resolve(args.profile)
    memory = IdentityMemory(profile, args.db, surface="cli")
    if command == "init":
        result = memory.bootstrap()
    elif command == "work":
        if memory.work is None:
            raise SystemExit("no work store: set TRAJECTA_WORK_ROOT or work_root in the profile")
        result = {"work_store": str(memory.work.root), "work": memory.work.work_items()}
    elif command == "view":
        from .view import serve

        # Read-only surface: bootstrap only a fresh store (as view.snapshot does);
        # a legacy-v4 store goes straight to serve.
        if memory.store.schema_info()["state"] == "uninitialized":
            memory.bootstrap()
        serve(memory, port=args.port, open_browser=not args.no_browser)
        return
    elif command == "setup":
        result = setup_config(profile.name, args.db, args.name)
        memory.bootstrap()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(
            "\nPaste this into your agent client's MCP config "
            "(Claude Desktop: Settings → Developer → Edit Config).",
            file=sys.stderr,
        )
        return
    elif command == "status":
        result = memory.status()
    elif command == "retrieve":
        result = memory.retrieve(
            args.cue,
            limit=args.limit,
            token_budget=args.budget,
            include_history=True if args.history else None,
            track=not args.readonly,
        )
        if args.packet:
            sys.stdout.write(result["packet"])
            return
    elif command == "log-phase":
        memory.bootstrap()
        result = memory.log_phase(
            args.event_id,
            title=args.title,
            summary=args.summary,
            content=args.content,
            follows=_list(args.follows),
            caused_by=_list(args.caused_by),
            depends_on=_list(args.depends_on),
            open_loop=args.open_loop,
            source_ref=args.source,
        )
    elif command == "log-fact":
        memory.bootstrap()
        result = memory.log_fact(
            args.fact_id, title=args.title, summary=args.summary,
            content=args.content, source_ref=args.source,
        )
    elif command == "timeline":
        result = {"timeline": memory.timeline(args.limit)}
    elif command == "decay":
        memory.bootstrap()
        result = memory.decay()
    elif command == "core-proposals":
        result = {"open_core_proposals": memory.open_core_proposals()}
    elif command == "approve-core":
        receipt = memory.issue_core_receipt(
            args.proposal_id,
            outcome="apply" if args.apply else "reject",
            decision_note=args.note,
            stdin=sys.stdin,
            stdout=sys.stdout,
        )
        result = _settle(receipt, args.issue_only, memory.identity_core_apply)
    elif command == "approve-retract":
        receipt = memory.issue_retract_receipt(
            args.record_id, reason=args.reason, stdin=sys.stdin, stdout=sys.stdout
        )
        result = _settle(receipt, args.issue_only, memory.identity_retract)
    elif command == "close-legacy-discussion":
        receipt = memory.issue_legacy_close_receipt(
            note=args.note, stdin=sys.stdin, stdout=sys.stdout
        )
        result = _settle(receipt, args.issue_only, memory.identity_close_legacy_discussion)
    elif command == "apply-receipt":
        result = memory.apply_receipt(args.receipt_id)
    elif command == "close-loop":
        memory.bootstrap()
        result = memory.close_loop(args.record_id, note=args.note)
    elif command == "migrate-to":
        migrated = memory.store.migrate_to(
            args.target, dry_run=args.dry_run, backup_path=args.backup
        )
        result = migrated if isinstance(migrated, dict) else migrated.schema_info()
    elif command == "import-aml":
        memory.bootstrap()
        from .migrate_aml import import_aml

        result = import_aml(memory, args.path)
    else:  # pragma: no cover
        raise SystemExit(f"unknown command {command}")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
