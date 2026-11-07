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
      return {
        ...base,
        stage: 'ready',
        status,
        settings,
        configExists,
        vaultMismatch: configExists && !samePath(settings.vault_path, this.vaultPath, this.platform),
      };
    } catch (error) {
      if (isOutdatedFailure(error)) return { ...base, stage: 'outdated', install: this.installInstructions() };
      return { ...base, stage: 'error', error: describeFailure(error) };
    }
  }

  refresh() {
    return this.exclusive(() => this.readState());
  }

  async seedConfig() {
    const updates = { vault_path: this.vaultPath };
    const gitSettings = path.join(this.vaultPath, this.app.vault.configDir, 'plugins', OBSIDIAN_GIT_ID, 'data.json');
    try {
      const strategy = obsidianGitPullStrategy(this.fileSystem.readFileSync(gitSettings, 'utf8'));
      if (strategy) updates['sync.pull_strategy'] = strategy;
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
    await this.writeVerified(updates);
  }

  async writeVerified(updates) {
    const configPath = this.configPath();
    const written = config.writeConfig(configPath, updates, { fileSystem: this.fileSystem });
    try {
      const saved = JSON.parse((await this.cli('config')).stdout);
      for (const [field, value] of Object.entries(updates)) {
        if (config.readField(saved, field) !== value) {
          throw new Error(`The companion read ${field} differently than it was written.`);
        }
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
      if (!state.configExists) await this.seedConfig();
      // `install` replaces a macOS plist and a reviewed machine definition may be registered.
      if (!state.status.service.installed) await this.cli('install');
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
      const written = await this.writeVerified(updates);
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
  // An older companion cannot report its service, so it is stopped and left off.
  installCompanion() {
    return this.exclusive(async () => {
      const uv = this.locate('uv');
      if (!uv) throw new Error('uv is not installed. Run the command shown in this tab first.');
      const before = await this.readState();
      const wasEnabled = before.stage === 'ready' && before.status.service.enabled === true;
      if (wasEnabled) await this.cli('disable');
      if (before.stage === 'outdated') await this.cli('disable').catch(() => {});
      await this.run(uv, commands.installArguments(), { timeout: 300_000, env: this.environment() });
      if (wasEnabled && this.gitActive()) await this.cli('enable');
      return this.readState();
    });
  }

  removeService() {
    return this.exclusive(async () => {
      await this.cli('uninstall');
      return this.readState();
    });
  }

  readLog() {
    return this.exclusive(async () => (await this.cli('log')).stdout);
  }
}

module.exports = { Companion, GIT_REQUIRED, describeFailure };
