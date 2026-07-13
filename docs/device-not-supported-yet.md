# My device is not supported yet

HiveHub already talks to a fixed catalogue of in-hive sensors, scales and bee
counters (see the [BLE sensor docs](README.md#in-hive-ble-sensors--bee-counters)
and the [config tool](../website/configurator.html)). If the wireless device you
own is **not** in that list, it isn't a dead end — it just needs a decoder added
to the firmware, and you can help make that happen quickly.

> **TL;DR** — Open a **GitHub issue** requesting support for your device, and
> attach a short **nRF Connect** capture of what it advertises or notifies. That
> capture is the exact data needed to write the integration.

---

## 1. Request support as a GitHub issue

**All support requests for new devices must be submitted as an issue on GitHub.**
Please don't send them by e-mail or bury them in an unrelated thread — an issue
is trackable, searchable by the next person with the same device, and keeps the
capture attached to the request.

👉 **[Open a new issue on the HiveHub repository](https://github.com/MacNite/HiveHub/issues/new)**

Before opening one, quickly [search existing
issues](https://github.com/MacNite/HiveHub/issues?q=is%3Aissue) — someone may
already have asked for the same device.

Please title it clearly, e.g. *"Add support for &lt;manufacturer&gt;
&lt;model&gt;"*, and include:

- **Device make / model** and a link to the product page or datasheet, if any.
- **What it measures** (temperature, humidity, weight, sound, vibration, bee
  count, battery, …) and which values you expect HiveHub to store.
- **How it talks** — is it a Bluetooth **advertising beacon** (broadcasts data
  with no connection) or a **GATT device** (you connect to it and read/subscribe
  to a characteristic)? If you're not sure, the nRF Connect steps below tell you
  which one it is.
- **The nRF Connect capture** from section 2 (paste the text or attach a
  screenshot).
- Ideally **two or more captures taken at known conditions** — e.g. the reading
  when the scale is empty vs. loaded with a known weight, or at two different
  temperatures. Paired captures make it far easier to reverse-engineer where each
  value sits in the payload.

The more of the above you provide, the faster the decoder can be written and
merged.

---

## 2. Get the data with nRF Connect

[**nRF Connect for Mobile**](https://www.nordicsemi.com/Products/Development-tools/nRF-Connect-for-mobile)
is a free Bluetooth Low Energy scanner from Nordic Semiconductor, available for
[Android](https://play.google.com/store/apps/details?id=no.nordicsemi.android.mcp)
and [iOS](https://apps.apple.com/app/nrf-connect-for-mobile/id1054362403). It
lets you see exactly what your device broadcasts and what it exposes over a
connection — which is precisely the information HiveHub's firmware needs. (The
existing HiveHub decoders were all confirmed against real nRF Connect captures.)

Install it, enable Bluetooth and location/nearby-devices permission, put the
device to be tested next to your phone, and follow the path that matches it.

### 2a. Find the device and its MAC address

1. Open nRF Connect and go to the **Scanner** tab, then **Scan**.
2. Find your device in the list. If it has no friendly name, use **RSSI** to spot
   it: move it right next to the phone and it should be the strongest (least
   negative, e.g. −40 dBm) entry. Tapping the ⭐/filter and setting an RSSI
   threshold helps hide distant devices.
3. Note the **MAC address** shown under the name (six hex pairs, e.g.
   `AA:BB:CC:DD:EE:FF`). HiveHub always needs this — it's how the firmware
   identifies *your* device. *(On iOS, Apple hides the real MAC and shows a
   random UUID instead; capture it anyway, but mention in the issue that it came
   from iOS so we know it needs the Android MAC or a firmware-side match.)*

### 2b. If it's an advertising beacon (broadcasts data, no connection)

Many in-hive sensors (like the HolyIot 25015 and RuuviTag) simply *broadcast*
their readings. You don't connect — you just read the advertisement.

1. In the scan result, expand your device's entry (tap it or the dropdown arrow)
   to reveal the **raw advertising data**.
2. Capture the **Manufacturer data** and/or **Service data** fields — these hold
   the sensor payload. Copy the full hex bytes. The first two bytes of
   manufacturer data are the **Company ID**; note it down too.
3. If the device advertises several **different frames** that alternate (some
   beacons rotate through temperature, then acceleration, then battery), watch
   the entry update for ~30 s and capture **each distinct payload** you see.
4. **Change something measurable and re-capture.** Warm the sensor in your hand,
   breathe on the humidity sensor, or add a known weight, then grab the new hex.
   Seeing which bytes changed (and by how much) is what pins down the scaling.

Paste those hex strings, the company ID, and what the true reading was at each
capture into the issue.

### 2c. If it's a GATT device (you connect to it)

Scales and some in-hive nodes (like the beehivemonitoring.com HiveHeart /
HiveScale) are read by **connecting** and subscribing to a characteristic.

1. In the scan list, tap **CONNECT** next to the device.
2. nRF Connect lists the **Services** (each with a UUID) and, under each, the
   **Characteristics** (each with its own UUID). Expand them.
3. Note the **Service UUID** and **Characteristic UUID** for anything that looks
   like sensor data. A characteristic that carries live readings almost always
   has **Notify** or **Indicate** in its properties.
4. For a **Read** characteristic, tap the ↓ (download) icon to read its current
   value; for a **Notify/Indicate** one, tap the ↓↓ (subscribe) icon to start
   receiving updates. The **Value** field shows the raw bytes (hex).
5. Capture the **raw value bytes**, then — as with beacons — **change a real
   measurement** (load the scale, warm the sensor) and capture the new bytes so
   the byte positions can be decoded.

Paste the service UUID, the characteristic UUID(s), their properties
(Notify/Read/…), and the raw value hex at each known condition into the issue.

### What a good capture looks like

```
Device:        Acme BeeScale 3000
MAC:           AA:BB:CC:DD:EE:FF   (Android)
Type:          GATT (connect + notify)
Service UUID:  0000abcd-0000-1000-8000-00805f9b34fb
Char UUID:     0000abce-0000-1000-8000-00805f9b34fb  (Notify)

Empty scale:   04 00 1a 2c 00 00 e3 07 ...
+5.00 kg:      04 00 1a 2c f4 01 e3 07 ...   (bytes 4–5 changed)
Temp ~22 °C:   ... (byte 6 = 0x16 = 22?)
```

Even a rougher capture than this is genuinely useful — attach whatever nRF
Connect shows you and note what the real reading was, and the rest can be worked
out from there.

---

## 3. What happens next

Once the issue and capture are in, the payload can be decoded and a driver added
to the firmware BLE bridge (the decoders live in `firmware/src/ble_sensor.cpp`
and `firmware/include/beehive_decode.h`, validated by tests in `test-data/`).
New devices are then exposed in the on-device provisioning portal and the
[config tool](../website/configurator.html) like the existing ones. Follow your
issue for progress.
