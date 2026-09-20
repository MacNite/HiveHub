# Listening to a hive

*Firmware **0.30.2** · server **0.5.2** · HiveInside **0.6.2** · issue [#71](https://github.com/MacNite/HiveInside/issues/71)*

A HiveInside node can be asked to record from inside
the hive. The hub relays the audio to the backend as it is captured, and the
dashboard plays it back when it arrives.

```
HiveInside                    HiveHub                     backend        browser
PDM mic → PCM16 → 32 KiB ring → BLE notify → 32 KiB ring → chunked POST → PCM/WAV
   16 kHz, 32 kB/s              137 kB/s measured          raw on disk    <audio>
```

> **Use HiveInside 0.6.2 or later.** The protocol works on 0.6.0 and 0.6.1, but
> a measurement cycle could drop the microphone's power rail underneath a
> running session: those builds return a full-length recording whose first ~50 ms
> carry sound and whose remainder is the noise floor of an unpowered mic. Nothing
> flags it — the byte count and checksum are perfect, because every byte the node
> sent did arrive. If a recording plays for a moment and then goes silent, check
> the node's version before anything else. HiveInside's
> `docs/audio-over-ble.md` has the mechanism.

This is the firmware-over-BLE relay in reverse, and deliberately so: the same
locate-scan, the same NimBLE client shape, the same crash-safe command
reporting. See [the OTA relay](hiveinside-ble-sensor.md#firmware-updates-ota-relay)
for the other direction, and HiveInside's
[`docs/audio-over-ble.md`](https://github.com/MacNite/HiveInside/blob/main/docs/audio-over-ble.md)
for the wire protocol.

## Why raw PCM, and why streaming

Both answers come from one measurement. Notification throughput from a
HiveInside node to an ESP32-C6 was measured at **137 kB/s** (15 ms connection
interval, 2M PHY, 251-byte PDUs). The microphone produces **32 kB/s**.

* **No codec.** Compressing would be fitting a pipe that is not full. Worse, a
  lost ADPCM packet corrupts decoder state and everything after it, while a lost
  PCM packet is just a hole. The wire format keeps a `format` byte with ADPCM
  reserved, so this can change without a protocol break.
* **Streamed, not buffered.** The node holds ~1 second of audio, not a whole
  clip: buffering 60 s would be 1.9 MB of static RAM on a 512 KB part that also
  runs a BLE stack.

The OTA relay's ~1.4 kB/s is not a counter-example. That path writes with
response (two connection intervals per chunk) and flushes RRAM synchronously
inside the ATT callback, where each write runs in a radio timeslot that
pre-empts connection events. It measures flash scheduling, not the radio.

## Setting it up

### 1. Generate the key

The node will not record without an authenticated request, and the hub will not
ask without a key. The two sides want the **same 32 bytes** in different
notations — a hex string on the hub, a C array on the node — so generate once
and print both:

```bash
KEY=$(openssl rand -hex 32)
echo "HiveHub  secrets.h:  #define HIVEINSIDE_AUDIO_PSK_HEX \"$KEY\""
echo "HiveInside audio_secret.h:"
echo "$KEY" | sed 's/../0x&, /g' | fold -sw 48 | sed 's/^/\t/;s/ $//'
```

Keep the output somewhere safe until both devices are flashed; nothing stores
the key anywhere else, so a lost key means reflashing both sides with a new one.

### 2. Put it on the hub

Add one line to `firmware/include/secrets.h` (gitignored — copy
`secrets.example.h` if you do not have one yet). It can go anywhere in the file;
next to the other HiveInside settings keeps it findable:

```c
#define HIVEINSIDE_AUDIO_PSK_HEX "40d813ad974c1506...5919426b"
```

64 hex characters, no `0x` prefix, no spaces. Anything else — a short string, a
stray `0x`, all zeros — is treated as "no key configured" and every session is
refused with a message saying so.

### 3. Put it on the node

Copy `firmware-nrf54lm20a/src/audio_secret.example.h` to
`firmware-nrf54lm20a/src/audio_secret.h` (also gitignored) and replace the
zeroed array with the rows the command above printed:

```c
#pragma once
#include <stdint.h>
static const uint8_t hive_audio_psk[32] = {
	0x40, 0xd8, 0x13, 0xad, 0x97, 0x4c, 0x15, 0x06,
	0x27, 0x03, 0xa0, 0x4e, 0x1f, 0xa3, 0x67, 0x25,
	0xf7, 0x09, 0x33, 0x30, 0xa8, 0xd3, 0xd1, 0x01,
	0x81, 0xfc, 0xdf, 0xe5, 0x59, 0x19, 0x42, 0x6b,
};
```

Both sides fail closed when the key is missing or all-zero. That is the intended
behaviour, not a bug to work around: the alternative is a microphone in a garden
that any BLE device in range can switch on.

### 4. Flash both

HiveHub with `HIVEINSIDE_AUDIO_ENABLED` (default on wherever `ENABLE_BLE_SCAN`
is), HiveInside with 0.6.2 or later.

> **`FORCE_RESEED` is not needed.** That flag only re-seeds the *claim code*
> from `secrets.h` into NVS preferences (see `device_prefs.cpp`). The audio key
> is never stored in preferences — the relay reads the compiled-in macro at the
> start of every session — so an ordinary flash or OTA is enough. Setting
> `FORCE_RESEED` would only re-push the claim code, which is unrelated and
> forces a re-registration you do not want.

**Update both sides together.** The key is checked on every session, so between
flashing one device and the other, audio fails with "authentication failed"
while measurements keep working normally. That asymmetry is the giveaway: if
readings are fine and only listening is broken, suspect the key before the
radio.

### 5. Mount the storage

`RECORDINGS_DIR` (default `/app/recordings`) needs a volume — the compose file
ships one. Without it the database keeps rows whose audio vanished on the next
container recreation.

## Using it

**Dashboard → Audio → "Hive audio".** Pick a hive, pick a length (10, 30 or 60
seconds), press **Record**. Then the part that surprises people:

> **It is not instant.** The hub deep-sleeps and only collects commands on its
> next wake, so a recording arrives within one reporting interval — minutes,
> not seconds. The panel says "Waiting for the hub to wake" for exactly this
> reason.

The panel is split: the request controls sit in the left rail above the list of
sessions, newest first, and selecting one fills the right-hand side with its
player, its quality figures and a download button. On a narrow screen the two
columns stack, which is what the shot below shows.

<img src="hive-audio-panel.png" alt="The Hive audio panel: hive and length
selectors above a Record button, a list of two sessions, and the selected
recording with its player, quality figures and a Checksum reading of &quot;not
confirmed&quot;." width="420"> A request follows its three
states in place — *waiting for the hub*, *being recorded*, done — and the
finished audio plays as an ordinary WAV.

The coloured dot beside each session is the same verdict the *Checksum* figure
spells out (see [How sure we are](#how-sure-we-are-confirmation)); the figures
are shown for every recording, not just broken ones, because a spliced recording
sounds perfectly continuous.

Only hives with a HiveInside are offered. Nothing else on a hub has a
microphone, and a request against a HolyIot or RuuviTag could only fail a wake
cycle later.

Shorter is usually better: 10–20 seconds is enough to hear what an insight
flagged, and costs proportionally less battery and less time with the hive off
the air — a connected node neither advertises nor samples, so its readings pause
for the length of the session.

### Why this is not "live listening"

The transport underneath *can* stream: the hub uploads while the microphone is
still running, the server writes the file as it lands, and
`GET /recordings/{id}/pcm?offset=` reads a session that is still being written.
An API consumer can use that to follow a recording in flight.

The dashboard deliberately does not, because the lead-in makes the label
dishonest. A "listen now" button that sits silent for several minutes before the
first sound sets an expectation the hardware cannot keep, and a beekeeper
staring at a silent player cannot tell a working request from a broken one. The
smaller promise — you ask for N seconds, and it appears when the hive has been
heard — is one the system always keeps.

## Reading the quality flags

A session can produce audio that is worth hearing and still not be whole. The
dashboard says so rather than letting you find out by ear, because a recording
with a hole in it plays as perfectly continuous sound — and an acoustic
judgement made on a splice is wrong in a way nothing later reveals.

| Flag | Shown as | Meaning | Where to look |
|---|---|---|---|
| `dropped_bytes` | Dropped | The node's own ring overran — audio never left the hive | The BLE link could not carry 32 kB/s. Move the node closer, or clear the path |
| `gaps` | Gaps | Sequence jumps on the air, or the node's own `FLAG_GAP` | Packets vanished between the node's radio and the hub's |
| `ring_overruns` | Hub buffer | The hub's staging ring refused a notification because the upload was behind | The hub's WiFi or backend, not the hive |
| CRC mismatch | Checksum | What arrived is not what the node computed: corruption in transit | |
| `clipped_pct` | Clipping | Samples hit the rail; lower the gain | |

Audio either side of a gap is real. The join is not.

`gaps` and `ring_overruns` were **one summed field** up to firmware 0.30.1, which
made the only question a field report actually asks — was that the radio or the
hub? — unanswerable without a serial cable. They are separate from 0.30.2 and
server 0.5.2 on; a recording made by an older hub reports the sum under `gaps`,
so the dashboard simply says less about it rather than guessing.

### How sure we are: `confirmation`

The hub reports on a session **twice**, and either report can go missing, so
"was this recording whole?" has more than two answers. `confirmation` is the one
the dashboard shows as the *Checksum* figure.

| Value | Shown as | What happened |
|---|---|---|
| `verified` | verified | The detailed `/finalize` report arrived and its CRC matched what the server computed over the bytes on disk |
| `reported` | unverified | `/finalize` arrived, but the CRCs disagree or the hub sent none. Only a CRC that actually disagrees marks the recording incomplete — an absent one is not evidence of damage |
| `confirmed` | hub confirmed | `/finalize` never arrived, but the hub's **command result** said the session succeeded. Nothing checked the bytes, but the hub did say it finished |
| `unknown` | not confirmed | Neither report arrived. The audio on disk plays; nothing corroborates it |

The middle two exist because `/finalize` is a single fire-and-forget POST that a
reset or a dropped packet takes with it, while the command result is retried,
swept when it goes stale, and written under the same crash-safe marker as every
other device command. Reading both is why a recording that is almost certainly
fine no longer gets told it lost contact with its hub. It is also why the
command result *closes* the row (`recordings.finalize_from_command_result`) and
a later `/finalize` only adds detail to it.

## Storage

Recordings are raw 16 kHz mono PCM16 on disk. The WAV header is generated per
request, so the same bytes serve both the live player and a `.wav` download, and
a recording still being written never needs its header rewritten.

**Nothing prunes them.** There is no retention sweep — a deliberate choice — so
the only thing between a self-hosted disk and a season of audio is the delete
button. One minute is about 1.9 MB.

## API

Three surfaces, the same functions behind each: master key
(`/api/v1/devices/…`, `/api/v1/recordings/…`), dashboard session
(`/api/v1/local/…`), HivePal user (`/api/v1/app/…`).

| Endpoint | Purpose |
|---|---|
| `POST …/devices/{id}/commands/record-audio?slot=&duration=&gain_db=` | queue a session; `duration=0` is live |
| `GET …/devices/{id}/recordings` | list, newest first, optionally `?hive=` |
| `GET …/recordings/{id}` | one recording's metadata and quality flags |
| `GET …/recordings/{id}/pcm?offset=` | raw PCM from an offset — reads a session while it is still being written; 204 means "nothing new yet". Not used by the dashboard; see "Why this is not live listening" above |
| `GET …/recordings/{id}/audio.wav` | the finished recording, playable anywhere |
| `DELETE …/recordings/{id}` | remove the row and its audio |

Requesting audio needs the admin role on the dashboard, or owner/admin in
HivePal. Listening to what already exists follows the usual per-device roles.

The hub's own ingest (`…/recordings/{id}/stream` and `/finalize`) is
device-authenticated and not for general use. Note that the hub builds those
URLs from its own device id and never from the command payload — a hub that
posted microphone audio to whatever address a queued command named would be one
malformed command away from streaming somebody's garden to a stranger.

## Privacy

Worth stating plainly, because it is easy to forget what this feature is: an
in-hive microphone in a garden can pick up human speech.

* Audio is stored unencrypted on your server.
* It is never part of public chart embeds and is not included in data exports.
* The BLE link is authenticated but not encrypted — the HMAC proves *who may
  ask*, not that the audio itself is private in flight. Someone with a sniffer
  in radio range could capture a session in progress.
* Recordings are kept until deleted, and the dashboard names who requested each.

## When the hub resets mid-recording

Hub firmware up to 0.30.0 could panic during an audio session and reboot. The
dashboard showed the recording as failed with `hub reset during relay
(panic/exception)`, and — misleadingly — "firmware transfer did not complete",
because the post-reset report was worded for the OTA relay that shipped first.
0.30.1 fixes the crash, and says "the audio session did not complete" when it
was a recording.

**It hit 30-pin ESP32 hubs far more than ESP32-C6 ones, and weak BLE links more
than strong ones.** Both follow from the same cause.

Audio notifications are delivered on NimBLE's host task, which is pinned to
core 0, while the upload loop runs on the Arduino loop task on core 1. On the
dual-core ESP32 those two run *at the same instant*; the ESP32-C6 is
single-core, so they can only interleave. When a session ended — and on a weak
link it ends by stall or dropped connection, with the node still transmitting —
the teardown freed the staging buffer while a notification was writing into it.
Depending on the timing that corrupted the heap (so some later, innocent
allocation faulted instead), wrote through a null pointer, or divided by a zero
buffer size, which traps on Xtensa but not on the C6's RISC-V core. Three
crashes, one race, all of them reported as `ESP_RST_PANIC`.

The staging ring is now a checked structure (`firmware/include/audio_ring.h`,
with host tests in `firmware/host_test/test_audio_ring.cpp`) that refuses to
touch memory once it has been detached, teardown detaches it before it frees
anything, and no size is ever used as a divisor. A notification that arrives
during teardown is counted as a dropped packet and reported with the session,
which is what the `gaps` figure is for.

Three smaller things on the same path went with it:

* The 4 KiB upload buffer moved off the loop task's 8 KiB stack, where it sat
  directly above mbedtls's own stack use during a TLS write.
* A failure to bring the BLE stack up is now an error rather than something the
  relay continues past. The hub asks for the radio with WiFi connected and a TLS
  session already open, which is the tightest the heap ever gets — and much
  tighter on the ESP32 than on the C6.
* The staging buffer shrinks to fit a fragmented heap (down to 8 KiB) instead of
  failing the session outright.

If you see a reset on 0.30.1 or later, the serial log is the place to look:
`monitor_filters = esp32_exception_decoder` is already set in
`platformio.ini`, so a panic prints a decoded backtrace rather than raw hex.


## Why this is a 30-pin ESP32 problem

A field report on 0.30.4 had a hub fail every recording with

```
[HEAP] audio-start: free=91296 largest=63476 min_ever=28084
E NimBLEDevice: esp_nimble_hci_init() failed; err=257
[HI-AUD] BLE stack would not start ... (free heap 16284, largest block 13812)
```

`err=257` is `ESP_ERR_NO_MEM`. The same firmware and the same hive worked on an
ESP32-C6. Four things differ, and only the last is about the silicon:

* **The classic board compiles a sensor path the C6 does not.** `config.h`
  forces `ENABLE_INMP441_MICS` off on the C6. On `esp32dev` every cycle runs a
  2048-point FFT in double precision — `vReal` and `vImag` are 16 kB each — plus
  two 8 kB sample buffers. They are all freed, so they cost no *space*; what
  they leave behind is **fragmentation**, which is why `audio-start` above shows
  91 kB free but only 63 kB in one piece.
* **The mic's I2S channel used to stay resident all cycle.** It was released in
  the deep-sleep prep, which is why `[INMP441] I2S uninstalled` printed *after*
  `[SLEEP]`. That is 8 kB of DMA-capable internal RAM held through the TLS
  handshake and the BLE init. Since 0.30.6 it is released as soon as the
  samples are read.
* **`esp32dev` asked for a bigger NimBLE mbuf pool.** `platformio.ini` set
  `CONFIG_BT_NIMBLE_MSYS1_BLOCK_COUNT=48` there where the C6 takes 24 from its
  own sdkconfig. The irony was in the reason: the pool was raised *because* the
  classic ESP32 is dual-core and its NimBLE host task competes with the WiFi
  driver for core 0. The property that forced the larger pool onto that board is
  the one that made it hardest to afford. 0.30.7 lowers it to 24.
* **Less usable DRAM, and a bigger controller.** The classic ESP32's SRAM is
  split with a fixed IRAM/DRAM boundary, and the shipped framework builds its
  controller in dual mode (`CONFIG_BTDM_CTRL_MODE_BTDM=y`) rather than BLE-only,
  so it is the larger variant even though NimBLE releases the Classic BT static
  memory at init.

### The ordering trap, and why the socket goes first

0.30.6 also reordered the relay to ask for the radio *before* opening the TLS
socket, reasoning that the BT controller is the one allocation here that can
neither shrink nor be retried, so it should get first refusal. **That was
wrong, and the field disproved it in a single cycle.** The radio started and
the handshake underneath it did not:

```
TLS connect to the backend failed        1.28 s from claimed_at to completed_at
```

against a 15-second connect timeout. An allocation had failed before anything
touched the network — and the same cycle went on to complete a TLS handshake to
the same host, to report that very failure, once the radio had been released.

The mis-ranking was about *which* constraint binds. The controller needs a large
total in many pieces. mbedtls needs **contiguity**, and a lot of it: this
framework ships `CONFIG_MBEDTLS_SSL_MAX_CONTENT_LEN=16384` with
`CONFIG_MBEDTLS_ASYMMETRIC_CONTENT_LEN` unset, so a session wants **two 16 kB
contiguous record buffers** and neither may be the small one. Contiguity is the
first thing fragmentation destroys, so whichever of the two runs second,
mbedtls suffers far worse for it. The 0.30.4 log had already said so: once the
controller took its share, `largest` collapsed to 13812 — under one record
buffer.

So the fragile allocation goes first, the robust one takes what is left, and
the ring — the only one of the three that can shrink and still produce a
recording — goes last. Getting all three to fit is a matter of lowering the
peak, not of reordering it. **Do not re-reorder this without a field log
showing the handshake surviving underneath a live controller.**

That log is now possible: 0.30.7 prints

```
[HEAP] audio-radio-up: free=… largest=… min_ever=…
```

with both the TLS session and the controller resident, and puts the free and
largest-block figures on the `TLS connect to the backend failed` line too. The
0.30.6 failure had to be diagnosed from `device_commands` timestamps in the
backend, because neither number was printed anywhere.

If you need to diagnose one of these without a serial cable, that query is
worth keeping:

```sql
SELECT id, claimed_at, completed_at,
       completed_at - claimed_at AS relay_duration,
       result->>'message' AS message
FROM device_commands
WHERE command_type = 'record_audio'
ORDER BY id DESC LIMIT 5;
```

A sub-second duration on a connect failure means an allocation that never
reached the network. Something near the 15-second timeout means a link or a
handshake problem instead. The two want completely different fixes.

### A failed BLE init no longer strands its memory

`NimBLEDevice::init()` brings the IDF controller up before it reaches the host,
so a failure at the host layer returned false with the controller still holding
its allocation — and nothing unwound it, because `NimBLEDevice::deinit()` guards
on a flag the failed init never set. In the 0.30.4 log that cost ~20 kB for the
rest of the cycle (91 kB at `audio-start`, 71 kB at `cycle-end` with everything
torn down), and the minimum free heap for the cycle was **1456 bytes** — the
command-result POST and the OTA check that follow each open a fresh TLS session
on that. The failed recording was the harmless half of it. Since 0.30.6
`blestack::acquire()` unwinds the controller by hand when init fails.

## When a recording comes back with seams

A hub on 0.30.1 recorded 27.3 s of a 30 s request with `Dropped: 0 B` and
`Gaps: 386`, and a 60 s request came back as 57.9 s with 313. Those numbers say
something quite specific, and they are worth reading through because the same
shape will come up again.

**`Dropped: 0 B` clears the radio of the obvious charge.** That counter is the
node's own ring overflowing, which is what happens when the BLE link cannot
carry 32 kB/s. At zero, the node was never blocked — it handed every byte to its
controller. So the audio went missing *after* it left the hive.

**The gap count matched the missing audio almost packet for packet.** 2.7 s of
16 kHz PCM is 86 kB, which is 360 notifications of 240 B; the hub reported 386
gaps. The 60 s recording was short by 2.1 s — 280 notifications — against 313
gaps. Roughly one gap per lost packet is the signature of *scattered single
packet* loss, not a few long stalls.

Two things on the hub can do that, and 0.30.2 addresses both:

* **NimBLE's receive pool was ~90 ms deep.** Every inbound notification is
  staged in NimBLE's `msys_1` mbuf pool, which defaulted to 12 blocks of 256 B —
  twelve notifications, about a ninth of a second. On the classic ESP32 the
  NimBLE host task shares core 0 with the WiFi driver, which is busy precisely
  because the same session is uploading over TLS. Whenever the host task was
  held off longer than that, the pool emptied and the controller dropped the
  next packet before any HiveHub code saw it. `platformio.ini` raised this to 48
  blocks (~370 ms) on `esp32dev` in 0.30.2, and **0.30.7 lowered it again to 24
  (~185 ms)** to buy back ~6 kB for the memory problem above — seams beat no
  recording. 24 is still twice NimBLE's own default, and the other two causes of
  the 0.30.2 seams (below) were fixed in code, not by buffering. The C6 gets 24
  from the IDF and is single-core, so its host task is not competing the same
  way.
* **Each HTTP chunk cost three TLS records.** Every `NetworkClientSecure::write()`
  is one `mbedtls_ssl_write()`, so writing the hex length line, the payload and
  the trailing CRLF separately spent three records — three lots of framing and
  their TCP segments — per chunk, two of them carrying no audio. The upload
  buffer now reserves room for the framing either side of the payload and the
  whole chunk goes out in one write.

Also removed: a `heap_caps_check_integrity()` call that 0.30.1 made right after
the BLE session opened. Walking every block in the heap is a fine thing to do at
a stage boundary and a poor thing to do while a node is streaming 32 kB/s into a
one-second ring; it is a cheap `logDiag()` now.


## Troubleshooting

| Symptom | Cause |
|---|---|
| "no HiveInside audio key configured on this hub" | `HIVEINSIDE_AUDIO_PSK_HEX` is missing, empty, not 64 hex characters, or all zeros in `secrets.h` — see [Generate the key](#1-generate-the-key) |
| "authentication failed" | The two keys differ, or the node has none. Usually one side was flashed and the other not; measurements keep working, which is what makes this look like a radio fault rather than a key one |
| "audio service not found" | The node is running firmware older than 0.6.0 |
| "HiveInside not found in scan" | Out of range, flat battery, or busy with an OTA |
| Stuck at "Waiting for the hub to wake" | Normal for up to one reporting interval; longer means the hub is offline |
| A recording stays at "requested" forever | The command never reached the queue. Check the server log for `could not queue record_audio`; the row is marked failed with the reason from 0.30.0 on |
| A recording sits at "streaming" | Neither the command result nor `/finalize` arrived — a reset mid-session, or a hub that went offline before its next wake. The audio is on disk and plays anyway; after five minutes the row resolves to ready with `confirmation: unknown`. `SELECT result->>'message' FROM device_commands WHERE command_type = 'record_audio' ORDER BY id DESC LIMIT 1;` is the hub's own account of what happened |
| Audio is loud for a moment then near-silence | The microphone lost power mid-session. On HiveInside before the sensor-rail fix, a measurement cycle already running when the session started would switch LDO1 off underneath it |
| "node busy" | An OTA or another session holds the node's single connection |
| Recording marked incomplete | See the quality flags above |
| Gaps, with `Dropped` at 0 | The link kept up but audio was lost after it left the node. Fixed in 0.30.2 — see [When a recording comes back with seams](#when-a-recording-comes-back-with-seams) |
| "hub reset during relay (panic/exception)" | A crash mid-session. Fixed in hub firmware 0.30.1 — see [When the hub resets mid-recording](#when-the-hub-resets-mid-recording) |
| "BLE stack would not start" | The hub could not bring the radio up at all. Almost always a 30-pin ESP32 with an unusually full heap; the serial log prints the free heap and the largest free block next to it. Hub firmware 0.30.7 makes this much less likely — see [Why this is a 30-pin ESP32 problem](#why-this-is-a-30-pin-esp32-problem) |
| "TLS connect to the backend failed" | The hub could not open the upload socket. Since 0.30.7 the serial log prints free heap and largest block beside it; a sub-second `relay_duration` in `device_commands` means an allocation failure rather than a network one. This was the 0.30.6 symptom of asking for the radio before the socket |
| "audio buffer trimmed to N B" (serial log, not an error) | The hub could not get the full 32 KiB staging buffer and took a smaller one. The recording still happens; a slow upload will show up as gaps sooner |
