# Troupe Desktop (Windows)

A Windows app for your Troupe server. It opens Troupe in its own window, connects to the server over
SSH by itself (so the server's web UI never has to be public), signs in automatically, and stays
in the tray to notify you when an agent needs approval.

## Install

Double-click **Install Troupe.cmd** in the setup folder. It installs for your user only (no admin
rights), adds Start menu and Desktop shortcuts, and lists Troupe in Settings > Apps for uninstalling.

On first launch, enter the server address, SSH user and private key file. They're saved in
`%USERPROFILE%\.troupe-desktop\config.json`:

```json
{ "host": "203.0.113.10", "user": "ubuntu", "key_path": "C:\\Users\\you\\.ssh\\troupe.pem", "remote_port": 8080 }
```

Requirements: Windows 10/11 with the built-in OpenSSH client and Edge WebView2 (both standard on Windows 11).
The SSH user must be able to run `sudo cat /var/lib/troupe/admin_token` (the default `ubuntu` user on
EC2 can), or set `"token"` in the config.

## Use

- Closing the window keeps Troupe in the tray. Right-click the tray icon for Open, Open in browser,
  Reconnect, Connection settings, and Quit.
- Launching Troupe again brings the open window back.
- If the network drops, the tunnel reconnects by itself.
- Log file: `%USERPROFILE%\.troupe-desktop\desktop.log`

## Build from source

```bash
pip install pywebview pystray pillow pyinstaller
python make_icon.py
pyinstaller --noconfirm --windowed --onedir --name Troupe --icon troupe.ico --add-data "troupe.ico;." troupe_desktop.py
```

Copy `dist/Troupe`, `install.ps1`, `uninstall.ps1` and `Install Troupe.cmd` into one setup folder.
