import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import struct
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('firmware_assets', Path(__file__).resolve().parents[1] / 'scripts/build-firmware-assets.py')
assets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assets)


def macho_fixture(bits=64, kind=2):
    if bits == 64:
        header = struct.pack('<8I', 0xfeedfacf, 0x0100000c, 0, kind, 2, 96, 0, 0)
        segment = struct.pack('<II64x', 0x19, 72)
    else:
        header = struct.pack('<7I', 0xfeedface, 7, 0, kind, 2, 80, 0)
        segment = struct.pack('<II48x', 1, 56)
    return header + segment + struct.pack('<IIQQ', 0x80000028, 24, len(header) + len(segment) + 24, 0) + b'fixture code'


class FirmwareAssetsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(self.source), *args], check=True, capture_output=True, text=True).stdout.strip()
        git('init')
        (self.source / 'main.cpp').write_text('// Public firmware fixture\n')
        git('add', 'main.cpp')
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture')
        self.commit = git('rev-parse', 'HEAD')
        (self.source / 'private.txt').write_text('PRIVATE_UNTRACKED_SENTINEL')
        binary = self.source / '.pio/build/example/firmware.bin'
        binary.parent.mkdir(parents=True)
        image = bytearray(256)
        image[0] = 0xE9
        image[12] = 9
        protocol = b'/v1/highlights\0/v1/covers\0X-Book-Title\0X-Book-Author\0'
        image[64:64 + len(protocol)] = protocol
        binary.write_bytes(image)
        self.binary = binary
        dep = self.source / '.pio/libdeps/example/Example'
        dep.mkdir(parents=True)
        (dep / 'LICENSE').write_text('Example public library license fixture')
        for name in ('framework-arduinoespressif32', 'framework-espidf', 'framework-arduinoespressif32-libs'):
            framework = self.root / 'core/packages' / name
            framework.mkdir(parents=True)
            (framework / 'LICENSE').write_text('Example public framework license fixture: ' + name)
        platform = self.root / 'core/platforms/espressif32'
        platform.mkdir(parents=True)
        (platform / 'builder.py').write_text('# Public build script fixture\n')
        self.manifest = self.root / 'manifest.json'
        self.data = {'schema_version': 2, 'devices': {'example_reader': {
            'id': 'example_reader', 'commit': self.commit, 'environment': 'example',
            'chip_id': 9, 'filename': 'passage-example.bin', 'prebuilt': None}}}
        self.manifest.write_text(json.dumps(self.data))
        self.args = argparse.Namespace(source=self.source, manifest=self.manifest, output=self.root / 'output',
            platformio_core=self.root / 'core', models=None, pio='fixture-pio', jobs=4)
        inner = self.root / 'core/penv/bin/python'
        inner.parent.mkdir(parents=True)
        inner.touch()
        actual_run = assets.run
        def fake_build(*args, **kwargs):
            if str(args[0]) in ('fixture-pio', str(inner), str(inner.with_name('pio'))):
                return subprocess.CompletedProcess(args, 0, stdout='PlatformIO Core, version 6.1.19', stderr='')
            return actual_run(*args, **kwargs)
        self.run_patch = patch.object(assets, 'run', side_effect=fake_build)
        self.run_mock = self.run_patch.start()
        self.addCleanup(self.run_patch.stop)

    def test_bundle_matches_binary_and_excludes_untracked_data(self):
        assets.assemble(self.args)
        output = self.args.output
        manifest = json.loads((output / 'firmware.json').read_text())
        prebuilt = manifest['devices']['example_reader']['prebuilt']
        self.assertEqual(prebuilt['sha256'], hashlib.sha256(self.binary.read_bytes()).hexdigest())
        self.assertEqual((output / prebuilt['bundled_path']).read_bytes(), self.binary.read_bytes())
        receipt = json.loads((output / 'firmware-build.json').read_text())
        archive_path = output / receipt['source_archive']['filename']
        self.assertEqual(assets.digest(archive_path), receipt['source_archive']['sha256'])
        with tarfile.open(archive_path) as archive:
            names = archive.getnames()
            self.assertIn('source/main.cpp', names)
            self.assertNotIn('source/private.txt', names)
            self.assertIn('source/.pio/libdeps/example/Example/LICENSE', names)
            self.assertIn('platform-espressif32/builder.py', names)
        self.assertEqual(len(json.loads((output / 'desktop/licenses/firmware/licenses.json').read_text())), 4)
        self.assertTrue(receipt['build_succeeded'])

    def test_wrong_chip_leaves_no_deliverable(self):
        data = bytearray(self.binary.read_bytes())
        data[12] = 5
        self.binary.write_bytes(data)
        with self.assertRaisesRegex(ValueError, 'Invalid application'):
            assets.assemble(self.args)
        self.assertFalse(self.args.output.exists())

    def test_highlights_only_firmware_cannot_package_without_cover_support(self):
        image = bytearray(256)
        image[0] = 0xE9
        image[12] = 9
        image[64:79] = b'/v1/highlights\0'
        self.binary.write_bytes(image)
        with self.assertRaisesRegex(ValueError, 'example_reader.*?/v1/covers, X-Book-Title, X-Book-Author'):
            assets.assemble(self.args)
        self.assertFalse(self.args.output.exists())

    def test_every_protocol_marker_is_required_in_the_built_image(self):
        original = self.binary.read_bytes()
        for marker in (b'/v1/highlights', b'/v1/covers', b'X-Book-Title', b'X-Book-Author'):
            with self.subTest(marker=marker):
                self.binary.write_bytes(original.replace(marker, b'\0' * len(marker)))
                with self.assertRaises(ValueError) as failure:
                    assets.assemble(self.args)
                self.assertIn('lacks required Passage protocol markers: ' + marker.decode('ascii'), str(failure.exception))
                self.assertFalse(self.args.output.exists())

    def test_jpegdec_compiled_example_is_excluded_but_source_and_artifacts_are_preserved(self):
        dependency = self.source / '.pio/libdeps/example/JPEGDEC'
        example = dependency / 'linux/examples/jpeg_perf_test'
        example.mkdir(parents=True)
        (example / 'jpeg_perf_test').write_bytes(macho_fixture())
        preserved = {'jpeg_perf_test.cpp': b'// Synthetic JPEGDEC example source',
                     'Makefile': b'jpeg_perf_test: jpeg_perf_test.cpp\n',
                     'LICENSE': b'Synthetic JPEGDEC example terms',
                     'firmware-required.bin': self.binary.read_bytes(),
                     'image-data': b'\xcf\xfa\xed\xfeordinary fixture data',
                     'java-class': b'\xca\xfe\xba\xbe\x00\x00\x00\x3dordinary Java fixture'}
        for name, data in preserved.items():
            (example / name).write_bytes(data)
        tool = dependency / 'tools/required-build-helper'
        tool.parent.mkdir()
        tool.write_bytes(macho_fixture())
        assets.assemble(self.args)
        archive_path = self.args.output / 'passage-firmware-source.tar.gz'
        prefix = 'source/.pio/libdeps/example/JPEGDEC/linux/examples/jpeg_perf_test/'
        with tarfile.open(archive_path) as archive:
            self.assertNotIn(prefix + 'jpeg_perf_test', archive.getnames())
            for name, data in preserved.items():
                self.assertEqual(archive.extractfile(prefix + name).read(), data)
            self.assertEqual(archive.extractfile('source/.pio/libdeps/example/JPEGDEC/tools/required-build-helper').read(), tool.read_bytes())
        manifest = json.loads((self.args.output / 'firmware.json').read_text())
        artifact = manifest['devices']['example_reader']['prebuilt']
        self.assertEqual((self.args.output / artifact['bundled_path']).read_bytes(), self.binary.read_bytes())
        licenses = json.loads((self.args.output / 'desktop/licenses/firmware/licenses.json').read_text())
        self.assertIn(prefix + 'LICENSE', [entry['source'] for entry in licenses])

    def test_macho_detection_checks_headers_and_commands_including_universal_images(self):
        path = self.root / 'host-example'
        for bits in (32, 64):
            path.write_bytes(macho_fixture(bits))
            self.assertTrue(assets.macho_executable(path))
        image = macho_fixture()
        header = struct.pack('>II5I', 0xcafebabe, 1, 0x0100000c, 0, 4096, len(image), 12)
        path.write_bytes(header + bytes(4096 - len(header)) + image)
        self.assertTrue(assets.macho_executable(path))
        for ordinary in (image[:32], macho_fixture(kind=6),
                         b'\xcf\xfa\xed\xfeordinary fixture data',
                         b'\xca\xfe\xba\xbe\x00\x00\x00\x3dordinary Java fixture'):
            path.write_bytes(ordinary)
            self.assertFalse(assets.macho_executable(path))

    def test_unrecognized_build_override_is_preserved(self):
        override = self.source / 'platformio.local.ini'
        override.write_text('[env:example]\nextra_scripts = private.py\n')
        with self.assertRaisesRegex(ValueError, 'Unrecognized local'):
            assets.assemble(self.args)
        self.assertIn('private.py', override.read_text())
        self.assertFalse(self.args.output.exists())

    def test_nested_core_must_match_pinned_version(self):
        previous = self.run_mock.side_effect
        def mismatch(*args, **kwargs):
            if str(args[0]).endswith('/penv/bin/pio'):
                return subprocess.CompletedProcess(args, 0, stdout='PlatformIO Core, version 6.2.0')
            return previous(*args, **kwargs)
        self.run_mock.side_effect = mismatch
        with self.assertRaisesRegex(ValueError, 'Nested firmware build core'):
            assets.assemble(self.args)
        self.assertFalse(self.args.output.exists())

    def test_source_revision_mismatch_rejected(self):
        self.data['devices']['example_reader']['commit'] = 'a' * 40
        self.manifest.write_text(json.dumps(self.data))
        with self.assertRaisesRegex(ValueError, 'pinned source'):
            assets.assemble(self.args)
        self.assertFalse(self.args.output.exists())

    def test_manifest_cannot_escape_bundle_directory(self):
        self.data['devices']['example_reader']['filename'] = '../escape.bin'
        self.manifest.write_text(json.dumps(self.data))
        with self.assertRaisesRegex(ValueError, 'Invalid firmware target'):
            assets.assemble(self.args)
        self.assertFalse(self.args.output.exists())

    def test_build_failure_cannot_package_old_binary(self):
        previous = self.run_mock.side_effect
        def failed(*args, **kwargs):
            if args[:2] == ('fixture-pio', 'run'):
                raise subprocess.CalledProcessError(1, args)
            return previous(*args, **kwargs)
        self.run_mock.side_effect = failed
        with self.assertRaises(subprocess.CalledProcessError):
            assets.assemble(self.args)
        self.assertFalse(self.args.output.exists())

    def test_source_mutation_during_build_is_rejected(self):
        previous = self.run_mock.side_effect
        def changed(*args, **kwargs):
            result = previous(*args, **kwargs)
            if args[:2] == ('fixture-pio', 'run'):
                (self.source / 'main.cpp').write_text('changed while building')
            return result
        self.run_mock.side_effect = changed
        with self.assertRaisesRegex(ValueError, 'changed during the build'):
            assets.assemble(self.args)
        self.assertFalse(self.args.output.exists())


if __name__ == '__main__':
    unittest.main()
