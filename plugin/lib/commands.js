'use strict';

const path = require('node:path');

const FORK_PACKAGE = 'git+https://github.com/corbinjurgens/obsidian-sync.git';

const SERVICE_MANAGERS = {
  win32: 'Task Scheduler',
  darwin: 'launchd',
  linux: 'systemd',
};

// The only companion commands this plugin may run. `sync` and `start` are absent
// on purpose: the login service owns the sync loop and defers to a running Obsidian.
const ACTIONS = {
  status: { args: ['status', '--json'], timeout: 60_000 },
  config: { args: ['config', '--json'], timeout: 30_000 },
  verify: { args: ['config', '--json', '--strict'], timeout: 30_000 },
  log: { args: ['log', '-n', '50'], timeout: 30_000 },
  install: { args: ['install'], timeout: 120_000 },
  enable: { args: ['enable'], timeout: 120_000 },
  // Windows waits up to 30 seconds for cooperative shutdown after a scheduler query.
  disable: { args: ['disable'], timeout: 120_000 },
  uninstall: { args: ['uninstall'], timeout: 120_000 },
};

function serviceManager(platform) {
  const manager = SERVICE_MANAGERS[platform];
  if (!manager) throw new Error(`Unsupported platform: ${platform}`);
  return manager;
}

function companionCommand(action, configPath) {
  const known = Object.hasOwn(ACTIONS, action) ? ACTIONS[action] : null;
  if (!known) throw new Error(`Refusing companion command: ${action}`);
  return { args: ['--config', configPath, ...known.args], timeout: known.timeout };
}

function installArguments() {
  return ['tool', 'install', '--force', FORK_PACKAGE];
}

function installInstructions(platform, uvPath) {
  const command = `uv ${installArguments().join(' ')}`;
  if (uvPath) return { command, prerequisite: null };
  return {
    command,
    prerequisite: platform === 'win32'
      ? 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
      : 'curl -LsSf https://astral.sh/uv/install.sh | sh',
  };
}

// A desktop app does not inherit a login shell's PATH, so uv's own bin directory
// and the usual package manager prefixes are searched as well.
function searchDirectories({ platform, home, env }) {
  const known = [env.UV_TOOL_BIN_DIR, env.XDG_BIN_HOME, path.join(home, '.local', 'bin')];
  if (platform !== 'win32') {
    known.push(path.join(home, '.cargo', 'bin'), '/opt/homebrew/bin', '/usr/local/bin');
  }
  const inherited = (env.PATH || env.Path || '').split(path.delimiter);
  return [...new Set([...known, ...inherited].filter(Boolean))];
}

function findExecutable(name, { platform, home, env, exists }) {
  const file = platform === 'win32' ? `${name}.exe` : name;
  for (const directory of searchDirectories({ platform, home, env })) {
    const candidate = path.join(directory, file);
    if (exists(candidate)) return candidate;
  }
  return null;
}

function childEnvironment({ platform, home, env }) {
  return {
    ...env,
    PATH: searchDirectories({ platform, home, env }).join(path.delimiter),
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    COLUMNS: '200',
    NO_COLOR: '1',
    GIT_TERMINAL_PROMPT: '0',
  };
}

module.exports = {
  FORK_PACKAGE,
  childEnvironment,
  companionCommand,
  findExecutable,
  installArguments,
  installInstructions,
  searchDirectories,
  serviceManager,
};
