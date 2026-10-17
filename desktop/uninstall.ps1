# Removes Troupe Desktop from this PC. Your agents, memory and data live on the server and are untouched.
# Your connection settings in %USERPROFILE%\.troupe-desktop are kept unless you delete that folder.
$ErrorActionPreference = 'SilentlyContinue'
Get-Process Troupe | Stop-Process -Force
Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" | Where-Object { $_.CommandLine -like '*ExitOnForwardFailure=yes -N -L 127.0.0.1:*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Milliseconds 500
Remove-Item (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Troupe.lnk') -Force
Remove-Item (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Troupe.lnk') -Force
Remove-Item 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\TroupeDesktop' -Recurse -Force
$dest = Join-Path $env:LOCALAPPDATA 'Programs\Troupe'
# This script lives inside $dest, so delete the folder from a separate process once we exit.
Start-Process -WindowStyle Hidden cmd.exe -ArgumentList "/c timeout /t 2 >nul & rmdir /s /q `"$dest`""
