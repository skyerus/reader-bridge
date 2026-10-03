import copy
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import sqlite3
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import archive_backup
from collector import Store as HighlightStore, initialize
import desktop
import lua_settings
import progress_setup
import progress_sync as sync
import setup


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = sync.Store(self.root/'positions.sqlite3')
        self.account = sync.credentials(self.root)
        self.server = sync.server(self.store,self.account,'127.0.0.1',0)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.item = {'document':'a'*32,'progress':'/body/DocFragment[5]/body/p[3]/text().0','percentage':0.45,'device':'Kindle','device_id':'kindle-fixture'}
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def request(self, method, path, item=None, headers=None):
        client = http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=2)
        try:
            body = json.dumps(item) if item is not None else None
            client.request(method,path,body,{**sync.auth_headers(self.account),'Content-Type':'application/json',**(headers or {})})
            response=client.getresponse();return response.status,json.loads(response.read())
        finally: client.close()
    def test_native_client_roundtrip_latest_upload_wins_even_when_rereading(self):
        self.assertEqual(self.request('GET','/users/auth')[0],200)
        self.assertEqual(self.request('GET','/syncs/progress/'+'a'*32)[0],404)
        self.assertEqual(self.request('PUT','/syncs/progress',self.item)[0],200)
        code,row=self.request('GET','/syncs/progress/'+'a'*32)
        self.assertEqual((code,row['progress'],row['device']), (200,self.item['progress'],'Kindle'))
        earlier=dict(self.item,progress='/body/DocFragment[2]/body/p[1]/text().0',percentage=.1,device='CrossPoint',device_id='crosspoint-reader')
        self.assertEqual(self.request('PUT','/syncs/progress',earlier)[0],200)
        row=self.request('GET','/syncs/progress/'+'a'*32)[1]
        self.assertEqual(row['percentage'],.1);self.assertEqual(row['device'],'CrossPoint')
        self.assertEqual(sync.Store(self.store.path).get('a'*32),row)
        self.assertEqual(len(sync.read_uploads(self.store.path)),2)
    def test_authentication_registration_and_invalid_payloads_never_write(self):
        self.assertEqual(self.request('PUT','/syncs/progress',self.item,{'x-auth-key':'wrong'})[0],401)
        self.assertEqual(self.request('POST','/users/create',{'username':'stranger','password':'key'})[0],403)
        for changed in [{'document':'../other'},{'percentage':float('nan')},{'percentage':1.5},{'percentage':True},{'progress':''},{'device_id':None}]:
            self.assertEqual(self.request('PUT','/syncs/progress',{**self.item,**changed})[0],400)
        self.assertEqual(sync.read_archive(self.store.path),[])
        self.assertEqual(self.request('PUT','/syncs/progress',{**self.item,'progress':'x'*70000})[0],413)
    def test_migration_keeps_original_timestamp_and_restore_keeps_current_state(self):
        item={**self.item,'timestamp':100}
        self.assertEqual(self.store.seed([item]),1)
        self.assertEqual(self.store.get('a'*32)['timestamp'],100)
        self.assertEqual(sync.read_uploads(self.store.path),[],'Migration must not claim real device traffic')
        newer=dict(item,percentage=.6,timestamp=200)
        self.store.seed([newer]);self.assertEqual(self.store.get('a'*32)['percentage'],.45)
        self.store.seed([newer],newer=True);self.assertEqual(self.store.get('a'*32)['percentage'],.6)
        self.store.seed([item],newer=True);self.assertEqual(self.store.get('a'*32)['timestamp'],200)
    def test_backups_include_positions_without_credentials_and_restore_both(self):
        app=self.root/'app';data,_=initialize(app/'collector/data');HighlightStore(data/'inbox.sqlite3')
        positions=sync.Store(app/'progress_sync/positions.sqlite3');positions.put(self.item)
        account=sync.credentials(app/'progress_sync')
        destination=self.root/'backups';destination.mkdir()
        receipt=archive_backup.snapshot(data/'inbox.sqlite3',app,destination,app/'cloud_backup')
        manifest,_=archive_backup.read_snapshot(receipt['path'])
        self.assertEqual(manifest['version'],2)
        self.assertEqual(manifest['positions'][0]['progress'],self.item['progress'])
        self.assertNotIn(account['password'],json.dumps(manifest));self.assertNotIn(account['username'],json.dumps(manifest))
        positions.put(dict(self.item,percentage=.7))
        archive_backup.restore(receipt['path'],data/'inbox.sqlite3',app)
        self.assertEqual(positions.get('a'*32)['percentage'],.7)
        other=self.root/'other';other_data,_=initialize(other/'collector/data');HighlightStore(other_data/'inbox.sqlite3')
        archive_backup.restore(receipt['path'],other_data/'inbox.sqlite3',other)
        self.assertEqual(sync.read_archive(other/'progress_sync/positions.sqlite3')[0]['percentage'],.45)
        self.assertFalse((other/'progress_sync/credentials.json').exists())
    def test_credentials_survive_restart_and_are_private(self):
        self.assertEqual(sync.credentials(self.root),self.account)
        self.assertEqual((self.root/'credentials.json').stat().st_mode & 0o777,0o600)

    def guarded(self, revision, **changes):
        return {**self.item, 'metadata':{'reader_bridge':{'version':1,'base_revision':revision}}, **changes}

    def test_delayed_kindle_upload_cannot_replace_xteink_position(self):
        self.store.require_guard(self.item['device_id'])
        original=self.request('PUT','/syncs/progress',self.guarded('missing'))[1]
        remote={**self.item,'progress':'xteink-new','percentage':.6,'device':'CrossPoint','device_id':'xteink'}
        self.request('PUT','/syncs/progress',remote)
        before=self.store.get(self.item['document'])
        code,result=self.request('PUT','/syncs/progress',self.guarded(original['reader_bridge_revision']))
        self.assertEqual(code,409);self.assertTrue(result['reader_bridge_conflict'])
        self.assertEqual(self.store.get(self.item['document']),before)
        self.assertEqual(result['current']['device'],'CrossPoint')
        self.assertEqual(next(x for x in sync.read_uploads(self.store.path) if x['device']=='Kindle')['count'],1)

    def test_old_queue_without_revision_is_blocked_after_pairing(self):
        self.store.put(dict(self.item,progress='new',device='CrossPoint',device_id='xteink'))
        self.store.require_guard(self.item['device_id'])
        self.assertEqual(self.request('PUT','/syncs/progress',self.item)[0],409)
        self.assertEqual(self.store.get(self.item['document'])['progress'],'new')

    def test_fetch_does_not_authorize_an_unacknowledged_old_position(self):
        revision=self.store.put(self.item)['reader_bridge_revision']
        self.store.put(dict(self.item,progress='new',device='CrossPoint',device_id='xteink'))
        self.request('GET','/syncs/progress/'+self.item['document'])
        self.assertEqual(self.request('PUT','/syncs/progress',self.guarded(revision))[0],409)

    def test_backward_reading_after_pull_and_explicit_force_are_allowed(self):
        revision=self.store.put(self.item)['reader_bridge_revision']
        earlier=self.guarded(revision, progress='rereading',percentage=.1)
        self.assertEqual(self.request('PUT','/syncs/progress',earlier)[0],200)
        self.assertEqual(self.store.get(self.item['document'])['percentage'],.1)
        force=self.guarded('unknown',progress='intentional',percentage=.05)
        force['metadata']['reader_bridge']['force']=True
        self.assertEqual(self.request('PUT','/syncs/progress',force)[0],200)
        self.assertEqual(self.store.get(self.item['document'])['progress'],'intentional')

    def test_echo_retains_remote_device_timestamp_and_revision(self):
        before={**self.item,'device':'CrossPoint','device_id':'xteink'}
        self.store.put(before)
        before=self.store.get(self.item['document'])
        self.assertEqual(self.request('PUT','/syncs/progress',self.guarded('unknown'))[0],200)
        self.assertEqual(self.store.get(self.item['document']),before)

    def test_revision_and_device_guard_survive_restart_and_same_second_updates(self):
        self.store.require_guard(self.item['device_id'])
        with patch.object(sync.time,'time',return_value=100):
            first=self.store.put(self.guarded('missing'))
            second=self.store.put(self.guarded(first['reader_bridge_revision'],progress='next'))
        self.assertNotEqual(first['reader_bridge_revision'],second['reader_bridge_revision'])
        restarted=sync.Store(self.store.path)
        self.assertEqual(restarted.get(self.item['document'])['reader_bridge_revision'],second['reader_bridge_revision'])
        with self.assertRaises(sync.Conflict): restarted.put(self.item)
        with self.assertRaises(sync.Conflict): restarted.put(self.guarded(first['reader_bridge_revision']))

    def test_only_one_concurrent_update_can_use_a_revision(self):
        revision=self.store.put(self.item)['reader_bridge_revision']
        barrier=threading.Barrier(2);results=[]
        def worker(position):
            barrier.wait()
            try: self.store.put(self.guarded(revision,progress=position));results.append('accepted')
            except sync.Conflict: results.append('conflict')
        threads=[threading.Thread(target=worker,args=(str(n),)) for n in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertCountEqual(results,['accepted','conflict'])

    def test_legacy_database_upgrades_without_faking_uploads(self):
        legacy=self.root/'legacy.sqlite3'
        item={**self.item,'timestamp':123}
        with sqlite3.connect(legacy) as con:
            con.execute('CREATE TABLE positions(document TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            con.execute('INSERT INTO positions VALUES(?,?)',(self.item['document'],json.dumps(item)))
        upgraded=sync.Store(legacy).get(self.item['document'])
        self.assertEqual({k:v for k,v in upgraded.items() if k!='reader_bridge_revision'},item)
        self.assertEqual(sync.read_uploads(legacy),[])

    def test_invalid_guard_cannot_bypass_protection(self):
        self.store.put(self.item);self.store.require_guard(self.item['device_id'])
        for control in [[],{'version':1,'base_revision':None},{'version':True,'base_revision':'unknown'},
                        {'version':1,'base_revision':'unknown','force':'true'}]:
            item={**self.item,'progress':'invalid','metadata':{'reader_bridge':control}}
            self.assertEqual(self.request('PUT','/syncs/progress',item)[0],400)


class LuaSettingsTests(unittest.TestCase):
    def test_roundtrip_preserves_literal_settings_without_executing_code(self):
        text='-- saved settings\nreturn { ["settings"] = { custom_server="https://example.org", auto_sync=true, ["other"]={ [1]="café\\010quote\\\"", [2]=false }, }, ["keep"]=2.3e-5 }'
        value=lua_settings.loads(text)
        self.assertEqual(lua_settings.loads(lua_settings.dumps(value)),value)
        for payload in ['return os.execute("bad")','return {} os.execute("bad")','return {x=function() end}','return {["x"]=1,["x"]=2}']:
            with self.assertRaises(ValueError): lua_settings.loads(payload)


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.bridge=desktop.Desktop(self.root/'app',agent_dir=self.root/'agents')
        self.bridge.state['progress_sync']={'enabled':True,'port':8085,'endpoint':'http://192.168.1.20:8085'}
        self.account=sync.credentials(self.bridge.progress_directory())
        self.kind=self.root/'Kindle';self.kor=self.kind/'koreader';(self.kor/'settings').mkdir(parents=True);(self.kor/'reader.lua').write_text('fixture')
        (self.kor/'settings.reader.lua').write_text(lua_settings.dumps({'device_id':'kindle-fixture'}))
        (self.kor/'frontend').mkdir();(self.kor/'frontend/userpatch.lua').write_text('-- registerPatchPluginFunc fixture')
        (self.kor/'plugins/kosync.koplugin').mkdir(parents=True)
        (self.kor/'plugins/kosync.koplugin/main.lua').write_text('\n'.join('function KOSync:'+name+'() end' for name in ('getMetadata','updateProgress','getProgress','syncToProgress','_onCloseDocument','_onNetworkConnected')))
        self.path=self.kor/'settings/kosync.lua'
        self.original={'settings':{'username':'old','userkey':'old-hash','custom_server':'https://sync.crosspointreader.com','auto_sync':False,'checksum_method':0,'sync_forward':2,'unknown':'keep'}}
        self.path.write_text(lua_settings.dumps(self.original))
        (self.kind/'fixture.epub').write_bytes(b'epub fixture bytes')
        self.document=next(iter(progress_setup.book_documents(self.kind)))
        self.item={'document':self.document,'progress':'/body/DocFragment[3]/body/p[2].0','percentage':.3,'device':'CrossPoint','device_id':'crosspoint-reader','timestamp':100}
    def tearDown(self): self.temp.cleanup()
    def test_migration_seeds_public_positions_and_keeps_other_settings_and_local_book(self):
        def http(url,**kw):
            return json.dumps({'username':'old'} if url.endswith('/users/auth') else self.item).encode()
        with patch.object(self.bridge,'progress_authenticated',return_value=True),patch.object(setup,'http',side_effect=http): self.bridge.pair_progress_kindle(str(self.kind))
        now=lua_settings.loads(self.path.read_text())['settings']
        self.assertEqual(now['custom_server'],'http://192.168.1.20:8085');self.assertEqual(now['username'],self.account['username'])
        self.assertFalse(now['auto_sync']);self.assertEqual(now['unknown'],'keep');self.assertEqual(now['sync_forward'],2)
        self.assertEqual(sync.read_archive(self.bridge.progress_directory()/'positions.sqlite3')[0]['timestamp'],100)
        self.assertEqual((self.kind/'fixture.epub').read_bytes(),b'epub fixture bytes')
        self.assertTrue(any(self.original['settings']['userkey'] in p.read_text() for p in (self.bridge.app/'backups').glob('*kosync.lua')))
        self.assertTrue((self.kor/'patches/2-reader-bridge-progress.lua').is_file())
        self.assertEqual(self.bridge.state['progress_sync']['kindle']['guard_version'],1)
        with self.assertRaises(sync.Conflict):
            sync.Store(self.bridge.progress_directory()/'positions.sqlite3').put(dict(self.item,device_id='kindle-fixture',progress='old'))

    def test_disabled_patches_refuse_pairing_without_changing_settings(self):
        (self.kor/'patches').mkdir();(self.kor/'patches/.patches_disabled').touch()
        before=self.path.read_bytes()
        with patch.object(self.bridge,'progress_authenticated',return_value=True):
            with self.assertRaises(setup.SetupError):self.bridge.pair_progress_kindle(str(self.kind))
        self.assertEqual(self.path.read_bytes(),before)

    def test_fresh_kindle_auto_sync_sets_required_wifi_action(self):
        self.path.unlink()
        reader = self.kor/'settings.reader.lua'
        reader.write_text(lua_settings.dumps({'device_id':'kindle-fixture',
            'wifi_enable_action':'prompt', 'auto_disable_wifi':True, 'auto_suspend_timeout_seconds':900}))
        before = reader.read_bytes()
        with patch.object(self.bridge,'progress_authenticated',return_value=True):
            self.bridge.pair_progress_kindle(str(self.kind))
        network = lua_settings.loads(reader.read_text())
        sync_settings = lua_settings.loads(self.path.read_text())['settings']
        # These are the two conditions used by native KOSync:init to keep auto sync enabled.
        self.assertTrue(sync_settings['auto_sync'])
        self.assertEqual(network['wifi_enable_action'],'turn_on')
        self.assertTrue(network['auto_disable_wifi'])
        self.assertEqual(network['auto_suspend_timeout_seconds'],900)
        self.assertNotIn('pages_before_update',sync_settings)
        self.assertTrue(any(p.read_bytes()==before for p in (self.bridge.app/'backups').glob('*settings.reader.lua')))

    def test_explicit_auto_sync_repairs_native_disabled_setting(self):
        self.original['settings'].update(username=self.account['username'],
            custom_server=self.bridge.state['progress_sync']['endpoint'])
        self.path.write_text(lua_settings.dumps(self.original))
        with patch.object(self.bridge,'progress_authenticated',return_value=True):
            self.bridge.mutate('pair_progress_kindle',{'mount':str(self.kind),'auto_sync':True})
        self.assertTrue(lua_settings.loads(self.path.read_text())['settings']['auto_sync'])
        self.assertEqual(lua_settings.loads((self.kor/'settings.reader.lua').read_text())['wifi_enable_action'],'turn_on')

    def test_manual_sync_keeps_network_preferences(self):
        self.path.unlink()
        reader = self.kor/'settings.reader.lua'
        before = reader.read_bytes()
        with patch.object(self.bridge,'progress_authenticated',return_value=True):
            self.bridge.pair_progress_kindle(str(self.kind),auto_sync=False)
        self.assertFalse(lua_settings.loads(self.path.read_text())['settings']['auto_sync'])
        self.assertEqual(reader.read_bytes(),before)

    def test_auto_sync_pairing_failure_restores_network_and_sync_settings(self):
        self.path.write_text(lua_settings.dumps({'settings':{'auto_sync':False}}))
        reader = self.kor/'settings.reader.lua'
        before_reader, before_sync = reader.read_bytes(), self.path.read_bytes()
        original_install = self.bridge.install_file
        def fail_patch(path,data):
            if path.name=='2-reader-bridge-progress.lua': raise OSError('USB disconnected')
            original_install(path,data)
        with patch.object(self.bridge,'progress_authenticated',return_value=True),patch.object(self.bridge,'install_file',side_effect=fail_patch):
            with self.assertRaisesRegex(OSError,'USB disconnected'):
                self.bridge.pair_progress_kindle(str(self.kind),auto_sync=True)
        self.assertEqual(reader.read_bytes(),before_reader)
        self.assertEqual(self.path.read_bytes(),before_sync)
        self.assertNotIn('kindle',self.bridge.state['progress_sync'])

    def test_invalid_auto_sync_option_cannot_change_reader(self):
        before=self.path.read_bytes()
        with self.assertRaises(setup.SetupError):
            self.bridge.mutate('pair_progress_kindle',{'mount':str(self.kind),'auto_sync':'yes'})
        self.assertEqual(self.path.read_bytes(),before)
    def test_failed_old_server_leaves_reader_settings_unchanged(self):
        before=self.path.read_bytes()
        with patch.object(self.bridge,'progress_authenticated',return_value=True),patch.object(setup,'http',side_effect=OSError('offline')):
            with self.assertRaises(setup.SetupError):self.bridge.pair_progress_kindle(str(self.kind))
        self.assertEqual(before,self.path.read_bytes());self.assertNotIn('kindle',self.bridge.state['progress_sync'])
    def test_queued_position_newer_than_server_is_migrated_but_same_account_repair_keeps_queue(self):
        queue_path=self.kor/'settings/kosync_queue.lua'
        queue_path.write_text(lua_settings.dumps({1:{**self.item,'progress':'later','queued_at':200}}))
        with patch.object(self.bridge,'progress_authenticated',return_value=True),patch.object(setup,'http',return_value=json.dumps(self.item).encode()):self.bridge.pair_progress_kindle(str(self.kind))
        self.assertEqual(sync.read_archive(self.bridge.progress_directory()/'positions.sqlite3')[0]['progress'],'later')
        self.assertEqual(lua_settings.loads(queue_path.read_text()),{})
        queue_path.write_text(lua_settings.dumps({1:{**self.item,'progress':'offline-new','queued_at':300}}))
        before=queue_path.read_bytes()
        with patch.object(self.bridge,'progress_authenticated',return_value=True),patch.object(setup,'http',side_effect=AssertionError('No external requests on re-pair')):self.bridge.pair_progress_kindle(str(self.kind))
        self.assertEqual(queue_path.read_bytes(),before)
    def test_xteink_configuration_preserves_behavior_and_separates_highlights(self):
        self.bridge.state['progress_sync']['kindle']={'endpoint':self.bridge.state['progress_sync']['endpoint']}
        card=self.root/'card';(card/'.crosspoint').mkdir(parents=True)
        config=card/'.crosspoint/koreader.json';config.write_text(json.dumps({'password_obf':'old','syncBehavior':0,'extra':'keep'}))
        highlights=card/'.crosspoint/highlight-sync.json';highlights.write_text('unchanged highlight token')
        with patch.object(self.bridge,'progress_authenticated',return_value=True):self.bridge.pair_progress_xteink(mount=str(card),model='xteink_x4_pro')
        value=json.loads(config.read_text());self.assertEqual(value['syncBehavior'],0);self.assertEqual(value['extra'],'keep')
        self.assertNotIn('password_obf',value);self.assertEqual(value['password'],self.account['password']);self.assertEqual(value['matchMethod'],1)
        self.assertEqual(highlights.read_text(),'unchanged highlight token')

    def test_crosspoint_only_positions_pair_without_kindle(self):
        card=self.root/'card';(card/'.crosspoint').mkdir(parents=True)
        with patch.object(self.bridge,'progress_authenticated',return_value=True):
            self.bridge.pair_progress_xteink(mount=str(card),model='xteink_x4')
            status=self.bridge.progress_status()
            self.assertTrue(status['xteink_paired'])
            self.assertFalse(status['kindle_paired'])
            self.assertEqual(status['xteink_model'],'xteink_x4')
            self.bridge.mutate('verify_local_progress', {'verified':True})
            self.assertTrue(self.bridge.progress_status()['verified'])
        self.assertEqual(json.loads((card/'.crosspoint/koreader.json').read_text())['serverUrl'],self.bridge.state['progress_sync']['endpoint'])

    def test_progress_model_must_match_highlight_reader(self):
        self.bridge.state['xteink']={'paired':True} # historical X4 Pro
        card=self.root/'card';(card/'.crosspoint').mkdir(parents=True)
        with patch.object(self.bridge,'progress_authenticated',return_value=True):
            with self.assertRaisesRegex(setup.SetupError,'same model'):
                self.bridge.pair_progress_xteink(mount=str(card),model='xteink_x4')
        self.assertFalse((card/'.crosspoint/koreader.json').exists())

    def test_crosspoint_only_existing_remote_account_is_preserved(self):
        card=self.root/'card';(card/'.crosspoint').mkdir(parents=True)
        config=card/'.crosspoint/koreader.json'
        original=json.dumps({'cfgVersion':2,'username':'previous','password_obf':'private','serverUrl':'https://sync.crosspointreader.com'})
        config.write_text(original)
        with patch.object(self.bridge,'progress_authenticated',return_value=True):
            with self.assertRaisesRegex(setup.SetupError,'Choose Finish setup for now') as error:
                self.bridge.pair_progress_xteink(mount=str(card),model='xteink_x4')
        self.assertNotIn('Kindle',str(error.exception))
        self.assertNotIn('private',str(error.exception))
        self.assertEqual(config.read_text(),original)
        self.assertNotIn('xteink',self.bridge.state['progress_sync'])

    def test_same_username_on_another_server_does_not_silently_switch(self):
        card=self.root/'card';(card/'.crosspoint').mkdir(parents=True)
        config=card/'.crosspoint/koreader.json'
        original=json.dumps({'cfgVersion':2,'username':self.account['username'],'password_obf':'private','serverUrl':'https://sync.crosspointreader.com'})
        config.write_text(original)
        with patch.object(self.bridge,'progress_authenticated',return_value=True):
            with self.assertRaisesRegex(setup.SetupError,'Choose Finish setup for now'):
                self.bridge.pair_progress_xteink(mount=str(card),model='xteink_x4')
        self.assertEqual(config.read_text(),original)
        self.assertNotIn('xteink',self.bridge.state['progress_sync'])

    def test_same_local_account_repair_preserves_reader_preferences(self):
        card=self.root/'card';(card/'.crosspoint').mkdir(parents=True)
        config=card/'.crosspoint/koreader.json'
        config.write_text(json.dumps({'cfgVersion':2,'username':self.account['username'],'password_obf':'reader-obfuscation',
                                     'serverUrl':self.bridge.state['progress_sync']['endpoint'],'syncBehavior':1,'extra':'keep'}))
        with patch.object(self.bridge,'progress_authenticated',return_value=True),patch.object(setup,'http',side_effect=AssertionError('No remote migration on repair')):
            self.bridge.pair_progress_xteink(mount=str(card),model='xteink_x4')
        value=json.loads(config.read_text())
        self.assertEqual(value['extra'],'keep');self.assertEqual(value['syncBehavior'],1)
        self.assertEqual(value['password'],self.account['password'])

    def test_protected_progress_config_refuses_lan_without_changing_pairing_or_positions(self):
        self.bridge.state['progress_sync'].update(xteink={'model':'xteink_x4','endpoint':'http://192.168.1.20:8085'},verified=True)
        self.bridge.save()
        before=copy.deepcopy(self.bridge.state);saved=self.bridge.state_path.read_bytes()
        store=sync.Store(self.bridge.progress_directory()/'positions.sqlite3');store.seed([self.item])
        positions=sync.read_archive(store.path)
        def http(url,**kwargs):
            if url.endswith('/api/status'): return b'{"device":"X4"}'
            raise HTTPError(url,403,'Protected',{},None)
        with patch.object(self.bridge,'progress_authenticated',return_value=True),patch.object(setup,'http',side_effect=http) as requests:
            with self.assertRaisesRegex(setup.SetupError,'USB drive mode') as error:
                self.bridge.pair_progress_xteink(device_url='http://192.168.1.42',model='xteink_x4')
        self.assertEqual(requests.call_count,2)
        self.assertNotIn(self.account['password'],str(error.exception))
        self.assertEqual(self.bridge.state,before);self.assertEqual(self.bridge.state_path.read_bytes(),saved)
        self.assertEqual(sync.read_archive(store.path),positions)
        self.assertFalse((self.bridge.app/'backups').exists())

    def test_protected_progress_upload_has_storage_recovery_and_keeps_pairing(self):
        self.bridge.save();before=copy.deepcopy(self.bridge.state);saved=self.bridge.state_path.read_bytes()
        def http(url,data=None,**kwargs):
            if url.endswith('/api/status'): return b'{"device":"X4"}'
            raise HTTPError(url,403 if '/upload?' in url else 404,'Unavailable',{},None)
        with patch.object(self.bridge,'progress_authenticated',return_value=True),patch.object(setup,'http',side_effect=http) as requests:
            with self.assertRaisesRegex(setup.SetupError,'USB drive mode'):
                self.bridge.pair_progress_xteink(device_url='http://192.168.1.42',model='xteink_x4')
        self.assertEqual(requests.call_count,3)
        self.assertFalse(any('/delete?' in call.args[0] for call in requests.call_args_list))
        self.assertEqual(self.bridge.state,before);self.assertEqual(self.bridge.state_path.read_bytes(),saved)

    def test_legacy_lan_does_not_delete_existing_progress_config_for_repair(self):
        original=json.dumps({'cfgVersion':2,'username':self.account['username'],'password_obf':'private',
                             'serverUrl':self.bridge.state['progress_sync']['endpoint'],'syncBehavior':1,'extra':'keep'}).encode()
        before=copy.deepcopy(self.bridge.state)
        with patch.object(self.bridge,'progress_authenticated',return_value=True),patch.object(setup,'http',side_effect=[b'{"device":"X4"}',original]) as requests:
            with self.assertRaisesRegex(setup.SetupError,'update existing progress settings safely'):
                self.bridge.pair_progress_xteink(device_url='http://192.168.1.42',model='xteink_x4')
        self.assertEqual(requests.call_count,2);self.assertEqual(self.bridge.state,before)
        self.assertFalse((self.bridge.app/'backups').exists())

    def test_first_time_legacy_lan_pairing_still_verifies_uploaded_config(self):
        reader={}
        def http(url,data=None,**kwargs):
            if url.endswith('/api/status'): return b'{"device":"X4"}'
            if '/upload?' in url:
                reader['config']=data.split(b'\r\n\r\n',1)[1].rsplit(b'\r\n--',1)[0]
                return b'ok'
            if 'config' not in reader: raise HTTPError(url,404,'Missing',{},None)
            return reader['config']
        with patch.object(self.bridge,'progress_authenticated',return_value=True),patch.object(setup,'http',side_effect=http) as requests:
            self.bridge.pair_progress_xteink(device_url='http://192.168.1.42',model='xteink_x4')
        value=json.loads(reader['config'])
        self.assertEqual(value['password'],self.account['password'])
        self.assertEqual(value['serverUrl'],self.bridge.state['progress_sync']['endpoint'])
        self.assertEqual(requests.call_count,4)
        self.assertTrue(self.bridge.progress_status()['xteink_paired'])

    def test_usb_pairing_restores_reader_and_pairing_if_write_or_save_fails(self):
        card=self.root/'card';(card/'.crosspoint').mkdir(parents=True)
        config=card/'.crosspoint/koreader.json'
        original=json.dumps({'cfgVersion':2,'username':self.account['username'],'password_obf':'private',
                             'serverUrl':self.bridge.state['progress_sync']['endpoint'],'extra':'keep'}).encode()
        self.bridge.state['progress_sync'].update(xteink={'model':'xteink_x4','endpoint':'http://192.168.1.20:8085'},verified=True)
        self.bridge.save();before=copy.deepcopy(self.bridge.state);saved=self.bridge.state_path.read_bytes()
        for failure in ('install_file','save'):
            with self.subTest(failure=failure):
                config.write_bytes(original)
                operation=getattr(self.bridge,failure)
                def fail(*args,**kwargs):
                    operation(*args,**kwargs)
                    raise OSError('simulated interrupted write')
                with patch.object(self.bridge,'progress_authenticated',return_value=True),patch.object(self.bridge,failure,side_effect=fail):
                    with self.assertRaises(OSError):self.bridge.pair_progress_xteink(mount=str(card),model='xteink_x4')
                self.assertEqual(config.read_bytes(),original)
                self.assertEqual(self.bridge.state,before);self.assertEqual(self.bridge.state_path.read_bytes(),saved)
                self.assertTrue(any(p.read_bytes()==original for p in (self.bridge.app/'backups').glob('*koreader.json')))

    def test_invalid_crosspoint_progress_config_stays_unchanged(self):
        card=self.root/'card';(card/'.crosspoint').mkdir(parents=True)
        config=card/'.crosspoint/koreader.json';config.write_bytes(b'{invalid-private-settings')
        before=copy.deepcopy(self.bridge.state)
        with patch.object(self.bridge,'progress_authenticated',return_value=True):
            with self.assertRaisesRegex(setup.SetupError,'Unrecognized Xteink settings') as error:
                self.bridge.pair_progress_xteink(mount=str(card),model='xteink_x4')
        self.assertNotIn('private',str(error.exception))
        self.assertEqual(config.read_bytes(),b'{invalid-private-settings');self.assertEqual(self.bridge.state,before)

    def test_different_xteink_account_cannot_silently_lose_its_remote_positions(self):
        self.bridge.state['progress_sync']['kindle']={'endpoint':self.bridge.state['progress_sync']['endpoint']}
        self.bridge.state['progress_sync']['migration_source']={'username':'old','server':'https://sync.crosspointreader.com'}
        card=self.root/'card';(card/'.crosspoint').mkdir(parents=True)
        config=card/'.crosspoint/koreader.json'
        original=json.dumps({'cfgVersion':2,'username':'different-account','password_obf':'private','serverUrl':'https://sync.crosspointreader.com'})
        config.write_text(original)
        with patch.object(self.bridge,'progress_authenticated',return_value=True):
            with self.assertRaises(setup.SetupError):self.bridge.pair_progress_xteink(mount=str(card),model='xteink_x4_pro')
        self.assertEqual(config.read_text(),original)

    def test_filename_matching_migrates_to_matching_binary_ids(self):
        self.original['settings']['checksum_method']=1
        self.path.write_text(lua_settings.dumps(self.original))
        old_document=hashlib.md5(b'fixture.epub').hexdigest()
        requested=[]
        def http(url,**kwargs):
            requested.append(url)
            return json.dumps({'username':'old'} if url.endswith('/users/auth') else dict(self.item,document=old_document)).encode()
        with patch.object(self.bridge,'progress_authenticated',return_value=True),patch.object(setup,'http',side_effect=http):self.bridge.pair_progress_kindle(str(self.kind))
        self.assertTrue(any(url.endswith('/'+old_document) for url in requested))
        rows=sync.read_archive(self.bridge.progress_directory()/'positions.sqlite3')
        self.assertEqual(rows[0]['document'],self.document)
        self.assertEqual(lua_settings.loads(self.path.read_text())['settings']['checksum_method'],0)

    def test_status_does_not_initialize_storage_and_unowned_port_is_rejected(self):
        empty=desktop.Desktop(self.root/'empty',agent_dir=self.root/'empty-agents')
        self.assertEqual(empty.progress_status()['book_count'],0)
        self.assertFalse(empty.app.exists())
        with patch.object(setup,'available',return_value=False):
            with self.assertRaises(setup.SetupError):empty.check_port('progress_sync',8085)

    def test_old_authenticated_service_can_upgrade_but_cannot_pair_guard_yet(self):
        def http(url,**kwargs):
            return json.dumps({'service':'reader-bridge-progress','state':'OK'} if url.endswith('/healthcheck') else {'username':self.account['username']}).encode()
        with patch.object(setup,'http',side_effect=http):
            self.assertFalse(self.bridge.progress_authenticated())
            self.assertTrue(self.bridge.progress_authenticated(require_guard=False))
            with self.assertRaises(setup.SetupError):self.bridge.paired_progress_endpoint()
            with patch.object(setup,'available',return_value=False),patch.object(self.bridge,'assert_ownership'),patch.object(self.bridge,'owned',return_value=True):
                self.bridge.check_port('progress_sync',8085)
