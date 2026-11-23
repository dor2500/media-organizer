"""Scan -> plan -> execute pipeline.

Determinism guarantees
  * Files are ordered by (resolved datetime, natural-sorted original name, full path) before
    names are assigned, so the same input always yields the same output names.
  * Within a target folder, the first file of a day gets DD.MM.ext, the next DD.MM-2.ext,
    DD.MM-3.ext ... The counter is shared across extensions, so names stay unique by stem.
  * Existing files in the output folder are never overwritten; their stems are reserved.
  * Byte-identical files (SHA-256) are detected among inputs and against files already
    present in the target folder. Default: keep the first, skip (and log) the rest.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import threading
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from .dates import MEDIA_EXTS, DateResult, resolve_date

LOG_DIR_NAME = "_organizer_logs"

# statuses
PLANNED = "planned"
DONE = "done"
DUPLICATE = "duplicate"
OUT_OF_RANGE = "out-of-range"
SKIPPED = "skipped"
ERROR = "error"

ProgressCB = Callable[[int, int, str], None]


@dataclass
class PlanItem:
    src: Path
    size: int = 0
    date: DateResult | None = None
    dest: Path | None = None
    status: str = PLANNED
    note: str = ""
    suffix: int = 1            # 1 = no suffix, 2 = "-2", ...
    duplicate_of: str = ""

    def row(self) -> dict:
        return {
            "status": self.status,
            "source": str(self.src),
            "destination": str(self.dest) if self.dest else "",
            "date": self.date.dt.strftime("%Y-%m-%d %H:%M:%S") if self.date else "",
            "date_source": self.date.source if self.date else "",
            "date_detail": self.date.detail if self.date else "",
            "collision_suffix": self.suffix if self.suffix > 1 else "",
            "duplicate_of": self.duplicate_of,
            "size": self.size,
            "note": self.note,
        }


@dataclass
class Options:
    source: Path
    output: Path
    mode: str = "copy"                 # copy | move
    recursive: bool = True
    year_from: int | None = None
    year_to: int | None = None
    duplicates: str = "skip"           # skip | keep
    use_filename_dates: bool = True
    verify: bool = True                # re-hash after copy


@dataclass
class Plan:
    options: Options
    items: list[PlanItem] = field(default_factory=list)
    ignored: list[Path] = field(default_factory=list)   # non-media files found


# ----------------------------------------------------------------------- helpers
def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _natural(s: str) -> tuple:
    """Natural sort key: 'IMG_9' < 'IMG_10', 'a' < 'a copy'."""
    import re
    return tuple((0, int(t), "") if t.isdigit() else (1, 0, t)
                 for t in re.split(r"(\d+)", s.casefold()) if t)


def scan(opts: Options) -> tuple[list[Path], list[Path]]:
    src = Path(opts.source)
    out = Path(opts.output)
    media, ignored = [], []
    skip_out = out.exists() and _is_within(out, src) and out.resolve() != src.resolve()
    walker = os.walk(src) if opts.recursive else [(str(src), [], os.listdir(src))]
    for root, dirs, files in walker:
        rp = Path(root)
        # never descend into our own log folder or into the output folder
        dirs[:] = sorted(
            d for d in dirs
            if d != LOG_DIR_NAME and not (skip_out and (rp / d).resolve() == out.resolve())
        )
        for name in sorted(files):
            p = rp / name
            if not p.is_file():
                continue
            (media if p.suffix.lower() in MEDIA_EXTS else ignored).append(p)
    return media, ignored


# ----------------------------------------------------------------------- planning
def build_plan(opts: Options, progress: ProgressCB | None = None,
               cancel: threading.Event | None = None) -> Plan:
    media, ignored = scan(opts)
    plan = Plan(opts, ignored=ignored)
    total = len(media)

    for i, p in enumerate(media, 1):
        if cancel and cancel.is_set():
            break
        item = PlanItem(src=p)
        try:
            item.size = p.stat().st_size
            item.date = resolve_date(p, use_filename=opts.use_filename_dates)
        except Exception as e:  # unreadable file etc.
            item.status, item.note = ERROR, f"cannot read: {e}"
        plan.items.append(item)
        if progress:
            progress(i, total, f"Reading dates: {p.name}")

    _assign(plan, progress)
    return plan


def _assign(plan: Plan, progress: ProgressCB | None) -> None:
    opts = plan.options
    out = Path(opts.output)
    candidates = [it for it in plan.items if it.status == PLANNED]

    for it in candidates:
        y = it.date.dt.year
        if (opts.year_from and y < opts.year_from) or (opts.year_to and y > opts.year_to):
            it.status, it.note = OUT_OF_RANGE, f"year {y} outside range"

    active = [it for it in candidates if it.status == PLANNED]
    active.sort(key=lambda it: (it.date.dt, _natural(it.src.stem), it.src.suffix.casefold(),
                                str(it.src).casefold()))

    # --- duplicate detection among inputs (hash only files whose sizes collide)
    hashes: dict[Path, str] = {}

    def h(p: Path) -> str:
        if p not in hashes:
            hashes[p] = sha256(p)
        return hashes[p]

    if opts.duplicates == "skip":
        by_size: dict[int, list[PlanItem]] = defaultdict(list)
        for it in active:
            by_size[it.size].append(it)
        for group in by_size.values():
            if len(group) < 2:
                continue
            first_by_hash: dict[str, PlanItem] = {}
            for it in group:  # group keeps the deterministic sort order
                try:
                    digest = h(it.src)
                except OSError as e:
                    it.status, it.note = ERROR, f"cannot hash: {e}"
                    continue
                if digest in first_by_hash:
                    it.status = DUPLICATE
                    it.duplicate_of = str(first_by_hash[digest].src)
                    it.note = "identical content to another input file"
                else:
                    first_by_hash[digest] = it

    # --- name assignment per target folder
    reserved: dict[Path, set[str]] = {}
    existing_by_size: dict[Path, dict[int, list[Path]]] = {}

    def folder_state(folder: Path):
        if folder not in reserved:
            stems, sizes = set(), defaultdict(list)
            if folder.is_dir():
                for e in folder.iterdir():
                    if e.is_file():
                        stems.add(e.stem.casefold())
                        sizes[e.stat().st_size].append(e)
            reserved[folder] = stems
            existing_by_size[folder] = sizes
        return reserved[folder], existing_by_size[folder]

    n = len(active)
    for i, it in enumerate(active, 1):
        if progress:
            progress(i, n, f"Planning: {it.src.name}")
        if it.status != PLANNED:
            continue
        dt = it.date.dt
        folder = out / f"{dt.year:04d}" / f"{dt.month:02d}"
        stems, sizes = folder_state(folder)

        # already present in output (e.g. re-run)? -> duplicate, never re-copied
        if opts.duplicates == "skip" and it.size in sizes:
            try:
                digest = h(it.src)
                match = next((e for e in sizes[it.size] if sha256(e) == digest), None)
            except OSError:
                match = None
            if match:
                it.status = DUPLICATE
                it.duplicate_of = str(match)
                it.note = "identical file already exists in output"
                continue

        base = f"{dt.day:02d}.{dt.month:02d}"
        ext = it.src.suffix.lower()
        k = 1
        while True:
            stem = base if k == 1 else f"{base}-{k}"
            if stem.casefold() not in stems:
                break
            k += 1
        stems.add(stem.casefold())
        it.suffix = k
        it.dest = folder / f"{stem}{ext}"


# ----------------------------------------------------------------------- execution
def _safe_target(dest: Path) -> Path:
    """Last-line defence against overwrite if something appeared since planning."""
    if not dest.exists():
        return dest
    base, ext = dest.stem.split("-")[0], dest.suffix
    k = 2
    while True:
        cand = dest.with_name(f"{base}-{k}{ext}")
        if not cand.exists():
            return cand
        k += 1


def execute(plan: Plan, progress: ProgressCB | None = None,
            cancel: threading.Event | None = None) -> list[dict]:
    opts = plan.options
    todo = [it for it in plan.items if it.status == PLANNED]
    manifest: list[dict] = []
    for i, it in enumerate(todo, 1):
        if cancel and cancel.is_set():
            it.note = "cancelled"
            continue
        if progress:
            progress(i, len(todo), f"{opts.mode.title()}: {it.src.name}")
        try:
            it.dest.parent.mkdir(parents=True, exist_ok=True)
            target = _safe_target(it.dest)
            if target != it.dest:
                it.note = f"target appeared during run, used {target.name}"
                it.dest = target
            src_hash = sha256(it.src) if opts.verify else None
            same_drive = os.path.splitdrive(str(it.src.resolve()))[0].lower() == \
                os.path.splitdrive(str(target.resolve()))[0].lower()
            if opts.mode == "move" and same_drive:
                os.rename(it.src, target)
            else:
                shutil.copy2(it.src, target)
                if src_hash and sha256(target) != src_hash:
                    target.unlink(missing_ok=True)
                    raise IOError("verification failed (hash mismatch)")
                if opts.mode == "move":
                    it.src.unlink()
            it.status = DONE
            manifest.append({"mode": opts.mode, "src": str(it.src), "dest": str(target),
                             "sha256": src_hash})
        except Exception as e:
            it.status, it.note = ERROR, str(e)
    return manifest


# ----------------------------------------------------------------------- reporting
def summarize(plan: Plan) -> dict:
    items = plan.items
    status = Counter(it.status for it in items)
    sources = Counter(it.date.source for it in items if it.date)
    details = Counter(it.date.detail for it in items if it.date)
    years = Counter(it.date.dt.year for it in items if it.date and it.dest)
    return {
        "total_media": len(items),
        "ignored_non_media": len(plan.ignored),
        "status": dict(status),
        "date_sources": dict(sources),
        "date_details": dict(details.most_common()),
        "collisions_renamed": sum(1 for it in items if it.dest and it.suffix > 1),
        "years": dict(sorted(years.items())),
    }


def summary_text(plan: Plan) -> str:
    s = summarize(plan)
    L = [
        f"Media files found     : {s['total_media']}",
        f"Non-media ignored     : {s['ignored_non_media']}",
        "Status                : " + ", ".join(f"{k}={v}" for k, v in sorted(s["status"].items())),
        "Date sources          : " + ", ".join(f"{k}={v}" for k, v in sorted(s["date_sources"].items())),
        f"Collisions (suffixed) : {s['collisions_renamed']}",
        "Years                 : " + ", ".join(f"{y}:{c}" for y, c in s["years"].items()),
        "",
        "Date detail breakdown:",
    ]
    L += [f"  {v:5d}  {k}" for k, v in s["date_details"].items()]
    return "\n".join(L)


def write_reports(plan: Plan, manifest: list[dict] | None, executed: bool) -> Path:
    out = Path(plan.options.output)
    logdir = out / LOG_DIR_NAME
    logdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    kind = "run" if executed else "preview"
    csv_path = logdir / f"{kind}-{stamp}.csv"
    rows = [it.row() for it in plan.items] + [
        {"status": SKIPPED, "source": str(p), "note": "not a supported media type"}
        for p in plan.ignored
    ]
    fields = list(PlanItem(src=Path()).row().keys())
    # utf-8-sig so Excel shows Hebrew paths correctly
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    opts = asdict(plan.options)
    opts = {k: str(v) if isinstance(v, Path) else v for k, v in opts.items()}
    (logdir / f"{kind}-{stamp}.txt").write_text(
        f"Media Organizer {kind} {stamp}\n{json.dumps(opts, ensure_ascii=False, indent=2)}\n\n"
        + summary_text(plan) + "\n", encoding="utf-8")
    if executed and manifest is not None:
        (logdir / f"undo-{stamp}.json").write_text(
            json.dumps({"created": stamp, "options": opts, "operations": manifest},
                       ensure_ascii=False, indent=2), encoding="utf-8")
    return csv_path


def latest_undo(output: Path) -> Path | None:
    d = Path(output) / LOG_DIR_NAME
    files = sorted(d.glob("undo-*.json")) if d.is_dir() else []
    return files[-1] if files else None


def undo(manifest_path: Path, progress: ProgressCB | None = None) -> dict:
    """Reverse a run: moves go back to their source; copies are deleted if unchanged."""
    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    ops = data["operations"]
    res = Counter()
    for i, op in enumerate(reversed(ops), 1):
        src, dest = Path(op["src"]), Path(op["dest"])
        if progress:
            progress(i, len(ops), f"Undo: {dest.name}")
        try:
            if not dest.exists():
                res["missing"] += 1
                continue
            if op.get("sha256") and sha256(dest) != op["sha256"]:
                res["changed-kept"] += 1
                continue
            if op["mode"] == "move":
                if src.exists():
                    res["source-exists-kept"] += 1
                    continue
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dest), str(src))
            else:
                dest.unlink()
            res["reverted"] += 1
            # tidy empty month/year folders
            for d in (dest.parent, dest.parent.parent):
                try:
                    d.rmdir()
                except OSError:
                    pass
        except Exception:
            res["error"] += 1
    Path(manifest_path).rename(Path(manifest_path).with_suffix(".undone.json"))
    return dict(res)
