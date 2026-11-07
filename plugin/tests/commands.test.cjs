'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const {
  FORK_PACKAGE, childEnvironment, companionCommand, findExecutable, installArguments, installInstructions, serviceManager,
} = require('../lib/commands');

test('every managed action passes the config before its subcommand', () => {
  const expected = {
    status: ['status', '--json'],
    config: ['config', '--json'],
    log: ['log', '-n', '50'],
    install: ['install'],
    enable: ['enable'],
    disable: ['disable'],
    uninstall: ['uninstall'],
  };
  for (const [action, args] of Object.entries(expected)) {
    const command = companionCommand(action, 'C:\\My Vault\\config.yaml');
    assert.deepEqual(command.args, ['--config', 'C:\\My Vault\\config.yaml', ...args]);
    assert.ok(command.timeout > 0);
  }
});

test('commands that sync directly cannot be built', () => {
  for (const action of ['sync', 'start', 'stop', 'installed-start', 'constructor', '__proto__']) {
    assert.throws(() => companionCommand(action, 'c'), /Refusing companion command/);
  }
});

test('disable can outlast the companion cooperative shutdown wait', () => {
  assert.ok(companionCommand('disable', 'c').timeout > 30_000);
});

test('service managers follow the operating system', () => {
  assert.equal(serviceManager('win32'), 'Task Scheduler');
  assert.equal(serviceManager('darwin'), 'launchd');
  assert.equal(serviceManager('linux'), 'systemd');
  assert.throws(() => serviceManager('freebsd'), /Unsupported platform/);
});

test('installation comes from the fork, never the upstream PyPI package', () => {
  assert.equal(FORK_PACKAGE, 'git+https://github.com/corbinjurgens/obsidian-sync.git');
  assert.deepEqual(installArguments(), ['tool', 'install', '--force', FORK_PACKAGE]);
});

test('instructions show the uv prerequisite for this operating system only when uv is missing', () => {
  assert.equal(installInstructions('linux', '/usr/bin/uv').prerequisite, null);
  assert.equal(installInstructions('win32', 'C:\\uv.exe').command, `uv tool install --force ${FORK_PACKAGE}`);
  assert.match(installInstructions('win32', null).prerequisite, /install\.ps1/);
  assert.match(installInstructions('darwin', null).prerequisite, /install\.sh/);
  assert.match(installInstructions('linux', null).prerequisite, /install\.sh/);
});

test('uv bin directory is found although the desktop PATH lacks it', () => {
  const home = path.join(path.sep, 'Users', 'apple');
  const wanted = path.join(home, '.local', 'bin', 'obsidian-sync');
  const found = findExecutable('obsidian-sync', {
    platform: 'darwin', home, env: { PATH: '/usr/bin' }, exists: (file) => file === wanted,
  });
  assert.equal(found, wanted);
});

test('Windows looks for the exe and honours the Path spelling', () => {
  const seen = [];
  const found = findExecutable('uv', {
    platform: 'win32',
    home: 'C:\\home',
    env: { Path: `C:\\tools${path.delimiter}C:\\other` },
    exists: (file) => { seen.push(file); return file.endsWith(path.join('other', 'uv.exe')) || file.endsWith('other/uv.exe'); },
  });
  assert.ok(found);
  assert.ok(seen.every((file) => file.endsWith('uv.exe')));
});

test('a missing executable is reported as null', () => {
  assert.equal(findExecutable('uv', { platform: 'linux', home: '/h', env: {}, exists: () => false }), null);
});

test('child processes get readable UTF-8 output and cannot prompt for credentials', () => {
  const env = childEnvironment({ platform: 'darwin', home: '/h', env: { PATH: '/usr/bin', KEEP: '1' } });
  assert.equal(env.KEEP, '1');
  assert.equal(env.PYTHONUTF8, '1');
  assert.equal(env.GIT_TERMINAL_PROMPT, '0');
  assert.ok(env.PATH.split(path.delimiter).includes(path.join('/h', '.local', 'bin')));
  assert.ok(env.PATH.split(path.delimiter).includes('/usr/bin'));
});
