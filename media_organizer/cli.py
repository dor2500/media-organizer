"""Command-line interface."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import APP_NAME, __version__
from .core import Options, build_plan, execute, latest_undo, summary_text, undo, write_reports


def _fix_console():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _bar(i: int, n: int, msg: str):
    if not sys.stdout.isatty():
        return
    w = 30
    done = int(w * i / max(n, 1))
    sys.stdout.write(f"\r[{'#' * done}{'.' * (w - done)}] {i}/{n} ")
    if i >= n:
        sys.stdout.write("\n")
    sys.stdout.flush()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="MediaOrganizer-cli",
        description=f"{APP_NAME} {__version__}: sort photos/videos into <out>/YYYY/MM/DD.MM.ext",
    )
    p.add_argument("source", nargs="?", help="folder containing photos/videos")
    p.add_argument("output", nargs="?", help="destination root folder")
    p.add_argument("--run", action="store_true",
                   help="actually copy/move files (default is a dry-run preview)")
    p.add_argument("--move", action="store_true", help="move instead of copy")
    p.add_argument("--no-recursive", action="store_true", help="do not scan sub-folders")
    p.add_argument("--year-from", type=int, help="only include files from this year on")
    p.add_argument("--year-to", type=int, help="only include files up to this year")
    p.add_argument("--keep-duplicates", action="store_true",
                   help="keep byte-identical files (with suffixes) instead of skipping them")
    p.add_argument("--no-filename-dates", action="store_true",
                   help="never derive dates from filenames")
    p.add_argument("--no-verify", action="store_true", help="skip SHA-256 verification of copies")
    p.add_argument("--list", action="store_true", help="print every file mapping")
    p.add_argument("--undo", metavar="OUTPUT_DIR", help="revert the most recent run in OUTPUT_DIR")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv=None) -> int:
    _fix_console()
    args = build_parser().parse_args(argv)

    if args.undo:
        m = latest_undo(Path(args.undo))
        if not m:
            print("No undo manifest found.")
            return 1
        print(f"Reverting {m.name} ...")
        print(undo(m, _bar))
        return 0

    if not args.source or not args.output:
        build_parser().print_help()
        return 2
    src, out = Path(args.source), Path(args.output)
    if not src.is_dir():
        print(f"Source folder not found: {src}")
        return 2

    opts = Options(
        source=src, output=out, mode="move" if args.move else "copy",
        recursive=not args.no_recursive, year_from=args.year_from, year_to=args.year_to,
        duplicates="keep" if args.keep_duplicates else "skip",
        use_filename_dates=not args.no_filename_dates, verify=not args.no_verify,
    )
    print(f"{APP_NAME} {__version__}\nSource : {src}\nOutput : {out}\nMode   : "
          f"{opts.mode}{'' if args.run else ' (DRY RUN - nothing will be changed)'}\n")
    plan = build_plan(opts, _bar)
    manifest = None
    if args.run:
        manifest = execute(plan, _bar)

    if args.list or not args.run:
        def order(x):
            return (0 if x.dest else 1, str(x.dest.parent) if x.dest else "",
                    x.date.dt if x.date else 0, x.suffix, x.src.name)
        for it in sorted(plan.items, key=order):
            rel = it.dest.relative_to(out) if it.dest else "-"
            d = it.date.label() if it.date else ""
            print(f"  [{it.status:12}] {it.src.name}  ->  {rel}   ({d}) {it.note}")
        for p in plan.ignored:
            print(f"  [skipped     ] {p.name}  (not a media file)")
        print()

    print(summary_text(plan))
    report = write_reports(plan, manifest, executed=args.run)
    print(f"\nReport: {report}")
    errors = sum(1 for it in plan.items if it.status == "error")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
