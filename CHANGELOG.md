# Changelog

All notable changes to this integration are documented here. Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.5.0] - 2026-09-05

### Fixed

- **The "Battery replaced" button stayed armed (`replace_active: true`) after its window had expired, and logged an error while doing so.** The timer that refreshes the entities at the end of the replacement window was scheduled with a bare `lambda`, which Home Assistant classifies as `HassJobType.Executor` and therefore runs on a worker thread — where the entity refresh (`async_write_ha_state()`) is not allowed. Every button press produced an error in the log 121 s later, and the attribute only corrected itself with the next received packet. The timer target is now a proper `@callback`, is no longer stacked when the button is pressed again, and is cancelled on unload.
- **Deleting a sensor device by hand removed it for good.** `async_remove_config_entry_device()` dropped the device and its entities from the registries but left the sensor in the coordinator's internal `_discovered` table with all its channels — so the next radio packet found nothing "new" to discover and the entities were never recreated, contrary to the documented "reappears if it sends another packet" behaviour. The internal state is now cleaned up as well (the same cleanup the automatic stale-sensor removal already did).
- **The bridge device could be deleted too**, taking the connected / radio-silence / stick-reset / debug entities of the config entry with it and leaving every sensor's `via_device` dangling until a reload. Only per-sensor devices are removable now.
- **The outlier confirmation never checked the value it was confirming.** `outlier_confirm_count` counted *any* consecutive out-of-range reading, so N wildly different garbage values confirmed each other just as readily as a real new level — while the log message and the option text promised "the same value". Confirmation now requires the readings to be consistent with each other (within the same delta limit), which rejects random decode garbage but still accepts a genuine jump (sensor moved, long dropout). Deliberately not a strict equality check: real readings fluctuate by 0.1 °C, which would leave a sensor stuck on its old value forever. Option texts and log message updated accordingly.
- **A restored value could overwrite a live reading right after a restart.** `async_added_to_hass()` checked "no live value yet", then awaited the recorder — and the serial reader thread keeps delivering packets during that await. The stale restored value then replaced the fresh one *and* the outlier cache, so the next few real packets were rejected as outliers. The check is now repeated after the await.
- **Stray sensors were registered as "known" even with auto-discovery switched off.** A single packet from a neighbour's sensor called `_discovered.setdefault()` regardless of the `auto_add_entities` setting, which produced battery-low notifications and "removed automatically" cleanup log entries for sensors that had no entities at all, and blocked the battery-replacement alias for a real sensor whose new radio ID happened to match. With auto-discovery off, `_discovered` is no longer touched at all.
- **On an options-triggered reload the old serial reader thread could still hold the port.** `async_stop()` waited a fixed 5 s for the thread, but a `readline()` blocks for up to the configured `serial_timeout` (up to 10 s). The join now waits `serial_timeout + 3` s, and the reader loop closes the serial device explicitly (`try/finally`) instead of leaving it to pyserial's garbage collection.
- Notifications fell back to **German for every non-English** Home Assistant system language; English is the fallback now, German only for German installations.
- Debug mode left the logger pinned at DEBUG after an options-triggered reload — the auto-off timer was cancelled without restoring the level.

### Added

- The initial setup step now **tests the serial port before creating the entry**. A typo'd path previously produced a working-looking entry that just logged `Serial error: …` every few seconds forever.
- `async_remove_entry()` deletes the entry's `lacrosse_jeelink_<entry_id>_aliases` store file when the integration is removed, instead of orphaning it in `.storage` forever.

### Changed

- The setup description of auto-discovery no longer claims sensors are added at the *first* packet — since 1.3.0 the discovery threshold (default 2 packets within 120 s) applies.

## [1.4.5] - 2026-09-05

### Fixed

- **The options dialog could not be saved at all without a Telegram entity.** Every save failed with `Entity is neither a valid entity ID nor a valid UUID` on the "Telegram entity for messages" field, no matter which setting was actually being changed ([#3](https://github.com/Lu-Fi/ha-lacrosse-jeelink/issues/3)). The field was declared `vol.Optional(..., default="")`: the frontend drops an empty picker from the submission, voluptuous then filled the `""` default back in, and the `EntitySelector` rejected it as an invalid entity ID. Affected everyone not using Telegram, since the initial release. The field now uses `suggested_value` instead of `default`, so an empty picker is simply left out of the saved options (= no notifications, as before).

## [1.4.4] - 2026-08-30

### Fixed

- **A cosmetic `Serial error: Event loop is closed` was logged at ERROR level during some HA shutdowns/reloads.** The serial reader thread could outrace the closing event loop while scheduling a callback (e.g. via `_set_connected`), and its own exception handler then tried to schedule another callback on the already-closed loop, propagating the error. All thread-to-loop handoffs now go through a small `_call_soon_threadsafe()` wrapper that silently drops the call if the loop is already closed — harmless, since the coordinator is being torn down anyway.

## [1.4.3] - 2026-08-21

### Fixed

- **EMT7110/LevelSender entities had no name in the UI.** The `voltage`, `current`, `power`, `energy`, `level` sensor translation keys and the `consumer_connected` binary sensor key (added in 1.4.0) existed only in `strings.json`, not in `translations/en.json`/`translations/de.json`. With `_attr_has_entity_name = True` that produced a blank entity name for all six. Both translation files now carry the same keys as `strings.json`.
- **The notify-entity picker offered any `notify.*` entity, but only Telegram ever worked.** `_notify_user()` calls `telegram_bot.send_message`, and core rejects that service's `entity_id` for anything outside the `telegram_bot` integration — picking e.g. a mobile-app notify entity silently failed (swallowed by this method's own warning-only error handling). The selector is now scoped to `integration: telegram_bot`, and the option text/README no longer imply broader `notify.*` support.
- Raised the `hacs.json` Home Assistant floor from `2024.6.0` to `2026.3.0` — the shipped `custom_components/lacrosse_jeelink/brand/` icons only load on 2026.3+ (as already noted in the 1.1.0 release), so the declared floor was never actually installable as low as it claimed.

### Changed

- `_device_label()` (used to name a sensor in battery-low/battery-replaced notifications) is called from the serial reader thread but read the device registry directly, unlike every other cross-thread interaction in this file. It now marshals the lookup through `asyncio.run_coroutine_threadsafe`, matching the rest of the file's loop-affinity handling. Low practical impact (it was a read), but worth doing right.

## [1.4.2] - 2026-08-03

### Fixed

- **Battery-low and battery-replaced notifications only named the sensor by its raw radio ID** (e.g. "LaCrosse Sensor 3: Batterie schwach"), forcing a manual lookup in the device registry to figure out which physical sensor that was. Both messages now include the device's user-assigned name when set (e.g. "LaCrosse Sensor 3 (Heizung Bad): Batterie schwach"). Newly-discovered-sensor messages are unaffected — there's no name to show yet at first discovery.
- **Notifications could fail silently.** `_notify_user()` called the generic `notify.send_message` action with `blocking=False`: Telegram sends with Markdown parsing by default, and message text here routinely contains hyphens/parentheses (e.g. "Funk-ID", "(Verbindung ...)") that Telegram's parser rejects with `BadRequest: can't parse entities` — a failure that `blocking=False` hid in a detached background task, invisible to this method's own error handling. Now calls `telegram_bot.send_message` with `parse_mode: plain_text` and `blocking=True`, so a real delivery failure is at least logged instead of vanishing.

## [1.4.1] - 2026-07-08

### Fixed

- **Manual device removal was impossible.** Home Assistant refused to delete any device belonging to this integration ("Config entry does not support device removal"), including harmless empty device entries left behind after a sensor's entities were removed by hand. Added `async_remove_config_entry_device()` — safe for all devices here since every per-sensor device (LaCrosse, EMT7110, LevelSender) is dynamically discovered from radio packets and simply reappears if the sensor sends another packet.

## [1.4.0] - 2026-07-08

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
