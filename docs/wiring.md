# HiveHub wiring reference

This document covers wiring for both supported hardware variants:

- **30-pin ESP32 DevKit** — original board, full sensor suite
- **XIAO ESP32-C6** — compact RISC-V module, wireless-sensor-only variant

---

## XIAO ESP32-C6 variant

> **Wired in-hive sensors are NOT supported on this board.**
> The XIAO ESP32-C6 uses only the 11 front-header pins (D0–D10), which are fully
> occupied by the two HX711 amplifiers, the SD card, I2C bus, and the setup button.
> There are no spare pins for DS18B20 probes or INMP441 microphones.
> **Use wireless BLE in-hive sensors** (HolyIot 25015, RuuviTag, or HiveInside
> ESP32-C6) to obtain in-hive temperature, humidity, acoustics, and vibration data.

### PlatformIO target

```
pio run -e xiao_esp32c6
```

### Pin mapping — XIAO ESP32-C6 (D0–D10 only)

| Signal | XIAO pin | GPIO | Notes |
|---|---|---|---|
| HX711 #1 DOUT | D6 | 16 | Scale 1 data |
| HX711 #1 SCK | D7 | 17 | Scale 1 clock |
| HX711 #2 DOUT | D0 | 0 | Scale 2 data |
| HX711 #2 SCK | D1 | 1 | Scale 2 clock |
| I2C SDA | D4 | 22 | RTC, SHT4x, optional INA219/MAX17048 |
| I2C SCL | D5 | 23 | RTC, SHT4x, optional INA219/MAX17048 |
| SD CS | D3 | 21 | SD card chip select |
| SD SCK | D8 | 19 | SPI clock (XIAO default SCK) |
| SD MISO | D9 | 20 | SPI MISO (XIAO default MISO) |
| SD MOSI | D10 | 18 | SPI MOSI (XIAO default MOSI) |
| Setup button | D2 | 2 | Button to GND, INPUT_PULLUP |

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
| 2× HX711 weight cells | ✅ |
| SD card (cache + backup) | ✅ |
| DS3231 RTC | ✅ |
| SHT4x ambient temp/humidity | ✅ |
| BLE wireless in-hive sensors (HolyIot, RuuviTag, HiveInside) | ✅ |
| OTA firmware update over WiFi | ✅ |
| WiFi provisioning portal | ✅ |
| Optional INA219 solar monitor | ✅ (I2C) |
| Optional MAX17048 fuel gauge | ✅ (I2C) |
| DS18B20 wired in-hive probes | ❌ no pins available |
| INMP441 wired microphones | ❌ no pins available |
| Deep-sleep timer wake | ✅ |
| Deep-sleep button wake | ✅ (GPIO wake, D2) |

---

## 30-pin ESP32 DevKit — pin mapping

The firmware pin definitions live in `firmware/include/config.h` (with optional per-device overrides in `secrets.h`). Keep this table aligned with those definitions whenever pins change.

| Signal | ESP32 GPIO | Direction | Notes |
|---|---:|---|---|
| HX711 #1 DOUT | 16 | Input | Scale 1 data |
| HX711 #1 SCK | 17 | Output | Scale 1 clock; used to power down HX711 during deep sleep |
| HX711 #2 DOUT | 32 | Input | Scale 2 data |
| HX711 #2 SCK | 33 | Output | Scale 2 clock; used to power down HX711 during deep sleep |
| DS18B20 data | 4 | Bidirectional | Shared 1-Wire bus for both hive probes |
| I2C SDA | 21 | Bidirectional | RTC, SHT4x, BeeCounter, optional INA219, optional MAX17048 |
| I2C SCL | 22 | Output | RTC, SHT4x, BeeCounter, optional INA219, optional MAX17048 |
| SD CS | 5 | Output | SD card chip select |
| SD SCK | 18 | Output | SPI clock |
| SD MISO | 23 | Input | SD card SPI MISO |
| SD MOSI | 19 | Output | SD card SPI MOSI |
| Setup button | 27 | Input | `INPUT_PULLUP`, button to GND |
| INMP441 BCLK | 14 | Output | I2S bit clock, shared by both mics (`ENABLE_INMP441_MICS`) |
| INMP441 WS | 13 | Output | I2S word select (LRCLK), shared by both mics |
| INMP441 SD | 34 | Input | I2S data from both mics; GPIO34 is input-only |
| LIS3DH/LIS2DH12 ×2 | — | I2C | Accelerometers on the shared I2C bus at `0x18` / `0x19` (`ENABLE_LIS3DH_ACCEL`) |

> Important pin notes: the firmware uses **GPIO23 as SD MISO** and **GPIO19 as SD MOSI** (many generic ESP32 examples use the opposite mapping). The two INMP441 microphones share one I2S bus; channel (left/right) is set in hardware by tying each mic's L/R pin to GND or 3.3 V. BeeCounters and the optional LIS3DH/LIS2DH12 accelerometers are not on dedicated GPIOs — they are polled over the shared I2C bus (BeeCounters at `0x30` / `0x31`, accelerometers at `0x18` / `0x19`).

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
| BeeCounter x2 | I2C | GPIO21 SDA, GPIO22 SCL (`0x30` / `0x31`) |
| LIS3DH / LIS2DH12 x2 | I2C | GPIO21 SDA, GPIO22 SCL (`0x18` / `0x19`) |
| INA219 | I2C | GPIO21 SDA, GPIO22 SCL |
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

## LIS3DH / LIS2DH12 accelerometers (per-hive vibration)

> **Legacy / optional.** The wired accelerometer is no longer wired into the stock
> firmware's measurement cycle — in-hive vibration now comes from a BLE sensor
> (see [accelerometer.md](accelerometer.md)). The driver is retained behind
> `ENABLE_LIS3DH_ACCEL` for custom builds; this wiring is kept for that case.

One low-g MEMS accelerometer per hive captures low-frequency comb/wall
vibration — most importantly the **~20 Hz pre-swarm signal** that hive
microphones cannot reach. Enable with `ENABLE_LIS3DH_ACCEL`. The **LIS3DH**
(purple GY-LIS3DH breakout, used for prototyping) and the **LIS2DH12TR** (final
BOM) share the same WHO_AM_I (`0x33`), register map and I2C addresses, so the
same firmware drives both. See [accelerometer.md](accelerometer.md) for the
rationale, bands and config.

### Which LIS3DH pins to connect (I2C mode)

| LIS3DH pin | Connect to | Required? | Notes |
|---|---|---|---|
| VCC | 3.3 V | Yes | Board regulator accepts 3.3 V |
| GND | GND | Yes | Common ground |
| SCL | GPIO22 | Yes | Shared I2C clock |
| SDA | GPIO21 | Yes | Shared I2C data |
| CS | 3.3 V | **Yes** | Selects I2C. CS **low at power-up = SPI**, so tie it high |
| SDO/SA0 | GND **or** 3.3 V | **Yes** | Sets the I2C address LSB: GND → `0x18` (hive 1), 3.3 V → `0x19` (hive 2) |
| INT1 | — | No | Data-ready / motion interrupt; unused (firmware polls) |
| INT2 | — | No | Second interrupt; unused |
| ADC1 / ADC2 / ADC3 | — | No | LIS3DH auxiliary ADC inputs; unused |

So the minimal per-board connection is **VCC, GND, SCL, SDA, CS→3.3 V, and
SDO→GND or 3.3 V** for the address. To run both hives, wire two boards in
parallel on SDA/SCL/VCC/GND and set one SDO low (`0x18`) and the other high
(`0x19`):

```text
Accelerometer 1 (hive 1): VCC->3.3V GND->GND SCL->GPIO22 SDA->GPIO21 CS->3.3V SDO->GND   (0x18)
Accelerometer 2 (hive 2): VCC->3.3V GND->GND SCL->GPIO22 SDA->GPIO21 CS->3.3V SDO->3.3V  (0x19)
```

Enable and tune in `secrets.h`:

```cpp
#define ENABLE_LIS3DH_ACCEL 1
#define LIS3DH_ADDR_SLOT_1  0x18   // hive 1
#define LIS3DH_ADDR_SLOT_2  0x19   // hive 2
#define LIS3DH_ODR_HZ       400    // 10/25/50/100/200/400
#define LIS3DH_SAMPLE_COUNT 256    // power of two; 256 @ 400 Hz ≈ 0.64 s
#define LIS3DH_RANGE_G      2      // 2/4/8/16 g
```

Per cycle the firmware reports, per hive, the broadband AC RMS and the energy in
three vibration bands — swarm (8–30 Hz), fanning (30–100 Hz) and activity
(100–200 Hz) — under the `accel_1_*` / `accel_2_*` keys.

> Mounting matters: bolt or firmly couple the sensor to the hive body or a brood
> frame so substrate-borne vibration transfers into it. A board dangling on
> flying leads mostly measures cable sway. The literature places transducers on
> the inner hive wall or perpendicular to the comb in a brood frame.

### LIS2DH12TR (final build)

The LIS2DH12 is ST's pin-compatible successor and is register- and
address-compatible for everything this firmware uses, so no code changes are
needed — wire it exactly as above (VCC, GND, SCL, SDA, CS→3.3 V, SDO for the
address). It is the recommended part for the final, easily sourced BOM.

---

## Power / connectivity (Power Module)

Cellular (SIM7080G) transport has been removed from the ESP32 firmware — the
Scale Module is **Wi-Fi only**. LTE/NB-IoT, solar charging, and battery
management now live on a separate **Power Module** that connects to the Scale
Module over I2C/ESP-NOW. The optional INA219 and MAX17048 telemetry below still
runs on the ESP32 itself over the shared I2C bus.

---

## Optional INA219 solar/load monitor

The INA219 shares the I2C bus.

| INA219 pin | ESP32 pin |
|---|---|
| VCC | 3.3 V |
| GND | GND |
| SDA | GPIO21 |
| SCL | GPIO22 |

Default address is `0x40`. Enable with:

```cpp
#define ENABLE_INA219_SOLAR 1
#define INA219_I2C_ADDRESS  0x40
```

The firmware reports bus voltage, shunt voltage, load voltage, current, and power when the module is present.

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

| Device | Address |
|---|---|
| DS3231 RTC | `0x68` |
| SHT4x | `0x44` |
| BeeCounter 1 / 2 | `0x30` / `0x31` |
| LIS3DH / LIS2DH12 1 / 2 | `0x18` / `0x19` (set by SDO/SA0) |
| INA219 | `0x40` by default |
| MAX17048 | Fixed by device/library |

Most breakout boards include SDA/SCL pull-ups. If multiple breakout boards are installed, verify the effective pull-up resistance is not too low.

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

Beyond the two HX711 pin channels, HiveHub can read load cells over I2C with the
**NAU7802** 24-bit ADC, and fan out up to eight of them with a **TCA9548A** 1-to-8
I2C multiplexer. An all-NAU7802 setup tops out at **16 scales** (8 chips behind the
mux); the full **18 wired channels** is reached by adding the 2 HX711 pin channels
(**2 HX711 + 16 NAU7802**). See [multi-hive.md](multi-hive.md) for the full feature
overview and the topology note explaining why a main-bus NAU7802 and the mux cannot
be combined.

### NAU7802 (one chip = two load cells)

The NAU7802 sits on the shared I2C bus at the fixed address `0x2A` and has two
differential inputs (CH1/CH2):

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

Pull-ups: keep 4.7 kΩ pull-ups on the **upstream** bus; each downstream channel
needs its own pull-ups too (most NAU7802 breakouts include them).

> All NAU7802s share `0x2A` (the chip has no address-select pin), so do **not**
> also place a NAU7802 on the upstream bus while populating mux channels — the two
> would collide. Use the mux (≤16 scales) **or** a single main-bus chip (2 scales),
> not both. To reach 18, pair 16 muxed NAU7802s with the 2 HX711 channels. See the
> topology note in [multi-hive.md](multi-hive.md).

### DS18B20 — up to 18 on one bus

All DS18B20 probes share the single 1-Wire data pin (`ONE_WIRE_PIN`, GPIO4 on the
DevKit) with one 4.7 kΩ pull-up to 3V3 for the whole bus. Each probe is mapped to
a hive by its **ROM address** in the portal (run the I2C/1-Wire scan first), so a
specific probe always serves a specific hive.
