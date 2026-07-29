#!/usr/bin/env bash
# Collect the PlatformIO build products of ONE environment into firmware/dist/
# and add a merged, ready-to-flash full-flash image.
#
#   ./firmware/ci/package_build.sh esp32dev
#   ./firmware/ci/package_build.sh xiao_esp32c6
#
# Run it after `pio run -e <env>`; it only copies and merges what that build
# already produced. Needs esptool on PATH (`pip install "esptool~=4.8"`) — the
# same tool PlatformIO itself flashes with.
#
# Output per environment (VERSION comes from FIRMWARE_VERSION in
# src/globals.cpp, BOARD from the label rename_firmware.py already uses):
#
#   hivehub_<board>_<version>.bin           application image only (OTA upload)
#   hivehub_<board>_<version>_full.bin      bootloader + partitions + otadata +
#                                           app, flashed as one file at 0x0
#   hivehub_<board>_<version>.elf           symbols, for decoding a backtrace
#   hivehub_<board>_<version>.sha256        checksums for this board's images
#   bootloader.bin / partitions.bin         the individual pieces
#
# The application-only .bin is the file the backend's OTA endpoint serves
# (POST /api/v1/app/devices/{id}/firmware); the _full.bin is for flashing a
# blank board over USB.
set -euo pipefail

ENV_NAME="${1:?usage: package_build.sh <platformio-env>}"

FIRMWARE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$FIRMWARE_DIR/.pio/build/$ENV_NAME"
DIST_DIR="$FIRMWARE_DIR/dist"

# Board label, esptool chip name and bootloader offset per environment. The
# labels must stay in sync with rename_firmware.py (BOARD_LABELS) and
# HIVEHUB_BOARD_LABEL in include/config.h, because the OTA endpoint matches
# releases to devices on exactly that string.
#
# The bootloader offset differs by SoC family: the original ESP32 (and S2) keep
# a 4 KB gap at the start of flash, while the newer RISC-V parts (C3/C6/H2) and
# the S3 place the bootloader at 0x0. Everything else in the layout is common:
# partition table at 0x8000, otadata at 0xe000 and app0 at 0x10000 (min_spiffs.csv,
# see board_build.partitions in platformio.ini).
case "$ENV_NAME" in
  esp32dev)
    BOARD_LABEL="esp32"
    CHIP="esp32"
    BOOTLOADER_OFFSET="0x1000"
    ;;
  xiao_esp32c6)
    BOARD_LABEL="esp32-c6"
    CHIP="esp32c6"
    BOOTLOADER_OFFSET="0x0"
    ;;
  *)
    echo "package_build.sh: unknown PlatformIO environment '$ENV_NAME'" >&2
    echo "Add it to the case block above (board label, esptool chip, bootloader offset)." >&2
    exit 1
    ;;
esac

VERSION="$(sed -n 's/.*FIRMWARE_VERSION[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
           "$FIRMWARE_DIR/src/globals.cpp")"
if [ -z "$VERSION" ]; then
  echo "package_build.sh: could not read FIRMWARE_VERSION from src/globals.cpp" >&2
  exit 1
fi

NAME="hivehub_${BOARD_LABEL}_${VERSION}"
APP_BIN="$BUILD_DIR/$NAME.bin"

if [ ! -f "$APP_BIN" ]; then
  echo "package_build.sh: $APP_BIN not found — run 'pio run -e $ENV_NAME' first." >&2
  echo "(If the name looks wrong, rename_firmware.py and this script disagree on the board label.)" >&2
  exit 1
fi

# boot_app0.bin lives in the Arduino framework package, not in the build dir. It
# is the initial otadata content; without it a freshly flashed board can boot
# from the wrong OTA slot after its first update.
CORE_DIR="${PLATFORMIO_CORE_DIR:-$HOME/.platformio}"
BOOT_APP0="$(find "$CORE_DIR/packages" -path '*/tools/partitions/boot_app0.bin' -print -quit 2>/dev/null || true)"
if [ -z "$BOOT_APP0" ]; then
  echo "package_build.sh: boot_app0.bin not found under $CORE_DIR/packages" >&2
  exit 1
fi

mkdir -p "$DIST_DIR"
cp "$APP_BIN" "$DIST_DIR/$NAME.bin"
cp "$BUILD_DIR/$NAME.elf" "$DIST_DIR/$NAME.elf"
cp "$BUILD_DIR/bootloader.bin" "$DIST_DIR/${NAME}_bootloader.bin"
cp "$BUILD_DIR/partitions.bin" "$DIST_DIR/${NAME}_partitions.bin"

# --flash_mode/_freq/_size keep: leave the header exactly as PlatformIO built it
# for this board, so the merged image flashes identically to `pio run -t upload`.
esptool.py --chip "$CHIP" merge_bin \
  -o "$DIST_DIR/${NAME}_full.bin" \
  --flash_mode keep --flash_freq keep --flash_size keep \
  "$BOOTLOADER_OFFSET" "$BUILD_DIR/bootloader.bin" \
  0x8000 "$BUILD_DIR/partitions.bin" \
  0xe000 "$BOOT_APP0" \
  0x10000 "$APP_BIN"

# Per-board checksum file, not a shared SHA256SUMS: the release workflow
# downloads every board's dist/ into one directory, where a shared name would
# have one board's file silently overwrite the other's. It concatenates these.
( cd "$DIST_DIR" && sha256sum "$NAME"*.bin > "$NAME.sha256" )

echo "package_build.sh: packaged $NAME (chip $CHIP, bootloader at $BOOTLOADER_OFFSET)"
ls -l "$DIST_DIR"
