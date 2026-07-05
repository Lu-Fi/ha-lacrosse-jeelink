# LaCrosse JeeLink Bridge

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/release/Lu-Fi/ha-lacrosse-jeelink.svg)](https://github.com/Lu-Fi/ha-lacrosse-jeelink/releases)

Home Assistant custom integration for **LaCrosse / TX35 / IT+ temperature and humidity sensors** received via a **JeeLink USB stick** (868 MHz, LaCrosse firmware) — fully local, no cloud, no FHEM required. The protocol handling is modelled on FHEM's proven `36_LaCrosse.pm`.

## Features

- Direct serial connection to the JeeLink stick (57600 baud, `OK 9` telegrams)
- **Automatic sensor discovery**: new sensors appear as devices with temperature, humidity (if present), calculated **dew point** (Magnus formula, identical to FHEM) and a battery-low binary sensor
- Second temperature channel (`temperature2`) for sensors with an external probe
- **Outlier filtering**: absolute limits (−50…60 °C, 1…100 %) plus delta filters (10 K / 20 % jumps); a genuine jump is accepted after N consecutive identical readings (configurable)
- **Battery replacement mode** (like FHEM's `replaceBatteryForSec`): press the per-sensor button, swap the battery within the configured window, and the sensor's new random radio ID is mapped onto the existing device — entities and history are preserved
- Values are **restored after a restart** (RestoreSensor) until fresh packets arrive
- Robust serial handling: DTR hardware reset, automatic recovery from known firmware hangs (`drecvintr exit`, `RFM12 hang`), automatic reconnect with configurable delay
- **Optional notifications** via any `notify` entity (Telegram, mobile app, …): connection lost/restored, new sensor discovered, battery low, battery replacement detected
- Debug switch with automatic timeout for troubleshooting reception issues
- Config UI in German and English; ships its own brand icon

## Hardware

Any board running the **LaCrosseITPlusReader sketch** (the firmware FHEM uses) works:

- **JeeLink v3 / v3c USB stick** — sticks sold as "FHEM JeeLink LaCrosse" are ready to go.
- **DIY Arduino clone** (e.g. Nano with CH340 + RFM69/RFM12 module) flashed with the same [LaCrosse firmware](https://svn.fhem.de/trac/browser/trunk/fhem/contrib/arduino) — this is what the integration was developed and tested on.

The serial handling covers both: 57600 baud (FHEM's default for this firmware), raw mode as a CH340 safety net, and a DTR reset that works on genuine JeeLinks (FTDI) and clones alike, since both wire DTR to the MCU reset for sketch uploads. Firmware quirks are handled identically to FHEM's `36_JeeLink.pm`: `drecvintr exit` and `RFM12 hang` trigger an automatic reset.

Sensors: LaCrosse/Technoline TX25, TX27, TX29 (IT+), TX35, TX37 and compatibles (30.3143, 30.3144, 30.3155, 30.3156, …).

On connect the integration sends the firmware commands `7m` (data-rate toggle mask 7 = cycle all three rates: 17.241 / 9.579 / 8.842 kbps) and `10t` (toggle every 10 s), so mixed sensor generations are received without manual rate tuning — equivalent to FHEM's `initCommands` attribute.

## Installation

### Via HACS (recommended)

1. HACS → three-dot menu → **Custom repositories** → add
   ```
   https://github.com/Lu-Fi/ha-lacrosse-jeelink
   ```
   with category **Integration**
2. Search for **"LaCrosse JeeLink"**, download, restart Home Assistant
3. **Settings → Devices & Services → Add Integration** → "LaCrosse JeeLink Bridge"

### Manual

Copy `custom_components/lacrosse_jeelink/` into your `config/custom_components/` directory and restart Home Assistant.

## Configuration

### Setup

| Setting | Description |
|---|---|
| Serial port | The JeeLink's serial device. The dropdown lists detected ports; you can also type a path manually — a stable `/dev/serial/by-id/…` symlink is recommended over `/dev/ttyUSBx`. |
| Auto-discover sensors | When on (default), every sensor whose packet is received creates its entities automatically. Turn off once all your sensors are known to ignore neighbours' sensors. |
| Outlier confirmation count | How many times the same outlier value must arrive consecutively before it is accepted as real (2–20, default 5). |

### Options (gear icon → "Configure", applied immediately via reload)

| Setting | Default | Description |
|---|---|---|
| Serial port / Auto-discover / Outlier confirmation | — | Same as setup, changeable later. |
| Serial read timeout (s) | `1` | Blocking read timeout on the port — only change for troubleshooting. |
| Reconnect delay (s) | `5` | Wait time before reconnecting after a serial error (1–600 s). |
| Auto-remove silent sensors after (h) | `0` (off) | Automatically removes auto-discovered sensors that haven't sent data for this long — e.g. a neighbour's sensor that briefly reached your receiver. Only sensors you have never touched are removed: renaming the device or any of its entities, assigning an area or adding labels/aliases all mark the sensor as *yours* and protect it permanently — a known sensor with an empty battery keeps its entities and its battery-replaced button. Checked every 15 minutes. |
| Battery replacement window (s) | `120` | How long the battery-replacement mode stays armed after pressing the button. |
| Debug mode auto-off (s) | `300` | Verbose logging switches itself off after this time. |
| Radio-silence warning after (min) | `15` | Warn when no radio packet has been parsed for this long even though the serial connection is up (silent firmware hang, antenna problem). A recovery message follows when data resumes. `0` disables the watchdog. |
| Send notifications | on | Master switch for all notifications. |
| Notify entity | empty | Target `notify.*` entity (Telegram, mobile app, …). Empty = no messages, regardless of the switches. |
| Notify: connection / radio silence / new sensor / battery low / battery replaced | all on | Each notification type can be enabled/disabled individually (in addition to the master switch). |

## Entities

### Bridge device ("LaCrosse JeeLink Bridge")

| Entity | Description |
|---|---|
| Connected (`binary_sensor`, diagnostic) | Serial connection to the JeeLink stick is up. |
| Radio silence (`binary_sensor`, problem, diagnostic) | On while the radio-silence watchdog is triggered (no packet parsed for the configured time despite an open connection). Use it in automations, e.g. to press the stick-reset button after prolonged silence. |
| Stick reset (`button`) | Performs a DTR hardware reset of the stick (also done automatically on known firmware hangs). |
| Debug mode (`switch`) | Enables verbose logging of every received telegram and every filter decision; auto-off after the configured timeout. |

### Per sensor (one device per radio ID, linked to the bridge)

| Entity | Description |
|---|---|
| Temperature (`sensor`, °C) | Main temperature channel. |
| Temperature2 (`sensor`, °C) | Second channel, only for sensors with an external probe. |
| Humidity (`sensor`, %) | Only created if the sensor actually reports humidity. |
| Dew point (`sensor`, °C) | Calculated from temperature + humidity (Magnus formula, same coefficients as FHEM). |
| Battery (`binary_sensor`, diagnostic) | On = battery low (bit 7 of the humidity byte). |
| Last received (`sensor`, timestamp, diagnostic) | When the last radio packet of this sensor was received — counts every parsed packet, even ones rejected by the outlier filter. Quantized to full minutes to keep database writes low. Useful for detecting dead sensors (empty battery, out of range) in automations. |
| Battery replaced (`button`) | Arms the replacement mode: swap the battery within the window and the new radio ID is aliased to this device. The button's `replace_active` attribute shows whether the window is currently open. |

## Notifications

If a notify entity is configured (and the master switch is on), the integration sends messages for: **connection lost / restored** (serial errors), **radio silence / data resumed** (no packets parsed for the configured time despite an open serial connection), **new sensor discovered** (with first readings), **battery low** (once per low-phase per sensor), and **battery replacement detected** (old ID → new radio ID). Every type can be switched on/off individually in the options. Message language follows the Home Assistant system language (German/English).

## Protocol notes (OK 9 telegram)

```
OK 9 <id> <flags> <tempMSB> <tempLSB> <hum>
        flags: bit7 = new battery, bits4-6 = type, bits0-3 = channel (2 = probe2)
        temp  = (MSB*256 + LSB − 1000) / 10 °C
        hum   : bit7 = battery low, bits0-6 = humidity (1–100; >100 = no humidity sensor)
```

## Troubleshooting

- **No data:** check that the stick runs the LaCrosse firmware (not RFM69/JeeLink classic sketches) and that no other process (FHEM, ser2net) holds the port — only one consumer at a time.
- **Sensor values jump:** increase the outlier confirmation count.
- **Reception debugging:** turn on the debug switch and watch the log — every raw line and filter decision is logged for the configured time.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## Disclaimer

Community project, not affiliated with LaCrosse Technology or JeeLabs. Use at your own risk.
