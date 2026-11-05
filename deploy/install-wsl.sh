#!/usr/bin/env bash
# Run inside Ubuntu 24.04 WSL2. This script never copies credentials.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
kit="${1:?Usage: bash deploy/install-wsl.sh /absolute/path/jira-auto-toolkit}"
kit="$(cd "$kit" && pwd)"
[[ "$(uname -m)" == x86_64 ]] || { echo 'Toolkit requires Linux x86_64'; exit 1; }
[[ "$root" != /mnt/* && "$root" != *' '* ]] || { echo 'Extract under a Linux home path without spaces'; exit 1; }
(cd "$kit" && sha256sum --check SHA256SUMS)
sudo apt-get update
sudo apt-get install -y python3 python3-venv git curl ca-certificates openssh-client gnupg ripgrep unzip binutils binutils-arm-none-eabi binutils-riscv64-unknown-elf
python3 -c 'import sys; assert sys.version_info >= (3,12), "Ubuntu 24.04 / Python 3.12+ required"'
mkdir -p "$HOME/.local/bin"
for tool in uv jira repo; do
  target="$HOME/.local/bin/$tool"
  if [[ -e "$target" ]] && ! cmp -s "$kit/bin/$tool" "$target"; then
    echo "Existing $target differs; retain it. Move it aside explicitly to install the bundled version."
  else
    install -m 755 "$kit/bin/$tool" "$target"
  fi
done
export PATH="$HOME/.local/bin:$PATH"
cd "$root"
"$kit/bin/uv" sync --frozen
"$kit/bin/uv" tool install --python /usr/bin/python3 "$kit/elf-fetch"
printf '\nInstalled. Next: python3 deploy/configure.py --help\n'
printf 'If needed, add export PATH="$HOME/.local/bin:$PATH" to ~/.profile\n'
