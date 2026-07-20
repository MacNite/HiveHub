# HiveHub wiring reference

This document covers wiring for both supported hardware variants:

- **XIAO ESP32-C6** — compact RISC-V module, the **recommended** board (used on the V0.4 Scale Module PCB); scales via I2C NAU7802
- **30-pin ESP32 DevKit** — legacy board with the full wired sensor suite (obsolete, no longer recommended)

---

## XIAO ESP32-C6 variant (recommended)

> The XIAO ESP32-C6 uses only the 11 front-header pins (D0–D10). There is **no
> HX711** on this board — load cells are read over I2C with the **NAU7802**
> 24-bit ADC (2 scales on the main bus, or up to 16 via the NAU7802 breakout
> PCB's TCA9548A mux; see [multi-hive.md](multi-hive.md)). A single **DS18B20
> 1-Wire bus on D1** is supported for wired in-hive temperature. The INMP441
> microphone is not supported — **use wireless BLE in-hive sensors** (HolyIot
> 25015, RuuviTag, or HiveInside ESP32-C6) for acoustics and vibration data.

### PlatformIO target

```
pio run -e xiao_esp32c6
```

### Pin mapping — XIAO ESP32-C6 (D0–D10 only)

| Signal | XIAO pin | GPIO | Notes |
|---|---|---|---|
| DS18B20 1-Wire | D1 | 1 | In-hive temperature bus, 4.7 kΩ pull-up to 3.3 V |
| I2C SDA | D4 | 22 | NAU7802 scales, RTC, SHT4x, optional MAX17048 |
| I2C SCL | D5 | 23 | NAU7802 scales, RTC, SHT4x, optional MAX17048 |
| SD CS | D3 | 21 | SD card chip select |
| SD SCK | D8 | 19 | SPI clock (XIAO default SCK) |
| SD MISO | D9 | 20 | SPI MISO (XIAO default MISO) |
| SD MOSI | D10 | 18 | SPI MOSI (XIAO default MOSI) |
| Setup button | D2 | 2 | Button to GND, INPUT_PULLUP |
| (unused) | D0 | 0 | Free |

Power the board from its USB-C connector (programming + serial via native USB-CDC)
or from the 5 V / 3 V3 header pins. All sensors connect to 3 V3.

### Antenna selection

The XIAO ESP32-C6 has a built-in ceramic patch antenna and a u.FL connector for
an external antenna. Selection uses the on-board FM8625H RF switch, driven by two
internal-trace GPIOs (not exposed on the headers):

- **GPIO3 (RF_SWITCH_EN)** — driven LOW at boot to enable the RF switch; required
  for either antenna.
- **GPIO14 (RF_ANT_SELECT)** — LOW selects the built-in ceramic antenna, HIGH
  selects the external u.FL antenna.

Firmware defaults to the built-in antenna.

To switch to the external u.FL antenna, add to `secrets.h`:

```cpp
#define XIAO_C6_USE_EXTERNAL_ANTENNA 1
```

The firmware logs the active antenna selection on every boot:

```
[ANT] XIAO C6: external antenna (EN GPIO3=LOW, SEL GPIO14=HIGH)
```

### What works / what does not

| Feature | Status |
|---|---|
| NAU7802 I2C scales (2 on main bus, up to 16 behind a TCA9548A mux) | ✅ |
| HX711 weight cells | ❌ no pins available — use the NAU7802 |
| SD card (cache + backup) | ✅ |
| DS3231 RTC | ✅ |
| SHT4x ambient temp/humidity | ✅ (default; SHT3x or BME280 selectable — BME280 adds ambient pressure) |
| DS18B20 wired in-hive probes | ⚙️ optional, **off by default** since v0.24.0 (1-Wire bus on D1) — enable `ENABLE_DS18B20_HIVE_TEMP` + its libraries |
| BLE wireless in-hive sensors (HolyIot, RuuviTag, HiveInside) | ✅ |
| HiveTraffic BeeCounter (BLE/GATT — the only supported bee-counter transport) | ✅ |
| OTA firmware update over WiFi | ✅ |
| WiFi provisioning portal | ✅ |
| Optional MAX17048 fuel gauge | ✅ (I2C) |
| INMP441 wired microphones | ❌ no pins available — pair a BLE sensor for acoustics |
| Deep-sleep timer wake | ✅ |
| Deep-sleep button wake | ✅ (GPIO wake, D2) |

---

## 30-pin ESP32 DevKit — pin mapping (legacy, no longer recommended)

The firmware pin definitions live in `firmware/include/config.h` (with optional per-device overrides in `secrets.h`). Keep this table aligned with those definitions whenever pins change.

| Signal | ESP32 GPIO | Direction | Notes |
|---|---:|---|---|
| HX711 #1 DOUT | 16 | Input | Scale 1 data |
| HX711 #1 SCK | 17 | Output | Scale 1 clock; used to power down HX711 during deep sleep |
| HX711 #2 DOUT | 32 | Input | Scale 2 data |
| HX711 #2 SCK | 33 | Output | Scale 2 clock; used to power down HX711 during deep sleep |
| DS18B20 data | 4 | Bidirectional | Shared 1-Wire bus for both hive probes |
| I2C SDA | 21 | Bidirectional | RTC, SHT4x, NAU7802/TCA9548A, optional MAX17048 |
| I2C SCL | 22 | Output | RTC, SHT4x, NAU7802/TCA9548A, optional MAX17048 |
| SD CS | 5 | Output | SD card chip select |
| SD SCK | 18 | Output | SPI clock |
| SD MISO | 23 | Input | SD card SPI MISO |
| SD MOSI | 19 | Output | SD card SPI MOSI |
| Setup button | 27 | Input | `INPUT_PULLUP`, button to GND |
| INMP441 BCLK | 14 | Output | I2S bit clock, shared by both mics (`ENABLE_INMP441_MICS`) |
| INMP441 WS | 13 | Output | I2S word select (LRCLK), shared by both mics |
| INMP441 SD | 34 | Input | I2S data from both mics; GPIO34 is input-only |

> Important pin notes: the firmware uses **GPIO23 as SD MISO** and **GPIO19 as SD MOSI** (many generic ESP32 examples use the opposite mapping). The two INMP441 microphones share one I2S bus; channel (left/right) is set in hardware by tying each mic's L/R pin to GND or 3.3 V.
>
> **No wired bee counters, no wired accelerometers.** BeeCounter entrance counting is **BLE/GATT-only** (the HiveTraffic counter — see [hivetraffic-bee-counter.md](hivetraffic-bee-counter.md)); the old wired I2C BeeCounter (`0x30`/`0x31`) is no longer supported. In-hive vibration comes from a paired BLE sensor; the old wired LIS3DH/LIS2DH12 driver has been removed.

---

## Component overview

| Component | Interface | ESP32 pins |
|---|---|---|
| HX711 #1 | Digital I/O | GPIO16 DOUT, GPIO17 SCK |
| HX711 #2 | Digital I/O | GPIO32 DOUT, GPIO33 SCK |
| DS18B20 x2 | 1-Wire | GPIO4 data with 4.7 kOhm pull-up to 3.3 V |
| SHT4x | I2C | GPIO21 SDA, GPIO22 SCL |
| DS3231 RTC | I2C | GPIO21 SDA, GPIO22 SCL |
| MicroSD card module | SPI | CS 5, SCK 18, MISO 23, MOSI 19 |
| Setup button | Digital input | GPIO27 to GND |
| INMP441 mics x2 | I2S | BCLK 14, WS 13, SD 34 (shared bus) |
| MAX17048 | I2C | GPIO21 SDA, GPIO22 SCL |

---

## Power supply

For development, power the ESP32 from USB. For field use, power the system from a regulated 5 V rail into the ESP32 `VIN` or 5 V pin, or use the breakout PCB's power modules.

```text
DC / solar / battery path -> regulator -> stable 5 V or 3.3 V rails -> ESP32 and sensors
All module grounds must be tied together.
```

Assembly notes:

- Set adjustable converters to the correct output voltage before connecting the ESP32.
- Keep the load-cell analog wiring away from switching regulators.
- Use one common ground reference, but route high-current solar/charge paths with wider traces or wires.
- For the breakout PCB, review `pcb-design/README.md` before ordering prototypes.

---

## HX711 load cell amplifiers

### HX711 #1 to ESP32

| HX711 pin | ESP32 pin |
|---|---|
| VCC | 3.3 V |
| GND | GND |
| DT / DOUT | GPIO16 |
| SCK | GPIO17 |

### HX711 #2 to ESP32

| HX711 pin | ESP32 pin |
|---|---|
| VCC | 3.3 V |
| GND | GND |
| DT / DOUT | GPIO32 |
| SCK | GPIO33 |

HX711 modules typically accept 2.7-5 V. Running them at 3.3 V avoids level shifting on the ESP32 GPIOs.

### Four 3-wire load cells per platform

A common platform scale uses four 3-wire half-bridge load cells. Use a combinator board or build the Wheatstone bridge manually. The exact color code varies by supplier, so verify with the load-cell datasheet.

Practical rules:

- Use four matched cells from the same kit.
- Keep all load-cell cable lengths similar.
- Twist or bundle each cell's wires and keep them away from regulator wiring.
- Check the unloaded A+/A- differential voltage before connecting to the HX711.
- Calibrate each channel after final mechanical installation.

---

## DS18B20 hive temperature probes

Both DS18B20 probes share GPIO4.

```text
DS18B20 VDD  -> 3.3 V
DS18B20 GND  -> GND
DS18B20 DATA -> GPIO4
GPIO4        -> 4.7 kOhm pull-up -> 3.3 V
```

Both sensors are connected in parallel. Waterproof probes often use red for VDD, black for GND, and yellow/white for data.

---

## SHT4x ambient sensor

| SHT4x pin | ESP32 pin |
|---|---|
| VCC | 3.3 V |
| GND | GND |
| SDA | GPIO21 |
| SCL | GPIO22 |

Place the SHT4x outside the electronics box but shield it from rain and direct sunlight.

The SHT4x is the **default** ambient sensor, but the firmware can drive one of
three families on this same I2C bus (pick exactly one — see `ENABLE_*_AMBIENT` in
`config.h`, and uncomment the matching library in `platformio.ini`):

| Sensor | Flag | I2C address | Notes |
|---|---|---|---|
| SHT4x | `ENABLE_SHT4X_AMBIENT` (default) | `0x44` | Temp + humidity |
| SHT3x (SHT31/SHT35) | `ENABLE_SHT3X_AMBIENT` | `0x44` (ADDR low) / `0x45` | Temp + humidity |
| BME280 | `ENABLE_BME280_AMBIENT` | `0x76` (SDO low) / `0x77` | Temp + humidity + **barometric pressure** (`ambient_pressure_hpa`) |

All three share the same wiring (VCC/GND/SDA/SCL) and reuse the pinned Adafruit
BusIO + Unified Sensor dependencies, so switching sensors is a flag + one library.

---

## DS3231 RTC

| DS3231 pin | ESP32 pin |
|---|---|
| VCC | 3.3 V |
| GND | GND |
| SDA | GPIO21 |
| SCL | GPIO22 |

Install a backup coin cell if the module supports it. The firmware can use the RTC as a fallback when Wi-Fi/NTP time sync fails.

---

## MicroSD module

The SD module uses SPI with the current firmware mapping below.

| SD module pin | ESP32 GPIO |
|---|---:|
| VCC | 3.3 V |
| GND | GND |
| CS | 5 |
| SCK | 18 |
| MISO | 23 |
| MOSI | 19 |

Use a FAT32-formatted card. The firmware keeps an append-only backup file and a retry queue for uploads that still need to reach the backend.

---

## Setup / factory reset button

Wire a momentary normally-open button between GPIO27 and GND.

```text
GPIO27 -> button -> GND
```

| Press | Firmware behavior |
|---|---|
| Short press | Start Wi-Fi provisioning AP |
| Long press, 10 seconds | Clear Preferences and reboot |

GPIO27 is RTC-capable and can wake the ESP32 from deep sleep when button wake is enabled.

---

## INMP441 stereo microphones

Two INMP441 I2S MEMS microphones share one I2S bus. Enable with `ENABLE_INMP441_MICS`.

| INMP441 pin | ESP32 GPIO | Firmware define |
|---|---:|---|
| SCK (BCLK) | 14 | `INMP441_BCLK_PIN` |
| WS (LRCLK) | 13 | `INMP441_WS_PIN` |
| SD (data) | 34 | `INMP441_SD_PIN` (ESP32 input-only) |
| VDD | 3.3 V | — |
| GND | GND | — |
| L/R | GND or 3.3 V | Selects left vs. right channel in hardware |

Wire one mic's **L/R** pin to GND (left channel) and the other's to 3.3 V (right
channel); BCLK, WS, and SD are shared. The firmware captures ~0.5 s of audio per
cycle (16 kHz, 8000 frames) and reports broadband RMS/peak plus per-band FFT
energy (sub-bass, hum, piping, stress, high) per channel.

---

## In-hive vibration — BLE sensors only

The wired LIS3DH/LIS2DH12 accelerometer driver has been **removed** from the
firmware. Per-hive vibration (including the ~20 Hz pre-swarm band) comes from a
paired in-hive BLE sensor instead — a HiveInside node gives full FFT bands, a
HolyIot 25015 or RuuviTag beacon gives a low-rate magnitude. See
[accelerometer.md](accelerometer.md).

---

## Power / connectivity (Power Module)

Cellular (SIM7080G) transport has been removed from the ESP32 firmware — the
Scale Module is **Wi-Fi only**. LTE/NB-IoT, solar charging, and battery
management now live on a separate **Power Module** that connects to the Scale
Module over I2C/ESP-NOW. The optional MAX17048 telemetry below still
runs on the ESP32 itself over the shared I2C bus.

---

## Optional MAX17048 LiPo fuel gauge

The MAX17048 shares the I2C bus and monitors the LiPo cell.

| MAX17048 pin | Connection |
|---|---|
| VCC / logic | 3.3 V, depending breakout board |
| GND | GND |
| SDA | GPIO21 |
| SCL | GPIO22 |
| BAT | LiPo battery positive through the breakout's intended connection |

Enable with:

```cpp
#define ENABLE_MAX17048_BATTERY 1
#define MAX17048_ALERT_PERCENT  20
```

The firmware reports battery voltage, state-of-charge, monitor status, and low-battery alert state.

---

## I2C bus summary

The shared bus runs at an **explicit 100 kHz** (`I2C_CLOCK_HZ` in
`firmware/include/config.h`) — the firmware sets and verifies the clock at
every boot and nothing changes it at runtime. Initialization and stuck-bus
recovery are checked and logged (`firmware/src/i2c_bus.cpp`).

| Device | Address |
|---|---|
| NAU7802 | `0x2A` (fixed — not configurable, no address-select pin) |
| TCA9548A mux | `0x70` |
| DS3231 RTC | `0x68` |
| SHT4x | `0x44` |
| MAX17048 | `0x36` |

(Wired BeeCounters at `0x30`/`0x31` and wired LIS3DH/LIS2DH12 accelerometers at
`0x18`/`0x19` are **no longer supported** — bee counting is BLE/GATT-only and
vibration comes from BLE in-hive sensors.)

### I2C pull-ups — avoid bus brownouts

Almost every breakout module ships with its own SDA/SCL pull-up resistors, and
they all end up **in parallel** on the shared bus:

- **NAU7802, MAX17048 and TCA9548A mux modules** generally carry **10 kΩ** pull-ups.
- **RTC (DS3231) modules and wired SHT40 breakouts** often carry **4.7 kΩ** pull-ups.

Stack a few of those and the combined pull-up becomes so strong that the bus
lines can no longer be pulled low reliably — the bus "browns out": devices stop
ACKing, drop off mid-transfer, or are detected intermittently. To avoid this:

- **Remove the 4.7 kΩ pull-up resistors from the RTC module.**
- **Do not populate additional pull-up resistors on the PCB** — leave the
  footprints empty. The 10 kΩ pull-ups already on the NAU7802 / MAX17048 / mux
  modules (and the SHT40's, if wired) are plenty.
- If devices still behave erratically, measure the effective pull-up: with
  everything connected and powered off the bus should read a few kΩ from
  SDA/SCL to 3.3 V — not well under 2 kΩ.

---

## Assembly tips

- Use an IP-rated enclosure and cable glands for all external probes and load-cell wiring.
- Put strain relief on load-cell and sensor cables.
- Label scale 1 and scale 2 wiring at both the sensor and electronics box.
- Use ferrules, locking connectors, or soldered joints where vibration or condensation is expected.
- Keep the SD card accessible for debugging, but protected from water ingress.
- In off-grid builds, test upload and battery-charge current before sealing the enclosure.

---

## Multi-hive I2C scales (NAU7802 + TCA9548A) — firmware v0.20.0+

HiveHub reads load cells over I2C with the **NAU7802** 24-bit ADC, and fans out
up to eight of them with a **TCA9548A** 1-to-8 I2C multiplexer — up to
**16 scales** per HiveHub (8 chips behind the mux, 2 channels each). See
[multi-hive.md](multi-hive.md) for the full feature overview and the topology
note explaining why a main-bus NAU7802 and the mux cannot be combined.

### NAU7802 (one chip = two load cells)

The NAU7802 sits on the shared I2C bus at the fixed address `0x2A` and has two
differential inputs (CH1/CH2). Supported topologies: **either** one direct
NAU7802 on the main bus (2 channels) **or** up to eight NAU7802s behind one
TCA9548A (16 channels) — never both at once.

> **CH2 hardware note.** The NAU7802 has an on-die 330 pF "PGA capacitor" that
> can be switched across the **channel-2 inputs** (PGA_PWR bit 7). It improves
> CH1-only designs but corrupts CH2 readings when a second load cell is wired
> there. The firmware sets this automatically from the hive registry: the cap is
> enabled only while CH2 is unused and disabled as soon as any hive maps a scale
> to CH2. Do **not** add an external filter capacitor directly across Vin2P/Vin2N
> either if CH2 carries a load cell.

| NAU7802 pin | Connect to |
| --- | --- |
| `VIN` / `VDD` | 3V3 |
| `GND` | GND |
| `SDA` | `I2C_SDA` (GPIO21 on the 30-pin DevKit) |
| `SCL` | `I2C_SCL` (GPIO22) |
| `VDDA` | tie to VDD (or its own 3V3 rail) |
| Channel 1 | first load cell (E+/E-/A+/A-) |
| Channel 2 | second load cell |

### TCA9548A multiplexer (up to 8 NAU7802 = 16 scales)

| TCA9548A pin | Connect to |
| --- | --- |
| `VCC` / `GND` | 3V3 / GND |
| `SDA` / `SCL` (upstream) | ESP32 `I2C_SDA` / `I2C_SCL` |
| `A0`/`A1`/`A2` | GND for address `0x70` (default) |
| `SD0..SD7` / `SC0..SC7` | one NAU7802's SDA/SCL per channel |

Pull-ups: the module pull-ups already present are enough — NAU7802 and mux
modules ship 10 kΩ pull-ups, and each downstream mux channel gets its own from
the NAU7802 breakout on it. Do **not** add extra pull-ups (see
[I2C pull-ups — avoid bus brownouts](#i2c-pull-ups--avoid-bus-brownouts)).

> All NAU7802s share `0x2A` (the chip has no address-select pin), so do **not**
> also place a NAU7802 on the upstream bus while populating mux channels — the two
> would collide. Use the mux (≤16 scales) **or** a single main-bus chip (2 scales),
> not both. See the topology note in [multi-hive.md](multi-hive.md).

### DS18B20 — up to 16 on one bus

All DS18B20 probes share the single 1-Wire data pin (`ONE_WIRE_PIN`, GPIO4 on the
DevKit) with one 4.7 kΩ pull-up to 3V3 for the whole bus. Each probe is mapped to
a hive by its **ROM address** in the portal (run the I2C/1-Wire scan first), so a
specific probe always serves a specific hive.
