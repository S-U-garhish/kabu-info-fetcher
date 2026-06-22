#!/usr/bin/env python3
"""Create a safe handoff snapshot of EDINET scripts and downloaded results."""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


SOURCE_FILES = (
    "edinet_downloader.py",
    "package_handoff.py",
    "test_edinet_downloader.py",
    "test_package_handoff.py",
    "README.md",
    "requirements.txt",
    ".gitignore",
    "companies.csv",
    "companies.example.csv",
    "EDINET.md",
    "ESE140206.pdf",
)


@dataclass(frozen=True)
class SnapshotFile:
    source: Path
    archive_name: str
    size: int
    modified: str
    category: str


def readable_size(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def is_stable_file(path: Path) -> bool:
    return (
        path.is_file()
        and not path.name.endswith(".part")
        and path.name != ".env"
    )


def snapshot_file(path: Path, archive_name: str, category: str) -> SnapshotFile:
    stat = path.stat()
    return SnapshotFile(
        source=path,
        archive_name=archive_name.replace("\\", "/"),
        size=stat.st_size,
        modified=datetime.fromtimestamp(stat.st_mtime)
        .astimezone()
        .isoformat(timespec="seconds"),
        category=category,
    )


def collect_sources(workspace: Path) -> list[SnapshotFile]:
    files: list[SnapshotFile] = []
    for name in SOURCE_FILES:
        path = workspace / name
        if is_stable_file(path):
            files.append(snapshot_file(path, name, "script"))
    return files


def collect_results(
    output: Path,
    include_state: bool,
) -> tuple[list[SnapshotFile], list[SnapshotFile]]:
    deliverables: list[SnapshotFile] = []
    state: list[SnapshotFile] = []
    for path in sorted(output.rglob("*")):
        if not is_stable_file(path):
            continue
        relative = path.relative_to(output)
        if relative.parts and relative.parts[0] == ".edinet":
            if include_state:
                state.append(
                    snapshot_file(
                        path,
                        f"output/{relative.as_posix()}",
                        "resume-state",
                    )
                )
            continue
        if path.name == ".complete":
            continue
        archive_parent: Path | None = None
        for parent in path.parents:
            if parent == output:
                break
            if parent.name.endswith(("_xbrl", "_csv")):
                archive_parent = parent
                break
        if archive_parent is not None and not (archive_parent / ".complete").exists():
            continue
        deliverables.append(
            snapshot_file(
                path,
                f"output/{relative.as_posix()}",
                "result",
            )
        )
    return deliverables, state


def partition_files(
    files: list[SnapshotFile],
    max_uncompressed_bytes: int,
) -> list[list[SnapshotFile]]:
    if not files:
        return []
    parts: list[list[SnapshotFile]] = []
    current: list[SnapshotFile] = []
    current_size = 0
    for item in files:
        if current and current_size + item.size > max_uncompressed_bytes:
            parts.append(current)
            current = []
            current_size = 0
        current.append(item)
        current_size += item.size
    if current:
        parts.append(current)
    return parts


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_zip(
    destination: Path,
    files: list[SnapshotFile],
    compression: int,
) -> None:
    temporary = destination.with_name(destination.name + ".part")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        temporary,
        "w",
        compression=compression,
        allowZip64=True,
        compresslevel=6 if compression == zipfile.ZIP_DEFLATED else None,
    ) as archive:
        for item in files:
            # Files produced by the downloader are atomically renamed into place.
            # If one disappears between snapshot and packaging, record the omission
            # rather than aborting the entire handoff.
            if item.source.exists():
                archive.write(item.source, item.archive_name)
    temporary.replace(destination)


def write_manifest(
    destination: Path,
    rows: list[tuple[SnapshotFile, str]],
    include_hashes: bool,
) -> None:
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "category",
                "archive",
                "path",
                "size_bytes",
                "modified",
                "sha256",
            ]
        )
        for item, archive_name in rows:
            digest = sha256_file(item.source) if include_hashes else ""
            writer.writerow(
                [
                    item.category,
                    archive_name,
                    item.archive_name,
                    item.size,
                    item.modified,
                    digest,
                ]
            )


def count_cached_dates(output: Path) -> int:
    lists = output / ".edinet" / "lists"
    return len(list(lists.glob("*.json"))) if lists.exists() else 0


def write_summary(
    destination: Path,
    label: str,
    output: Path,
    deliverables: list[SnapshotFile],
    state: list[SnapshotFile],
    archive_names: list[str],
) -> None:
    extensions = Counter(
        item.source.suffix.lower() or "(no extension)" for item in deliverables
    )
    company_dirs = {
        Path(item.archive_name).parts[1]
        for item in deliverables
        if len(Path(item.archive_name).parts) >= 3
    }
    lines = [
        f"EDINET handoff snapshot: {label}",
        f"Created: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"Source output: {output.resolve()}",
        f"Company directories: {len(company_dirs)}",
        f"Deliverable files: {len(deliverables)}",
        f"Deliverable size (uncompressed): {readable_size(sum(x.size for x in deliverables))}",
        f"Resume-state files included: {len(state)}",
        f"Cached EDINET list dates observed: {count_cached_dates(output)}",
        "",
        "Files by extension:",
    ]
    lines.extend(
        f"  {extension}: {count}"
        for extension, count in sorted(extensions.items())
    )
    lines.extend(["", "Archives:"])
    lines.extend(f"  {name}" for name in archive_names)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="実行中のEDINET成果物とスクリプトを引継ぎ用ZIPにします"
    )
    parser.add_argument("--output", type=Path, default=Path("output"))
    parser.add_argument("--package-dir", type=Path, default=Path("handoff"))
    parser.add_argument(
        "--label",
        default=datetime.now().astimezone().strftime("%Y%m%d_%H%M%S"),
    )
    parser.add_argument(
        "--max-archive-gb",
        type=float,
        default=4.0,
        help="成果物ZIP1本あたりの非圧縮サイズ目安（既定4GB）",
    )
    parser.add_argument(
        "--include-state",
        action="store_true",
        help="再開用のoutput/.edinetキャッシュも別ZIPに含めます",
    )
    parser.add_argument(
        "--hash",
        action="store_true",
        help="manifestにSHA-256を記録します（大容量時は時間がかかります）",
    )
    parser.add_argument(
        "--store",
        action="store_true",
        help="圧縮せず格納し、CPU時間を節約します",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_archive_gb <= 0:
        print("--max-archive-gb は0より大きくしてください", file=sys.stderr)
        return 2
    if not args.output.is_dir():
        print(f"成果物ディレクトリがありません: {args.output}", file=sys.stderr)
        return 2

    workspace = Path(__file__).resolve().parent
    package_dir = args.package_dir.resolve()
    package_dir.mkdir(parents=True, exist_ok=True)
    compression = zipfile.ZIP_STORED if args.store else zipfile.ZIP_DEFLATED
    max_bytes = int(args.max_archive_gb * 1024**3)

    sources = collect_sources(workspace)
    deliverables, state = collect_results(args.output.resolve(), args.include_state)
    result_parts = partition_files(deliverables, max_bytes)
    state_parts = partition_files(state, max_bytes)
    manifest_rows: list[tuple[SnapshotFile, str]] = []
    archive_names: list[str] = []

    source_name = f"edinet_scripts_{args.label}.zip"
    write_zip(package_dir / source_name, sources, compression)
    archive_names.append(source_name)
    manifest_rows.extend((item, source_name) for item in sources)

    for index, part in enumerate(result_parts, start=1):
        suffix = "" if len(result_parts) == 1 else f"_part{index:03d}"
        name = f"edinet_results_{args.label}{suffix}.zip"
        write_zip(package_dir / name, part, compression)
        archive_names.append(name)
        manifest_rows.extend((item, name) for item in part)

    for index, part in enumerate(state_parts, start=1):
        suffix = "" if len(state_parts) == 1 else f"_part{index:03d}"
        name = f"edinet_resume_state_{args.label}{suffix}.zip"
        write_zip(package_dir / name, part, compression)
        archive_names.append(name)
        manifest_rows.extend((item, name) for item in part)

    manifest_name = f"manifest_{args.label}.csv"
    summary_name = f"progress_summary_{args.label}.txt"
    write_manifest(package_dir / manifest_name, manifest_rows, args.hash)
    write_summary(
        package_dir / summary_name,
        args.label,
        args.output.resolve(),
        deliverables,
        state,
        archive_names,
    )

    print(f"Snapshot: {args.label}")
    print(f"Deliverable files: {len(deliverables)}")
    print(f"Archives: {len(archive_names)}")
    for name in archive_names:
        path = package_dir / name
        print(f"  {path} ({readable_size(path.stat().st_size)})")
    print(f"  {package_dir / manifest_name}")
    print(f"  {package_dir / summary_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
