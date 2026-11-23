# 📷 Media Organizer

A cross-platform (**Windows, macOS, Linux**) desktop app and command-line tool for sorting
photos and videos into a clean **`YYYY / MM / DD.MM.ext`** library — with **multi-folder
scanning**, **offline AI person / face search**, and **one-click GitHub Release updates**.

Full Unicode / Hebrew path support, deterministic naming, collision-safe, and one-click Undo.

![Media Organizer – face search & organization preview](docs/screenshot-preview.png)

## ⚡ Quick Install

### Windows (PowerShell one-liner)
```powershell
irm https://raw.githubusercontent.com/dor2500/media-organizer/main/install.ps1 | iex
```
Installs `MediaOrganizer.exe` & `MediaOrganizer-cli.exe` to `%LOCALAPPDATA%\MediaOrganizer`, adds it to your `PATH`, and creates Desktop & Start Menu shortcuts.

### macOS & Linux
```bash
curl -fsSL https://raw.githubusercontent.com/dor2500/media-organizer/main/install.sh | bash
```

Or download binaries directly from [GitHub Releases](../../releases/latest):
- **Windows:** `MediaOrganizer.exe` (GUI) & `MediaOrganizer-cli.exe` (CLI)
- **macOS:** `MediaOrganizer-macos.zip` (`MediaOrganizer.app` + `MediaOrganizer-cli`)
- **Linux:** `MediaOrganizer-linux.tar.gz` (`MediaOrganizer` + `MediaOrganizer-cli`)

## ✨ Features

- **Paste-and-go dark-mode GUI** + scriptable **CLI**, built for Windows, macOS, and Linux.
- **In-app Auto-Updater (`🔄 בדוק עדכונים`):** checks GitHub Releases for newer versions, shows release notes, and downloads/installs updates directly.
- **Multi-folder scanning & custom destination:** paste one or more source folder paths (one per line) and choose/paste the exact destination folder where all organized files will be saved.
- **AI Person / Face Search (optional, 100% offline):** upload a reference photo of someone (`--person` in CLI or **העלה תמונת אדם** in GUI). Uses bundled OpenCV **YuNet + SFace** ONNX models to find and organize every photo & video of that person across all selected folders.
- **Smart date detection** with strict fallback priority:
  1. **Metadata:** EXIF `DateTimeOriginal` → `CreateDate` → `DateTime`; videos via `ffprobe` or native MP4/MOV `mvhd`/`mdhd` atoms.
  2. **Filename:** `20020201_101112`, `IMG-20020201-WA0003`, `2002-02-01`, `01.02.2002`, `16.9.65`, `02-2023`, `12.2021`, `03-19`, `5.19`, Hebrew/English month names (`יולי 2015`, `July 2015`, typo-tolerant), and bare years (`1965`, `סביב 1965`). Generic camera names (`IMG_2812.JPG`, `DSCF7114.JPG`, `scan0012.jpg`) are never treated as dates.
  3. **Filesystem:** earliest of `st_ctime` and `st_mtime`.
- **Same-date collisions:** `01.02.jpg`, `01.02-2.jpg`, `01.02-3.jpg`, … in deterministic order.
- **Duplicates & safety:** SHA-256 duplicate detection, copy-by-default with checksum verification, never overwrites, CSV/text reports in `_organizer_logs/`, and one-click **Undo**.

## 🗂️ Output structure

![Organized output tree](docs/output-tree.png)

## 🚀 Usage

### GUI
1. **Paste** one or more source folder paths (or click **➕ הוסף תיקייה**).
2. **Choose or paste the destination folder** where everything will be saved.
3. *(Optional)* **Upload a person's photo** (**🖼️ העלה תמונת אדם…**) to search and organize only photos/videos containing their face.
4. Click **✨ סדר הכל**.
5. Use **🔄 בדוק עדכונים** anytime to check for and install new releases.

### CLI
```powershell
# Dry-run preview
MediaOrganizer-cli "D:\Phone dump" "D:\Photos"

# Search across multiple folders for a specific person and save into one organized folder
MediaOrganizer-cli "D:\Photos" "E:\Backups" --out "D:\Grandpa_Photos" --person "C:\ref.jpg" --run

# Move only 2000–2005
MediaOrganizer-cli "D:\Scans" "D:\Photos" --run --move --year-from 2000 --year-to 2005

# Revert the most recent run
MediaOrganizer-cli --undo "D:\Photos"
```

## 🛠️ Build from source

```powershell
git clone https://github.com/dor2500/media-organizer
cd media-organizer
python -m venv .venv
.\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest
.\build.ps1   # Windows  |  ./build.sh on macOS / Linux
```

## License

MIT
