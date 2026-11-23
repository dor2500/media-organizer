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
NO_FACE_MATCH = "no-face"
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
    output: Path
    source: Path | None = None
    sources: list[Path] = field(default_factory=list)
    mode: str = "copy"                 # copy | move
    recursive: bool = True
    year_from: int | None = None
    year_to: int | None = None
    duplicates: str = "skip"           # skip | keep
    use_filename_dates: bool = True
    verify: bool = True                # re-hash after copy
    person_image: Path | None = None   # optional reference photo to filter by face
    face_threshold: float = 0.363

    def __post_init__(self):
        self.output = Path(self.output)
        if self.sources:
            self.sources = [Path(s) for s in self.sources]
        elif self.source is not None:
            self.sources = [Path(self.source)]
        if self.sources and self.source is None:
            self.source = self.sources[0]
        if self.person_image is not None:
            self.person_image = Path(self.person_image)


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


def scan(opts: Options, progress: ProgressCB | None = None,
         cancel: threading.Event | None = None) -> tuple[list[Path], list[Path]]:
    out = Path(opts.output)
    media, ignored = [], []
    seen: set[Path] = set()
    for src in opts.sources:
        src = Path(src)
        skip_out = out.exists() and _is_within(out, src) and out.resolve() != src.resolve()
        walker = os.walk(src) if opts.recursive else [(str(src), [], os.listdir(src))]
        for root, dirs, files in walker:
            if cancel and cancel.is_set():
                return media, ignored
            rp = Path(root)
            if progress:
                progress(0, 0, f"סורק: {rp.name or rp}  (נמצאו {len(media)} קבצי מדיה)")
            # never descend into our own log folder, NAS recycle bins, or the output folder
            dirs[:] = sorted(
                d for d in dirs
                if d not in (LOG_DIR_NAME, ".recycle", "$RECYCLE.BIN", "@eaDir", "System Volume Information")
                and not (skip_out and (rp / d).resolve() == out.resolve())
            )
            for name in sorted(files):
                p = rp / name
                if not p.is_file():
                    continue
                rp_file = p.resolve()
                if rp_file in seen:
                    continue
                seen.add(rp_file)
                (media if p.suffix.lower() in MEDIA_EXTS else ignored).append(p)
    return media, ignored


# ----------------------------------------------------------------------- planning
def build_plan(opts: Options, progress: ProgressCB | None = None,
               cancel: threading.Event | None = None) -> Plan:
    matcher = None
    if opts.person_image:
        from .faces import FaceMatcher

        if progress:
            progress(0, 0, f"טוען מודל זיהוי פנים ותמונת ייחוס: {opts.person_image.name}")
        matcher = FaceMatcher(opts.person_image, threshold=opts.face_threshold)

    media, ignored = scan(opts, progress=progress, cancel=cancel)
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

    _assign(plan, progress, matcher=matcher, cancel=cancel)
    return plan


def _assign(plan: Plan, progress: ProgressCB | None, matcher=None,
            cancel: threading.Event | None = None) -> None:
    opts = plan.options
    out = Path(opts.output)
    candidates = [it for it in plan.items if it.status == PLANNED]

    for it in candidates:
        y = it.date.dt.year
        if (opts.year_from and y < opts.year_from) or (opts.year_to and y > opts.year_to):
            it.status, it.note = OUT_OF_RANGE, f"year {y} outside range"

    if matcher is not None:
        in_range = [it for it in candidates if it.status == PLANNED]
        for i, it in enumerate(in_range, 1):
            if cancel and cancel.is_set():
                break
            if progress:
                progress(i, len(in_range), f"Searching face: {it.src.name}")
            try:
                matched, score = matcher.match_file(it.src)
            except Exception as e:
                it.status, it.note = ERROR, f"face scan error: {e}"
                continue
            if matched:
                it.note = f"face match ({score:.2f})"
            else:
                it.status = NO_FACE_MATCH
                it.note = f"no face match (best {score:.2f})" if score is not None else "no face detected"

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

    namer = _Namer(out, opts.duplicates == "skip", h)
    n = len(active)
    for i, it in enumerate(active, 1):
        if progress:
            progress(i, n, f"Planning: {it.src.name}")
        if it.status != PLANNED:
            continue
        namer.assign(it)


class _Namer:
    """Assigns YYYY/MM/DD.MM[-k].ext names, reserving stems already in the output and
    flagging inputs that are byte-identical to a file already there."""

    def __init__(self, out: Path, skip_dups: bool, hasher: Callable[[Path], str]):
        self.out, self.skip_dups, self.h = out, skip_dups, hasher
        self.reserved: dict[Path, set[str]] = {}
        self.existing_by_size: dict[Path, dict[int, list[Path]]] = {}

    def _folder_state(self, folder: Path):
        if folder not in self.reserved:
            stems, sizes = set(), defaultdict(list)
            if folder.is_dir():
                for e in folder.iterdir():
                    if e.is_file():
                        stems.add(e.stem.casefold())
                        sizes[e.stat().st_size].append(e)
            self.reserved[folder] = stems
            self.existing_by_size[folder] = sizes
        return self.reserved[folder], self.existing_by_size[folder]

    def assign(self, it: PlanItem) -> None:
        dt = it.date.dt
        folder = self.out / f"{dt.year:04d}" / f"{dt.month:02d}"
        stems, sizes = self._folder_state(folder)

        # already present in output (e.g. re-run)? -> duplicate, never re-copied
        if self.skip_dups and it.size in sizes:
            try:
                digest = self.h(it.src)
                match = next((e for e in sizes[it.size] if sha256(e) == digest), None)
            except OSError:
                match = None
            if match:
                it.status = DUPLICATE
                it.duplicate_of = str(match)
                it.note = "identical file already exists in output"
                return

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

    def record(self, dest: Path, size: int) -> None:
        """A file was just written: later inputs identical to it count as duplicates."""
        self._folder_state(dest.parent)[1][size].append(dest)


# ----------------------------------------------------------------------- live (face) mode
def organize_live(opts: Options, progress: ProgressCB | None = None,
                  cancel: threading.Event | None = None,
                  on_item: Callable[[PlanItem], None] | None = None) -> tuple[Plan, list[dict]]:
    """Face search that copies/moves each match *as soon as it is found*.

    Unlike build_plan()+execute(), nothing waits for the whole library to be
    searched: a cancelled or interrupted run keeps everything found so far, and
    the undo manifest covers exactly those files.  Names are assigned in scan
    order (a later, earlier-dated photo of the same day gets the next -k suffix).
    """
    from .faces import FaceMatcher

    if progress:
        progress(0, 0, f"טוען מודל זיהוי פנים ותמונת ייחוס: {opts.person_image.name}")
    matcher = FaceMatcher(opts.person_image, threshold=opts.face_threshold)

    media, ignored = scan(opts, progress=progress, cancel=cancel)
    plan = Plan(opts, ignored=ignored)
    manifest: list[dict] = []
    namer = _Namer(Path(opts.output), opts.duplicates == "skip", sha256)
    copied_by_size: dict[int, list[tuple[str, Path]]] = defaultdict(list)   # inputs already written
    found = 0
    total = len(media)

    for i, p in enumerate(media, 1):
        if cancel and cancel.is_set():
            break
        it = PlanItem(src=p)
        plan.items.append(it)
        if progress:
            progress(i, total, f"התאמות: {found}   מחפש פנים: {p.name}")
        try:
            it.size = p.stat().st_size
            it.date = resolve_date(p, use_filename=opts.use_filename_dates)
        except Exception as e:
            it.status, it.note = ERROR, f"cannot read: {e}"
            continue
        y = it.date.dt.year
        if (opts.year_from and y < opts.year_from) or (opts.year_to and y > opts.year_to):
            it.status, it.note = OUT_OF_RANGE, f"year {y} outside range"
            continue
        try:
            matched, score = matcher.match_file(p)
        except Exception as e:
            it.status, it.note = ERROR, f"face scan error: {e}"
            continue
        if not matched:
            it.status = NO_FACE_MATCH
            it.note = f"no face match (best {score:.2f})" if score is not None else "no face detected"
            continue

        it.note = f"face match ({score:.2f})"
        digest = None
        if opts.duplicates == "skip" and it.size in copied_by_size:
            try:
                digest = sha256(p)
            except OSError as e:
                it.status, it.note = ERROR, f"cannot hash: {e}"
                continue
            prev = next((src for d, src in copied_by_size[it.size] if d == digest), None)
            if prev:
                it.status, it.duplicate_of = DUPLICATE, str(prev)
                it.note = "identical content to another input file"
                if on_item:
                    on_item(it)
                continue
        namer.assign(it)
        if it.status == PLANNED:
            op = _transfer(it, opts)
            if op:
                manifest.append(op)
                found += 1
                namer.record(it.dest, it.size)
                copied_by_size[it.size].append((op["sha256"] or digest or sha256(it.dest), p))
        if on_item:
            on_item(it)

    # files never reached because of cancel stay out of the plan/report
    return plan, manifest


# ----------------------------------------------------------------------- execution
def _transfer(it: PlanItem, opts: Options) -> dict | None:
    """Copy or move one planned item to it.dest. Returns its manifest entry or None on error."""
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
        return {"mode": opts.mode, "src": str(it.src), "dest": str(target), "sha256": src_hash}
    except Exception as e:
        it.status, it.note = ERROR, str(e)
        return None


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
        op = _transfer(it, opts)
        if op:
            manifest.append(op)
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
    def _ser(v):
        if isinstance(v, Path):
            return str(v)
        if isinstance(v, list):
            return [_ser(x) for x in v]
        return v

    opts = {k: _ser(v) for k, v in asdict(plan.options).items()}
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
