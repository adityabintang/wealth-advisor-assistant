"""CLI entry point (``wealth-advisor``). Each run writes a validated JSON + a text report."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from .app import Application, RuntimeOptions, build_application
from .config import Settings, load_settings
from .hitl import AutoApproveReviewer, PendingReviewer, Reviewer
from .integrations.crm_mock import FAILURE_MODES
from .logging_setup import configure_logging
from .models import AdvisoryOutput, ReviewDecision, RunResult
from .reporting import batch_summary, build_failure, build_report, render, to_text
from .tools.client_data import client_id_from_path

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Wealth Advisor Assistant - multi-agent advisory pipeline.")
console = Console()


# ----------------------------------------------------------------------------- interactive reviewer
class ConsoleReviewer:
    """Interactive HITL: approve / edit / reject in the terminal."""

    def __init__(self, reviewer_name: str, out: Console) -> None:
        self.name = reviewer_name
        self.out = out

    def review(self, packet: dict[str, Any]) -> ReviewDecision:
        o = self.out
        o.rule(f"[bold yellow]Human review required - {packet.get('client_name')} ({packet['client_id']})")
        for r in packet.get("reasons", []):
            o.print(f"  [yellow]* {r}")
        o.print(f"\n[bold]Summary:[/] {packet['summary']}\n")
        if packet.get("anomalies"):
            t = Table(title="Findings")
            for c in ("ID", "Severity", "Finding"):
                t.add_column(c)
            for a in packet["anomalies"]:
                t.add_row(a["anomaly_id"], a["severity"], a["title"])
            o.print(t)
        t = Table(title="Draft recommendations")
        for c in ("ID", "Priority", "Action"):
            t.add_column(c)
        for r in packet["recommendations"]:
            t.add_row(r["rec_id"], r["priority"], r["action"])
        o.print(t)

        choice = Prompt.ask("Decision", choices=["approve", "edit", "reject"], default="approve", console=o)
        note = Prompt.ask("Reviewer note (optional)", default="", console=o) or None
        if choice != "edit":
            return ReviewDecision(action=choice, reviewer=self.name, mode="interactive", note=note)  # type: ignore[arg-type]

        valid = {r["rec_id"] for r in packet["recommendations"]}
        removed_raw = Prompt.ask("IDs to remove (comma-separated, blank for none)", default="", console=o)
        removed = [x.strip().upper() for x in removed_raw.split(",") if x.strip().upper() in valid]
        modified: dict[str, str] = {}
        while True:
            rid = Prompt.ask("ID to rewrite (blank to finish)", default="", console=o).strip().upper()
            if not rid:
                break
            if rid not in valid or rid in removed:
                o.print("[red]Unknown or removed ID")
                continue
            modified[rid] = Prompt.ask(f"New action for {rid}", console=o)
        return ReviewDecision(action="edit", reviewer=self.name, mode="interactive", note=note, removed_rec_ids=removed, modified_actions=modified)


# ----------------------------------------------------------------------------- helpers
def _setup(verbose: bool, log_file: Optional[Path], settings: Settings) -> Path:
    path = log_file or settings.paths.log_dir / "wealth_advisor.jsonl"
    configure_logging(path, console_level=logging.INFO if verbose else logging.WARNING)
    return path


def _reviewer(auto_approve: bool, non_interactive: bool, reviewer_name: str) -> Reviewer:
    if auto_approve:
        return AutoApproveReviewer()
    if non_interactive or not sys.stdin.isatty():
        return PendingReviewer()
    return ConsoleReviewer(reviewer_name, console)


def _build(settings: Settings, crm_failure: Optional[str], no_llm: bool, memory_path: Optional[Path], no_memory: bool) -> Application:
    if crm_failure and crm_failure not in FAILURE_MODES:
        raise typer.BadParameter(f"--crm-failure must be one of {', '.join(FAILURE_MODES)}")
    mem: Any = ":memory:" if no_memory else memory_path
    return build_application(settings, RuntimeOptions(crm_failure=crm_failure, use_llm=not no_llm, memory_path=mem))


def _write(result: RunResult, out_dir: Path, *, label: Optional[str] = None) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = label or result.client_id
    if result.output:
        jp = out_dir / f"{stem}.advisory.json"
        tp = out_dir / f"{stem}.report.txt"
        jp.write_text(result.output.model_dump_json(indent=2), encoding="utf-8")
        tp.write_text(to_text(build_report(result.output)), encoding="utf-8")
        return [jp, tp]
    ep = out_dir / f"{stem}.error.json"
    ep.write_text(result.model_dump_json(indent=2, exclude={"output"}), encoding="utf-8")
    tp = out_dir / f"{stem}.report.txt"
    tp.write_text(to_text(build_failure(result)), encoding="utf-8")
    return [ep, tp]


def _show(result: RunResult, quiet: bool) -> None:
    if quiet:
        return
    render(console, build_report(result.output) if result.output else build_failure(result))


def _client_files(settings: Settings) -> list[Path]:
    return sorted(settings.paths.clients_dir.glob("*.json"))


# Common options -----------------------------------------------------------------
AutoApprove = typer.Option(False, "--auto-approve", help="Approve HITL checkpoints automatically (demo/CI; recorded in output and log).")
NonInteractive = typer.Option(False, "--non-interactive", help="Never prompt; HITL items end as pending_review.")
CrmFailure = typer.Option(None, "--crm-failure", help=f"Force a CRM failure mode for every client: {', '.join(FAILURE_MODES)}.")
NoLLM = typer.Option(False, "--no-llm", help="Disable the optional LLM narrative even if configured.")
OutputDir = typer.Option(None, "--output-dir", "-o", help="Where to write <client>.advisory.json / .report.txt (default: output/).")
LogFile = typer.Option(None, "--log-file", help="JSON-lines log file (default: logs/wealth_advisor.jsonl).")
MemoryPath = typer.Option(None, "--memory-path", help="SQLite long-term memory path (default: .state/memory.sqlite).")
NoMemory = typer.Option(False, "--no-memory", help="Use an ephemeral in-memory store (nothing persisted).")
Verbose = typer.Option(False, "--verbose", "-v", help="Show INFO-level log events on the console.")
Quiet = typer.Option(False, "--quiet", "-q", help="Do not print the report to the console.")
ReviewerName = typer.Option("advisor", "--reviewer", help="Reviewer name recorded for interactive decisions.")


@app.command()
def run(
    client_id: str = typer.Argument(..., help="Client id, e.g. C001"),
    file: Optional[Path] = typer.Option(None, "--file", "-f", help="Explicit client JSON file (overrides lookup in data/clients)."),
    auto_approve: bool = AutoApprove,
    non_interactive: bool = NonInteractive,
    crm_failure: Optional[str] = CrmFailure,
    no_llm: bool = NoLLM,
    output_dir: Optional[Path] = OutputDir,
    log_file: Optional[Path] = LogFile,
    memory_path: Optional[Path] = MemoryPath,
    no_memory: bool = NoMemory,
    verbose: bool = Verbose,
    quiet: bool = Quiet,
    reviewer: str = ReviewerName,
    label: Optional[str] = typer.Option(None, "--label", help="Output file stem (default: client id)."),
) -> None:
    """Analyse one client end-to-end."""
    settings = load_settings()
    log_path = _setup(verbose, log_file, settings)
    application = _build(settings, crm_failure, no_llm, memory_path, no_memory)
    try:
        result = application.orchestrator.run(client_id, _reviewer(auto_approve, non_interactive, reviewer), str(file) if file else None)
    finally:
        application.close()
    _show(result, quiet)
    paths = _write(result, output_dir or settings.paths.output_dir, label=label)
    console.print(f"[dim]Wrote {', '.join(str(p) for p in paths)}; log: {log_path}")
    raise typer.Exit(code=0 if result.status == "completed" else 1)


@app.command("run-all")
def run_all(
    auto_approve: bool = AutoApprove,
    non_interactive: bool = NonInteractive,
    crm_failure: Optional[str] = CrmFailure,
    no_llm: bool = NoLLM,
    output_dir: Optional[Path] = OutputDir,
    log_file: Optional[Path] = LogFile,
    memory_path: Optional[Path] = MemoryPath,
    no_memory: bool = NoMemory,
    verbose: bool = Verbose,
    quiet: bool = Quiet,
    reviewer: str = ReviewerName,
) -> None:
    """Analyse every sample client in data/clients. One failing client never stops the others."""
    settings = load_settings()
    log_path = _setup(verbose, log_file, settings)
    application = _build(settings, crm_failure, no_llm, memory_path, no_memory)
    out_dir = output_dir or settings.paths.output_dir
    review = _reviewer(auto_approve, non_interactive, reviewer)
    results: list[RunResult] = []
    try:
        for path in _client_files(settings):
            cid = client_id_from_path(path)
            result = application.orchestrator.run(cid, review, str(path))
            results.append(result)
            _show(result, quiet)
            _write(result, out_dir, label=path.stem)
    finally:
        application.close()
    console.print(batch_summary(results))
    console.print(f"[dim]Outputs in {out_dir}; log: {log_path}")


@app.command("list-clients")
def list_clients() -> None:
    """List sample client files."""
    settings = load_settings()
    t = Table(title=f"Clients in {settings.paths.clients_dir}")
    t.add_column("Client id")
    t.add_column("File")
    for p in _client_files(settings):
        t.add_row(client_id_from_path(p), p.name)
    console.print(t)


@app.command()
def history(client_id: str, memory_path: Optional[Path] = MemoryPath) -> None:
    """Show long-term memory for a client (previous runs, anomaly history, decisions)."""
    settings = load_settings()
    configure_logging(None)
    application = build_application(settings, RuntimeOptions(use_llm=False, memory_path=memory_path))
    try:
        data = application.memory.get_client_history(client_id, limit=20)
    finally:
        application.close()
    t = Table(title=f"Runs for {client_id}")
    for c in ("run_id", "generated_at", "total_assets", "confidence", "degraded", "review", "#anomalies"):
        t.add_column(c)
    for r in data["runs"]:
        t.add_row(r["run_id"], r["generated_at"], f"{r['total_assets'] or 0:,.0f}", f"{r['confidence']:.2f}", str(bool(r["degraded"])), r["review_status"], str(len(r["anomaly_fingerprints"])))
    console.print(t)
    a = Table(title="Anomaly history")
    for c in ("fingerprint", "first_seen", "last_seen", "occurrences", "acknowledged"):
        a.add_column(c)
    for fp, rec in data["anomalies"].items():
        a.add_row(fp, rec["first_seen"], rec["last_seen"], str(rec["occurrences"]), str(bool(rec["acknowledged"])))
    console.print(a)


@app.command("reset-memory")
def reset_memory(memory_path: Optional[Path] = MemoryPath, yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Delete everything in long-term memory."""
    if not yes and not typer.confirm("Erase long-term memory?"):
        raise typer.Abort()
    settings = load_settings()
    configure_logging(None)
    application = build_application(settings, RuntimeOptions(use_llm=False, memory_path=memory_path))
    application.memory.reset()
    application.close()
    console.print("[green]Long-term memory cleared.")


@app.command()
def schema(out: Optional[Path] = typer.Option(None, "--out", help="Write the JSON Schema to this file.")) -> None:
    """Print (or write) the JSON Schema of AdvisoryOutput."""
    text = json.dumps(AdvisoryOutput.model_json_schema(), indent=2)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        console.print(f"[green]Schema written to {out}")
    else:
        console.print_json(text)


@app.command()
def tools() -> None:
    """List registered tools (the agents' only gateway to data, CRM, memory and LLM)."""
    settings = load_settings()
    configure_logging(None)
    application = build_application(settings, RuntimeOptions(use_llm=False, memory_path=":memory:"))
    t = Table(title="Tool registry")
    t.add_column("Name")
    t.add_column("Description")
    for d in application.tools.describe():
        t.add_row(d["name"], d["description"])
    application.close()
    console.print(t)


if __name__ == "__main__":  # pragma: no cover
    app()
