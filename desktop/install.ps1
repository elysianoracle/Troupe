# Troupe Desktop installer (per-user, no admin rights needed).
# Copies the app to %LOCALAPPDATA%\Programs\Troupe, adds Start menu + Desktop shortcuts,
# registers it in Settings > Apps for uninstalling, and starts it.
$ErrorActionPreference = 'Stop'
$src = Join-Path $PSScriptRoot 'Troupe'
$dest = Join-Path $env:LOCALAPPDATA 'Programs\Troupe'
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Troupe.lnk'
$desktop = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Troupe.lnk'
$logFile = Join-Path $PSScriptRoot 'install-result.txt'
$version = '0.1.0'

function Log($m) { Add-Content -Path $logFile -Value ("{0} {1}" -f (Get-Date -Format s), $m) }
Set-Content -Path $logFile -Value "Troupe Desktop install"

try {
    Get-Process Troupe -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Milliseconds 500
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Copy-Item -Path (Join-Path $src '*') -Destination $dest -Recurse -Force
    Copy-Item -Path (Join-Path $PSScriptRoot 'uninstall.ps1') -Destination $dest -Force
    $exe = Join-Path $dest 'Troupe.exe'
    Log "copied app to $dest"

    $shell = New-Object -ComObject WScript.Shell
    foreach ($lnkPath in @($startMenu, $desktop)) {
        $lnk = $shell.CreateShortcut($lnkPath)
        $lnk.TargetPath = $exe
        $lnk.WorkingDirectory = $dest
        $lnk.IconLocation = "$exe,0"
        $lnk.Description = 'Your team of AI agents'
        $lnk.Save()
        Log "shortcut $lnkPath"
    }

    $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\TroupeDesktop'
    New-Item -Path $key -Force | Out-Null
    $uninstall = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$dest\uninstall.ps1`""
    $props = @{
        DisplayName = 'Troupe'; DisplayVersion = $version; Publisher = 'Troupe (open source)';
        DisplayIcon = "$exe,0"; InstallLocation = $dest; UninstallString = $uninstall;
        NoModify = 1; NoRepair = 1
    }
    foreach ($k in $props.Keys) { New-ItemProperty -Path $key -Name $k -Value $props[$k] -Force | Out-Null }
    $size = [int]((Get-ChildItem $dest -Recurse | Measure-Object Length -Sum).Sum / 1KB)
    New-ItemProperty -Path $key -Name EstimatedSize -Value $size -PropertyType DWord -Force | Out-Null
    Log "registered in Settings > Apps"

    Log ("check: exe={0} startmenu={1} desktop={2} registry={3}" -f (Test-Path $exe), (Test-Path $startMenu), (Test-Path $desktop), (Test-Path $key))
    Start-Process -FilePath $exe -WorkingDirectory $dest
    Log "started Troupe"
    Log "OK"
} catch {
    Log "FAILED: $_"
    exit 1
}
