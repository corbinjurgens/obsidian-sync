'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const {
  backupPath, formFromConfig, patchConfigText, resolveConfigPath, restoreConfig, validateForm, writeConfig,
} = require('../lib/config');

const PYYAML_DUMP = `vault_path: C:/vault
sync:
  interval_seconds: 600
  pull_strategy: merge
  remote: origin
  branch: main
commit:
  template: '{{date}} wrapped by PyYAML at eighty columns [host={{hostname}}] [files={{numFiles}}]
    [changed={{files}}]'
  date_format: '%Y-%m-%d %H:%M:%S'
  list_files_in_body: false
log:
  path: C:/log/sync.log
  max_size_mb: 10
lock_path: C:/log/daemon.lock
`;

const FORM = {
  interval: '300', pullStrategy: 'merge', remote: 'origin', branch: 'main',
  backoffInitial: '0', backoffMultiplier: '2', backoffMax: '900', template: 'sync: {{hostname}}',
};

function tempDir(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'sync-companion-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}

test('patching replaces only the named keys', () => {
  const patched = patchConfigText(PYYAML_DUMP, { 'sync.interval_seconds': 300 });
  assert.equal(patched, PYYAML_DUMP.replace('interval_seconds: 600', 'interval_seconds: 300'));
});

test('patching consumes a wrapped scalar without touching its neighbours', () => {
  const patched = patchConfigText(PYYAML_DUMP, { 'commit.template': 'sync: {{hostname}}' });
  assert.match(patched, /^ {2}template: "sync: \{\{hostname\}\}"\n {2}date_format: /m);
  assert.doesNotMatch(patched, /changed=/);
});

test('patching adds missing keys inside their section and creates missing sections', () => {
  const patched = patchConfigText('vault_path: /v\nsync:\n  remote: origin\n\nlog:\n  max_size_mb: 10\n', {
    'sync.backoff_max_seconds': 60,
    'commit.template': 'x',
  });
  assert.equal(patched, 'vault_path: /v\nsync:\n  remote: origin\n  backoff_max_seconds: 60\n\nlog:\n  max_size_mb: 10\ncommit:\n  template: "x"\n');
});

test('patching keeps comments, unknown keys, indentation and line endings', () => {
  const text = '# mine\r\nsync:\r\n    # why\r\n    interval_seconds: 60\r\n    extra: 1\r\ncustom: yes\r\n';
  const patched = patchConfigText(text, { 'sync.interval_seconds': 5 });
  assert.equal(patched, '# mine\r\nsync:\r\n    # why\r\n    interval_seconds: 5\r\n    extra: 1\r\ncustom: yes\r\n');
});

test('patching does not match a same-named key in another section', () => {
  const patched = patchConfigText('commit:\n  remote: keep\nsync:\n  remote: origin\n', { 'sync.remote': 'up' });
  assert.equal(patched, 'commit:\n  remote: keep\nsync:\n  remote: "up"\n');
});

test('patching escapes strings as YAML double-quoted scalars', () => {
  assert.equal(patchConfigText('', { 'commit.template': 'a "b" \\ {{date}}' }), 'commit:\n  template: "a \\"b\\" \\\\ {{date}}"\n');
});

test('patching refuses flow-style sections instead of corrupting them', () => {
  assert.throws(() => patchConfigText('sync: {remote: origin}\n', { 'sync.remote': 'up' }), /block-style/);
});

test('an empty config seeds a loadable file', () => {
  assert.equal(patchConfigText('', { vault_path: 'C:/My Vault', 'sync.pull_strategy': 'merge' }),
    'vault_path: "C:/My Vault"\nsync:\n  pull_strategy: "merge"\n');
});

test('writing backs up the exact previous file and leaves no temporary file', (t) => {
  const root = tempDir(t);
  const file = path.join(root, 'config.yaml');
  fs.writeFileSync(file, PYYAML_DUMP);
  const now = new Date('2026-11-07T01:02:03.004Z');
  const written = writeConfig(file, { 'sync.branch': 'trunk' }, { now });
  assert.equal(written.backup, backupPath(file, now));
  assert.equal(fs.readFileSync(written.backup, 'utf8'), PYYAML_DUMP);
  assert.match(fs.readFileSync(file, 'utf8'), /branch: "trunk"/);
  assert.deepEqual(fs.readdirSync(root).sort(), ['config.yaml', path.basename(written.backup)].sort());
});

test('a second write never overwrites an earlier backup', (t) => {
  const file = path.join(tempDir(t), 'config.yaml');
  fs.writeFileSync(file, PYYAML_DUMP);
  const now = new Date('2026-11-07T01:02:03.004Z');
  writeConfig(file, { 'sync.branch': 'a' }, { now });
  assert.throws(() => writeConfig(file, { 'sync.branch': 'b' }, { now }), /EEXIST/);
  assert.match(fs.readFileSync(file, 'utf8'), /branch: "a"/);
});

test('a refused layout writes nothing, not even a backup', (t) => {
  const root = tempDir(t);
  const file = path.join(root, 'config.yaml');
  fs.writeFileSync(file, 'sync: {remote: origin}\n');
  assert.throws(() => writeConfig(file, { 'sync.remote': 'up' }), /block-style/);
  assert.deepEqual(fs.readdirSync(root), ['config.yaml']);
});

test('creating a config makes its directory and needs no backup', (t) => {
  const file = path.join(tempDir(t), 'new', 'config.yaml');
  assert.deepEqual(writeConfig(file, { vault_path: '/v' }), { backup: null, created: true });
  assert.equal(fs.readFileSync(file, 'utf8'), 'vault_path: "/v"\n');
});

test('restoring returns the previous bytes or removes a created file', (t) => {
  const root = tempDir(t);
  const file = path.join(root, 'config.yaml');
  fs.writeFileSync(file, PYYAML_DUMP);
  restoreConfig(file, writeConfig(file, { 'sync.branch': 'x' }));
  assert.equal(fs.readFileSync(file, 'utf8'), PYYAML_DUMP);

  const created = path.join(root, 'created.yaml');
  restoreConfig(created, writeConfig(created, { vault_path: '/v' }));
  assert.equal(fs.existsSync(created), false);
});

test('the Windows task metadata selects a custom config path', () => {
  const custom = 'C:\\Users\\Corbin\\AppData\\Local\\obsidian-sync\\config.yaml';
  const readText = () => JSON.stringify({ config_path: custom });
  assert.equal(resolveConfigPath({ platform: 'win32', home: 'C:\\Users\\Corbin', readText }), custom);
});

test('the default config path applies without Windows metadata and off Windows', () => {
  const missing = () => { throw Object.assign(new Error('gone'), { code: 'ENOENT' }); };
  assert.equal(resolveConfigPath({ platform: 'win32', home: '/h', readText: missing }),
    path.join('/h', '.config', 'obsidian-sync', 'config.yaml'));
  assert.equal(resolveConfigPath({ platform: 'darwin', home: '/h', readText: () => '{"config_path":"/other"}' }),
    path.join('/h', '.config', 'obsidian-sync', 'config.yaml'));
});

test('a valid form becomes typed config fields', () => {
  const { values, errors } = validateForm(FORM);
  assert.deepEqual(errors, []);
  assert.equal(values['sync.interval_seconds'], 300);
  assert.equal(values['sync.backoff_initial_seconds'], 0);
  assert.equal(values['sync.backoff_multiplier'], 2);
});

test('invalid numbers are rejected with their labels', () => {
  const { errors } = validateForm({ ...FORM, interval: '0', backoffInitial: '-1', backoffMultiplier: '0.5', backoffMax: '1.5' });
  assert.equal(errors.length, 4);
  assert.match(errors.join(' '), /Sync interval.*First retry delay.*Longest retry delay.*Retry multiplier/);
  assert.equal(validateForm({ ...FORM, backoffMultiplier: 'abc' }).errors.length, 1);
  assert.equal(validateForm({ ...FORM, interval: '' }).errors.length, 1);
});

test('a remote URL is refused so credentials cannot land in the config', () => {
  for (const remote of ['https://user:token@github.com/x.git', 'git@github.com:x.git', '-upload-pack=x', '']) {
    assert.match(validateForm({ ...FORM, remote }).errors.join(' '), /remote name/);
  }
});

test('option-like branches and multi-line templates are refused', () => {
  assert.equal(validateForm({ ...FORM, branch: '--force' }).errors.length, 1);
  assert.equal(validateForm({ ...FORM, branch: 'feature/x' }).errors.length, 0);
  assert.equal(validateForm({ ...FORM, template: 'a\nb' }).errors.length, 1);
  assert.equal(validateForm({ ...FORM, pullStrategy: 'reset' }).errors.length, 1);
});

test('form values round-trip from companion config output', () => {
  const form = formFromConfig({
    sync: { interval_seconds: 600, pull_strategy: 'merge', remote: 'origin', branch: 'main', backoff_initial_seconds: 0, backoff_multiplier: 2.0, backoff_max_seconds: 900 },
    commit: { template: 'sync: {{hostname}}' },
  });
  assert.deepEqual(validateForm(form).errors, []);
});
