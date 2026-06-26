# current state of PCB-Design (as of 26th July 2026)

Scale Module (for xiao esp32-c6): v0.4 - currently untested
Scale Module (for esp32 30pin): V0.3 ok
Power Module: V0.3 ok

# HiveScale PCB Design — Scale Module V0.2

This directory contains the KiCad schematic and PCB layout for the HiveScale **Scale Module**. It is a breakout board that accepts off-the-shelf modules on pin headers — no SMD soldering required. All modules are simply plugged in.

The Scale Module is the central board of the HiveScale system. Power and connectivity (LTE, solar, battery) are handled by a separate **Power Module**, which connects to this board via I2C or ESPnow.

---

## Modules on board

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

---

## ESP32 pin mapping

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

---

## INMP441 stereo microphone wiring

Both INMP441 modules share a single I2S bus. Channel selection is hardware-configured via the **L/R pin** on each module:

| Module | Connector | L/R pin | Channel |
|---|---|---|---|
| Sound Sensor Hive 1 | J6 | GND | Left channel |
| Sound Sensor Hive 2 | J16 | 3.3 V | Right channel |

The L/R assignment is **not visible in the schematic** — it is determined by which power rail is connected to the L/R pad on each module. The PCB header for J6 ties L/R to GND and the header for J16 ties L/R to 3.3 V. Verify this during layout and assembly.

All three bus lines (BCLK, WS, SD) are shared between both modules. Each module must have VDD connected to 3.3 V and GND to GND.

```
INMP441 Hive 1 (J6):  VDD -> 3.3V  GND -> GND  BCLK -> GPIO14  WS -> GPIO13  SD -> GPIO34  L/R -> GND
INMP441 Hive 2 (J16): VDD -> 3.3V  GND -> GND  BCLK -> GPIO14  WS -> GPIO13  SD -> GPIO34  L/R -> 3.3V
```

---

## BeeCounter wiring (J20)

The BeeCounter module connects via three pins:

| J20 pin | Signal |
|---|---|
| 1 | GPIO13 |
| 2 | GPIO14 |
| 3 | GND |

> Note: GPIO13 is shared with INMP441 WS. In firmware, the I2S peripheral takes ownership of GPIO13 during audio sampling. Confirm that the BeeCounter firmware logic does not conflict with the I2S peripheral when both are active. If conflicts arise during development, move BeeCounter to one of the GPIO pins exposed on the expansion header (J18).

> **Firmware integration note:** the current HiveScale firmware
> (`firmware/src/bee_counter_client.cpp`) communicates with the BeeCounter as an
> **I2C slave** at addresses `0x30` (hive 1) / `0x31` (hive 2) on the shared bus
> (SDA GPIO21 / SCL GPIO22) — including the OTA-over-I2C firmware relay. Reconcile
> the J20 wiring above with the I2C bus before fabricating, and route the
> BeeCounter to SDA/SCL rather than the discrete GPIOs.

---

## I2C bus

| Device | Address |
|---|---|
| DS3231 RTC | `0x68` |
| SHT40 | `0x44` |

Both 4.7 kΩ pull-up resistors (SDA-4.7k1, SCL-4.7k1) are on board. Do not add additional pull-ups on plugged-in modules unless the total effective pull-up resistance becomes too low. If multiple I2C modules with built-in pull-ups are installed, verify the combined resistance.

---

## Power

The board is powered via J10 (Power In, 5 V) or through J19 (Power Module Header). The ESP32 and all 3.3 V peripherals are supplied from the ESP32 on-board regulator or from the Power Module.

- All module GNDs must share a common ground.
- Keep load-cell analog wiring away from any switching regulators or high-frequency signals.
- The SD card module and RTC backup coin cell are independently powered from 3.3 V.

---

## Connectors

### J1 — ESP32 left (TX/RX side), 15-pin

Carries UART, I2C, SD SPI, DS18B20, button, and several GPIO signals.

### J2 — ESP32 right (power/ADC side), 15-pin

Carries power rails, HX711 signals, INMP441 I2S signals, and ADC-capable GPIOs.

### J17 — I2C expansion, 6-pin

Exposes 3.3 V, GND, SDA, SCL for additional I2C devices.

### J18 — Expansion header, 12-pin

Breaks out remaining GPIOs for future expansion or custom peripherals.

### J19 — Power Module header

Connects to the separate Power Module (solar, battery, LTE) via I2C or ESPnow. Exact pinout depends on Power Module revision.

---

## Fabrication

Fabrication outputs are in the `fabrication/` subdirectory. Before ordering:

- Confirm all module header footprints match the physical modules you are using (pin pitch, row spacing).
- Verify the INMP441 L/R pin routing as described above.
- Verify pull-up resistor values on the I2C bus.
- Order a small prototype run before field deployment.

---

## Assembly notes

- Plug modules into headers — do not solder modules directly to the board.
- Install the DS3231 coin cell before sealing the enclosure.
- Route load-cell wiring away from the SD module and any switching supplies.
- Label Scale 1 and Scale 2 wiring at both the load-cell combinator and the PCB terminals.
- Use ferrules or locking connectors on load-cell screw terminals where vibration is expected.
- The INMP441 modules are sensitive to mechanical vibration. Mount them so the mic port faces the hive interior, not the electronics compartment.