#!/bin/sh
# Compiles the firmware in source/ into CIRCUITPY/*.mpy and copies boot.py and code.py.
# Usage: tools/build_mpy.sh /path/to/mpy-cross
# mpy-cross must come from the same CircuitPython version that runs on the board.
set -e
MPY_CROSS="${1:-mpy-cross}"
cd "$(dirname "$0")/.."
"$MPY_CROSS" --version
for src in source/*.py; do
    name=$(basename "$src" .py)
    case "$name" in
        boot|code) cp "$src" "CIRCUITPY/$name.py" ;;
        *) "$MPY_CROSS" -o "CIRCUITPY/$name.mpy" "$src" ;;
    esac
done
echo "Built CIRCUITPY/ from source/."
