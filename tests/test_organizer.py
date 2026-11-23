import os
import struct
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from media_organizer.core import (DONE, DUPLICATE, NO_FACE_MATCH, OUT_OF_RANGE, Options,
                                  build_plan, execute, latest_undo, undo, write_reports)
from media_organizer.dates import (SRC_FILENAME, SRC_FILESYSTEM, SRC_METADATA, filename_date,
                                   mp4_date, resolve_date)


def make_jpeg(path: Path, exif_dt: str | None = None, color=(200, 30, 30)):
    img = Image.new("RGB", (8, 8), color)
    kw = {}
    if exif_dt:
        ex = Image.Exif()
        ex.get_ifd(0x8769)[0x9003] = exif_dt
        kw["exif"] = ex
    img.save(path, "JPEG", **kw)
    return path


def make_mp4(path: Path, when: datetime):
    secs = int((when - datetime(1904, 1, 1, tzinfo=timezone.utc)).total_seconds())
    mvhd_payload = b"\x00\x00\x00\x00" + struct.pack(">II", secs, secs) + b"\x00" * 88
    mvhd = struct.pack(">I4s", 8 + len(mvhd_payload), b"mvhd") + mvhd_payload
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    ftyp = struct.pack(">I4s", 16, b"ftyp") + b"isom\x00\x00\x02\x00"
    path.write_bytes(ftyp + moov)
    return path


def set_mtime(p: Path, dt: datetime):
    ts = dt.timestamp()
    os.utime(p, (ts, ts))


@pytest.fixture
def heb(tmp_path):
    d = tmp_path / "סבא חוגג 90 בדיקה"
    d.mkdir()
    return d


# ---------------------------------------------------------------- date sources
def test_exif_priority_over_filename(heb):
    p = make_jpeg(heb / "20190101_120000.jpg", "2002:02:01 10:00:00")
    r = resolve_date(p)
    assert r.source == SRC_METADATA and r.dt == datetime(2002, 2, 1, 10, 0)


def test_mp4_mvhd(heb):
    when = datetime(2003, 5, 6, 12, 0, tzinfo=timezone.utc)
    r = mp4_date(make_mp4(heb / "סרטון.mp4", when))
    assert r and r.source == SRC_METADATA
    assert abs(r.dt - when.astimezone().replace(tzinfo=None)) < timedelta(seconds=1)


@pytest.mark.parametrize("name,expected", [
    ("IMG_20020201_101112.jpg", datetime(2002, 2, 1, 10, 11, 12)),
    ("IMG-20020201-WA0003.jpg", datetime(2002, 2, 1)),
    ("2004-07-15 party.jpg", datetime(2004, 7, 15)),
    ("16.9.1965.jpeg", datetime(1965, 9, 16)),
    ("16.9.65.jpeg", datetime(1965, 9, 16)),
    ("02-2023-2.jpeg", datetime(2023, 2, 1)),
    ("12.2021.jpeg", datetime(2021, 12, 1)),
    ("03-19.jpeg", datetime(2019, 3, 1)),
    ("5.19.jpeg", datetime(2019, 5, 1)),
    ("08-91.jpeg", datetime(1991, 8, 1)),
    ("יולי 2015.jpeg", datetime(2015, 7, 1)),
    ("פבואר 2016.jpeg", datetime(2016, 2, 1)),      # typo-tolerant
    ("July 2015.jpg", datetime(2015, 7, 1)),
    ("סביב 1965.jpeg", datetime(1965, 1, 1)),
    ("1965-2.jpeg", datetime(1965, 1, 1)),
])
def test_filename_patterns(name, expected):
    r = filename_date(Path(name))
    assert r is not None and r.source == SRC_FILENAME
    assert r.dt.date() == expected.date()


@pytest.mark.parametrize("name", [
    "IMG_2812.JPG", "DSCF7114.JPG", "scan0012.jpg", "01.02.jpg", "05.02-12.jpg",
    "16-10.jpeg", "68.jpeg", "לא ידוע.jpeg",
])
def test_generic_names_not_parsed(name):
    assert filename_date(Path(name)) is None


def test_filesystem_fallback(heb):
    p = make_jpeg(heb / "IMG_2812.JPG")
    set_mtime(p, datetime(2001, 3, 4, 5, 6, 7))
    r = resolve_date(p)
    assert r.source == SRC_FILESYSTEM and r.dt.year == 2001 and r.dt.month == 3


def test_month_only_borrows_fs_day(heb):
    p = make_jpeg(heb / "03-2018.jpeg")
    set_mtime(p, datetime(2018, 3, 17, 9, 0))
    r = resolve_date(p)
    assert r.source == SRC_FILENAME and r.dt.day == 17


# ---------------------------------------------------------------- planning / naming
def test_collisions_duplicates_and_structure(heb, tmp_path):
    out = tmp_path / "פלט"
    for i, n in enumerate(["c.jpg", "a.jpg", "b.JPEG"]):
        make_jpeg(heb / n, "2002:02:01 10:00:0%d" % i, color=(i * 50, 10, 10))
    # exact byte duplicate of a.jpg:
    (heb / "a copy.jpg").write_bytes((heb / "a.jpg").read_bytes())
    (heb / "notes.txt").write_text("hi", encoding="utf-8")

    plan = build_plan(Options(source=heb, output=out))
    mapping = {it.src.name: it.dest.name for it in plan.items if it.dest}
    # ordered by EXIF time: c (10:00:00), a (10:00:01), b (10:00:02); extensions lower-cased
    assert mapping == {"c.jpg": "01.02.jpg", "a.jpg": "01.02-2.jpg", "b.JPEG": "01.02-3.jpeg"}
    dups = [it for it in plan.items if it.status == DUPLICATE]
    assert [d.src.name for d in dups] == ["a copy.jpg"]
    assert len(plan.ignored) == 1
    assert all(it.dest.parent == out / "2002" / "02" for it in plan.items if it.dest)

    # deterministic: planning again yields the same mapping
    plan2 = build_plan(Options(source=heb, output=out))
    m1 = {it.src.name: it.dest for it in plan.items}
    m2 = {it.src.name: it.dest for it in plan2.items}
    assert m1 == m2

    manifest = execute(plan)
    write_reports(plan, manifest, executed=True)
    assert sum(1 for it in plan.items if it.status == DONE) == 3
    assert all((heb / n).exists() for n in ["a.jpg", "b.JPEG", "c.jpg"])  # copy keeps sources

    # re-run: everything is already there -> all duplicates, nothing overwritten
    plan3 = build_plan(Options(source=heb, output=out))
    assert all(it.status == DUPLICATE for it in plan3.items)


def test_keep_duplicates(heb, tmp_path):
    make_jpeg(heb / "x.jpg", "2005:01:02 00:00:00")
    (heb / "y.jpg").write_bytes((heb / "x.jpg").read_bytes())
    plan = build_plan(Options(source=heb, output=tmp_path / "o", duplicates="keep"))
    assert sorted(it.dest.name for it in plan.items) == ["02.01-2.jpg", "02.01.jpg"]


def test_year_range(heb, tmp_path):
    make_jpeg(heb / "a.jpg", "1999:01:01 00:00:00")
    make_jpeg(heb / "b.jpg", "2003:01:01 00:00:00", color=(1, 2, 3))
    plan = build_plan(Options(source=heb, output=tmp_path / "o", year_from=2000, year_to=2005))
    st = {it.src.name: it.status for it in plan.items}
    assert st == {"a.jpg": OUT_OF_RANGE, "b.jpg": "planned"}


def test_move_and_undo(heb, tmp_path):
    out = tmp_path / "out"
    make_jpeg(heb / "תמונה.jpg", "2004:03:01 08:00:00")
    plan = build_plan(Options(source=heb, output=out, mode="move"))
    manifest = execute(plan)
    write_reports(plan, manifest, executed=True)
    assert not (heb / "תמונה.jpg").exists()
    assert (out / "2004" / "03" / "01.03.jpg").exists()
    res = undo(latest_undo(out))
    assert res.get("reverted") == 1
    assert (heb / "תמונה.jpg").exists()
    assert not (out / "2004").exists()


def test_output_inside_source_not_rescanned(heb):
    make_jpeg(heb / "a.jpg", "2004:03:01 08:00:00")
    out = heb / "organized"
    plan = build_plan(Options(source=heb, output=out))
    execute(plan)
    plan2 = build_plan(Options(source=heb, output=out))
    assert len(plan2.items) == 1  # organized/ copy is not picked up as input


def test_multiple_sources(tmp_path):
    d1, d2, out = tmp_path / "תיקייה 1", tmp_path / "תיקייה 2", tmp_path / "פלט"
    d1.mkdir()
    d2.mkdir()
    make_jpeg(d1 / "a.jpg", "2020:01:02 10:00:00", color=(10, 20, 30))
    make_jpeg(d2 / "b.jpg", "2020:01:02 11:00:00", color=(40, 50, 60))
    plan = build_plan(Options(sources=[d1, d2, d1], output=out))  # d1 repeated -> deduplicated
    assert sorted(it.dest.name for it in plan.items) == ["02.01-2.jpg", "02.01.jpg"]


def test_face_filter_no_face_in_ref_raises(heb, tmp_path):
    blank = make_jpeg(heb / "blank.jpg", "2020:01:02 10:00:00")
    with pytest.raises(ValueError, match="No face detected"):
        build_plan(Options(sources=[heb], output=tmp_path / "out", person_image=blank))


def test_face_filter_with_mock_matcher(heb, tmp_path, monkeypatch):
    import media_organizer.faces as faces_mod

    class FakeMatcher:
        def __init__(self, ref_image, threshold=0.363):
            pass

        def match_file(self, path: Path):
            return ("match" in path.name), (0.65 if "match" in path.name else 0.12)

    monkeypatch.setattr(faces_mod, "FaceMatcher", FakeMatcher)
    ref = make_jpeg(tmp_path / "ref.jpg")
    make_jpeg(heb / "match_1.jpg", "2021:05:01 10:00:00", color=(10, 10, 10))
    make_jpeg(heb / "other_1.jpg", "2021:05:01 11:00:00", color=(20, 20, 20))
    plan = build_plan(Options(sources=[heb], output=tmp_path / "out", person_image=ref))
    st = {it.src.name: it.status for it in plan.items}
    assert st == {"match_1.jpg": "planned", "other_1.jpg": NO_FACE_MATCH}


def test_updater_version_comparison_and_mock(monkeypatch):
    import io
    import json
    from media_organizer import updater

    assert updater.parse_version("v1.2.0") > updater.parse_version("1.1.0")
    assert updater.parse_version("v1.2.0") == updater.parse_version("1.2.0")

    payload = json.dumps({
        "tag_name": "v99.0.0",
        "body": "Big update",
        "html_url": "https://github.com/dor2500/media-organizer/releases/tag/v99.0.0",
        "assets": [
            {"name": "MediaOrganizer.exe", "browser_download_url": "https://example.com/win.exe"},
            {"name": "MediaOrganizer-macos.zip", "browser_download_url": "https://example.com/mac.zip"},
            {"name": "MediaOrganizer-linux.tar.gz", "browser_download_url": "https://example.com/lin.tar.gz"},
        ],
    }).encode("utf-8")

    class FakeResp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda req, timeout=10: FakeResp(payload))
    info = updater.check_for_updates()
    assert info.is_newer is True
    assert info.tag == "v99.0.0"
    assert info.asset_name is not None




# ---------------------------------------------------------------- RTL display helper
def test_rtl_moves_edge_punctuation_and_keeps_latin_runs():
    from media_organizer.bidi import LRM, rtl
    assert rtl("Hello") == "Hello"                       # no Hebrew -> untouched
    assert rtl("שלום עולם") == "שלום עולם"               # pure Hebrew -> untouched
    assert rtl("בחר יעד…") == "…בחר יעד"                 # trailing ellipsis goes to the left
    assert rtl("(אחת שתיים)") == "(אחת שתיים)"            # edge brackets reversed + mirrored
    assert rtl("שגיאה: D:/x") == f"{LRM}D:/x{LRM} :שגיאה"  # path ends up on the left
    assert rtl("אחת 2023 שתיים") == f"שתיים {LRM}2023{LRM} אחת"
    assert rtl("א.\nב.") == ".א\n.ב"                     # line by line


def test_wrap_breaks_in_logical_order():
    from media_organizer.bidi import wrap
    assert wrap("אחת שתיים שלוש", len, 9) == "אחת שתיים\nשלוש"
    assert wrap("  א ב ג", len, 5) == "  א ב\n  ג"
