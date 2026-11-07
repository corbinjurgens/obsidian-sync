'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const script = path.resolve(__dirname, '..', 'scripts', 'install.mjs');

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'sync-companion-install-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const vault = path.join(root, 'Example vault');
  fs.mkdirSync(path.join(vault, '.obsidian'), { recursive: true });
  return { root, vault, target: path.join(vault, '.obsidian', 'plugins', 'obsidian-sync-companion') };
}

function install(args, env = {}) {
  return spawnSync(process.execPath, [script, ...args], { encoding: 'utf8', env: { ...process.env, WASABI_VAULT: '', ...env } });
}

test('installs runtime files only, from WASABI_VAULT', (t) => {
  const { vault, target } = fixture(t);
  assert.equal(install([], { WASABI_VAULT: vault }).status, 0);
  assert.deepEqual(fs.readdirSync(target).sort(), ['lib', 'main.js', 'manifest.json', 'styles.css']);
  assert.ok(fs.existsSync(path.join(target, 'lib', 'companion.js')));
});

test('updating keeps local files and removes stale code', (t) => {
  const { vault, target } = fixture(t);
  assert.equal(install(['--vault', vault]).status, 0);
  fs.writeFileSync(path.join(target, 'data.json'), '{}');
  fs.writeFileSync(path.join(target, 'lib', 'stale.js'), '');
  fs.writeFileSync(path.join(target, 'main.js'), 'old');
  assert.equal(install(['--vault', vault]).status, 0);
  assert.equal(fs.readFileSync(path.join(target, 'data.json'), 'utf8'), '{}');
  assert.equal(fs.existsSync(path.join(target, 'lib', 'stale.js')), false);
  assert.notEqual(fs.readFileSync(path.join(target, 'main.js'), 'utf8'), 'old');
});

test('refuses a folder that is not a vault', (t) => {
  const { root } = fixture(t);
  const result = install(['--vault', root]);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /\.obsidian is missing/);
});

test('refuses a missing vault setting', () => {
  const result = install([]);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /WASABI_VAULT/);
});
