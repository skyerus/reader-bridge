#!/usr/bin/env python3
"""Native app JSON bridge. Status caps highlights at 500; count/export cover all live quotes."""
import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import io
import hashlib
import hmac
import json
import os
from pathlib import Path
import plistlib
import re
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.error import HTTPError

# The backend may live inside a signed .app, including when invoked directly.
sys.dont_write_bytecode = True

import setup
import device_profiles
import archive_backup
from progress_setup import ProgressSetup
from collector import Store, initialize, item_key
from device_covers import stored_path
from covers import CoverLibrary, book_key, epub_cover, MAX_EPUB, MAX_IMAGE
from db import clean_cover_url

MAX_REQUEST = 64 * 1024
MAX_IMPORT = 16 * 1024 * 1024
MAX_EXPORT = 64 * 1024 * 1024
HIGHLIGHTS_LIMIT = 500


def text_arg(value, name):
    if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > 4096 or '\x00' in value:
        raise setup.SetupError('Provide a valid ' + name + '.')
    return value


def port_arg(value):
    if type(value) is not int or not 1024 <= value <= 65535:
        raise setup.SetupError('Choose an integer port from 1024 to 65535.')
    return value


def bool_arg(value, name):
    if type(value) is not bool:
        raise setup.SetupError(name + ' must be true or false.')
    return value


def highlight_date(raw):
    """Recognize recorded ISO dates, never infer a date from an import or ID."""
    if not isinstance(raw, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?', raw):
        return None
    try:
        value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        # Date-only and timezone-free records keep their recorded wall time.
        # Use UTC as a stable ordering convention, not the Mac's current zone.
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def highlight_sort_key(row):
    recorded = highlight_date(row['created_at'])
    return (recorded is None, -recorded.timestamp() if recorded else 0,
            row['title'].casefold(), row['author'].casefold(), row['text'].casefold(), row['id'])


class Desktop(ProgressSetup, setup.Bridge):
    def legacy_spec(self, path, descriptor=None):
        """Only the known sibling layout and a fully pinned launch job qualify."""
        legacy = setup.guarded(self.app.parent / 'Reading Highlights')
        path = setup.guarded(path)
        if path.parent != self.agent_dir or path.suffix != '.plist':
            raise setup.SetupError('Existing collector descriptor is invalid.')
        raw = path.read_bytes()
        spec = plistlib.loads(raw)
        args = spec.get('ProgramArguments')
        label = spec.get('Label')
        if (not isinstance(label, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', label)
                or spec.get('WorkingDirectory') != str(legacy)
                or not isinstance(args, list) or len(args) != 15
                or not all(isinstance(a, str) for a in args)
                or not Path(args[0]).is_absolute()
                or args[1:8] != [str(legacy / 'collector.py'), 'serve', '--state-dir', str(legacy / 'data'), '--host', '0.0.0.0', '--port']
                or not args[8].isdigit() or not 1024 <= int(args[8]) <= 65535
                or args[9] != '--repo' or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args[10])
                or args[11] != '--branch' or not args[12].strip() or args[13] != '--publish-interval'
                or not args[14].isdigit() or int(args[14]) <= 0):
            raise setup.SetupError('Existing collector launch configuration is not recognized.')
        for relative in ('collector.py', 'db.py', 'data/token', 'data/inbox.sqlite3'):
            if not setup.guarded(legacy / relative).is_file():
                raise setup.SetupError('Existing collector files are missing.')
        token = (legacy / 'data/token').read_bytes()
        if not token.strip() or len(token) > 4096:
            raise setup.SetupError('Existing collector token is invalid.')
        pinned = {'agent': str(path), 'plist_sha256': hashlib.sha256(raw).hexdigest(), 'token_sha256': hashlib.sha256(token).hexdigest()}
        if descriptor is not None and descriptor != pinned:
            raise setup.SetupError('Existing collector configuration changed. Recheck its original setup before reconnecting.')
        return spec, pinned

    def legacy_loaded(self, spec, path):
        if sys.platform != 'darwin':
            return False
        loaded = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{spec["Label"]}'], capture_output=True, text=True, timeout=5)
        def field(name):
            match = re.search(r'^\s*' + re.escape(name) + r' = (.+?)\s*$', loaded.stdout, re.MULTILINE)
            return match.group(1) if match else None
        if (loaded.returncode != 0 or field('path') != str(path)
                or field('program') != spec['ProgramArguments'][0]
                or field('working directory') != spec['WorkingDirectory']
                or not re.fullmatch(r'\d+', field('pid') or '')):
            return False
        listener = subprocess.run(['/usr/sbin/lsof', '-nP', f'-iTCP:{spec["ProgramArguments"][8]}', '-sTCP:LISTEN', '-Fp'], capture_output=True, text=True, timeout=5)
        pids = {line[1:] for line in listener.stdout.splitlines() if re.fullmatch(r'p\d+', line)}
        return listener.returncode == 0 and pids == {field('pid')}

    def legacy_auth(self, port, endpoint=None):
        token_path = setup.guarded(self.app.parent / 'Reading Highlights/data/token')
        return self.authenticate_at(endpoint or f'http://127.0.0.1:{port}', token_path, legacy=True)

    def existing(self):
        connected = 'existing_collector' in self.state
        descriptor = self.state.get('existing_collector')
        unavailable = {'available': False, 'connected': connected, 'healthy': False, 'port': 8084, 'archive': ''}
        if connected:
            if not isinstance(descriptor, dict) or set(descriptor) != {'agent', 'plist_sha256', 'token_sha256'} or not all(isinstance(v, str) for v in descriptor.values()):
                return unavailable, None
            paths = [Path(descriptor['agent'])]
        elif self.state.get('collector') or (self.app / 'collector/data').exists():
            return unavailable, None
        else:
            paths = sorted(self.agent_dir.glob('*.plist'))
        candidates = []
        for path in paths:
            try:
                spec, pinned = self.legacy_spec(path, descriptor if connected else None)
                healthy = self.legacy_loaded(spec, path) and self.legacy_auth(int(spec['ProgramArguments'][8]))
                if connected or healthy:
                    candidates.append(({'available': healthy, 'connected': connected, 'healthy': healthy, 'port': int(spec['ProgramArguments'][8]), 'archive': spec['ProgramArguments'][10]}, pinned))
            except (OSError, ValueError, TypeError, setup.SetupError, subprocess.TimeoutExpired, plistlib.InvalidFileException):
                continue
        return candidates[0] if len(candidates) == 1 else (unavailable, None)

    def collector_data_dir(self):
        if 'existing_collector' not in self.state:
            return super().collector_data_dir()
        descriptor = self.state['existing_collector']
        if not isinstance(descriptor, dict) or set(descriptor) != {'agent', 'plist_sha256', 'token_sha256'} or not all(isinstance(v, str) for v in descriptor.values()):
            raise setup.SetupError('Existing collector descriptor is invalid.')
        self.legacy_spec(Path(descriptor['agent']), descriptor)
        return setup.guarded(self.app.parent / 'Reading Highlights/data')

    def collector_port(self):
        if 'existing_collector' in self.state:
            self.collector_data_dir()
            spec, _ = self.legacy_spec(Path(self.state['existing_collector']['agent']), self.state['existing_collector'])
            return int(spec['ProgramArguments'][8])
        return self.state.get('collector', {}).get('port', 8084)

    def assert_managed_collector(self):
        if 'existing_collector' in self.state:
            raise setup.SetupError('This collector is managed by your existing Reading Highlights setup. Use that setup to start, stop, or change its backup settings.')

    def existing_pairing(self):
        try:
            data = self.collector_data_dir()
            pairing = setup.guarded(data.parent / 'pairing/xteink-highlight-sync.json')
            settings = json.loads(pairing.read_text())
            if hmac.compare_digest(settings.get('token', ''), (data / 'token').read_text().strip()):
                from urllib.parse import urlsplit
                value = settings.get('endpoint', settings.get('url', ''))
                endpoint = setup.private_url(value.removesuffix('/v1/highlights'))
                if urlsplit(endpoint).port == self.collector_port() and self.authenticated(self.collector_port(), endpoint):
                    return endpoint
        except (OSError, ValueError, TypeError, setup.SetupError):
            pass
        return ''

    @contextlib.contextmanager
    def inbox_connection(self, path):
        # Read-only SQL still permits SQLite's own WAL shared-memory housekeeping.
        # Never initialize, checkpoint, migrate, or change external archive rows.
        con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            con.execute('PRAGMA query_only=ON')
            yield con
        finally:
            con.close()

    def existing_sources(self):
        path = self.collector_data_dir() / 'inbox.sqlite3'
        with self.inbox_connection(path) as con:
            return {row[0] for row in con.execute("SELECT DISTINCT source FROM inbox WHERE device NOT IN ('kindle-clippings-import', 'reader-bridge-archive-import')")}

    @staticmethod
    def owned_runtime(program):
        if program == sys.executable:
            return True
        if not isinstance(program, str):
            return False
        path = Path(program)
        # A renamed or replaced application may have removed this interpreter.
        # Recognize only our shipped bundle layout; the saved command, state
        # directory and launchd identity must still match before replacement.
        return (path.is_absolute() and '..' not in path.parts and len(path.parts) >= 7
                and path.parts[-6] in {'Reader Bridge.app', 'Passage.app'}
                and path.parts[-5:-1] == ('Contents', 'Resources', 'runtime', 'bin')
                and re.fullmatch(r'python3(?:\.\d+)?', path.name) is not None)

    def owned(self, kind):
        path = self.agent_path(kind)
        if not path.exists() or self.state.get(kind, {}).get('agent') != str(path):
            return False
        try:
            setup.guarded(path)
            spec = plistlib.loads(path.read_bytes())
            args = spec.get('ProgramArguments', [])
            directory = self.app / kind
            return (spec.get('Label') == setup.LABELS[kind]
                    and spec.get('WorkingDirectory') == str(directory)
                    and isinstance(args, list) and len(args) >= 2
                    and (self.owned_runtime(args[0]) and args[1:] == [str(directory / 'archive_backup.py'), '--state-dir', str(directory)] if kind == 'cloud_backup'
                         else self.owned_runtime(args[0]) and args[1:] == [str(directory / 'progress_sync.py'), '--state-dir', str(directory), '--port', str(self.state[kind].get('port'))] if kind == 'progress_sync'
                         else str(directory / 'collector.py') in args if kind == 'collector'
                         else args[0] == str(directory / 'venv/bin/cps')))
        except (OSError, ValueError, plistlib.InvalidFileException):
            return False

    def assert_ownership(self, kind):
        if self.agent_path(kind).exists() and not self.owned(kind):
            raise setup.SetupError('An unowned or changed Reader Bridge service exists. Resolve it before installing or stopping this service.')

    def assert_loaded_ownership(self, kind):
        loaded = self.loaded_service(f'gui/{os.getuid()}/{setup.LABELS[kind]}')
        if loaded.returncode == 113:
            return
        if not self.owned(kind):
            raise setup.SetupError('An unowned Reader Bridge service is already loaded. Existing services were left alone.')
        spec = plistlib.loads(self.agent_path(kind).read_bytes())
        def field(name):
            value = re.search(r'^\s*' + re.escape(name) + r' = (.+)\s*$', loaded.stdout, re.MULTILINE)
            return value.group(1).strip() if value else None
        if (field('path') != str(self.agent_path(kind))
                or field('program') != spec['ProgramArguments'][0]
                or field('working directory') != spec['WorkingDirectory']):
            raise setup.SetupError('The loaded service differs from this app’s saved service. Existing services were left alone; resolve the conflicting job first.')

    def authenticated(self, port, endpoint=None):
        if 'existing_collector' in self.state:
            existing, descriptor = self.existing()
            return bool(descriptor and existing['healthy'] and (not endpoint or self.legacy_auth(existing['port'], endpoint)))
        return self.authenticate_at(endpoint or f'http://127.0.0.1:{port}', self.collector_data_dir() / 'token')

    def authenticate_at(self, base, token_path, legacy=False):
        if not token_path.is_file():
            return False
        try:
            health = json.loads(setup.http(base + '/healthz', timeout=2, maximum=4096))
            if (not legacy and health.get('service') != 'reader-bridge') or health.get('status') != 'ok':
                return False
            # Invalid empty batch authenticates but cannot insert or delete data.
            try:
                setup.http(base + '/v1/highlights', b'{}', headers={'Authorization': 'Bearer ' + token_path.read_text().strip(), 'Content-Type': 'application/json'}, timeout=2, maximum=4096)
            except HTTPError as exc:
                return exc.code == 400 and json.loads(exc.read(4096)).get('error') == 'invalid highlight batch'
        except (OSError, ValueError, setup.SetupError):
            pass
        return False

    def check_port(self, kind, port):
        port_arg(port)
        self.assert_ownership(kind)
        if setup.available(port):
            return
        if self.owned(kind) and self.state.get(kind, {}).get('port') == port:
            if kind == 'collector' and self.authenticated(port):
                return
            # An authenticated older service is ours to upgrade even when it
            # does not yet support the current pairing protocol.
            if kind == 'progress_sync' and self.progress_authenticated(require_guard=False):
                return
            if kind == 'library' and self.library_listener_owned(port):
                return
        raise setup.SetupError(f'Port {port} is occupied by an unverified service. Choose another port; existing services were left alone.')

    def library_listener_owned(self, port):
        # A matching plist cannot establish ownership of a listening socket.
        # Require launchd's loaded path and PID to match the actual listener.
        if sys.platform != 'darwin':
            return False
        try:
            loaded = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{setup.LABELS["library"]}'], capture_output=True, text=True, timeout=5)
            listener = subprocess.run(['/usr/sbin/lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN', '-Fp'], capture_output=True, text=True, timeout=5)
            pid = re.search(r'^\s*pid = (\d+)\s*$', loaded.stdout, re.MULTILINE)
            path = re.search(r'^\s*path = (.+)\s*$', loaded.stdout, re.MULTILINE)
            pids = {line[1:] for line in listener.stdout.splitlines() if re.fullmatch(r'p\d+', line)}
            return (loaded.returncode == listener.returncode == 0 and bool(pid) and bool(path)
                    and path.group(1).strip() == str(self.agent_path('library')) and pids == {pid.group(1)})
        except (OSError, subprocess.TimeoutExpired):
            return False

    def launch(self, kind, arguments, directory, start=True):
        self.assert_ownership(kind)
        self.assert_loaded_ownership(kind)
        super().launch(kind, arguments, directory, start)

    def stop_owned(self, kind):
        self.assert_ownership(kind)
        self.assert_loaded_ownership(kind)
        if self.owned(kind):
            super().stop_owned(kind)

    @staticmethod
    def stable_installation():
        if str(setup.SOURCE).startswith('/Volumes/') or 'AppTranslocation' in setup.SOURCE.parts:
            raise setup.SetupError('Move Passage to Applications and reopen it before installing background services.')

    def collector(self, *args, **kwargs):
        self.assert_managed_collector()
        self.stable_installation()
        super().collector(*args, **kwargs)

    def start_local(self, port, disable=False):
        self.assert_managed_collector()
        self.stable_installation()
        if sys.platform != 'darwin':
            raise setup.SetupError('Background collector installation requires macOS.')
        existing = self.state.get('collector', {})
        changed_port = existing.get('port') is not None and existing['port'] != port
        if changed_port and (self.state.get('kindle', {}).get('installed') or self.state.get('xteink', {}).get('paired')):
            raise setup.SetupError('This collector already has paired readers. Keep its current port so their saved pairing addresses continue to work.')
        self.check_port('collector', port)
        if changed_port and self.state.get('url'):
            from urllib.parse import urlsplit
            host = urlsplit(setup.private_url(self.state['url'])).hostname
            self.state['url'] = f'http://{host}:{port}'
        if existing.get('archive') and not disable and not existing.get('backup_disabled'):
            self.collector(existing['archive'], port=port)
            return
        target = self.app / 'collector'
        self.save()
        for filename in ('collector.py', 'db.py', 'device_covers.py'):
            self.install_file(target / filename, (setup.SOURCE / filename).read_bytes())
        directory, _ = initialize(target / 'data')
        Store(directory / 'inbox.sqlite3')
        self.state['collector'] = {**existing, 'port': port, 'backup_disabled': bool(disable or existing.get('backup_disabled')), 'mode': 'local'}
        self.save()
        self.launch('collector', [sys.executable, target / 'collector.py', 'serve', '--state-dir', directory, '--host', '0.0.0.0', '--port', str(port), '--no-publish'], target)
        for _ in range(30):
            if self.authenticated(port):
                return
            time.sleep(.25)
        raise setup.SetupError('Collector could not start. Check the private service logs and retry; your inbox is retained.')

    def live_rows(self):
        path = self.collector_data_dir() / 'inbox.sqlite3'
        if not path.exists():
            return [], 0
        setup.guarded(path)
        # Store's constructor initializes storage: status must never call it.
        with self.inbox_connection(path) as con:
            con.execute('BEGIN')
            deleted = {r[0] for r in con.execute('SELECT quote_key FROM tombstones')}
            pending = con.execute('SELECT count(*) FROM inbox WHERE published IS NULL OR published!=revision').fetchone()[0]
            pending += con.execute('SELECT count(*) FROM tombstones WHERE published=0').fetchone()[0]
            rows = {}
            for source, payload in con.execute('SELECT source,payload FROM inbox ORDER BY source,device,id'):
                item = json.loads(payload)
                if item.get('deleted'):
                    continue
                key = item_key(item)
                if key in deleted:
                    continue
                if key not in rows:
                    rows[key] = {'id': key, 'title': item['book_title'], 'author': item['author'], 'text': item['text'], 'source': source, 'created_at': item.get('created_at', ''), 'book_id': book_key(item['book_title'], item['author']), 'cover_url': clean_cover_url(item.get('cover_url')), 'cover_path': ''}
                else:
                    if not rows[key]['cover_url']:
                        rows[key]['cover_url'] = clean_cover_url(item.get('cover_url'))
                    recorded = highlight_date(item.get('created_at', ''))
                    previous = highlight_date(rows[key]['created_at'])
                    # A duplicate without a date must not hide a recorded one.
                    # Keep the earliest known creation date of the same passage.
                    if recorded and (previous is None or recorded < previous):
                        rows[key]['created_at'] = item['created_at']
            device_covers = {}
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='book_covers'").fetchone():
                device_covers = {key: stored_path(path.parent, digest, extension)
                                 for key, digest, extension in con.execute('SELECT book_key,sha256,extension FROM book_covers')}
            by_book = {row['book_id']: row['cover_url'] for row in rows.values() if row['cover_url']}
            for row in rows.values():
                row['cover_url'] = by_book.get(row['book_id'], '')
                row['cover_path'] = device_covers.get(row['book_id'], '')
            return sorted(rows.values(), key=highlight_sort_key), pending

    def cloud_backup_status(self):
        config = self.state.get('cloud_backup', {})
        receipt_path = setup.guarded(self.app / 'cloud_backup/receipt.json')
        receipt = {}
        try:
            if receipt_path.is_file():
                receipt = json.loads(receipt_path.read_text())
        except (OSError, ValueError):
            pass
        # A receipt from a previous destination must not advertise an unrelated file.
        snapshot_path = ''
        destination = config.get('destination', '')
        if destination and receipt.get('path'):
            candidate = Path(receipt['path'])
            try:
                if candidate.suffix == '.readerbridge' and candidate.is_file() and candidate.resolve().parent == Path(destination).resolve():
                    snapshot_path = str(candidate)
            except OSError:
                pass
        checked = highlight_date(receipt.get('checked_at', ''))
        if config.get('enabled') and (checked is None or (datetime.now(timezone.utc) - checked).total_seconds() > 180):
            receipt.setdefault('error', 'Waiting for the background backup service. If this persists, set up the backup folder again.')
        return {'enabled': bool(config.get('enabled')), 'provider': config.get('provider', 'icloud'),
                'folder': destination, 'snapshot_path': snapshot_path, 'saved_at': receipt.get('saved_at', '') if snapshot_path else '',
                'error': receipt.get('error', ''), 'cloud_upload_verified': False}

    def configure_cloud_backup(self, provider, folder):
        if sys.platform != 'darwin':
            raise setup.SetupError('Use archive_backup.py with your scheduler on this platform.')
        self.stable_installation()
        if provider not in ('icloud', 'folder'):
            raise setup.SetupError('Choose iCloud Drive or a backup folder.')
        folder = setup.guarded(Path(text_arg(folder, 'backup folder')).expanduser())
        if not folder.is_dir():
            raise setup.SetupError('Choose an available folder first.')
        if provider == 'icloud':
            cloud = setup.guarded(Path.home() / 'Library/Mobile Documents/com~apple~CloudDocs')
            if not cloud.is_dir() or not (folder == cloud or cloud in folder.parents):
                raise setup.SetupError('Choose a folder in iCloud Drive. Enable iCloud Drive in System Settings if it is unavailable.')
        if folder == self.app or self.app in folder.parents or self.collector_data_dir() == folder or self.collector_data_dir() in folder.parents:
            raise setup.SetupError('Choose a backup folder outside the live app data.')
        self.assert_ownership('cloud_backup')
        self.assert_loaded_ownership('cloud_backup')
        target = setup.guarded(self.app / 'cloud_backup')
        install_id = self.state.get('backup_installation') or __import__('uuid').uuid4().hex
        destination = setup.guarded(folder / 'Reader Bridge Backups' / install_id)
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        config = {'database': str(self.collector_data_dir() / 'inbox.sqlite3'), 'app':str(self.app), 'destination':str(destination)}
        if not Path(config['database']).is_file():
            raise setup.SetupError('Start your local bridge before enabling backup.')
        # First prove the selected destination works, then enable background work.
        archive_backup.snapshot(config['database'], self.app, destination, target)
        self.stop_owned('cloud_backup')
        for name in ('archive_backup.py','progress_sync.py'):
            self.install_file(target / name, (setup.SOURCE / name).read_bytes())
        setup.atomic_write(target / 'config.json', archive_backup.encode(config))
        self.state['backup_installation'] = install_id
        self.state['cloud_backup'] = {**self.state.get('cloud_backup', {}), 'enabled':True, 'provider':provider, 'destination':str(destination)}
        self.save()
        self.launch('cloud_backup', [sys.executable, target / 'archive_backup.py', '--state-dir', target], target)

    def status(self, query='', book_id=''):
        existing, descriptor = self.existing()
        service = {'port': existing['port'], 'archive': existing['archive']} if existing['connected'] else self.state.get('collector', {})
        kindle = self.state.get('kindle', {})
        xteink = self.state.get('xteink', {})
        library = self.state.get('library', {})
        port = port_arg(service.get('port', 8084))
        warnings = []
        if existing['connected'] and descriptor is None:
            warnings.append('The existing collector configuration or files changed. Recheck the original Reading Highlights setup; this app has left it unchanged.')
        try:
            rows, pending = self.live_rows()
        except (OSError, ValueError, KeyError, sqlite3.Error, setup.SetupError):
            rows, pending = [], 0
            warnings.append('The local inbox could not be read. Data was left unchanged; retry or inspect the private app directory.')
        endpoint = self.state.get('url')
        if existing['connected'] and descriptor:
            endpoint = endpoint or self.existing_pairing()
            try:
                sources = self.existing_sources()
                kindle = {**kindle, 'installed': kindle.get('installed') or 'koreader' in sources}
                xteink = {**xteink, 'paired': xteink.get('paired') or 'crosspoint' in sources}
            except (OSError, ValueError, sqlite3.Error, setup.SetupError):
                pass
        count = len(rows)
        try:
            covers = CoverLibrary(self.app)
            for row in rows:
                metadata = covers.metadata(row['title'], row['author'])
                row['cover_path'] = metadata['cover_path'] or row['cover_path']
                row['cover_url'] = metadata['cover_url'] or row['cover_url']
        except (OSError, ValueError, setup.SetupError):
            warnings.append('Covers could not be read. Your highlights are still available.')
        books = {}
        for row in rows:
            key = row['book_id']
            if key not in books:
                books[key] = {k: row[k] for k in ('title', 'author', 'cover_url', 'cover_path')}
                books[key].update({'id': key, 'count': 0, 'latest_highlight_at': None})
            books[key]['count'] += 1
            recorded = highlight_date(row['created_at'])
            if recorded is not None:
                timestamp = recorded.timestamp()
                latest = books[key]['latest_highlight_at']
                if latest is None or timestamp > latest:
                    books[key]['latest_highlight_at'] = timestamp
        if book_id:
            rows = [row for row in rows if row['book_id'] == book_id]
        if query:
            needle = query.casefold()
            rows = [row for row in rows if any(needle in row[key].casefold() for key in ('title', 'author', 'text'))]
        undated = sum(highlight_date(row['created_at']) is None for row in rows)
        mounts = []
        try:
            for mount in Path('/Volumes').iterdir():
                if mount.is_symlink():
                    continue
                if any((mount / p / 'reader.lua').is_file() for p in ('koreader', '.adds/koreader')):
                    mounts.append({'name': mount.name, 'path': str(mount), 'kind': 'kindle'})
                elif (mount / '.crosspoint').is_dir():
                    mounts.append({'name': mount.name, 'path': str(mount), 'kind': 'xteink'})
        except OSError:
            pass
        addresses = []
        # Prefer the active network over VM/bridge addresses returned first by
        # hostname resolution. No connection or packet to an outside host needed.
        if sys.platform == 'darwin':
            try:
                route = subprocess.run(['/sbin/route', '-n', 'get', 'default'], capture_output=True, text=True, timeout=3)
                interface = re.search(r'^\s*interface:\s*([A-Za-z0-9]+)\s*$', route.stdout, re.MULTILINE)
                if route.returncode == 0 and interface:
                    address = subprocess.run(['/usr/sbin/ipconfig', 'getifaddr', interface.group(1)], capture_output=True, text=True, timeout=3)
                    if address.returncode == 0:
                        addresses.append(setup.private_url(f'http://{address.stdout.strip()}:{port}'))
            except (OSError, subprocess.TimeoutExpired, setup.SetupError):
                pass
        host = socket.gethostname().split('.')[0]
        if re.fullmatch(r'[A-Za-z0-9-]+', host):
            addresses.append(f'http://{host}.local:{port}')
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                candidate = f'http://{info[4][0]}:{port}'
                try:
                    setup.private_url(candidate)
                except setup.SetupError:
                    continue
                if candidate not in addresses:
                    addresses.append(candidate)
        except OSError:
            pass
        healthy = existing['healthy'] if existing['connected'] else self.authenticated(port) if service else False
        if service and not healthy:
            warnings.append('Collector is stopped or unreachable. Readers retain pending uploads until it is available.')
        mode = 'github' if service.get('archive') and not service.get('backup_disabled') else 'local'
        return {'local_progress': self.progress_status(), 'cloud_backup': self.cloud_backup_status(), 'existing_setup': existing, 'service': {'installed': existing['connected'] or self.owned('collector'), 'healthy': healthy, 'port': port, 'mode': mode, 'archive': service.get('archive', '') if mode == 'github' else '', 'pending_backup': pending if mode == 'github' else 0},
                'kindle': {'paired': bool(kindle.get('installed')), 'connected': bool(kindle.get('mount') and any((Path(kindle['mount']) / p / 'reader.lua').is_file() for p in ('koreader', '.adds/koreader'))), 'mount': kindle.get('mount', '')},
                'supported_devices': device_profiles.public_profiles(),
                'xteink': {'model': device_profiles.saved_model(xteink), 'model_verification': xteink.get('model_verification', 'legacy' if xteink.get('paired') else ''), 'paired': bool(xteink.get('paired')), 'firmware_staged': bool(xteink.get('firmware_staged')), 'url': xteink.get('device_url') or ''},
                'mounts': mounts, 'highlights': rows[:HIGHLIGHTS_LIMIT], 'highlight_count': count, 'highlights_matches': len(rows), 'highlights_limit': HIGHLIGHTS_LIMIT,
                'books': sorted(books.values(), key=lambda b: (b['title'].casefold(), b['author'].casefold())),
                'highlights_order': 'newest_first' if undated < len(rows) else 'book_title', 'highlights_undated': undated,
                'progress_verified': bool(self.state.get('progress_sync', {}).get('verified') and (not self.state.get('progress_sync', {}).get('kindle') or self.state.get('progress_sync', {}).get('kindle',{}).get('guard_version') == 1) if self.state.get('progress_sync', {}).get('enabled') else self.state.get('progress', {}).get('verified')), 'endpoint': endpoint or (addresses[0] if addresses else ''), 'addresses': addresses, 'warnings': warnings,
                'library': {'installed': self.owned('library'), 'port': library.get('port', 8083), 'books': library.get('books', '')}}

    def endpoint(self, value):
        endpoint = setup.private_url(text_arg(value, 'collector LAN address'))
        if not self.authenticated(self.collector_port(), endpoint):
            raise setup.SetupError('That LAN address does not reach this collector. Start it and check your address, firewall and Wi-Fi before pairing.')
        return endpoint

    def mutate(self, command, args):
        extra = {}
        if command in {'start_collector', 'stop_collector', 'configure_backup', 'disable_backup', 'install_library'}:
            self.assert_managed_collector()
        if command == 'connect_existing':
            if self.state.get('collector') or (self.app / 'collector/data').exists():
                raise setup.SetupError('This app already has collector configuration or data. Existing setups were left unchanged.')
            existing, descriptor = self.existing()
            if not descriptor or not existing['healthy']:
                raise setup.SetupError('No verified running Reading Highlights collector was found. Check the existing setup and retry.')
            self.state['existing_collector'] = descriptor
            self.save()
        elif command == 'start_collector':
            self.start_local(port_arg(args.get('port', 8084)))
        elif command == 'stop_collector':
            if sys.platform != 'darwin':
                raise setup.SetupError('Stopping a background service requires macOS.')
            self.assert_ownership('collector')
            self.assert_loaded_ownership('collector')
            if self.owned('collector'):
                path = self.agent_path('collector')
                result = subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{setup.LABELS["collector"]}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if result.returncode and self.authenticated(self.collector_port()):
                    raise setup.SetupError('The collector could not stop. Retry after checking macOS background service permissions.')
                self.backup(path)
                path.unlink()
                self.state['collector'].pop('agent', None)
                self.save()
        elif command == 'pair_kindle':
            self.kindle(text_arg(args.get('mount'), 'Kindle volume'), self.endpoint(args.get('endpoint')))
        elif command == 'pair_xteink':
            if args.get('model_confirmed') is not True:
                raise setup.SetupError('Confirm your exact CrossPoint model before pairing or staging firmware.')
            model = text_arg(args.get('model'), 'CrossPoint model')
            spec = self.device_profile(model)
            firmware = bool_arg(args.get('firmware', False), 'firmware')
            mount, url = args.get('mount'), args.get('device_url')
            if bool(mount) == bool(url):
                raise setup.SetupError('Select either an SD card volume or an Xteink LAN address.')
            self.xteink(mount=text_arg(mount, 'SD volume') if mount else None, device_url=text_arg(url, 'Xteink LAN address') if url else None, url=self.endpoint(args.get('endpoint')) if spec['capabilities']['highlights'] else None, firmware=firmware, model=model)
        elif command == 'import_clippings':
            path = setup.guarded(Path(text_arg(args.get('path'), 'clippings file')).expanduser())
            if not path.is_file() or path.stat().st_size > MAX_IMPORT:
                raise setup.SetupError('Select a My Clippings.txt file no larger than 16 MiB.')
            if not self.authenticated(self.collector_port()):
                raise setup.SetupError('Start your local collector before importing clippings.')
            from import_clippings import parse_clippings
            with path.open('rb') as source:
                content = source.read(MAX_IMPORT + 1)
            if len(content) > MAX_IMPORT:
                raise setup.SetupError('Select a My Clippings.txt file no larger than 16 MiB.')
            rows = parse_clippings(content.decode('utf-8-sig'))
            port = self.collector_port()
            token = (self.collector_data_dir() / 'token').read_text().strip()
            for row in rows:
                payload = json.dumps({'source': 'koreader', 'device_id': 'kindle-clippings-import', 'highlights': [row]}).encode()
                result = json.loads(setup.http(f'http://127.0.0.1:{port}/v1/highlights', payload, headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, maximum=4096, timeout=15))
                if result.get('accepted') != [row['id']]:
                    raise setup.SetupError('Collector did not acknowledge every import. Retry safely; duplicates are ignored.')
        elif command == 'import_archive':
            path = setup.guarded(Path(text_arg(args.get('path'), 'archive file')).expanduser())
            if not path.is_file() or path.stat().st_size > MAX_EXPORT:
                raise setup.SetupError('Choose a JSON archive no larger than 64 MiB.')
            from import_archive import parse_archive
            try:
                with path.open('rb') as source:
                    data = source.read(MAX_EXPORT + 1)
                if len(data) > MAX_EXPORT:
                    raise ValueError('Choose a JSON archive no larger than 64 MiB.')
                parsed, tombstones = parse_archive(data)
            except ValueError as exc:
                raise setup.SetupError(str(exc)) from exc
            if not self.authenticated(self.collector_port()):
                raise setup.SetupError('Start your local collector before importing highlights.')
            token = (self.collector_data_dir() / 'token').read_text().strip()
            # Restore deletion history first, before a replay could reintroduce a quote.
            for offset in range(0, len(tombstones), 128):
                batch = tombstones[offset:offset + 128]
                try:
                    result = json.loads(setup.http(f'http://127.0.0.1:{self.collector_port()}/v1/tombstones',
                        json.dumps({'tombstones': batch}).encode(), headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, maximum=16384, timeout=15))
                except HTTPError as exc:
                    if exc.code == 404:
                        raise setup.SetupError('Update your collector before restoring this archive’s deletion history. No highlights were imported.') from exc
                    raise
                if result.get('accepted') != batch:
                    raise setup.SetupError('Deletion-history import was interrupted. Retry safely before importing more highlights.')
            # Bound payloads even when a record contains a long quote.
            for entry in parsed:
                row = entry['item']
                payload = json.dumps({'source': entry['source'], 'device_id': 'reader-bridge-archive-import', 'highlights': [row]}).encode()
                result = json.loads(setup.http(f'http://127.0.0.1:{self.collector_port()}/v1/highlights', payload,
                    headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, maximum=4096, timeout=15))
                if result.get('accepted') != [row['id']]:
                    raise setup.SetupError('Import was interrupted. Retry safely; duplicates are ignored.')
            live, _ = self.live_rows()
            live_ids = {row['id'] for row in live}
            cover_rows = [entry['cover'] for entry in parsed if entry['item']['id'] in live_ids]
            try:
                saved, failed = CoverLibrary(self.app).import_rows(cover_rows)
            except (OSError, ValueError, setup.SetupError):
                saved, failed = 0, len({book_key(r['title'], r['author']) for r in cover_rows})
            extra['import_result'] = {'highlights': len({e['item']['id'] for e in parsed} & live_ids), 'covers': saved, 'unavailable': failed}
        elif command == 'cache_covers':
            rows = self.status()['books']
            try:
                saved, failed = CoverLibrary(self.app).import_rows(rows)
            except (OSError, ValueError, setup.SetupError) as exc:
                raise setup.SetupError('Covers could not be saved. Check the app folder permissions and retry.') from exc
            extra['import_result'] = {'highlights': 0, 'covers': saved, 'unavailable': failed}
        elif command == 'set_cover':
            title = text_arg(args.get('title'), 'book title')
            author = args.get('author', '')
            if not isinstance(author, str) or len(author.encode()) > 4096:
                raise setup.SetupError('Author must be text no longer than 4096 bytes.')
            live, _ = self.live_rows()
            if book_key(title, author) not in {row['book_id'] for row in live}:
                raise setup.SetupError('Select a book from your highlight library first.')
            path = setup.guarded(Path(text_arg(args.get('path'), 'cover file')).expanduser())
            try:
                if not path.is_file() or path.stat().st_size > (MAX_EPUB if path.suffix.lower() == '.epub' else MAX_IMAGE):
                    raise ValueError('Choose a JPEG or PNG up to 5 MiB, or an EPUB up to 128 MiB.')
                data = epub_cover(path) if path.suffix.lower() == '.epub' else path.read_bytes()
                covers = CoverLibrary(self.app)
                covers.put(title, author, data)
                covers.save()
            except Exception as exc:
                # File details and EPUB contents do not belong in user-facing errors.
                message = str(exc) if type(exc) is ValueError else 'Could not read this cover. Choose a valid JPEG, PNG or EPUB.'
                raise setup.SetupError(message) from exc
        elif command == 'export':
            path = setup.guarded(Path(text_arg(args.get('path'), 'export file')).expanduser())
            if path == self.app or self.app in path.parents or (self.app.parent / 'Reading Highlights') in path.parents or path.exists():
                raise setup.SetupError('Choose a new export filename outside the private app directory.')
            rows, _ = self.live_rows()
            try:
                covers = CoverLibrary(self.app)
                for row in rows:
                    metadata = covers.metadata(row['title'], row['author'])
                    row['cover_path'] = metadata['cover_path'] or row['cover_path']
                    row['cover_url'] = metadata['cover_url'] or row['cover_url']
                exported_rows = covers.export_rows(rows)
            except (OSError, ValueError, setup.SetupError):
                # Artwork is optional: it must never block the archive recovery path.
                exported_rows = [{k: v for k, v in row.items() if k != 'cover_path'} for row in rows]
                extra['export_warning'] = 'Highlights and deletion history exported. Cached covers could not be read and were omitted.'
            database = self.collector_data_dir() / 'inbox.sqlite3'
            tombstones = []
            if database.exists():
                with self.inbox_connection(database) as con:
                    tombstones = sorted(row[0] for row in con.execute('SELECT quote_key FROM tombstones'))
            archive = {'format': 'reader-bridge', 'version': 1, 'highlights': exported_rows, 'tombstones': tombstones}
            data = (json.dumps(archive, ensure_ascii=False, indent=2) + '\n').encode()
            if len(data) > MAX_EXPORT:
                raise setup.SetupError('Export exceeds 64 MiB. Preserve the inbox and use a database export tool.')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, 'wb') as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            extra['exported'] = len(rows)
        elif command == 'start_progress':
            self.start_progress(args.get('endpoint'), port_arg(args['port']) if args.get('port') is not None else None)
        elif command == 'stop_progress':
            self.stop_progress()
        elif command == 'pair_progress_kindle':
            self.pair_progress_kindle(text_arg(args.get('mount'), 'Kindle volume'),
                                     bool_arg(args['auto_sync'], 'auto_sync') if 'auto_sync' in args else None)
        elif command == 'pair_progress_xteink':
            self.pair_progress_xteink(args.get('mount'), args.get('device_url'), text_arg(args.get('model'), 'CrossPoint model'))
        elif command == 'verify_local_progress':
            progress = self.progress_status()
            if not (progress['kindle_paired'] or progress['xteink_paired']) or not self.progress_authenticated():
                raise setup.SetupError('Connect a reader to the running progress service before confirming the test.')
            if progress['kindle_paired'] and self.state['progress_sync']['kindle'].get('guard_version') != 1:
                raise setup.SetupError('Reconnect Kindle to install stale-upload protection before confirming the test.')
            self.state['progress_sync']['verified'] = bool_arg(args.get('verified'), 'verified')
            self.save()
        elif command == 'verify_progress':
            self.state['progress'] = {'verified': bool_arg(args.get('verified'), 'verified'), 'server': 'https://sync.crosspointreader.com'}
            self.save()
        elif command == 'configure_cloud_backup':
            self.configure_cloud_backup(args.get('provider', 'icloud'), args.get('folder'))
        elif command == 'backup_now':
            if not self.state.get('cloud_backup', {}).get('enabled'):
                raise setup.SetupError('Choose a backup destination first.')
            config = json.loads((self.app / 'cloud_backup/config.json').read_text())
            try:
                archive_backup.snapshot(self.collector_data_dir() / 'inbox.sqlite3', self.app, config['destination'], self.app / 'cloud_backup')
            except (OSError, ValueError) as exc:
                raise setup.SetupError('Backup could not finish. Check the destination and available space; previous backups are retained.') from exc
        elif command == 'disable_cloud_backup':
            self.stop_owned('cloud_backup')
            path = self.agent_path('cloud_backup')
            if self.owned('cloud_backup'):
                self.backup(path)
                path.unlink()
            self.state.setdefault('cloud_backup', {})['enabled'] = False
            self.state['cloud_backup'].pop('agent', None)
            self.save()
        elif command == 'restore_backup':
            if not self.authenticated(self.collector_port()):
                raise setup.SetupError('Start your local bridge before restoring a backup.')
            try:
                archive_backup.restore(text_arg(args.get('path'), 'backup file'), self.collector_data_dir() / 'inbox.sqlite3', self.app)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                raise setup.SetupError('Backup could not be restored. Choose a downloaded Reader Bridge backup; existing data and recovery snapshots are retained.') from exc
        elif command == 'configure_backup':
            archive = text_arg(args.get('archive'), 'personal archive OWNER/REPO')
            create = bool_arg(args.get('create', False), 'create')
            self.collector(archive, create=create, port=port_arg(self.state.get('collector', {}).get('port', 8084)))
            self.state['collector']['backup_disabled'] = False
            self.state['collector']['mode'] = 'github'
            self.save()
        elif command == 'disable_backup':
            if not self.state.get('collector'):
                raise setup.SetupError('Start the collector first.')
            self.start_local(port_arg(self.state['collector'].get('port', 8084)), disable=True)
        elif command == 'install_library':
            self.stable_installation()
            self.library(text_arg(args.get('books'), 'Calibre library folder'), port_arg(args.get('port', 8083)))
        else:
            raise setup.SetupError('Unknown desktop command.')
        return {**self.status(args.get('query', ''), args.get('book_id', '')), **extra}


@contextlib.contextmanager
def mutation_lock(app):
    app = setup.guarded(app)
    app.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = setup.guarded(app / '.desktop.lock')
    with lock.open('a') as handle:
        os.chmod(lock, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise setup.SetupError('Another setup action is running. Wait for it to finish and retry.')
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def execute(request, app=setup.DEFAULT_APP, agent_dir=None):
    if not isinstance(request, dict):
        raise setup.SetupError('Request must be a JSON object.')
    command = text_arg(request.get('command'), 'command')
    commands = {'start_progress', 'stop_progress', 'pair_progress_kindle', 'pair_progress_xteink', 'verify_local_progress', 'connect_existing', 'status', 'start_collector', 'stop_collector', 'pair_kindle', 'pair_xteink', 'import_clippings', 'import_archive', 'set_cover', 'cache_covers', 'export', 'verify_progress', 'configure_backup', 'disable_backup', 'install_library', 'configure_cloud_backup', 'disable_cloud_backup', 'backup_now', 'restore_backup'}
    if command not in commands:
        raise setup.SetupError('Unknown desktop command.')
    app = setup.guarded(Path(app).expanduser())
    if command == 'status':
        query = request.get('query', '')
        if not isinstance(query, str) or len(query) > 1000 or '\x00' in query:
            raise setup.SetupError('Search must be text no longer than 1000 characters.')
        book_id = request.get('book_id', '')
        if not isinstance(book_id, str) or (book_id and not re.fullmatch(r'[a-f0-9]{64}', book_id)):
            raise setup.SetupError('Select a valid book.')
        return Desktop(app, agent_dir=agent_dir).status(query, book_id)
    with mutation_lock(app):
        return Desktop(app, agent_dir=agent_dir).mutate(command, request)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-dir', type=Path, default=setup.DEFAULT_APP)
    args = parser.parse_args(argv)
    try:
        body = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(body) > MAX_REQUEST:
            raise setup.SetupError('Desktop request exceeds 64 KiB.')
        request = json.loads(body)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            data = execute(request, args.app_dir)
        response = {'ok': True, 'data': data}
    except Exception as exc:
        response = {'ok': False, 'error': str(exc) if isinstance(exc, setup.SetupError) else 'Action could not complete. Check your inputs, connectivity and folder permissions, then retry; completed checkpoints are preserved.'}
    print(json.dumps(response, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
