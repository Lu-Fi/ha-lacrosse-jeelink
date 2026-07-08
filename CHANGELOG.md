# Changelog

All notable changes to this integration are documented here. Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **EMT7110 support** (LaCrosse power/energy plug): decodes `OK EMT7110 …` telegrams, which the LaCrosseITPlusReader sketch already emits over the same serial port. New per-device sensors: voltage, current, power, accumulated energy, and a "consumer connected" binary sensor. Own device (`LaCrosse EMT7110 <id>`) and own ID namespace (`emt_<id>`) so it never collides with a LaCrosse IT+ radio ID and never shows up mislabelled as a weather sensor.
- **LevelSender support** (DIY tank/cistern fill-level sender): decodes `OK LS …` telegrams. New per-device sensors: fill level, temperature (with the same outlier filter as LaCrosse sensors), and battery voltage. Own device (`LevelSender <id>`, manufacturer "DIY / LevelSender") and own ID namespace (`ls_<id>`) — the sender's 4-bit ID would otherwise be indistinguishable from a LaCrosse radio ID.
- Both protocols share the existing discovery threshold, radio-silence watchdog, entity-registry preload (works after a restart), and stale-sensor auto-cleanup with the LaCrosse IT+ sensors — no separate configuration needed.

## [1.3.0] - 2026-07-08

### Added

- **Discovery threshold** (like FHEM's `autoCreateThreshold`): a brand-new sensor is only created after N packets within T seconds (options `discovery_min_packets` / `discovery_window_sec`, default **2 packets / 120 s**). Real IT+ sensors transmit every 4–8 seconds and pass the threshold within seconds; one-shot decode flukes and fringe receptions from neighbours never create registry entries (and no "new sensor" notifications) in the first place. Set the packet count to 1 to restore the previous create-immediately behaviour. Battery-replacement aliases are not affected.

## [1.2.1] - 2026-07-08

### Fixed

- **Automatic cleanup of stray sensors never removed anything** on recent Home Assistant versions. Since HA's aliases-v2 migration, every entity's alias list contains an internal sentinel (`ComputedNameType._singleton`), which made the "user has customised this entity" protection check truthy for *all* entities — every stale sensor was treated as adopted and kept forever. The check now only counts real, non-empty string aliases.
- The cleanup now logs its skip reasons (not stale, no timestamp, device/entity customised — including which entity and which attribute) at debug level, so a silent non-removal is diagnosable.

## [1.2.0] - 2026-07-05

### Added

- **Configurable firmware init commands** (option `init_commands`, default `7m 10t`) — the space-separated commands sent to the sketch on every connect and after firmware-hang resets, equivalent to FHEM's `initCommands` attribute. Users with a single sensor generation can pin a fixed data rate (e.g. `0m 17241r`) to save sensor battery.

### Changed

- **Code base fully translated to English** (comments, docstrings, log messages) for public contributions; behaviour unchanged.
- Localisation audit: all config/options labels and descriptions exist in German and English, entity name keys are identical across both languages, and every notification (Telegram & co.) is sent in the Home Assistant system language (DE/EN pairs verified).

## [1.1.2] - 2026-07-05

### Added

- **Stick identification / firmware version**: the integration now requests the firmware banner on connect (`v` command, also emitted after every reset) and parses the `[LaCrosseITPlusReader…]` line — the same mechanism FHEM uses for its model/settings internals. The result (firmware name/version, radio module, frequency, data rate) is shown as the bridge device's **firmware version** and as a `firmware` attribute on the "Connected" sensor.

## [1.1.1] - 2026-07-05

### Fixed

- **Startup race left random sensors unavailable after a restart.** The serial reader thread was started before the entity platforms were set up. If a sensor's packet arrived in that window, its channels were marked as discovered while the discovery events fell on deaf ears (no callbacks registered yet) — the registry preload then skipped them as "already known", leaving all of that sensor's entities (including the battery-replaced button) unavailable until the next restart. The reader now starts only after platform setup and registry preload are complete.

## [1.1.0] - 2026-07-05

First public release (HACS custom repository).

### Added

- **Configurable timeouts** via the Options dialog, applied immediately through an automatic reload: serial read timeout, reconnect delay, battery replacement window, debug auto-off.
- **Optional notifications** via any `notify` entity (Telegram, mobile app, …): connection lost/restored, new sensor discovered (with first readings), battery low (once per low-phase), battery replacement detected. Master switch + target entity in the Options dialog; message language follows the HA system language (DE/EN).
- New diagnostic binary sensor **"Connected"** on the bridge device showing the serial connection state.
- New diagnostic timestamp sensor **"Last received"** per sensor device: when the last radio packet arrived (counts every parsed packet, even filter-rejected ones; minute resolution to keep database writes low). Restored across restarts.
- **Radio-silence watchdog**: warns when no radio packet has been parsed for a configurable time (default 15 min, 0 = off) even though the serial connection is up — catches silent firmware hangs and antenna problems that the connection-loss message can't see. Sends a recovery message when data resumes. The watchdog state is also exposed as a **"Radio silence" problem binary sensor** on the bridge device for use in automations (e.g. auto-pressing the stick-reset button).
- **Per-type notification switches**: connection lost/restored, radio silence, new sensor, battery low, and battery replacement can each be enabled/disabled individually, in addition to the master switch.
- **Automatic cleanup of stray sensors** (option, default off): auto-discovered sensors that haven't sent data for a configurable number of hours are removed automatically — but only if the user has never touched them. Renaming the device or any entity, assigning an area, or adding labels/aliases protects a sensor permanently, so a known sensor with an empty battery keeps its entities and battery-replaced button. Checked every 15 minutes; internal state (aliases, caches) is cleaned up along with the registry entries.
- Reconnect delay range extended to 1–600 s.
- Brand icon shipped with the integration (supported natively since Home Assistant 2026.3).
- Localized integration name (DE/EN), README, changelog, HACS metadata, CI validation (hassfest + HACS action) and release workflow.

### Fixed

- **Battery-replacement flow survives restarts and dead sensors.** Previously, after a Home Assistant restart only the temperature/humidity entities of known sensors were preloaded from the entity registry — the battery sensor and, crucially, the "Battery replaced" button of a sensor that no longer transmits (empty battery!) stayed unavailable until a packet arrived, which for a dead sensor never happens. All entities of known sensors are now reconstructed centrally right after startup, so the button is always available when you need it.
- The radio-ID alias created by a battery replacement is now **persisted** (`.storage/`); previously it lived only in memory, and a restart after a battery swap would have re-created the sensor as a new device under its new radio ID.
- The button's `replace_active` attribute now resets when the replacement window expires, instead of showing "true" until the next received packet.

### Changed

- The serial port no longer has a hardcoded, adapter-specific default — the setup dropdown lists the detected ports instead (manual entry still possible for `/dev/serial/by-id/…` symlinks).
- Options changes now take effect immediately (entry reload) instead of requiring a Home Assistant restart.
- `stty` port initialisation is now best-effort (Linux nicety) instead of a hard requirement — the integration also works on systems without `stty`; pyserial sets the parameters itself.
- The reconnect wait is interruptible, so unloading/reloading the integration no longer blocks for the full delay.
- Manifest cleanup for public distribution: documentation/issue-tracker URLs, code owners, logger declaration.

## [1.0.0] - 2026-06

Initial private version.

### Added

- Serial JeeLink reader (57600 baud, `OK 9` telegrams, protocol modelled on FHEM `36_LaCrosse.pm`) with DTR reset and automatic recovery from known firmware hangs (`drecvintr exit`, `RFM12 hang`).
- Automatic sensor discovery with per-sensor devices: temperature, optional second channel (probe2), humidity, calculated dew point (Magnus formula), battery-low binary sensor.
- Outlier filtering with absolute limits and delta thresholds; confirmation counter for genuine jumps.
- Battery replacement mode like FHEM's `replaceBatteryForSec`: per-sensor button, new radio ID is aliased onto the existing device.
- State restore after restart (RestoreSensor), debug switch with auto-off, DTR reset button.
