#!/usr/bin/env sh
# Build and run the host-level I2C hardening tests. Needs only a C++17 g++/clang++
# — no Arduino toolchain: the tests exercise the exact production headers
# (i2c_iface.h, nau7802_checked.h, scale_math.h, scale_topology.h) against the
# scripted mock bus in mock_i2c.h.
set -e
cd "$(dirname "$0")"
: "${CXX:=g++}"
mkdir -p build
"$CXX" -std=gnu++17 -Wall -Wextra -Werror -I../include \
       -o build/test_i2c_hardening test_i2c_hardening.cpp
./build/test_i2c_hardening

"$CXX" -std=gnu++17 -Wall -Wextra -Werror -I../include \
       -o build/test_sht4x_recovery test_sht4x_recovery.cpp
./build/test_sht4x_recovery
