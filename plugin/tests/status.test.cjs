'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { describeFailure, isOutdatedFailure, parseStatus, samePath } = require('../lib/status');

const REPORT = {
  daemon: 'Running',
  pid: '13608',
  service: { installed: true, enabled: true, detail: 'Running' },
  vault: 'C:\\Users\\corbi\\wasabi\\Notes',
  last_sync: '2026-11-07 15:55:54',
  last_log: '2026-11-07 16:15:55,904 [INFO] Next sync in 600 seconds',
};

test('a status report is normalized', () => {
  assert.deepEqual(parseStatus(JSON.stringify(REPORT)), {
    daemon: 'Running',
    pid: '13608',
    service: { installed: true, enabled: true, detail: 'Running' },
    vault: REPORT.vault,
    vaultProblem: null,
    lastSync: REPORT.last_sync,
    lastLog: REPORT.last_log,
  });
});

test('an unknown service stays unknown rather than reading as disabled', () => {
  const parsed = parseStatus(JSON.stringify({ ...REPORT, service: { installed: null, enabled: null, detail: 'timed out' } }));
  assert.equal(parsed.service.installed, null);
  assert.equal(parsed.service.enabled, null);
  assert.equal(parsed.service.detail, 'timed out');
  assert.equal(parseStatus(JSON.stringify({ daemon: 'Stopped' })).service.enabled, null);
});

test('a vault problem is reported, and its absence is null', () => {
  assert.equal(parseStatus(JSON.stringify({ ...REPORT, vault_problem: 'Git remote not configured: origin' })).vaultProblem,
    'Git remote not configured: origin');
  assert.equal(parseStatus(JSON.stringify(REPORT)).vaultProblem, null);
});

test('a fresh install has no sync or log yet', () => {
  const parsed = parseStatus(JSON.stringify({ ...REPORT, pid: null, last_sync: null, last_log: null }));
  assert.equal(parsed.pid, null);
  assert.equal(parsed.lastSync, null);
  assert.equal(parsed.lastLog, null);
});

test('unreadable output is an error, never an empty status', () => {
  assert.throws(() => parseStatus('+---+\n| Status | Running |'), /unreadable/);
  assert.throws(() => parseStatus('null'), /incomplete/);
  assert.throws(() => parseStatus('{}'), /incomplete/);
});

test('an old companion is recognized by click rejecting --json', () => {
  assert.equal(isOutdatedFailure({ stderr: "Usage: obsidian-sync status [OPTIONS]\nError: No such option: --json\n" }), true);
  assert.equal(isOutdatedFailure({ stderr: "Error: No such option '--json'" }), true);
  assert.equal(isOutdatedFailure({ stderr: 'Error: Config not found' }), false);
  assert.equal(isOutdatedFailure(new Error('spawn failed')), false);
});

test('failures describe the last stderr line, and timeouts say so', () => {
  assert.equal(describeFailure({ stderr: 'Usage: x\nError: task queued\n' }), 'Error: task queued');
  assert.equal(describeFailure({ killed: true, message: 'Command failed', stderr: '' }), 'Timed out: Command failed');
  assert.equal(describeFailure(new Error('plain')), 'plain');
});

test('paths compare across slash styles, and by case only on Windows', () => {
  assert.equal(samePath('C:/Users/corbi/wasabi/Notes', 'C:\\Users\\corbi\\wasabi\\notes\\', 'win32'), true);
  assert.equal(samePath('/Users/a/Notes', '/Users/a/notes', 'darwin'), false);
  assert.equal(samePath('/Users/a/Notes/', '/Users/a/Notes', 'linux'), true);
});
