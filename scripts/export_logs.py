"""Export a portable log archive using only the Python standard library."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path


def copy_log(source: Path, target: Path) -> None:
    """Replace a copied log atomically, including when the source is still growing."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    shutil.copyfile(source, temporary)
    temporary.replace(target)


def export_logs(projects: Path, provider: Path, runs: Path | None, output: Path) -> dict:
    """Copy transcripts and referenced details; retain archived transcripts on re-export."""
    projects, output = projects.expanduser().resolve(), output.expanduser().resolve()
    if not projects.is_dir():
        raise ValueError(f"Claude projects directory not found: {projects}")
    if output.is_relative_to(projects) or projects.is_relative_to(output):
        raise ValueError("Export folder must be separate from the Claude projects directory")
    if runs is not None and not runs.expanduser().is_file():
        raise ValueError(f"Coding-offload summary not found: {runs}")
    output.mkdir(parents=True, exist_ok=True)
    count = 0
    for source in projects.rglob("*.jsonl"):
        copy_log(source, output / "projects" / source.relative_to(projects))
        count += 1
    warnings = []
    if provider.expanduser().is_file():
        copy_log(provider.expanduser(), output / "claude_providers.jsonl")
    else:
        warnings.append("No provider history supplied; older requests use model-name inference.")
    records = []
    details = 0
    if runs is not None:
        runs = runs.expanduser().resolve()
        for line in runs.read_text(errors="replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                warnings.append("Skipped an incomplete or invalid summary line.")
                continue
            if not isinstance(record, dict):
                continue
            original = record.pop("log_path", None)
            record.pop("pid", None)  # A VM PID must never be checked on the dashboard host.
            if original:
                source = Path(original).expanduser()
                if not source.is_absolute():
                    source = runs.parent / source
                name = hashlib.sha256(str(source.resolve()).encode()).hexdigest() + ".log"
                if source.is_file():
                    copy_log(source, output / "details" / name)
                    record["log_path"] = "details/" + name
                    details += 1
                else:
                    warnings.append(f"Detail log missing for run {record.get('id', 'unknown')}; summary retained.")
            records.append(record)
        temporary = output / "sambanova_runs.jsonl.tmp"
        temporary.write_text("".join(json.dumps(record) + "\n" for record in records))
        temporary.replace(output / "sambanova_runs.jsonl")
    manifest = {"exported_at": datetime.now(timezone.utc).isoformat(),
                "transcript_files": count, "detail_files": details, "warnings": warnings}
    temporary = output / "manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    temporary.replace(output / "manifest.json")
    return manifest


def main() -> None:
    """Export the current machine's logs without API keys or Claude settings."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projects", type=Path, default=Path.home() / ".claude/projects")
    parser.add_argument("--provider-log", type=Path,
                        default=Path.home() / ".local/share/cost-lens/claude_providers.jsonl")
    parser.add_argument("--runs", type=Path, help="Optional tracked coding-offload JSONL summary")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = export_logs(args.projects, args.provider_log, args.runs, args.output)
    except (ValueError, OSError) as error:
        parser.exit(1, f"Export failed: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
