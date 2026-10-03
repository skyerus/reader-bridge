#!/usr/bin/env python3
"""Assemble pinned firmware, notices and corresponding source for a Passage build."""
import argparse
import configparser
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_PROTOCOL_MARKERS = (b'/v1/highlights', b'/v1/covers', b'X-Book-Title', b'X-Book-Author')


def run(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def digest(path):
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(block)
    return checksum.hexdigest()


def tracked_files(source):
    """Read tracked files only, including initialized submodules, never local data."""
    names = run('git', '-C', source, 'ls-files', '-z', '--recurse-submodules', capture_output=True).stdout
    for name in names.split(b'\0'):
        if name:
            path = source / os.fsdecode(name)
            if path.is_file() and not path.is_symlink():
                yield path


def add_file(archive, path, name):
    info = archive.gettarinfo(str(path), arcname=name)
    info.uid = info.gid = info.mtime = 0
    info.uname = info.gname = ''
    with path.open('rb') as stream:
        archive.addfile(info, stream)


def macho_executable(path):
    """Recognize executable headers/load commands, including universal images.

    Magic bytes alone also occur in ordinary data and Java class files.
    """
    thin = {b'\xce\xfa\xed\xfe': ('<', 28), b'\xfe\xed\xfa\xce': ('>', 28),
            b'\xcf\xfa\xed\xfe': ('<', 32), b'\xfe\xed\xfa\xcf': ('>', 32)}
    fat = {b'\xca\xfe\xba\xbe': ('>', 20), b'\xbe\xba\xfe\xca': ('<', 20),
           b'\xca\xfe\xba\xbf': ('>', 32), b'\xbf\xba\xfe\xca': ('<', 32)}
    cpus = {7, 12, 18, 0x01000007, 0x0100000c, 0x01000012, 0x0200000c}
    with path.open('rb') as stream:
        length = path.stat().st_size
        def executable(offset, size, expected_cpu=None):
            stream.seek(offset)
            magic = stream.read(4)
            if magic not in thin:
                return False
            endian, header_size = thin[magic]
            header = magic + stream.read(header_size - 4)
            if len(header) != header_size or size < header_size:
                return False
            fields = struct.unpack(endian + 'I' * (header_size // 4), header)
            cpu, kind, commands, command_bytes = fields[1], fields[3], fields[4], fields[5]
            if (cpu not in cpus or expected_cpu not in (None, cpu) or kind != 2
                    or not 0 < commands <= 32768 or not commands * 8 <= command_bytes <= size - header_size):
                return False
            cursor, segment, entry = offset + header_size, False, False
            limit = cursor + command_bytes
            for _ in range(commands):
                stream.seek(cursor)
                data = stream.read(8)
                if len(data) != 8:
                    return False
                command, count = struct.unpack(endian + 'II', data)
                if count < 8 or count % 4 or cursor + count > limit:
                    return False
                segment |= command == 1 and count >= 56 or command == 0x19 and count >= 72
                entry |= command in (4, 5) and count >= 16 or command == 0x80000028 and count >= 24
                cursor += count
            return cursor == limit and segment and entry
        magic = stream.read(4)
        if magic in thin:
            return executable(0, length)
        if magic not in fat or length < 8:
            return False
        endian, entry_size = fat[magic]
        count = struct.unpack(endian + 'I', stream.read(4))[0]
        if not 0 < count <= 32 or 8 + count * entry_size > length:
            return False
        slices = []
        for _ in range(count):
            entry = struct.unpack(endian + ('IIQQII' if entry_size == 32 else 'IIIII'), stream.read(entry_size))
            cpu, _, offset, size, alignment = entry[:5]
            if (cpu not in cpus or offset < 8 + count * entry_size or size < 28
                    or offset + size > length or alignment > 31 or offset % (1 << alignment)):
                return False
            slices.append((offset, size, cpu))
        if any(left[0] + left[1] > right[0] for left, right in zip(sorted(slices), sorted(slices)[1:])):
            return False
        return all(executable(*entry) for entry in slices)


def source_bundle(source, environments, core, destination, notice_dir):
    notice_files = []
    seen_notices = set()
    def include(archive, path, name):
        add_file(archive, path, name)
        if re.match(r'^(licen[cs]e|copying|notice|copyright|ofl|ftl)', path.name, re.I):
            sha = digest(path)
            if sha not in seen_notices:
                target = notice_dir / 'licenses' / (sha[:12] + '-' + path.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                seen_notices.add(sha)
                notice_files.append({'source': name, 'file': target.relative_to(notice_dir).as_posix(), 'sha256': sha})
    with tarfile.open(destination, 'w:gz') as archive:
        for path in tracked_files(source):
            include(archive, path, 'source/' + path.relative_to(source).as_posix())
        override = source / 'platformio.local.ini'
        if override.is_file():
            add_file(archive, override, 'source/platformio.local.ini')
        platform = core / 'platforms/espressif32'
        if not platform.is_dir():
            raise ValueError('The resolved ESP32 platform build scripts are required')
        for path in sorted(platform.rglob('*')):
            if path.is_file() and not path.is_symlink() and '.git' not in path.parts and '__pycache__' not in path.parts:
                include(archive, path, 'platform-espressif32/' + path.relative_to(platform).as_posix())
        for environment in environments:
            dependencies = source / '.pio/libdeps' / environment
            if not dependencies.is_dir():
                raise ValueError(f'Missing resolved library sources for {environment}')
            for path in sorted(dependencies.rglob('*')):
                if not path.is_file() or path.is_symlink() or '.git' in path.parts:
                    continue
                relative = path.relative_to(dependencies)
                # JPEGDEC ships a compiled macOS jpeg_perf_test inside its
                # Linux examples. Keep corresponding source, build files and
                # notices, without shipping this unrelated host executable.
                if 'examples' in relative.parts[1:-1] and macho_executable(path):
                    continue
                include(archive, path, f'source/.pio/libdeps/{environment}/' + relative.as_posix())
        for package in ('framework-arduinoespressif32', 'framework-espidf', 'framework-arduinoespressif32-libs'):
            framework = core / 'packages' / package
            if not framework.is_dir():
                raise ValueError(f'{package} sources or provenance are required with the firmware bundle')
            for path in sorted(framework.rglob('*')):
                if not path.is_file() or path.is_symlink() or '.git' in path.parts or '__pycache__' in path.parts:
                    continue
                # ESP-IDF source is included above; retain vendor binary-package
                # provenance/notices without duplicating gigabytes of objects.
                if package.endswith('-libs') and not (re.match(r'^(licen[cs]e|copying|notice|copyright|ofl|ftl)', path.name, re.I) or path.name == 'package.json'):
                    continue
                include(archive, path, package + '/' + path.relative_to(framework).as_posix())
        managed = source / 'managed_components'
        if managed.is_dir():
            for path in sorted(managed.rglob('*')):
                if path.is_file() and not path.is_symlink() and '.git' not in path.parts:
                    include(archive, path, 'source/managed_components/' + path.relative_to(managed).as_posix())
    return notice_files


def prepare_toolchain(args, source, environments):
    override = source / 'platformio.local.ini'
    expected = '\n'.join('[env:' + environment + ']\nlib_deps =\n  ${base.lib_deps}\n  greiman/SdFat @ 2.3.1\n' for environment in environments)
    if override.exists():
        actual_config = configparser.ConfigParser(interpolation=None)
        expected_config = configparser.ConfigParser(interpolation=None)
        actual_config.read_string(override.read_text())
        expected_config.read_string(expected)
        normalize = lambda config: {section: dict(config[section]) for section in config.sections()}
        if normalize(actual_config) != normalize(expected_config):
            raise ValueError('Unrecognized local firmware override; use a fresh checkout')
    override.write_text(expected)
    core = args.platformio_core.resolve()
    environment = {**os.environ, 'PLATFORMIO_CORE_DIR': str(core)}
    command = [args.pio, 'pkg', 'install']
    for name in environments:
        command.extend(['-e', name])
    run(*command, cwd=source, env=environment)
    # The pinned SDK starts a nested PlatformIO process from its private penv.
    # Both cores must agree or the SDK deletes SCons while it is executing.
    python = core / 'penv/bin/python'
    if not python.is_file():
        raise ValueError('PlatformIO did not initialize its nested build environment')
    run(python, '-m', 'pip', 'install', 'pioarduino==6.1.19', env=environment)
    nested = run(core / 'penv/bin/pio', '--version', capture_output=True, text=True, env=environment).stdout.strip()
    if nested != 'PlatformIO Core, version 6.1.19':
        raise ValueError('Nested firmware build core does not match the pinned version')


def assemble(args):
    source = args.source.resolve()
    manifest = json.loads(args.manifest.read_text())
    if manifest.get('schema_version') != 2:
        raise ValueError('A version 2 device manifest is required')
    devices = manifest['devices']
    selected = args.models or list(devices)
    specs = [devices[model] for model in selected]
    for model, spec in zip(selected, specs):
        if (not re.fullmatch(r'[a-z][a-z0-9_]{1,63}', model) or spec.get('id') != model or
                not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', spec.get('environment', '')) or
                not re.fullmatch(r'[a-z0-9][a-z0-9._-]*\.bin', spec.get('filename', '')) or
                not re.fullmatch(r'[a-f0-9]{40}', spec.get('commit', ''))):
            raise ValueError('Invalid firmware target in manifest')
    commits = {spec['commit'] for spec in specs}
    if len(commits) != 1 or run('git', '-C', source, 'rev-parse', 'HEAD', capture_output=True, text=True).stdout.strip() not in commits:
        raise ValueError('Firmware checkout does not match every selected pinned source')
    if run('git', '-C', source, 'status', '--porcelain', '--untracked-files=no', capture_output=True, text=True).stdout.strip():
        raise ValueError('Firmware source must have no tracked modifications')
    submodules = run('git', '-C', source, 'submodule', 'status', '--recursive', capture_output=True, text=True).stdout
    if any(line.startswith(('-', '+', 'U')) for line in submodules.splitlines()):
        raise ValueError('Firmware submodules must match their pinned revisions')
    environments = sorted({spec['environment'] for spec in specs})
    version = run(args.pio, '--version', capture_output=True, text=True).stdout.strip()
    if version != 'PlatformIO Core, version 6.1.19':
        raise ValueError('Firmware builds require pinned PlatformIO 6.1.19')
    prepare_toolchain(args, source, environments)
    command = [args.pio, 'run', '-j', str(args.jobs)]
    for environment in environments:
        command.extend(['-e', environment])
    run(*command, cwd=source, env={**os.environ, 'PLATFORMIO_CORE_DIR': str(args.platformio_core.resolve())})
    if (run('git', '-C', source, 'rev-parse', 'HEAD', capture_output=True, text=True).stdout.strip() not in commits or
            run('git', '-C', source, 'status', '--porcelain', '--untracked-files=no', capture_output=True, text=True).stdout.strip() or
            run('git', '-C', source, 'submodule', 'status', '--recursive', capture_output=True, text=True).stdout != submodules):
        raise ValueError('Firmware source changed during the build; no release bundle was produced')
    output = args.output.resolve()
    if output.exists():
        raise ValueError('Choose a new output directory; existing release artifacts are preserved')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='passage-firmware-', dir=output.parent) as temporary:
        stage = Path(temporary)
        notice_dir = stage / 'desktop/licenses/firmware'
        notice_dir.mkdir(parents=True)
        receipt = {'schema_version': 1, 'source_commit': next(iter(commits)), 'submodules': submodules.splitlines(), 'environments': environments, 'platformio': version, 'build_succeeded': True, 'models': {}}
        for model, spec in zip(selected, specs):
            binary = source / '.pio/build' / spec['environment'] / 'firmware.bin'
            size = binary.stat().st_size
            with binary.open('rb') as stream:
                header = stream.read(24)
            if not 24 < size <= 32 * 1024 * 1024 or header[0] != 0xE9 or int.from_bytes(header[12:14], 'little') != spec['chip_id']:
                raise ValueError(f'Invalid application firmware for {model}')
            # A valid ESP32 image can still predate Passage's cover uploader.
            # Check the built image, not source files that may compile unused.
            image = binary.read_bytes()
            missing = [marker.decode('ascii') for marker in REQUIRED_PROTOCOL_MARKERS if marker not in image]
            if missing:
                raise ValueError(f'Firmware for {model} lacks required Passage protocol markers: {", ".join(missing)}')
            relative = Path('desktop/firmware') / spec['filename']
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(binary, target)
            artifact = {'model': model, 'environment': spec['environment'], 'commit': spec['commit'], 'sha256': digest(target), 'size': size,
                        'bundled_path': relative.as_posix(), 'license_notice': 'desktop/licenses/firmware/NOTICE.txt'}
            devices[model]['prebuilt'] = artifact
            receipt['models'][model] = copy.deepcopy(artifact)
        source_name = 'passage-firmware-source.tar.gz'
        notices = source_bundle(source, environments, args.platformio_core.resolve(), stage / source_name, notice_dir)
        (notice_dir / 'licenses.json').write_text(json.dumps(notices, indent=2) + '\n')
        (notice_dir / 'NOTICE.txt').write_text(
            'Passage firmware — third-party notices\n\n'
            'These application images are built from CrossPoint Reader and FreeInk SDK (MIT), with third-party components.\n'
            'wolfSSL is GPL-2.0-or-later; Arduino ESP32 and WebSockets are LGPL-2.1-or-later. Original copyright and license terms are retained in licenses/.\n'
            'The separate Passage desktop application is not linked with these embedded firmware libraries.\n\n'
            f'Reader, SDK, resolved libraries, Arduino and ESP-IDF framework source: {source_name}, distributed alongside this bundle.\n'
            f'CrossPoint commit: {next(iter(commits))}\n'
            'Source archive includes platformio.local.ini used for the build. PlatformIO downloads the pinned toolchain and ESP-IDF platform packages specified by platformio.ini.\n'
            'Build with PlatformIO 6.1.19: pio run -e <environment>. Environment and binary hashes are listed in firmware-build.json.\n'
        )
        receipt['source_archive'] = {'filename': source_name, 'sha256': digest(stage / source_name), 'size': (stage / source_name).stat().st_size}
        (stage / 'firmware-build.json').write_text(json.dumps(receipt, indent=2) + '\n')
        (stage / 'firmware.json').write_text(json.dumps(manifest, indent=2) + '\n')
        for path in sorted(stage.rglob('*')):
            if path.is_file() and path.suffix != '.sha256':
                path.with_name(path.name + '.sha256').write_text(f'{digest(path)}  {path.name}\n')
        shutil.copytree(stage, output)
    print(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, default=ROOT / 'firmware.json')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--platformio-core', type=Path, default=Path.home() / '.platformio')
    parser.add_argument('--pio', default='pio')
    parser.add_argument('--models', nargs='+')
    parser.add_argument('--jobs', type=int, choices=range(1, 17), default=4, help='Parallel compiler jobs (default: 4)')
    args = parser.parse_args()
    try:
        assemble(args)
    except (ValueError, OSError, configparser.Error, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Firmware packaging failed: {error}\n')


if __name__ == '__main__':
    main()
