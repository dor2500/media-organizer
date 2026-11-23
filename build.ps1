# Build standalone Windows executables with PyInstaller.
#   dist\MediaOrganizer.exe      - GUI (no console window)
#   dist\MediaOrganizer-cli.exe  - command line
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    python -m venv .venv
    & $py -m pip install -r requirements-dev.txt
}
& $py tools\make_icon.py
$common = @("--noconfirm", "--clean", "--onefile", "--icon", "assets\icon.ico",
            "--add-data", "assets;assets",
            "--collect-data", "customtkinter", "--collect-all", "pillow_heif")
& $py -m PyInstaller @common --windowed --name MediaOrganizer run_gui.py
& $py -m PyInstaller @common --console  --name MediaOrganizer-cli run_cli.py
Get-ChildItem dist\*.exe | Format-Table Name, @{n="MB";e={[math]::Round($_.Length/1MB,1)}}
