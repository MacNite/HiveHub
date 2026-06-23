"""PlatformIO extra script: name the build artifacts after the board + version.

Instead of the default ``firmware.bin`` / ``firmware.elf`` this reads the
single source of truth for the version — ``FIRMWARE_VERSION`` defined in
``src/globals.cpp`` — and combines it with a friendly board label derived from
the active build environment, renaming the program to
``hivescale_<board>_<version>`` so each env produces a distinct artifact, e.g.
``hivescale_esp32_0.9.2.bin`` and ``hivescale_esp32-c6_0.9.2.bin``.

Referenced from platformio.ini via ``extra_scripts = pre:rename_firmware.py``.
"""

import os
import re

Import("env")  # noqa: F821 - provided by PlatformIO

# Friendly board labels for the artifact name, keyed by PlatformIO env name.
# Anything not listed falls back to its raw env name (sanitized).
BOARD_LABELS = {
    "esp32dev": "esp32",
    "xiao_esp32c6": "esp32-c6",
}


def board_label():
    env_name = env.subst("$PIOENV")  # noqa: F821 - provided by PlatformIO
    label = BOARD_LABELS.get(env_name, env_name)
    return re.sub(r"[^0-9A-Za-z._-]", "_", label)


def read_firmware_version():
    globals_path = os.path.join(env.subst("$PROJECT_SRC_DIR"), "globals.cpp")  # noqa: F821
    try:
        with open(globals_path, "r", encoding="utf-8") as handle:
            source = handle.read()
    except OSError as exc:
        print("[rename_firmware] Could not read %s: %s" % (globals_path, exc))
        return None

    match = re.search(r'FIRMWARE_VERSION\s*=\s*"([^"]+)"', source)
    if not match:
        print("[rename_firmware] FIRMWARE_VERSION not found in globals.cpp")
        return None
    return match.group(1)


version = read_firmware_version()
if version:
    # Sanitize so the version can't produce an invalid file name.
    safe_version = re.sub(r"[^0-9A-Za-z._-]", "_", version)
    progname = "hivescale_%s_%s" % (board_label(), safe_version)
    env.Replace(PROGNAME=progname)  # noqa: F821
    print("[rename_firmware] Build artifacts will be named %s.bin / .elf" % progname)
else:
    print("[rename_firmware] Falling back to default firmware.bin name")
