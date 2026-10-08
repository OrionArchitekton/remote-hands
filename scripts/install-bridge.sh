#!/usr/bin/env bash
# Test, scan, package, and install the window bridge into Cursor's WSL server.
# WSL windows run workspace extensions from there.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"

# Find the newest Cursor WSL server (by modification time) before anything is built. With
# nullglob a glob that matches nothing is an empty array, so "not installed" is handled
# here and not as a failed ls under pipefail.
shopt -s nullglob
servers=("$HOME"/.cursor-server/bin/*/bin/cursor-server)
shopt -u nullglob
if [ "${#servers[@]}" -eq 0 ]; then
  echo "Cursor's WSL server is not installed: open any WSL folder in Cursor once, then rerun." >&2
  exit 1
fi
server="${servers[0]}"
for candidate in "${servers[@]}"; do
  if [ "$candidate" -nt "$server" ]; then
    server="$candidate"
  fi
done

cd "$repo/bridge"
version="$(node -p "require('./package.json').version")"
# --ignore-scripts: no dependency code runs at install; packaging needs none of it
# (@vscode/vsce-sign's install script would fetch a binary that signing alone uses).
npm ci --ignore-scripts --no-audit --no-fund
npm test
npm run package
"$server" --install-extension "$repo/bridge/dist/remote-hands-bridge.vsix" --force
# Whole-line match: only this exact version counts, not any version of the extension.
listed="$("$server" --list-extensions --show-versions)"
if ! grep -Fx "remote-hands.remote-hands-bridge@$version" <<<"$listed"; then
  echo "Install did not register remote-hands-bridge@$version; check the output above." >&2
  exit 1
fi
echo "Installed. New WSL windows load it at start; an open window loads it after Developer: Reload Window."
