# HiveHub PCB design

**Current state (as of 9 July 2026): all published PCBs are tested and working.**

## Recommended setup

- Use the **ESP32-C6 Scale Module (V0.4)** as your main HiveHub controller. It collects data from up to 18 BLE sensors and reads **2 scales through its on-board NAU7802**.
- Use the **NAU7802 breakout PCB (v0.2)** *without* an MCU to attach up to **16 wired scales** to a HiveHub (8× NAU7802 behind the on-board TCA9548A mux).
- You can also populate the NAU7802 breakout PCB *with* a XIAO MCU and run it as a **standalone BLE scale sensor** with up to 16 scales. Do **not** use the breakout with an MCU as a HiveHub itself — it has no RTC and no SD card.
- The **ESP32 30-pin Scale Module** is **no longer recommended** — it is obsolete and will be discontinued soon.
- The **Power Module** still works, but it will **probably be discontinued soon** in favour of powering the C6 Scale Module directly.

## Boards in this directory

| Directory | Board | Version | Status |
|---|---|---|---|
| `scale module/esp32-c6/` | Scale Module — XIAO ESP32-C6 | **V0.4** | ✅ **Recommended** — tested and working |
| `NAU7802 breakout pcb/` | NAU7802 breakout (16-scale I2C frontend) | **v0.2** | ✅ Recommended — tested and working |
| `power module/` | Power Module (solar / battery / connectivity) | **V0.3** | ⚠️ Tested and working — probably discontinued soon |
| `scale module/esp32 (30 pin)/` | Scale Module — ESP32 30-pin DevKit | **V0.3** | ❌ Obsolete — no longer recommended, will be discontinued |

Each board folder contains the KiCad project (`.kicad_pro` / `.kicad_sch` / `.kicad_pcb`) and ready-to-order Gerber/drill outputs in its `fabrication/` subdirectory.

---

## Scale Module — XIAO ESP32-C6 (V0.4) — recommended

The central board of a HiveHub. A breakout board that accepts off-the-shelf modules on pin headers — no SMD soldering required. Built around the compact **Seeed Studio XIAO ESP32-C6** (RISC-V, Wi-Fi 6 + BLE), it uses only the 11 front-header pins (D0–D10).

Scales are read over I2C via the **on-board NAU7802** 24-bit load-cell ADC (2 differential channels = 2 scales). More wired scales are added with the NAU7802 breakout PCB on the I2C expansion header (up to 16 muxed channels). There is **no HX711 and no INMP441 microphone** on this board — use paired BLE in-hive sensors for acoustics and vibration.

### On board

| Module / connector | Interface | Notes |
|---|---|---|
| XIAO ESP32-C6 | — | Main controller, plugs into socket headers |
| NAU7802 | I2C `0x2A` | Load-cell ADC — 2 scale channels with screw-terminal load-cell inputs |
| DS3231 RTC | I2C `0x68` | Timekeeping with coin-cell backup |
| SHT40 Ambient | I2C `0x44` | Ambient temperature and humidity |
| SD module | SPI | MicroSD for local cache and backup |
| DS18B20 header | 1-Wire (D1) | In-hive temperature bus, 4.7 kΩ pull-up on board |
| BeeCounter header | I2C `0x30`/`0x31` | Wired entrance bee counter |
| MAX17048 | I2C | LiPo battery voltage / state-of-charge gauge |
| TPS63020 buck-boost | — | Battery/solar input to regulated 3.3 V rail, with power selector |
| Power Module header | — | Connection to the separate Power Module (optional) |
| I2C expansion header | I2C | For the NAU7802 breakout PCB and other I2C devices |
| Setup pushbutton | Digital (D2) | Short press: provisioning AP · long press: factory reset |

### Firmware pin map (from `firmware/include/config.h`, `[env:xiao_esp32c6]`)

| Signal | XIAO pin | GPIO | Notes |
|---|---|---:|---|
| DS18B20 1-Wire | D1 | 1 | In-hive temperature bus (4.7 kΩ pull-up on board) |
| I2C SDA | D4 | 22 | NAU7802 scales, RTC, SHT40, BeeCounter, MAX17048, expansion |
| I2C SCL | D5 | 23 | Shared I2C bus clock |
| SD CS | D3 | 21 | SD card chip select |
| SD SCK / MISO / MOSI | D8 / D9 / D10 | 19 / 20 / 18 | MicroSD over SPI |
| Setup button | D2 | 2 | Button to GND, `INPUT_PULLUP`; deep-sleep GPIO wake |
| (unused) | D0 | 0 | Free |

> **Antenna:** the XIAO ESP32-C6 has a built-in ceramic antenna and a u.FL connector. The firmware drives the on-board RF switch (GPIO3 enable, GPIO14 select) and defaults to the built-in antenna; set `XIAO_C6_USE_EXTERNAL_ANTENNA 1` in `secrets.h` for the u.FL antenna.

Build the firmware with `pio run -e xiao_esp32c6`.

---

## NAU7802 breakout PCB (v0.2)

I2C load-cell frontend: **8× NAU7802** behind **TCA9548A** I2C multiplexers, for up to **16 scales** (each NAU7802 reads 2 load cells). Connects to the Scale Module's I2C expansion header.

Two ways to populate it:

- **Without an MCU (recommended):** a pure I2C scale expander for a HiveHub. The NAU7802 has a fixed address (`0x2A`), so the mux is what makes more than one chip per bus possible — see [docs/multi-hive.md](../docs/multi-hive.md) for the bus topology and firmware behaviour.
- **With a XIAO MCU (optional footprint):** a standalone battery-powered **BLE scale sensor** broadcasting up to 16 scale readings to a HiveHub. Do not use this as a full HiveHub — the board has no RTC and no SD card, so it cannot timestamp or cache measurements offline.

Solder jumpers select the bus/power options; see the schematic before assembly.

---

## Power Module (V0.3) — probably discontinued soon

Handles off-grid power for a Scale Module: solar charging, battery, regulated output, and a header to the Scale Module (I2C/ESP-NOW). Tested and working, but with the ESP32-C6 Scale Module carrying its own TPS63020 regulator and MAX17048 fuel gauge, this separate board will **probably be discontinued soon**. Prefer powering the C6 Scale Module directly.

---

## Scale Module — ESP32 30-pin (V0.3) — obsolete

> ❌ **No longer recommended.** This board and its firmware target (`pio run -e esp32dev`) keep working, but the design is obsolete and will be discontinued soon. Use the ESP32-C6 Scale Module for new builds. The reference below is retained for existing boards.

Breakout board for a 30-pin ESP32 DevKit with the full wired sensor suite: 2× HX711 scale amplifiers, DS18B20 1-Wire bus, 2× INMP441 I2S microphones, SD, RTC, SHT40, and BeeCounter.

### Modules on board

| Ref | Module | Interface | Notes |
|---|---|---|---|
| J1, J2 | ESP32 30-pin Dev Board | — | Main controller; plugs into left and right header rows |
| J4, J7 | HX711 Scale 1 | Digital I/O | Load cell amplifier for scale platform 1 |
| J3, J8 | HX711 Scale 2 | Digital I/O | Load cell amplifier for scale platform 2 |
| J11 | Load cell 1 input | Screw terminal | Direct load cell wires for scale 1 |
| J12 | Load cell 2 input | Screw terminal | Direct load cell wires for scale 2 |
| J9 | SD module | SPI | MicroSD card for local cache and backup |
| J5 | DS3231 RTC | I2C | Real-time clock with coin cell backup |
| J13 | DS18B20 Hive 1 | 1-Wire | Internal hive temperature probe |
| J14 | DS18B20 Hive 2 | 1-Wire | Internal hive temperature probe |
| J15 | SHT40 Ambient | I2C | External ambient temperature and humidity |
| J6 | INMP441 Sound Sensor Hive 1 | I2S | MEMS microphone — left channel |
| J16 | INMP441 Sound Sensor Hive 2 | I2S | MEMS microphone — right channel |
| J20 | BeeCounter | Digital I/O | Bee traffic counter module |
| SW1 | Pushbutton | Digital input | Short press: provisioning AP; long press: factory reset |
| J10 | Power In | — | 5 V supply input |
| J19 | Power Module Header | — | Connection to separate Power Module |
| J17 | I2C expansion | I2C | Shared bus header for additional I2C devices |
| J18 | Expansion Header (12-pin) | Mixed | GPIO breakout for future use |

### ESP32 pin mapping

| Signal | ESP32 GPIO | Direction | Notes |
|---|---:|---|---|
| HX711 #1 DOUT | 16 | Input | Scale 1 data |
| HX711 #1 SCK | 17 | Output | Scale 1 clock; held HIGH during deep sleep to power down HX711 |
| HX711 #2 DOUT | 32 | Input | Scale 2 data |
| HX711 #2 SCK | 33 | Output | Scale 2 clock; held HIGH during deep sleep to power down HX711 |
| DS18B20 1-Wire | 4 | Bidirectional | Shared bus for both hive probes; 4.7 kΩ pull-up to 3.3 V on board |
| I2C SDA | 21 | Bidirectional | RTC, SHT40, I2C expansion |
| I2C SCL | 22 | Output | RTC, SHT40, I2C expansion; 4.7 kΩ pull-up to 3.3 V on board |
| SD CS | 5 | Output | SD card chip select |
| SD SCK | 18 | Output | SPI clock |
| SD MISO | 23 | Input | |
| SD MOSI | 19 | Output | |
| Setup button | 27 | Input | `INPUT_PULLUP`, active low |
| INMP441 BCLK | 14 | Output | I2S bit clock shared by both microphones |
| INMP441 WS (LRCLK) | 13 | Output | I2S word select shared by both microphones |
| INMP441 SD (data) | 34 | Input | I2S data; GPIO34 is input-only on ESP32 |
| BeeCounter signal A | 12 | Input | Beam sensor channel A |
| BeeCounter signal B | 15 | Input | Beam sensor channel B (see schematic) |

> GPIO34 is input-only and has no internal pull-up. The INMP441 SD line is an open-drain output; pull-up on board is not required but confirm with your module's datasheet.

### INMP441 stereo microphone wiring

Both INMP441 modules share a single I2S bus. Channel selection is hardware-configured via the **L/R pin** on each module:

| Module | Connector | L/R pin | Channel |
|---|---|---|---|
| Sound Sensor Hive 1 | J6 | GND | Left channel |
| Sound Sensor Hive 2 | J16 | 3.3 V | Right channel |

The L/R assignment is **not visible in the schematic** — it is determined by which power rail is connected to the L/R pad on each module. The PCB header for J6 ties L/R to GND and the header for J16 ties L/R to 3.3 V.

All three bus lines (BCLK, WS, SD) are shared between both modules. Each module must have VDD connected to 3.3 V and GND to GND.

```
INMP441 Hive 1 (J6):  VDD -> 3.3V  GND -> GND  BCLK -> GPIO14  WS -> GPIO13  SD -> GPIO34  L/R -> GND
INMP441 Hive 2 (J16): VDD -> 3.3V  GND -> GND  BCLK -> GPIO14  WS -> GPIO13  SD -> GPIO34  L/R -> 3.3V
```

### BeeCounter wiring (J20)

| J20 pin | Signal |
|---|---|
| 1 | GPIO13 |
| 2 | GPIO14 |
| 3 | GND |

> Note: GPIO13 is shared with INMP441 WS. In firmware, the I2S peripheral takes ownership of GPIO13 during audio sampling.

> **Firmware integration note:** the current HiveHub firmware
> (`firmware/src/bee_counter_client.cpp`) communicates with the BeeCounter as an
> **I2C slave** at addresses `0x30` (hive 1) / `0x31` (hive 2) on the shared bus
> (SDA GPIO21 / SCL GPIO22) — including the OTA-over-I2C firmware relay. Route
> the BeeCounter to SDA/SCL rather than the discrete GPIOs.

### Power and connectors

The board is powered via J10 (Power In, 5 V) or through J19 (Power Module Header). J17 exposes 3.3 V, GND, SDA, SCL for additional I2C devices; J18 breaks out the remaining GPIOs.

---

## Shared I2C bus notes

| Device | Address |
|---|---|
| NAU7802 | `0x2A` (fixed — hence the TCA9548A mux at `0x70` on the breakout) |
| DS3231 RTC | `0x68` |
| SHT40 | `0x44` |
| BeeCounter 1 / 2 | `0x30` / `0x31` |

4.7 kΩ pull-up resistors for SDA/SCL are on the boards. Do not add additional pull-ups on plugged-in modules unless the total effective pull-up resistance stays reasonable — if multiple I2C modules with built-in pull-ups are installed, verify the combined resistance.

---

## Fabrication

Fabrication outputs (Gerbers + drill files, plus a ready-to-upload `.zip`) are in each board's `fabrication/` subdirectory. Before ordering:

- Confirm all module header footprints match the physical modules you are using (pin pitch, row spacing).
- Verify pull-up resistor values on the I2C bus.
- For the 30-pin board only: verify the INMP441 L/R pin routing as described above.

## Assembly notes

- Plug modules into headers — do not solder modules directly to the board.
- Install the DS3231 coin cell before sealing the enclosure.
- Route load-cell wiring away from the SD module and any switching supplies.
- Label each scale's wiring at both the load-cell combinator and the PCB terminals.
- Use ferrules or locking connectors on load-cell screw terminals where vibration is expected.
