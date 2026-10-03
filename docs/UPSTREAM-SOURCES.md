# Verified upstream references and publication notes

Core installation links rechecked 2026-10-02; the detailed progress, dictionary, and library references retain the 2026-09-29 check. Use primary installation guides and recheck the live jailbreak wizard immediately before modifying a device.

| Primary source | Supports |
| --- | --- |
| https://kindlemodding.org/jailbreaking/ | Read first; jailbreak method varies, live wizard, method-specific update prevention and post-jailbreak work. |
| https://kindlemodding.org/jailbreak-wizard.html | Exact model/variant/firmware gate. No universal Paperwhite jailbreak recommendation. |
| https://kindlemodding.org/jailbreaking/jailbreak-faq.html | If incompatible, wait; stock downgrading is not a general workaround. |
| https://github.com/koreader/koreader/wiki/Installation-on-Kindle-devices | Current KPM, manual/scriptlet, KUALA and legacy paths. KPM only when supported by that jailbreak; kindlehf >=5.16.3. Exit KOReader before USB storage. EPUB supported, DRM/KFX unsupported. |
| https://github.com/koreader/koreader/releases | Upstream packages; select device/firmware variant per guide, not merely newest filename. |
| https://github.com/koreader/koreader/wiki/Progress-sync | Auto sync on opening/closing documents; Binary matching requires exact same file; account shared across reader installations. |
| https://github.com/skyerus/crosspoint-reader/blob/e33bf7006e9081a149ca44e63f17686d817b50ee/USER_GUIDE.md | Default CrossPoint KOSync server, per-server accounts, Ask Every Time upload/apply, OPDS settings, SD updater. Pinned source revision used by this release. |
| https://sync.crosspointreader.com/ | Public KOReader-compatible progress service; separate from the Mac collector and personal archive. |
| https://github.com/crosspoint-reader/crosspoint-reader#install-firmware | Official CrossPoint installation instructions, hardware-specific images, and USB-locked device guidance. |
| https://crosspointreader.com/#flash-tools | Official web installer; explicitly select the exact model. Passage application images use the separate SD-card update instructions in Setup. |
| https://updates.crosspointreader.com/ | Official firmware status page, not the device installer. |
| https://github.com/janeczku/calibre-web | Native Calibre-Web application and documentation. Installer pins 0.6.27; first-run LAN exposure must wait for secure account setup. |

The custom highlighting behavior is implemented in the [public CrossPoint fork at the pinned revision](https://github.com/skyerus/crosspoint-reader/tree/e33bf7006e9081a149ca44e63f17686d817b50ee); it is not attributed to upstream released CrossPoint. Tests and compilation do not replace fresh device acceptance tests.

## Packaging boundaries

- Publish Reader Bridge's own source, clean examples, dependency pins, notices, and reproducible build instructions. Keep device IDs, tokens, actual accounts, private archive addresses, database contents, book names and local hostnames out of fixtures/docs.
- CrossPoint's top-level source LICENSE is MIT. KOReader identifies as AGPL and includes component notices; a top-level license does not clear every bundled dependency.
- The Mac candidate packages checksum-pinned custom CrossPoint application images, corresponding source, a build receipt, and dependency licenses/notices. Supply the matching source archive and checksum with every distributed image. Review the actual dependency inventory, including wolfSSL/ESP-IDF terms, rather than relying on the top-level source license. See [firmware packaging](RELEASING.md#include-application-firmware).
- Link to upstream jailbreak methods and KOReader packages rather than vendoring exploit bundles. Do not include Amazon-purchased books, proprietary Kindle dictionaries/fonts, account exports or keys.
- Public project source and a user's private quotes archive are separate repositories. Never default to the developer's archive.

## Maintainer acceptance gates

A clean Mac installation is not proven merely because an existing developer setup works. Test fresh setup and rerun/idempotency in a temporary app directory, service restart, unavailable GitHub/LAN, occupied ports, missing prerequisites, and preservation of tokens/queues/device identities.

On actual devices verify Kindle plugin startup, one quote per paired source, an offline quote later arriving, a deliberately disposable deletion staying deleted after replay, and progress in both directions using the same EPUB when claiming position sync. Test the selected model's touch or button highlight controls; for X4 Pro, test the edge gesture in all four orientations. State explicitly which physical checks were completed against the exact released artifact. Profiles and build success are separate evidence.
