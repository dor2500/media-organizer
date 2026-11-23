"""Date extraction with a strict, documented fallback priority.

Priority (first valid hit wins):
  1. Embedded metadata
       images: EXIF DateTimeOriginal -> DateTimeDigitized (CreateDate) -> DateTime
       videos: ffprobe creation_time (if ffprobe is on PATH)
               -> MP4/MOV/3GP 'mvhd' atom (MediaCreateDate), parsed natively
  2. Date encoded in the filename (20020201_..., IMG-20020201-WA0001, 2002-02-01,
     01.02.2002, 02-2002, 02-19 ...). Generic camera names such as IMG_2812 are ignored.
  3. Filesystem timestamps: the earliest of st_ctime (creation on Windows) and st_mtime.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp",
    ".heic", ".heif", ".avif", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf",
    ".rw2", ".raf", ".srw", ".pef",
}
VIDEO_EXTS = {
    ".mp4", ".m4v", ".mov", ".3gp", ".3g2", ".avi", ".mkv", ".wmv", ".mts",
    ".m2ts", ".mpg", ".mpeg", ".webm", ".flv",
}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS
QT_EXTS = {".mp4", ".m4v", ".mov", ".3gp", ".3g2"}

MIN_YEAR = 1900

# Source categories (used for reporting)
SRC_METADATA = "metadata"
SRC_FILENAME = "filename"
SRC_FILESYSTEM = "filesystem"


@dataclass
class DateResult:
    dt: datetime
    source: str      # category: metadata | filename | filesystem
    detail: str      # e.g. "EXIF DateTimeOriginal", "filename MM-YYYY", "st_mtime"

    def label(self) -> str:
        if self.detail.startswith(self.source):
            return self.detail
        return f"{self.source}: {self.detail}"


def _max_year() -> int:
    return datetime.now().year + 1


def _valid(dt: datetime | None) -> bool:
    return dt is not None and MIN_YEAR <= dt.year <= _max_year()


# --------------------------------------------------------------------------- EXIF
try:  # Pillow is optional at import time so the module stays testable
    from PIL import Image

    try:
        from pillow_heif import register_heif_opener  # type: ignore

        register_heif_opener()
    except Exception:  # pragma: no cover - optional
        pass
except Exception:  # pragma: no cover
    Image = None  # type: ignore

_EXIF_TAGS = (
    (0x9003, "EXIF DateTimeOriginal", True),   # in Exif IFD
    (0x9004, "EXIF CreateDate", True),          # DateTimeDigitized, Exif IFD
    (0x0132, "EXIF DateTime", False),           # IFD0 (modify date)
)


def _parse_exif_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    s = str(value).strip().strip("\x00")
    if not s or s.startswith("0000"):
        return None
    m = re.match(r"(\d{4})[:\-/.](\d{2})[:\-/.](\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?", s)
    if not m:
        return None
    y, mo, d, hh, mm, ss = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0))
    except ValueError:
        return None


def exif_date(path: Path) -> DateResult | None:
    if Image is None:
        return None
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None
            try:
                sub = exif.get_ifd(0x8769)
            except Exception:
                sub = {}
            for tag, name, in_sub in _EXIF_TAGS:
                raw = (sub.get(tag) if in_sub else None) or exif.get(tag)
                dt = _parse_exif_dt(raw)
                if _valid(dt):
                    return DateResult(dt, SRC_METADATA, name)
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------- Video
_QT_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)
_CONTAINERS = {b"moov", b"trak", b"mdia"}


def _utc_to_local(dt_utc: datetime) -> datetime:
    try:
        return dt_utc.astimezone().replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return dt_utc.replace(tzinfo=None)


def _walk_atoms(f, start: int, end: int, want: tuple[bytes, ...]):
    """Yield (type, payload_offset, payload_size) for atoms on the path in `want`."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            return
        size, typ = struct.unpack(">I4s", hdr)
        hlen = 8
        if size == 1:
            ext = f.read(8)
            if len(ext) < 8:
                return
            size = struct.unpack(">Q", ext)[0]
            hlen = 16
        elif size == 0:
            size = end - pos
        if size < hlen:
            return
        if typ == want[0]:
            if len(want) == 1:
                yield typ, pos + hlen, size - hlen
            else:
                yield from _walk_atoms(f, pos + hlen, pos + size, want[1:])
        pos += size


def _qt_header_time(f, off: int) -> datetime | None:
    f.seek(off)
    vf = f.read(4)
    if len(vf) < 4:
        return None
    version = vf[0]
    raw = f.read(8 if version == 1 else 4)
    if len(raw) < (8 if version == 1 else 4):
        return None
    secs = struct.unpack(">Q" if version == 1 else ">I", raw)[0]
    if secs == 0:
        return None
    try:
        return _utc_to_local(_QT_EPOCH + timedelta(seconds=secs))
    except OverflowError:
        return None


def mp4_date(path: Path) -> DateResult | None:
    """Read creation time from the QuickTime/MP4 movie header (mvhd) or media header (mdhd)."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            for _, off, _ in _walk_atoms(f, 0, size, (b"moov", b"mvhd")):
                dt = _qt_header_time(f, off)
                if _valid(dt):
                    return DateResult(dt, SRC_METADATA, "QuickTime CreateDate (mvhd)")
            for _, off, _ in _walk_atoms(f, 0, size, (b"moov", b"trak", b"mdia", b"mdhd")):
                dt = _qt_header_time(f, off)
                if _valid(dt):
                    return DateResult(dt, SRC_METADATA, "QuickTime MediaCreateDate (mdhd)")
    except Exception:
        return None
    return None


def ffprobe_date(path: Path) -> DateResult | None:
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        out = subprocess.run(
            [exe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=30, creationflags=flags,
        )
        data = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
    except Exception:
        return None
    candidates = [data.get("format", {}).get("tags", {})]
    candidates += [s.get("tags", {}) for s in data.get("streams", [])]
    for tags in candidates:
        tags = {k.lower(): v for k, v in (tags or {}).items()}
        for key in ("com.apple.quicktime.creationdate", "creation_time", "date"):
            v = tags.get(key)
            if not v:
                continue
            try:
                s = v.strip().replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                dt = _utc_to_local(dt) if dt.tzinfo else dt
            except ValueError:
                dt = _parse_exif_dt(v)
            if _valid(dt) and not (dt.year == 1970 and dt.month == 1 and dt.day == 1):
                return DateResult(dt, SRC_METADATA, f"video {key}")
    return None


def metadata_date(path: Path) -> DateResult | None:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        r = exif_date(path)
        if r:
            return r
    if ext in VIDEO_EXTS or ext in QT_EXTS:
        r = ffprobe_date(path)
        if r:
            return r
        if ext in QT_EXTS:
            return mp4_date(path)
    return None


# --------------------------------------------------------------------------- Filename
_Y = r"(19\d{2}|20\d{2})"
_M = r"(0[1-9]|1[0-2])"
_D = r"(0[1-9]|[12]\d|3[01])"
_SEP = r"[-_. ]?"
_RE_YMD = re.compile(
    rf"(?<!\d){_Y}{_SEP}{_M}{_SEP}{_D}(?:[-_. T]?(\d{{2}})[-_.:]?(\d{{2}})[-_.:]?(\d{{2}}))?(?!\d)"
)
_RE_DMY = re.compile(r"(?<!\d)(0?[1-9]|[12]\d|3[01])[-_.](0?[1-9]|1[0-2])[-_.]" + _Y + r"(?!\d)")
_RE_YM = re.compile(rf"(?<!\d){_Y}[-_.]{_M}(?![\d])")
# DD.MM.YY - same separator required twice, so "01.02-12" (this tool's own
# collision-suffixed output) is never mis-read as 01.02.2012.
_RE_DMYY = re.compile(r"^(0?[1-9]|[12]\d|3[01])([-_.])(0?[1-9]|1[0-2])\2(\d{2})(?:[-_ (].*)?$")
# Whole-stem month-precision names, e.g. "02-2023", "02-2023-2", "12.2021", "03-19", "5.19".
_RE_MY = re.compile(r"^(0?[1-9]|1[0-2])[-_.]" + _Y + r"(?:[-_ (.].*)?$")
_RE_MYY = re.compile(r"^(0?[1-9]|1[0-2])([-_.])(\d{2})(?:[-_ (].*)?$")
_RE_YEAR = re.compile(r"(?<![0-9A-Za-z])(19\d{2}|20\d{2})(?![0-9A-Za-z])")

_MONTH_NAMES = {
    # Hebrew
    "ינואר": 1, "פברואר": 2, "מרץ": 3, "מרס": 3, "אפריל": 4, "מאי": 5, "יוני": 6,
    "יולי": 7, "אוגוסט": 8, "ספטמבר": 9, "אוקטובר": 10, "נובמבר": 11, "דצמבר": 12,
    # English
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _month_from_word(word: str) -> int | None:
    """Exact or fuzzy (typo-tolerant) month-name match, e.g. 'פבואר' -> 2."""
    import difflib

    w = word.casefold()
    cands = [w]
    if len(w) > 3 and w[0] in "בלה":  # Hebrew prefixes: ב/ל/ה + month
        cands.append(w[1:])
    for c in cands:
        if c in _MONTH_NAMES:
            return _MONTH_NAMES[c]
    long_names = [n for n in _MONTH_NAMES if len(n) >= 4]
    for c in cands:
        if len(c) >= 4:
            hit = difflib.get_close_matches(c, long_names, n=1, cutoff=0.8)
            if hit:
                return _MONTH_NAMES[hit[0]]
    return None


def _mk(y, mo, d, hh=0, mi=0, ss=0) -> datetime | None:
    try:
        dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0))
    except ValueError:
        try:
            dt = datetime(int(y), int(mo), int(d))
        except ValueError:
            return None
    return dt if _valid(dt) else None


def filename_date(path: Path, fs_hint: datetime | None = None) -> DateResult | None:
    stem = path.stem
    m = _RE_YMD.search(stem)
    if m:
        dt = _mk(*m.groups())
        if dt:
            return DateResult(dt, SRC_FILENAME, "filename YYYYMMDD")
    m = _RE_DMY.search(stem)
    if m:
        d, mo, y = m.groups()
        dt = _mk(y, mo, d)
        if dt:
            return DateResult(dt, SRC_FILENAME, "filename DD-MM-YYYY")

    # Month-precision names: borrow the day from the filesystem date when it falls in
    # the same month, otherwise use day 01.
    def month_only(y: int, mo: int, label: str) -> DateResult | None:
        if fs_hint and fs_hint.year == y and fs_hint.month == mo:
            dt = _mk(y, mo, fs_hint.day, fs_hint.hour, fs_hint.minute, fs_hint.second)
            return DateResult(dt, SRC_FILENAME, f"{label} (day from filesystem)") if dt else None
        dt = _mk(y, mo, 1)
        return DateResult(dt, SRC_FILENAME, f"{label} (day unknown -> 01)") if dt else None

    def yy_to_year(yy: int) -> int:
        return 2000 + yy if yy <= datetime.now().year % 100 else 1900 + yy

    m = _RE_DMYY.match(stem)
    if m:
        d, _, mo, yy = m.groups()
        dt = _mk(yy_to_year(int(yy)), mo, d)
        if dt:
            return DateResult(dt, SRC_FILENAME, "filename DD-MM-YY")

    m = _RE_YM.search(stem)
    if m:
        return month_only(int(m.group(1)), int(m.group(2)), "filename YYYY-MM")
    m = _RE_MY.match(stem)
    if m:
        return month_only(int(m.group(2)), int(m.group(1)), "filename MM-YYYY")

    # Month name + 4-digit year ("יולי 2015", "July 2015", typo-tolerant)
    ym = _RE_YEAR.search(stem)
    if ym:
        for word in re.findall(r"[^\W\d_]+", stem):
            mo = _month_from_word(word)
            if mo:
                return month_only(int(ym.group(1)), mo, "filename month-name YYYY")

    m = _RE_MYY.match(stem)
    if m:
        mo, sep, yy = int(m.group(1)), m.group(2), int(m.group(3))
        # With '.', only accept when the 2nd number can't be a month, so this tool's
        # own DD.MM output names (e.g. "05.02") are never re-interpreted.
        if sep != "." or yy > 12:
            return month_only(yy_to_year(yy), mo, "filename MM-YY")

    # Bare year ("1965", "1965-2", "סביב 1965") -> year precision only.
    if ym:
        y = int(ym.group(1))
        if fs_hint and fs_hint.year == y:
            return DateResult(fs_hint, SRC_FILENAME, "filename YYYY (month/day from filesystem)")
        dt = _mk(y, 1, 1)
        if dt:
            return DateResult(dt, SRC_FILENAME, "filename YYYY (month/day unknown -> 01.01)")
    return None


# --------------------------------------------------------------------------- Filesystem
def filesystem_date(path: Path) -> DateResult:
    st = path.stat()
    picks = []
    for attr in ("st_birthtime", "st_ctime", "st_mtime"):
        v = getattr(st, attr, None)
        if v:
            try:
                picks.append((datetime.fromtimestamp(v), attr))
            except (OverflowError, OSError, ValueError):
                pass
    picks = [p for p in picks if _valid(p[0])] or picks
    dt, attr = min(picks, key=lambda p: p[0])
    return DateResult(dt, SRC_FILESYSTEM, attr)


# --------------------------------------------------------------------------- Public
def resolve_date(path: Path, use_filename: bool = True) -> DateResult:
    path = Path(path)
    r = metadata_date(path)
    if r:
        return r
    fs = filesystem_date(path)
    if use_filename:
        r = filename_date(path, fs_hint=fs.dt)
        if r:
            return r
    return fs
