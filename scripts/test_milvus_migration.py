"""Exercise the offline migration gate with a Docker double and real Linux flock.

No Docker daemon or host services are accessed by this test suite.
"""
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
VOLUME = "originalproject_milvus-data"

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys, time
a = sys.argv[1:]
p = pathlib.Path(os.environ['FAKE_STATE'])
s = json.loads(p.read_text())
with open(os.environ['FAKE_LOG'], 'a') as out: out.write(json.dumps(a) + '\n')
def save(): p.write_text(json.dumps(s))
def output(value): print(value); sys.exit(0)
if a[0] == 'info': output('fake-daemon')
if a[:2] == ['container', 'ls']:
    name = a[a.index('--filter')+1].removeprefix('name=^/').removesuffix('$')
    output(name if name in s['containers'] else '')
if a[:2] == ['image', 'inspect']:
    if s.get('missing_image'): sys.exit(1)
    output('sha256:' + '1'*64)
if a[:2] == ['volume', 'ls']:
    output(s['volume'] if s.get('volume_exists', True) else '')
if a[:2] == ['volume', 'inspect']:
    if not s.get('volume_exists', True): sys.exit(1)
    output(s['volume'] + '|2026-11-18T00:00:00Z|local|' + os.environ['FAKE_SOURCE'])
if a[0] == 'inspect':
    c = s['containers'].get(a[-1])
    if c is None: sys.exit(1)
    fmt = a[a.index('--format')+1]
    if '.Config.Env' in fmt:
        for key in ['ETCD_USE_EMBED', 'ETCD_ENDPOINTS', 'ETCD_DATA_DIR']:
            if '"' + key + '"' in fmt:
                output(key + '=' + c[key] if key in c else '')
        sys.exit(2)
    if '.Mounts' in fmt:
        if 'println .Destination' in fmt: output('\n'.join(['/var/lib/milvus', *c.get('nested_mounts',[])]))
        output(c.get('mount_type', 'volume') + '\t' + c.get('volume', s['volume']))
    if '.Config.Image' in fmt: output(c.get('config_image', 'milvusdb/milvus:v2.6.17'))
    if '.Config.Cmd' in fmt: output(c.get('data_arg', '--data-dir=/var/lib/milvus/etcd'))
    if '.Image' in fmt: output('sha256:' + '1'*64)
    if '.Id' in fmt: output('2'*64)
    if '.State.Status' in fmt: output(c['state'])
    sys.exit(2)
if a[0] == 'stop':
    s['containers'][a[-1]]['state'] = 'exited'; save(); output(a[-1])
if a[0] == 'ps':
    output('\n'.join(n for n,c in s['containers'].items() if c['state'] in ['running','restarting','paused'] and c.get('volume',s['volume']) == s['volume']))
if a[0] == 'run':
    assert '--read-only' in a
    assert a[a.index('--network')+1] == 'none'
    assert a[a.index('--mount')+1] == 'type=volume,source='+s['volume']+',target=/source,readonly'
    ep = a[a.index('--entrypoint')+1]
    if ep == 'du':
        sys.exit(subprocess.run(['du','-sk',os.environ['FAKE_SOURCE']]).returncode)
    assert ep == 'tar', a
    s['tar_runs'] = s.get('tar_runs',0)+1; save()
    if s.get('backup_fail'):
        sys.stdout.write('incomplete archive'); sys.exit(1)
    if os.environ.get('FAKE_TAR_GATE') and s['tar_runs'] == 1:
        gate = pathlib.Path(os.environ['FAKE_TAR_GATE']); gate.with_suffix('.started').touch()
        until = time.monotonic()+15
        while not gate.exists() and time.monotonic() < until: time.sleep(.02)
        if not gate.exists(): sys.exit(3)
    args = a[a.index('--entrypoint')+3:]
    args[args.index('/source')] = os.environ['FAKE_SOURCE']
    code = subprocess.run(['tar',*args]).returncode
    if s.get('mutate_source') and s['tar_runs'] == 1:
        pathlib.Path(os.environ['FAKE_SOURCE'],'etcd','member','data').write_text('changed by other writer')
    sys.exit(code)
sys.exit(2)
'''


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base / 'source'
        (self.source / 'etcd/member').mkdir(parents=True)
        (self.source / 'etcd/member/data').write_bytes(b'original metadata')
        (self.source / 'rocksmq').mkdir()
        (self.source / 'rocksmq/messages').write_bytes(b'original vector messages')
        self.state_path = self.base / 'state.json'
        self.log = self.base / 'calls.jsonl'
        self.state = {'volume': VOLUME, 'volume_exists': True, 'containers': {
            'aidocumines_milvus': {'state': 'running', 'ETCD_USE_EMBED': 'true', 'ETCD_DATA_DIR': '/var/lib/milvus/etcd'},
        }}
        self.write_state()
        executable = self.base / 'docker'
        executable.write_text(FAKE_DOCKER, newline='\n')
        executable.chmod(0o700)
        self.env = {**os.environ, 'PATH': str(self.base)+os.pathsep+os.environ['PATH'],
                    'FAKE_STATE': str(self.state_path), 'FAKE_LOG': str(self.log), 'FAKE_SOURCE': str(self.source),
                    'MILVUS_BACKUP_DIR': str(self.base/'backups'), 'AIDOCUMINES_RECOVERY_LOCK_FILE': str(self.base/'recovery.lock'),
                    'AIDOCUMINES_COMPOSE_PROJECT': 'originalproject'}
        self.env.pop('AIDOCUMINES_RECOVERY_LOCK_FD', None)

    def write_state(self):
        self.state_path.write_text(json.dumps(self.state))

    def run_helper(self, **overrides):
        return subprocess.run(['bash', str(ROOT/'deploy/prepare-milvus-etcd.sh')], env={**self.env, **overrides},
                              capture_output=True, text=True, timeout=30)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def metadata(self, **values):
        self.state['containers']['aidocumines_milvus_etcd'] = {
            'state': 'exited', 'config_image': 'quay.io/coreos/etcd:v3.5.23', **values}

    def archives(self):
        return list((self.base/'backups').rglob('*.tar.gz'))

    def test_backup_is_verified_offline_private_and_preserves_original_data(self):
        before = {str(p.relative_to(self.source)): p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Verified offline Milvus backup', result.stdout)
        self.assertEqual(before, {str(p.relative_to(self.source)): p.read_bytes() for p in self.source.rglob('*') if p.is_file()})
        archive, = self.archives()
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertEqual(archive.parent.stat().st_mode & 0o777, 0o700)
        self.assertTrue((archive.parent/'verified-snapshot').exists())
        listing = subprocess.check_output(['tar','-tzf',str(archive)], text=True)
        self.assertIn('./etcd/member/data', listing)
        self.assertIn('./rocksmq/messages', listing)
        calls = self.calls()
        stop = next(i for i,c in enumerate(calls) if c[0] == 'stop')
        first_read = next(i for i,c in enumerate(calls) if c[0] == 'run')
        self.assertLess(stop, first_read)
        self.assertFalse(any(c[0] in ['start','restart','rm','update'] for c in calls))

    def test_retry_reuses_only_a_verified_unchanged_snapshot(self):
        self.assertEqual(self.run_helper().returncode, 0)
        archive, = self.archives()
        previous = archive.read_bytes()
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Verified existing offline backup', result.stdout)
        self.assertEqual(self.archives(), [archive])
        self.assertEqual(archive.read_bytes(), previous)
        (self.source/'rocksmq/messages').write_bytes(b'new snapshot')
        self.assertEqual(self.run_helper().returncode, 0)
        self.assertEqual(len(self.archives()), 2)
        self.assertEqual(archive.read_bytes(), previous)

    def test_failed_backup_aborts_without_receipt_or_restart(self):
        self.state['backup_fail'] = True; self.write_state()
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('offline backup failed', result.stderr)
        self.assertEqual(self.archives(), [])
        self.assertEqual(list((self.base/'backups').rglob('*.partial')), [])
        self.assertEqual(list((self.base/'backups').rglob('verified-snapshot')), [])
        self.assertEqual(json.loads(self.state_path.read_text())['containers']['aidocumines_milvus']['state'], 'exited')
        self.assertEqual((self.source/'etcd/member/data').read_bytes(), b'original metadata')

    def test_insufficient_backup_disk_space_aborts_without_archive(self):
        df = self.base/'df'
        df.write_text('#!/bin/sh\nprintf "Filesystem 1024-blocks Used Available Capacity Mounted\\nnone 100 100 0 100%% /\\n"\n', newline='\n')
        df.chmod(0o700)
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('insufficient backup disk space', result.stderr)
        self.assertEqual(self.archives(), [])
        self.assertEqual((self.source/'etcd/member/data').read_bytes(), b'original metadata')

    def test_corrupt_prior_archive_cannot_be_reused_as_migration_proof(self):
        self.assertEqual(self.run_helper().returncode, 0)
        archive, = self.archives()
        archive.write_bytes(b'corrupt saved archive')
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('Verified existing offline backup', result.stdout)
        self.assertEqual(len(self.archives()), 2)
        self.assertEqual(archive.read_bytes(), b'corrupt saved archive')
        receipt = (archive.parent/'verified-snapshot').read_text()
        self.assertNotIn('archive='+archive.name+'\n', receipt)

    def test_source_changes_during_backup_are_rejected(self):
        self.state['mutate_source'] = True; self.write_state()
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('source contents changed', result.stderr)
        self.assertEqual(self.archives(), [])

    def test_conflicting_embedded_and_external_writers_are_not_modified(self):
        self.metadata(state='running'); self.write_state()
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('coexist', result.stderr)
        self.assertFalse(any(c[0] in ['stop','run','start','rm','update'] for c in self.calls()))

    def test_another_running_volume_consumer_blocks_backup(self):
        self.state['containers']['unknown-writer'] = {'state': 'running'}; self.write_state()
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unknown-writer', result.stderr)
        self.assertFalse(any(c[0] == 'run' for c in self.calls()))

    def test_missing_volume_and_bind_mount_are_rejected_before_stopping(self):
        for kind in ['missing', 'bind']:
            with self.subTest(kind=kind):
                self.state['volume_exists'] = kind != 'missing'
                self.state['containers']['aidocumines_milvus']['mount_type'] = 'bind' if kind == 'bind' else 'volume'
                self.write_state()
                self.assertNotEqual(self.run_helper().returncode, 0)
                self.assertFalse(any(c[0] == 'stop' for c in self.calls()))

    def test_nested_embedded_metadata_mount_is_rejected_before_any_backup(self):
        self.state['containers']['aidocumines_milvus']['nested_mounts'] = ['/var/lib/milvus/etcd']
        self.write_state()
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('parent-volume backup would omit that data', result.stderr)
        self.assertFalse(any(c[0] in ['stop','run'] for c in self.calls()))
        self.assertFalse((self.base/'backups').exists())

    def test_nested_vector_or_rocksmq_mount_is_rejected_before_any_backup(self):
        for destination in ['/var/lib/milvus/rdb_data', '/var/lib/milvus/data/vectors']:
            with self.subTest(destination=destination):
                self.state['containers']['aidocumines_milvus']['nested_mounts'] = [destination]
                self.write_state()
                result = self.run_helper()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('nested storage mount', result.stderr)
                self.assertFalse(any(c[0] in ['stop','run'] for c in self.calls()))
                self.assertFalse((self.base/'backups').exists())

    def test_nested_external_metadata_mount_is_rejected_for_either_handoff_state(self):
        for mode in ['embedded', 'external']:
            with self.subTest(mode=mode):
                self.metadata(nested_mounts=['/var/lib/milvus/etcd'])
                if mode == 'external':
                    self.state['containers']['aidocumines_milvus'].update(ETCD_USE_EMBED='false', ETCD_ENDPOINTS='milvus_etcd:2379')
                self.write_state()
                result = self.run_helper()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('nested storage mount', result.stderr)
                self.assertFalse(any(c[0] in ['stop','run'] for c in self.calls()))
                self.assertFalse((self.base/'backups').exists())

    def test_custom_or_missing_embedded_data_dir_requires_manual_resolution(self):
        for value in [None, '/custom/etcd']:
            old = self.state['containers']['aidocumines_milvus']
            if value is None: old.pop('ETCD_DATA_DIR', None)
            else: old['ETCD_DATA_DIR'] = value
            self.write_state()
            result = self.run_helper()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('data path', result.stderr)
            self.assertFalse(any(c[0] == 'stop' for c in self.calls()))

    def test_absent_embed_flag_is_treated_as_legacy_embedded_mode(self):
        self.state['containers']['aidocumines_milvus'].pop('ETCD_USE_EMBED')
        self.write_state()
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.archives()), 1)

    def test_verified_already_external_deployment_is_left_running(self):
        self.state['containers']['aidocumines_milvus'].update(ETCD_USE_EMBED='false', ETCD_ENDPOINTS='milvus_etcd:2379')
        self.metadata(state='running'); self.write_state()
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('verified external etcd volume', result.stdout)
        self.assertFalse(any(c[0] in ['stop','run'] for c in self.calls()))

    def test_unknown_external_endpoint_version_volume_and_path_are_rejected(self):
        self.state['containers']['aidocumines_milvus'].update(ETCD_USE_EMBED='false', ETCD_ENDPOINTS='other:2379')
        self.metadata(state='running'); self.write_state()
        self.assertNotEqual(self.run_helper().returncode, 0)
        self.state['containers']['aidocumines_milvus']['ETCD_ENDPOINTS'] = 'milvus_etcd:2379'
        for values in [{'volume':'different'}, {'config_image':'etcd:latest'}, {'data_arg':'--data-dir=/custom'}]:
            self.metadata(**values); self.write_state()
            self.assertNotEqual(self.run_helper().returncode, 0)
        self.assertFalse(any(c[0] in ['stop','run'] for c in self.calls()))

    def test_missing_main_container_does_not_mean_existing_volume_is_empty(self):
        self.state['containers'].clear(); self.write_state()
        self.assertNotEqual(self.run_helper().returncode, 0)
        self.metadata(state='running'); self.write_state()
        self.assertEqual(self.run_helper().returncode, 0)

    def test_fresh_deployment_requires_project_and_no_existing_store(self):
        self.state['containers'].clear(); self.state['volume_exists'] = False; self.write_state()
        self.assertEqual(self.run_helper().returncode, 0)
        self.assertNotEqual(self.run_helper(AIDOCUMINES_COMPOSE_PROJECT='').returncode, 0)

    def test_different_compose_project_cannot_replace_the_actual_volume(self):
        result = self.run_helper(AIDOCUMINES_COMPOSE_PROJECT='differentproject')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Compose project', result.stderr)
        self.assertFalse(any(c[0] in ['stop','run'] for c in self.calls()))

    def test_shared_lock_blocks_watchdog_through_backup_and_parent_handoff(self):
        gate = self.base/'release-backup'
        ready = self.base/'handoff-ready'
        release = self.base/'release-handoff'
        library = shlex.quote(str(ROOT/'deploy/recovery-lock.sh'))
        helper = shlex.quote(str(ROOT/'deploy/prepare-milvus-etcd.sh'))
        command = f'source {library}; acquire_recovery_lock wait || exit; bash {helper} || exit; touch "$READY"; while [[ ! -e "$RELEASE" ]]; do sleep .02; done'
        proc = subprocess.Popen(['bash','-c',command], env={**self.env,'FAKE_TAR_GATE':str(gate),'READY':str(ready),'RELEASE':str(release)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is not None or proc.kill())
        def wait_for(path):
            until = time.monotonic()+10
            while not path.exists() and time.monotonic() < until and proc.poll() is None: time.sleep(.02)
            self.assertTrue(path.exists(), f'process ended={proc.poll()} before {path}')
        def watchdog_try():
            return subprocess.run(['bash','-c',f'source {library}; acquire_recovery_lock try'], env=self.env, capture_output=True, text=True).returncode
        wait_for(gate.with_suffix('.started'))
        self.assertEqual(watchdog_try(), 75)
        gate.touch(); wait_for(ready)
        self.assertEqual(watchdog_try(), 75)
        release.touch()
        stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, stderr+stdout)
        self.assertEqual(watchdog_try(), 0)


class RecoveryLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name); self.lock = self.base/'recovery.lock'
        self.env = {**os.environ,'AIDOCUMINES_RECOVERY_LOCK_FILE':str(self.lock)}
        self.env.pop('AIDOCUMINES_RECOVERY_LOCK_FD',None)
        self.library = shlex.quote(str(ROOT/'deploy/recovery-lock.sh'))

    def run_lock(self, prefix='', **values):
        return subprocess.run(['bash','-c',prefix+f'source {self.library}; acquire_recovery_lock try'], env={**self.env,**values}, capture_output=True, text=True)

    def test_new_lock_is_regular_world_readable_and_never_truncated(self):
        self.assertEqual(self.run_lock().returncode, 0)
        self.assertEqual(self.lock.stat().st_mode & 0o777, 0o644)
        self.lock.write_text('unchanged sentinel'); self.lock.chmod(0o444)
        self.assertEqual(self.run_lock().returncode, 0)
        self.assertEqual(self.lock.read_text(), 'unchanged sentinel')

    def test_busy_lock_returns_75_and_symlinks_are_rejected(self):
        self.lock.touch()
        with self.lock.open('r') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.run_lock().returncode, 75)
        self.lock.unlink(); target=self.base/'target'; target.write_text('safe'); self.lock.symlink_to(target)
        self.assertNotEqual(self.run_lock().returncode, 0)
        self.assertEqual(target.read_text(), 'safe')

    def test_inherited_descriptor_must_reference_the_real_lock_inode(self):
        other=self.base/'other'; other.touch(); self.lock.touch()
        result=self.run_lock(prefix=f'exec 9< {shlex.quote(str(other))}; ', AIDOCUMINES_RECOVERY_LOCK_FD='9')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('does not refer', result.stderr)

    def test_directory_and_unopened_inherited_descriptor_are_rejected(self):
        self.lock.mkdir()
        self.assertNotEqual(self.run_lock().returncode, 0)
        self.lock.rmdir()
        self.assertNotEqual(self.run_lock(AIDOCUMINES_RECOVERY_LOCK_FD='200').returncode, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
