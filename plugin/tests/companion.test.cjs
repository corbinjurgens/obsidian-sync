'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { Companion } = require('../lib/companion');

const FORM = {
  interval: '300', pullStrategy: 'merge', remote: 'origin', branch: 'main',
  backoffInitial: '0', backoffMultiplier: '2', backoffMax: '900', template: 'sync: {{hostname}}',
};

function defaults(vault) {
  return {
    vault_path: vault,
    sync: { interval_seconds: 600, pull_strategy: 'merge', remote: 'origin', branch: 'main', backoff_initial_seconds: 0, backoff_multiplier: 2.0, backoff_max_seconds: 900 },
    commit: { template: 'sync: {{hostname}}' },
  };
}

// A mocked CLI: `service` and the config on disk stand in for the real companion.
function harness(t, { vaultProblem = null, service = { installed: true, enabled: false, detail: 'Disabled' }, gitActive = true, withConfig = true, outdated = false, fail = {}, uv = true, platform = 'linux' } = {}) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'sync-companion-'));
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  const vault = path.join(home, 'vault');
  fs.mkdirSync(path.join(vault, '.obsidian', 'plugins', 'obsidian-git'), { recursive: true });
  const configFile = path.join(home, '.config', 'obsidian-sync', 'config.yaml');
  const settings = defaults(vault);
  if (withConfig) {
    fs.mkdirSync(path.dirname(configFile), { recursive: true });
    fs.writeFileSync(configFile, `vault_path: ${vault}\nsync:\n  interval_seconds: 600\n`);
  }
  const calls = [];
  const run = async (file, args) => {
    const action = file === 'uv' ? 'uv' : args[4] === '--strict' ? 'verify' : args[2];
    calls.push(action === 'uv' ? args.join(' ') : action);
    if (fail[action]) throw Object.assign(new Error('failed'), { stderr: fail[action] });
    if (action === 'status') {
      if (outdated) throw Object.assign(new Error('failed'), { stderr: 'Error: No such option: --json' });
      const text = fs.existsSync(configFile) ? fs.readFileSync(configFile, 'utf8') : '';
      const configured = /vault_path: "?(.+?)"?$/m.exec(text);
      return { stdout: JSON.stringify({ daemon: 'Stopped', service, vault_problem: vaultProblem, vault: configured ? JSON.parse(`"${configured[1]}"`.replaceAll(/(?<!\\)\\(?!\\)/g, '\\\\')) : vault }) };
    }
    if (action === 'config' || action === 'verify') {
      const text = fs.existsSync(configFile) ? fs.readFileSync(configFile, 'utf8') : '';
      const saved = structuredClone(settings);
      const interval = /interval_seconds: (\d+)/.exec(text);
      if (interval) saved.sync.interval_seconds = Number(interval[1]);
      const seededVault = /vault_path: "?(.+?)"?$/m.exec(text);
      if (seededVault) saved.vault_path = seededVault[1].replaceAll('\\\\', '\\');
      for (const key of ['pull_strategy', 'remote', 'branch', 'backoff_initial_seconds', 'backoff_multiplier', 'backoff_max_seconds']) {
        const match = new RegExp(`${key}: "?([^"\\n]+)"?`).exec(text);
        if (match) saved.sync[key] = typeof saved.sync[key] === 'number' ? Number(match[1]) : match[1];
      }
      const template = /template: (".*")/.exec(text);
      if (template) saved.commit.template = JSON.parse(template[1]);
      return { stdout: JSON.stringify(saved) };
    }
    if (action === 'enable') service.enabled = true;
    if (action === 'disable') service.enabled = false;
    if (action === 'install') service.installed = true;
    return { stdout: '' };
  };
  const plugins = gitActive ? { enabledPlugins: new Set(['obsidian-git']), plugins: { 'obsidian-git': {} } } : { enabledPlugins: new Set(), plugins: {} };
  const app = { plugins, vault: { configDir: '.obsidian' } };
  const companion = new Companion(app, vault, {
    platform, home, env: {}, run, executable: 'obsidian-sync',
    exists: (file) => (path.basename(file) === 'uv' ? uv : fs.existsSync(file)),
  });
  const realLocate = companion.locate.bind(companion);
  companion.locate = (name) => (name === 'uv' ? (uv ? 'uv' : null) : realLocate(name));
  return { calls, companion, configFile, service, vault, home };
}

function mutations(calls) {
  return calls.filter((call) => !['status', 'config', 'verify'].includes(call));
}

test('enabling is refused while Obsidian Git is inactive, with no CLI call', async (t) => {
  const { calls, companion } = harness(t, { gitActive: false });
  await assert.rejects(companion.setEnabled(true), /Obsidian Git/);
  assert.deepEqual(calls, []);
});

test('disabling stays possible without Obsidian Git', async (t) => {
  const { calls, companion } = harness(t, { gitActive: false, service: { installed: true, enabled: true, detail: 'Running' } });
  await companion.setEnabled(false);
  assert.deepEqual(mutations(calls), ['disable']);
});

test('an installed service is enabled without being reinstalled', async (t) => {
  const { calls, companion } = harness(t);
  await companion.setEnabled(true);
  assert.deepEqual(mutations(calls), ['enable']);
});

test('a missing service is installed first', async (t) => {
  const { calls, companion } = harness(t, { service: { installed: false, enabled: false, detail: 'Absent' } });
  await companion.setEnabled(true);
  assert.deepEqual(mutations(calls), ['install', 'enable']);
});

test('first enable seeds this vault and the Obsidian Git pull method', async (t) => {
  const { companion, configFile, vault } = harness(t, { withConfig: false });
  fs.writeFileSync(path.join(vault, '.obsidian', 'plugins', 'obsidian-git', 'data.json'), '{"syncMethod":"merge"}');
  await companion.setEnabled(true);
  const text = fs.readFileSync(configFile, 'utf8');
  assert.ok(text.includes(JSON.stringify(vault)));
  assert.match(text, /pull_strategy: "merge"/);
});

test('enabling refuses a config for another vault', async (t) => {
  const { calls, companion, configFile } = harness(t);
  fs.writeFileSync(configFile, 'vault_path: /some/other/vault\n');
  await assert.rejects(companion.setEnabled(true), /another vault/);
  assert.deepEqual(mutations(calls), []);
});

test('enabling refuses when the service state is unknown', async (t) => {
  const { calls, companion } = harness(t, { service: { installed: null, enabled: null, detail: 'scheduler timeout' } });
  await assert.rejects(companion.setEnabled(true), /scheduler timeout/);
  assert.deepEqual(mutations(calls), []);
});

test('no plugin path ever runs sync or start', async (t) => {
  const { calls, companion } = harness(t, { service: { installed: true, enabled: true, detail: 'Running' } });
  await companion.apply(FORM);
  await companion.setEnabled(false);
  await companion.setEnabled(true);
  await companion.readLog();
  await companion.removeService();
  await companion.installCompanion();
  assert.ok(calls.every((call) => !['sync', 'start', 'stop'].includes(call)));
});

test('saving backs up, verifies through the CLI and restarts an enabled service', async (t) => {
  const { calls, companion, configFile } = harness(t, { service: { installed: true, enabled: true, detail: 'Running' } });
  const before = fs.readFileSync(configFile, 'utf8');
  const result = await companion.apply(FORM);
  assert.equal(result.restarted, true);
  assert.deepEqual(mutations(calls), ['disable', 'enable']);
  assert.equal(fs.readFileSync(result.backup, 'utf8'), before);
  assert.match(fs.readFileSync(configFile, 'utf8'), /interval_seconds: 300/);
});

test('saving does not start a disabled service', async (t) => {
  const { calls, companion } = harness(t);
  const result = await companion.apply(FORM);
  assert.equal(result.restarted, false);
  assert.deepEqual(mutations(calls), []);
});

test('saving unchanged values writes nothing', async (t) => {
  const { calls, companion, configFile } = harness(t, { service: { installed: true, enabled: true, detail: 'Running' } });
  const result = await companion.apply({ ...FORM, interval: '600' });
  assert.equal(result.changed, false);
  assert.deepEqual(mutations(calls), []);
  assert.deepEqual(fs.readdirSync(path.dirname(configFile)), ['config.yaml']);
});

test('invalid forms write nothing', async (t) => {
  const { calls, companion, configFile } = harness(t);
  const before = fs.readFileSync(configFile, 'utf8');
  await assert.rejects(companion.apply({ ...FORM, interval: 'soon' }), /Sync interval/);
  assert.deepEqual(calls, []);
  assert.equal(fs.readFileSync(configFile, 'utf8'), before);
});

test('saving restores the previous file when the companion cannot read the result', async (t) => {
  const { companion, configFile } = harness(t);
  const before = fs.readFileSync(configFile, 'utf8');
  let reads = 0;
  const run = companion.run;
  companion.run = async (file, args, options) => {
    if (args[2] === 'config' && ++reads === 2) throw Object.assign(new Error('x'), { stderr: 'yaml.scanner.ScannerError: bad' });
    return run(file, args, options);
  };
  await assert.rejects(companion.apply(FORM), /Config not changed: yaml\.scanner/);
  assert.equal(fs.readFileSync(configFile, 'utf8'), before);
});

test('with Obsidian Git disabled, saving leaves an enabled service alone', async (t) => {
  const { calls, companion } = harness(t, { gitActive: false, service: { installed: true, enabled: true, detail: 'Running' } });
  const result = await companion.apply(FORM);
  assert.equal(result.changed, true);
  assert.equal(result.restarted, false);
  assert.deepEqual(mutations(calls), []);
});

test('a failed restart is reported as saved but not restarted', async (t) => {
  const { companion } = harness(t, { service: { installed: true, enabled: true, detail: 'Running' }, fail: { disable: 'Error: shutdown pending' } });
  await assert.rejects(companion.apply(FORM), /Settings saved, but the service did not restart: Error: shutdown pending/);
});

test('a missing companion reports the install command', async (t) => {
  const { companion } = harness(t);
  companion.executableOverride = null;
  companion.exists = () => false;
  const state = await companion.refresh();
  assert.equal(state.stage, 'missing');
  assert.match(state.install.command, /^uv tool install --force git\+https:/);
});

test('an old companion asks for an update and keeps the form closed', async (t) => {
  const { companion } = harness(t, { outdated: true });
  const state = await companion.refresh();
  assert.equal(state.stage, 'outdated');
  await assert.rejects(companion.setEnabled(true), /not ready/);
});

test('updating a current companion leaves its service alone', async (t) => {
  const { calls, companion } = harness(t, { service: { installed: true, enabled: true, detail: 'Running' } });
  await companion.installCompanion();
  assert.deepEqual(mutations(calls), ['tool install --force git+https://github.com/corbinjurgens/obsidian-sync.git']);
});

test('updating an old companion stops it and never turns it on', async (t) => {
  const { calls, companion } = harness(t, { outdated: true, fail: { disable: 'Error: no task' } });
  await companion.installCompanion();
  assert.ok(calls.includes('disable'));
  assert.ok(!calls.includes('enable'));
});

test('a failed update says the service stays stopped', async (t) => {
  const { companion } = harness(t, { outdated: true, fail: { uv: 'error: file in use' } });
  await assert.rejects(companion.installCompanion(), /Update failed: error: file in use\. The service stays stopped/);
});

test('saving is verified with the strict checks of scheduled startup', async (t) => {
  const { calls, companion, configFile } = harness(t, { service: { installed: true, enabled: true, detail: 'Running' }, fail: { verify: 'Error: Invalid config value: log.max_size_mb' } });
  const before = fs.readFileSync(configFile, 'utf8');
  await assert.rejects(companion.apply(FORM), /Config not changed: Error: Invalid config value/);
  assert.equal(fs.readFileSync(configFile, 'utf8'), before);
  assert.ok(!calls.includes('disable'), 'the service is not stopped');
});

test('saving restores the file when an unrelated field changed', async (t) => {
  const { companion, configFile } = harness(t);
  const before = fs.readFileSync(configFile, 'utf8');
  const run = companion.run;
  companion.run = async (file, args, options) => {
    const result = await run(file, args, options);
    if (args[4] !== '--strict') return result;
    const saved = JSON.parse(result.stdout);
    saved.sync.branch = 'lost';
    return { stdout: JSON.stringify(saved) };
  };
  await assert.rejects(companion.apply(FORM), /read the file differently/);
  assert.equal(fs.readFileSync(configFile, 'utf8'), before);
});

test('a tilde vault path is compared after expansion', async (t) => {
  const { companion, vault } = harness(t);
  const run = companion.run;
  companion.run = async (file, args, options) => {
    const result = await run(file, args, options);
    if (args[2] !== 'config') return result;
    return { stdout: JSON.stringify({ ...JSON.parse(result.stdout), vault_path: '~/vault' }) };
  };
  const state = await companion.refresh();
  assert.equal(state.status.vault, vault);
  assert.equal(state.vaultMismatch, false);
});

test('before a config exists the form offers the Obsidian Git pull method', async (t) => {
  const { companion, vault } = harness(t, { withConfig: false });
  fs.writeFileSync(path.join(vault, '.obsidian', 'plugins', 'obsidian-git', 'data.json'), '{"syncMethod":"rebase"}');
  assert.equal((await companion.refresh()).settings.sync.pull_strategy, 'rebase');
});

test('removing a service with a custom config is refused', async (t) => {
  const { calls, companion, home } = harness(t, { platform: 'win32' });
  const metadata = path.join(home, '.config', 'obsidian-sync', 'windows-task.json');
  fs.writeFileSync(metadata, JSON.stringify({ config_path: path.join(home, 'custom.yaml') }));
  await assert.rejects(companion.removeService(), /custom config/);
  assert.deepEqual(mutations(calls), []);
});

test('removing a default service runs uninstall', async (t) => {
  const { calls, companion } = harness(t);
  await companion.removeService();
  assert.deepEqual(mutations(calls), ['uninstall']);
});

test('installing without uv is refused', async (t) => {
  const { companion } = harness(t, { uv: false });
  await assert.rejects(companion.installCompanion(), /uv is not installed/);
});

test('management commands never overlap', async (t) => {
  const { companion } = harness(t);
  let active = 0;
  let peak = 0;
  const run = companion.run;
  companion.run = async (...args) => {
    peak = Math.max(peak, ++active);
    await new Promise((resolve) => setImmediate(resolve));
    active--;
    return run(...args);
  };
  await Promise.all([companion.refresh(), companion.setEnabled(true), companion.refresh(), companion.setEnabled(false)]);
  assert.equal(peak, 1);
});

test('a failed action does not block the next one', async (t) => {
  const { companion } = harness(t, { fail: { uninstall: 'Error: pending' } });
  await assert.rejects(companion.removeService(), /failed/);
  assert.equal((await companion.refresh()).stage, 'ready');
});

test('enabling is refused for a vault that cannot sync, before any service change', async (t) => {
  const { calls, companion } = harness(t, { vaultProblem: 'Git remote not configured: origin', service: { installed: false, enabled: false, detail: 'Absent' } });
  await assert.rejects(companion.setEnabled(true), /cannot sync yet: Git remote not configured/);
  assert.deepEqual(mutations(calls), []);
});

test('a first enable checks the vault after seeding its config', async (t) => {
  const { calls, companion, configFile } = harness(t, { withConfig: false, vaultProblem: 'Vault is not a Git repository' });
  await assert.rejects(companion.setEnabled(true), /not a Git repository/);
  assert.ok(fs.existsSync(configFile), 'the seeded config stays for this vault');
  assert.deepEqual(mutations(calls), []);
});
