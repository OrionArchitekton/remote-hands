#!/usr/bin/env bash
# Build the wheel in WSL, copy it and the installer to Windows, and run the installer.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
cmd="$(command -v cmd.exe || echo /mnt/c/Windows/System32/cmd.exe)"
# %LOCALAPPDATA% of the Windows user, as a WSL path. </dev/null: see the note on powershell below.
win_local="$(wslpath -u "$("$cmd" /d /c echo %LOCALAPPDATA% </dev/null 2>/dev/null | tr -d '\r')")"
app="$win_local/remote-hands"
ps="$(command -v powershell.exe || echo /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe)"

cd "$repo"
rm -rf dist
uv build --wheel
wheel="$(ls dist/*.whl)"
mkdir -p "$app/dist" "$win_local/Temp"
cp "$wheel" "$app/dist/"
# The service runs on Windows and cannot see this WSL user's home, so it reads the allowed roots
# (where a session may start) from here. Kept if it exists; edit it to change the roots.
if [ ! -e "$app/config.json" ]; then
  printf '{"allowed_roots": ["%s/src/", "%s/.worktrees/"]}\n' "$HOME" "$HOME" >"$app/config.json"
fi
cp scripts/install-windows.ps1 "$win_local/Temp/remote-hands-install.ps1"
# </dev/null: a Windows child must not inherit the WSL interop pipe, or this shell hangs.
"$ps" -NoProfile -ExecutionPolicy Bypass \
  -File "$(wslpath -w "$win_local/Temp/remote-hands-install.ps1")" \
  -Wheel "$(wslpath -w "$app/dist/$(basename "$wheel")")" </dev/null
