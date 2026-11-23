"""Check GitHub Releases for updates and self-update the running binary."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__

REPO = "dor2500/media-organizer"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"


@dataclass
class ReleaseInfo:
    version: str
    tag: str
    notes: str
    html_url: str
    asset_name: str | None
    asset_url: str | None
    is_newer: bool


def parse_version(tag: str) -> tuple[int, ...]:
    clean = tag.strip().lstrip("vV").split("-")[0]
    parts = []
    for p in clean.split("."):
        digits = "".join(c for c in p if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _pick_asset(assets: list[dict]) -> tuple[str | None, str | None]:
    names = {a["name"]: a["browser_download_url"] for a in assets}
    if sys.platform == "win32":
        for cand in ("MediaOrganizer.exe", "MediaOrganizer-windows.exe"):
            if cand in names:
                return cand, names[cand]
    elif sys.platform == "darwin":
        for name, url in names.items():
            if "macos" in name.lower() or name.endswith(".dmg"):
                return name, url
    else:
        for name, url in names.items():
            if "linux" in name.lower():
                return name, url
    return None, None


def check_for_updates(timeout: int = 10) -> ReleaseInfo:
    req = urllib.request.Request(
        API_LATEST,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "MediaOrganizer"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    tag = data.get("tag_name", "")
    ver = tag.lstrip("vV")
    asset_name, asset_url = _pick_asset(data.get("assets", []))
    is_newer = parse_version(tag) > parse_version(__version__)
    return ReleaseInfo(
        version=ver or tag,
        tag=tag,
        notes=(data.get("body") or "").strip(),
        html_url=data.get("html_url") or RELEASES_URL,
        asset_name=asset_name,
        asset_url=asset_url,
        is_newer=is_newer,
    )


def download_and_apply(info: ReleaseInfo, progress_cb=None) -> str:
    """Download release asset. On frozen Windows EXE, stage a self-replacing restart script.
    Returns a user-facing status/path message (caller should exit if it says 'restarting').
    """
    if not info.asset_url or not info.asset_name:
        raise RuntimeError("No matching binary asset found in this release.")

    dl_dir = Path(tempfile.gettempdir()) / "media_organizer_update"
    dl_dir.mkdir(parents=True, exist_ok=True)
    dest = dl_dir / info.asset_name

    req = urllib.request.Request(info.asset_url, headers={"User-Agent": "MediaOrganizer"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress_cb and total:
                    progress_cb(done, total, f"Downloading {info.asset_name}...")

    frozen = getattr(sys, "frozen", False)
    if frozen and sys.platform == "win32" and dest.suffix.lower() == ".exe":
        current_exe = Path(sys.executable).resolve()
        bat = dl_dir / "apply_update.bat"
        pid = os.getpid()
        bat.write_text(
            "@echo off\r\n"
            f"set PID={pid}\r\n"
            ":waitloop\r\n"
            "tasklist /FI \"PID eq %PID%\" 2>NUL | find /I \"%PID%\" >NUL\r\n"
            "if not errorlevel 1 (\r\n"
            "  timeout /t 1 /nobreak >NUL\r\n"
            "  goto waitloop\r\n"
            ")\r\n"
            "timeout /t 2 /nobreak >NUL\r\n"
            f"copy /Y \"{dest}\" \"{current_exe}\" >NUL\r\n"
            "set _MEIPASS=\r\n"
            "set _MEIPASS2=\r\n"
            f"start \"\" \"{current_exe}\"\r\n"
            "del \"%~f0\"\r\n",
            encoding="utf-8",
        )
        subprocess.Popen(["cmd", "/c", str(bat)], creationflags=0x08000000)
        return "restarting"

    # macOS / Linux / running from source: save to ~/Downloads and return path
    downloads = Path.home() / "Downloads"
    if downloads.is_dir():
        final = downloads / info.asset_name
        dest.replace(final)
        dest = final
    return str(dest)
