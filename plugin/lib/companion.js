'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFile } = require('node:child_process');

const commands = require('./commands');
const config = require('./config');
const { OBSIDIAN_GIT_ID, isObsidianGitActive, obsidianGitPullStrategy } = require('./dependency');
const { describeFailure, isOutdatedFailure, parseStatus, samePath } = require('./status');

const GIT_REQUIRED = 'Enable the Obsidian Git community plugin first. The companion only covers the time Obsidian is closed.';

function runProcess(file, args, { timeout, env }) {
  return new Promise((resolve, reject) => {
    execFile(file, args, { timeout, env, windowsHide: true, encoding: 'utf8', maxBuffer: 4_000_000 }, (error, stdout, stderr) => {
      if (error) reject(Object.assign(error, { stdout, stderr }));
      else resolve({ stdout, stderr });
    });
  });
}

class Companion {
  constructor(app, vaultPath, overrides = {}) {
    this.app = app;
    this.vaultPath = vaultPath;
    this.platform = overrides.platform ?? process.platform;
    this.home = overrides.home ?? os.homedir();
    this.env = overrides.env ?? process.env;
    this.exists = overrides.exists ?? fs.existsSync;
    this.run = overrides.run ?? runProcess;
    this.fileSystem = overrides.fileSystem ?? fs;
    this.executableOverride = overrides.executable ?? null;
    this.queue = Promise.resolve();
  }

  // The companion's management commands must run one at a time.
  exclusive(task) {
    const result = this.queue.then(task, task);
    this.queue = result.catch(() => {});
    return result;
  }

  gitActive() {
    return isObsidianGitActive(this.app);
  }

  serviceManager() {
    return commands.serviceManager(this.platform);
  }

  locate(name) {
    if (name === 'obsidian-sync' && this.executableOverride) return this.executableOverride;
    return commands.findExecutable(name, { platform: this.platform, home: this.home, env: this.env, exists: this.exists });
  }

  installInstructions() {
    return commands.installInstructions(this.platform, this.locate('uv'));
  }

  configPath() {
    return config.resolveConfigPath({
      platform: this.platform,
      home: this.home,
      readText: (file) => this.fileSystem.readFileSync(file, 'utf8'),
    });
  }

  environment() {
    return commands.childEnvironment({ platform: this.platform, home: this.home, env: this.env });
  }

  async cli(action) {
    const executable = this.locate('obsidian-sync');
    if (!executable) throw new Error('The obsidian-sync companion is not installed.');
    const { args, timeout } = commands.companionCommand(action, this.configPath());
    return this.run(executable, args, { timeout, env: this.environment() });
  }

  async readState() {
    const gitActive = this.gitActive();
    const executable = this.locate('obsidian-sync');
    const base = { gitActive, platform: this.platform, configPath: this.configPath() };
    if (!executable) return { ...base, stage: 'missing', install: this.installInstructions() };
    try {
      const status = parseStatus((await this.cli('status')).stdout);
      const settings = JSON.parse((await this.cli('config')).stdout);
      const configExists = this.exists(base.configPath);
      if (!configExists) settings.sync.pull_strategy = this.gitPullStrategy() ?? settings.sync.pull_strategy;
      return {
        ...base,
        stage: 'ready',
        status,
        settings,
        configExists,
        vaultMismatch: configExists && !samePath(status.vault ?? '', this.vaultPath, this.platform),
      };
    } catch (error) {
      if (isOutdatedFailure(error)) return { ...base, stage: 'outdated', install: this.installInstructions() };
      return { ...base, stage: 'error', error: describeFailure(error) };
    }
  }

  refresh() {
    return this.exclusive(() => this.readState());
  }

  gitPullStrategy() {
    const settings = path.join(this.vaultPath, this.app.vault.configDir, 'plugins', OBSIDIAN_GIT_ID, 'data.json');
    try {
      return obsidianGitPullStrategy(this.fileSystem.readFileSync(settings, 'utf8'));
    } catch (error) {
      if (error.code === 'ENOENT') return null;
      throw error;
    }
  }

  // Checked the way scheduled startup checks it, against every field and not only the
  // changed ones, so a save cannot leave a config the service would refuse or misread.
  async writeVerified(updates, before) {
    const configPath = this.configPath();
    const written = config.writeConfig(configPath, updates, { fileSystem: this.fileSystem });
    try {
      const saved = JSON.parse((await this.cli('verify')).stdout);
      if (JSON.stringify(saved) !== JSON.stringify(config.withUpdates(before, updates))) {
        throw new Error('The companion read the file differently than it was written.');
      }
      return written;
    } catch (error) {
      config.restoreConfig(configPath, written, { fileSystem: this.fileSystem });
      throw new Error(`Config not changed: ${describeFailure(error)}`);
    }
  }

  setEnabled(enabled) {
    return this.exclusive(async () => {
      if (!enabled) {
        await this.cli('disable');
        return this.readState();
      }
      if (!this.gitActive()) throw new Error(GIT_REQUIRED);
      const state = await this.readState();
      if (state.stage !== 'ready') throw new Error('The companion is not ready. See its status in this tab.');
      if (state.status.service.installed === null) {
        throw new Error(`Cannot read the service state: ${state.status.service.detail}`);
      }
      if (state.vaultMismatch) {
        throw new Error(`The companion config syncs another vault: ${state.settings.vault_path}. Run obsidian-sync config --edit to change it.`);
      }
      let ready = state;
      if (!state.configExists) {
        const strategy = { 'sync.pull_strategy': state.settings.sync.pull_strategy };
        await this.writeVerified({ vault_path: this.vaultPath, ...strategy }, state.settings);
        ready = await this.readState();
      }
      if (ready.status.vaultProblem) throw new Error(`This vault cannot sync yet: ${ready.status.vaultProblem}`);
      // `install` replaces a macOS plist and a reviewed machine definition may be registered.
      if (!ready.status.service.installed) await this.cli('install');
      await this.cli('enable');
      return this.readState();
    });
  }

  // Saves the form; a running service reads its config only at startup, so it restarts.
  apply(form) {
    return this.exclusive(async () => {
      const { values, errors } = config.validateForm(form);
      if (errors.length) throw new Error(errors.join(' '));
      const state = await this.readState();
      if (state.stage !== 'ready') throw new Error('The companion is not ready. See its status in this tab.');
      if (state.vaultMismatch) {
        throw new Error(`The companion config syncs another vault: ${state.settings.vault_path}. Run obsidian-sync config --edit to change it.`);
      }
      const updates = Object.fromEntries(Object.entries(values).filter(
        ([field, value]) => !state.configExists || config.readField(state.settings, field) !== value,
      ));
      if (!state.configExists) updates.vault_path = this.vaultPath;
      if (Object.keys(updates).length === 0) return { state, changed: false, restarted: false };
      const written = await this.writeVerified(updates, state.settings);
      let restarted = false;
      if (state.status.service.enabled) {
        if (this.gitActive()) {
          try {
            await this.cli('disable');
            await this.cli('enable');
          } catch (error) {
            throw new Error(`Settings saved, but the service did not restart: ${describeFailure(error)}`);
          }
          restarted = true;
        }
      }
      return { state: await this.readState(), changed: true, restarted, backup: written.backup };
    });
  }

  // Replacing the tool while its Windows interpreter runs would fail, so stop it first.
  // An older companion cannot report its service, so the service stays off afterwards.
  // A failed stop usually means nothing was running; if not, uv reports the file in use.
  installCompanion() {
    return this.exclusive(async () => {
      const uv = this.locate('uv');
      if (!uv) throw new Error('uv is not installed. Run the command shown in this tab first.');
      const before = await this.readState();
      if (before.stage === 'outdated') await this.cli('disable').catch(() => {});
      try {
        await this.run(uv, commands.installArguments(), { timeout: 300_000, env: this.environment() });
      } catch (error) {
        const stopped = before.stage === 'outdated' ? ' The service stays stopped until this succeeds.' : '';
        throw new Error(`Update failed: ${describeFailure(error)}.${stopped}`);
      }
      return this.readState();
    });
  }

  // Uninstalling forgets a custom config's path, and the next enable would start a new config.
  removeService() {
    return this.exclusive(async () => {
      if (this.configPath() !== config.defaultConfigPath(this.home)) {
        throw new Error(`This service uses a custom config: ${this.configPath()}. Run obsidian-sync uninstall in a terminal.`);
      }
      await this.cli('uninstall');
      return this.readState();
    });
  }

  readLog() {
    return this.exclusive(async () => (await this.cli('log')).stdout);
  }
}

module.exports = { Companion, GIT_REQUIRED, describeFailure };
