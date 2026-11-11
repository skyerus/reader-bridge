# Building and releasing Passage

This guide is for maintainers. Readers should start with [installing the Mac app](MAC-APP.md).

`desktop/release.json` owns the version and persistent bundle identifier. `desktop/resources.json` lists application resources explicitly. Preserve `com.readerbridge.desktop`, existing service labels, and `~/Library/Application Support/Reader Bridge` when changing branding or upgrading. The installed app path is also part of a running service's configuration.

## Local development package

Use a clean task worktree and a fresh output directory. The build machine needs macOS, Xcode command line tools, and Python 3.12 or newer. End users use the bundled runtime.

```sh
python3 -m unittest discover -s tests -v
swift test --package-path desktop
python3 scripts/build-macos.py --dmg --output dist/development
dist/development/Passage.app/Contents/Resources/runtime/bin/python3 -I -B \
  scripts/acceptance-macos.py dist/development/Passage.app \
  --report dist/development/automated-acceptance.json
```

The default signature is ad hoc. The disk image name ends in `-development.dmg` and its readme identifies it as unnotarized. Do not put it in consumer download instructions.

Automated acceptance uses explicit disposable app/service directories, a test-only home-directory lookup substitution, a restricted command PATH, reader fixtures, random ports, and unique test LaunchAgent labels. It runs the interpreter inside the app and verifies that real HTTP exchange, service restart, tokens, cover bytes, backup/restore, queue retention, stale-position rejection, and cleanup work without basic setup invoking developer commands. It verifies the app signature before and after. It does not use a real reader, archive, or existing service and never repurposes the user's HOME. The host OS and GUI session already exist; this is not a clean-OS, native first-open, physical device, or actual logout/login test.

## Include application firmware

Generate a bundle using `scripts/build-firmware-assets.py` against the exact firmware source pin. Install `pioarduino==6.1.19` in a private build environment and pass its `pio` executable with `--pio`. The builder installs resolved packages, pins the SDK's nested core to the same version, and creates the SdFat override. This matches the pinned firmware's release workflow and avoids the [PlatformIO 6.2 tool-removal bug](https://github.com/pioarduino/platform-espressif32/issues/529). The output includes a schema-2 `firmware.json`, application images under `desktop/firmware`, a build receipt, dependency licenses and notices under `desktop/licenses/firmware`, and `passage-firmware-source.tar.gz`.

X4 Pro and X4 Classic use the prebuilt PSRAM SDK. A previous custom SDK build in the same PlatformIO core can leave common libraries that override that variant: the firmware still boots, but has no PSRAM and may fail to generate book covers. Packaging now rejects these profiles unless their linker maps contain assigned PSRAM initialization and heap-registration symbols. If this check fails, rebuild the affected model with `--models xteink_x4_pro` (or `xteink_x4_classic`) and a new, dedicated `--platformio-core` directory. Confirm nonzero PSRAM on the physical reader, then verify a newly generated Home thumbnail and sleep cover; existing cached covers do not exercise the decoder.

Pass `--firmware-bundle PATH` when packaging. The app builder compares the injected registry with the release-owned profiles, verifies each image's SHA-256, size and ESP processor header, and checks the receipt, declared license hashes, and corresponding-source hash. The receipt must confirm a successful build using the pinned PlatformIO Core 6.1.19. Only declared files are copied. Binaries need not be committed to Git. A bundle cannot change hardware routing or source pins.

Firmware goes into `Contents/Resources/bridge/desktop/firmware`; dependency notices retain the matching relative paths. Only the build receipt and source download instructions go into `Contents/Resources/firmware-source`; the large source archive is never embedded in the app or disk image. The release pipeline copies the source archive directly from the verified firmware bundle into a separate release asset beside the disk image, checking it against the signed app’s source receipt before and after copying. Keep that archive and its checksum available with every distributed image. The inventory retains third-party terms; do not describe a top-level MIT license as clearing every dependency.

A source-only checkout with null prebuilt records can pair a reader already running compatible Passage firmware. Its consumer flow must report an unavailable image rather than installing Git/PlatformIO. Explicit `--developer-build` is reserved for the advanced command-line installer.

## Prepare a signed candidate

Use an installed **Developer ID Application** identity and an existing `notarytool` keychain profile. Apple Development, Apple Distribution, and ad-hoc identities do not satisfy this path. Credentials and private keys must remain outside repository files and logs. This script does not create a certificate or alter the keychain.

```sh
python3 scripts/release-macos.py \
  --output dist/candidate-arm64 \
  --sign-identity 'Developer ID Application: YOUR NAME (TEAMID)' \
  --notary-profile passage-notary \
  --firmware-bundle /path/to/verified-firmware-bundle
```

Add `--keychain PATH` for a dedicated existing signing/notarization keychain. Build each Mac architecture on a matching host. The source tree must be clean and committed before a candidate is built.

The pipeline signs nested native code inside out with the hardened runtime and secure timestamps, verifies the complete app, submits its ZIP to Apple, requires **Accepted**, and staples the ticket to the app. It then creates and signs the disk image, separately requires accepted notarization and a valid stapled ticket, and assesses both with Gatekeeper. It mounts the final image read-only, copies the actual app to a disposable Applications folder, adds a test quarantine attribute, reassesses it, and runs packaged acceptance on that installed copy. Gatekeeper policy is never disabled or bypassed. Apple describes these requirements in [notarizing macOS software](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution) and the [custom notarization workflow](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow).

On success, `release-candidate.json` records the exact commit, architecture, artifact digest, signing team, accepted submission IDs, automated results, and per-model firmware availability. It deliberately has `ready: false`. A failed or incomplete signing, notarization, assessment, or acceptance step cannot emit a distribution-ready manifest.

The manual **Prepare notarized Mac candidate** workflow performs this on the current default-branch commit, using the protected `macos-release` environment. It builds pinned firmware and uses a disposable runner keychain. Set environment variable `MACOS_SIGN_IDENTITY` and secrets `MACOS_CERTIFICATE_P12` (base64), `MACOS_CERTIFICATE_PASSWORD`, `NOTARY_KEY_P8`, `NOTARY_KEY_ID`, and `NOTARY_ISSUER_ID`. The workflow uploads candidate artifacts only and has no release-publishing permission. The normal Test workflow produces clearly labeled development artifacts on Apple Silicon and Intel [runner labels](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).

## Clean-Mac and reader acceptance

On a separate Mac or freshly installed macOS VM with no Python, GitHub CLI, Homebrew, Xcode or compiler installed, download the exact candidate disk image. Use the [new-user walkthrough](MAC-APP.md) without running commands. Record macOS version, architecture, date, and its disk image SHA-256. Do not reuse a populated developer HOME as clean-Mac evidence.

Check normal Gatekeeper opening, first-run permissions, one-reader completion, archive and cover delivery, offline/reconnect, occupied-port handling, backups and restore. Log out and back in to verify service startup and preserved data. In a separate disposable upgrade fixture, replace an older app at the same filename/path and confirm service/runtime recovery and retained tokens/queues. Never use a live user's installation for this test. With two accepted readers, test reading progress in both directions using the same EPUB. Repeat physical checks for each model claimed by release notes; a shared build environment is not a physical compatibility pass.

Copy `docs/release-acceptance.example.json` to a private acceptance report. Fill in observed results and short redacted evidence; the example is deliberately failing. Do not include pairing credentials, private account information, or personal quotes. Only tested profiles can be listed as accepted.

```sh
python3 scripts/release-macos.py \
  --finalize dist/candidate-arm64/release-candidate.json \
  --acceptance /path/to/completed-clean-mac-acceptance.json
```

Run finalization on a maintainer Mac using the downloaded candidate artifacts: keep its disk image, JSON evidence and firmware source archive together. A separate app bundle is not required. Finalization verifies the disk image's SHA-256, mounts it read-only, and copies its app into a disposable directory. It checks that app's signed metadata, signature and Gatekeeper assessment against the candidate, plus the physical acceptance report and corresponding firmware source. Only then does `release-manifest.json` contain `state: distribution-ready`, `ready: true`, and the accepted reader profiles. Neither stage publishes anything.

## Publish only the verified scope

A signed, notarized candidate may be published as a GitHub **Pre-release** while the separate clean-Mac and exact-artifact physical tests remain open. Publish its unchanged disk image/checksum, matching firmware source archive/checksum, and redacted `release-candidate.json` with `ready: false`. Name the tag as a release candidate and state the tested architecture, automated evidence, prior integration models, and outstanding acceptance work. A compiled profile must not be described as physically accepted. Never publish an ad-hoc development package through this path.

For stable promotion, retain the candidate evidence, accepted notary logs, clean-Mac report and final manifest. Publish the unchanged disk image, its final checksum, the matching firmware source archive/checksum, and the redacted distribution-ready release manifest. State which architecture, macOS versions and reader models were physically tested, and whether position sync passed. Keep candidate/development downloads distinct. Once assets are published, update the README and first-install guide's download links and independently check the public download and its digest.

Do not label a source ZIP, private upload, signed-only package, or isolated namespace test as a finished Mac release.

## Optional Book Inbox source add-on

Book Inbox has an independent version in `scripts/package-book-inbox.py`; it is
not part of the Mac app bundle or its version. After the source PR's Python,
Lua and Mac checks pass and it is merged, check out the exact merged default-
branch commit in a clean release worktree and run:

```sh
python3 scripts/package-book-inbox.py --output /path/to/fresh/release-output
```

The packager reads an explicit Git allowlist, refuses a dirty tree, and produces
a reproducible archive and checksum. It includes only the service, plugin,
synthetic checks, license and setup guide, with an internal commit/file-hash
manifest. Run the Python and Lua checks again from an extracted archive.
Publish the unchanged archive and checksum as a GitHub pre-release with a
`book-inbox-vVERSION` tag targeting that exact commit. State that manual setup
and separately installed prerequisites are required; this is an experimental
source add-on, not a signed or integrated Mac app release. Keep the existing
Mac-app download current, link the add-on from its setup guide and README, then
independently download the public archive and verify its checksum and manifest.
