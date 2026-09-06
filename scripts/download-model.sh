#!/bin/bash
# Download a whisper.cpp ggml model into Omavoice's model directory.
# Usage: scripts/download-model.sh small.en
set -euo pipefail
name="${1:-small.en}"
dir="${XDG_DATA_HOME:-$HOME/.local/share}/omavoice/models"
mkdir -p "$dir"
url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${name}.bin"
echo "Downloading ggml-${name}.bin to $dir"
curl -L --fail --progress-bar -o "$dir/ggml-${name}.bin.part" "$url"
mv "$dir/ggml-${name}.bin.part" "$dir/ggml-${name}.bin"
echo "Done: $dir/ggml-${name}.bin"
