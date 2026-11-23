# Media Organizer - One-line Windows Installer
# Run in PowerShell:
#   irm https://raw.githubusercontent.com/dor2500/media-organizer/main/install.ps1 | iex
$ErrorActionPreference = "Stop"

$repo = "dor2500/media-organizer"
$installDir = Join-Path $env:LOCALAPPDATA "MediaOrganizer"
New-Item -ItemType Directory -Force -Path $installDir | Out-Null

Write-Host "Fetching latest release from github.com/$repo ..." -ForegroundColor Cyan
$rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" -Headers @{ "User-Agent" = "MediaOrganizer-Installer" }
Write-Host "Installing Media Organizer $($rel.tag_name) -> $installDir" -ForegroundColor Green

foreach ($name in @("MediaOrganizer.exe", "MediaOrganizer-cli.exe")) {
    $asset = $rel.assets | Where-Object { $_.name -eq $name } | Select-Object -First 1
    if ($asset) {
        $dest = Join-Path $installDir $name
        Write-Host "  Downloading $name ..."
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $dest -UseBasicParsing
    }
}

# Add installDir to User PATH if not already present
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not ($userPath -split ";" | Where-Object { $_ -eq $installDir })) {
    [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(";") + ";" + $installDir).TrimStart(";"), "User")
    $env:Path = "$env:Path;$installDir"
    Write-Host "  Added $installDir to User PATH."
}

# Create Start Menu & Desktop shortcuts for the GUI
$guiExe = Join-Path $installDir "MediaOrganizer.exe"
if (Test-Path $guiExe) {
    $wsh = New-Object -ComObject WScript.Shell
    $startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Media Organizer.lnk"
    $desktop = Join-Path ([Environment]::GetFolderPath("Desktop")) "Media Organizer.lnk"
    foreach ($lnkPath in @($startMenu, $desktop)) {
        $s = $wsh.CreateShortcut($lnkPath)
        $s.TargetPath = $guiExe
        $s.WorkingDirectory = $installDir
        $s.Description = "Media Organizer - Sort photos & videos + AI face search"
        $s.Save()
    }
    Write-Host "  Created Desktop & Start Menu shortcuts."
}

Write-Host "`nDone! Run 'MediaOrganizer' or 'MediaOrganizer-cli --help', or double-click the Desktop shortcut." -ForegroundColor Green
