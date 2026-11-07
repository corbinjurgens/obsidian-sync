'use strict';

const { formFromConfig } = require('./config');
const { GIT_REQUIRED, describeFailure } = require('./companion');

module.exports = function makeSettingsTab(obsidian, plugin) {
  const { Modal, Notice, PluginSettingTab, Setting } = obsidian;

  class ConfirmModal extends Modal {
    constructor(app, text, onConfirm) {
      super(app);
      this.text = text;
      this.onConfirm = onConfirm;
    }

    onOpen() {
      this.contentEl.createEl('p', { text: this.text });
      new Setting(this.contentEl)
        .addButton((button) => button.setButtonText('Cancel').onClick(() => this.close()))
        .addButton((button) => button.setButtonText('Continue').setWarning().onClick(() => {
          this.close();
          this.onConfirm();
        }));
    }

    onClose() {
      this.contentEl.empty();
    }
  }

  return class CompanionSettingTab extends PluginSettingTab {
    constructor(app) {
      super(app, plugin);
      this.state = null;
      this.form = null;
      this.busy = null;
      this.log = null;
    }

    display() {
      // Reopening shows the config as it is now, including edits made elsewhere.
      this.form = null;
      this.log = null;
      this.render();
      this.perform('Reading companion status', () => plugin.companion.refresh());
    }

    // Runs one action, then shows the state the companion actually reports.
    async perform(label, task) {
      if (this.busy) return;
      this.busy = label;
      this.render();
      try {
        const result = await task();
        if (result?.message) new Notice(result.message);
      } catch (error) {
        new Notice(describeFailure(error), 12_000);
      }
      try {
        this.state = await plugin.companion.refresh();
        if (this.state.settings && !this.form) this.form = formFromConfig(this.state.settings);
      } catch (error) {
        this.state = { stage: 'error', error: describeFailure(error), gitActive: plugin.companion.gitActive() };
      }
      this.busy = null;
      this.render();
    }

    render() {
      const root = this.containerEl;
      root.empty();
      root.addClass('osc-settings');
      if (this.busy) root.createDiv({ cls: 'osc-busy', text: `${this.busy}...` });
      const state = this.state;
      if (!state) return;
      this.renderWarnings(root, state);
      this.renderToggle(root, state);
      this.renderStatus(root, state);
      this.renderInstall(root, state);
      if (state.stage === 'ready' && this.form) this.renderForm(root);
      if (state.stage === 'ready') this.renderService(root, state);
    }

    renderWarnings(root, state) {
      const warn = (text) => root.createDiv({ cls: 'osc-warning', text });
      if (!state.gitActive) {
        warn(state.status?.service.enabled
          ? 'Obsidian Git is disabled, but the companion is still enabled and keeps syncing while Obsidian is closed.'
          : GIT_REQUIRED);
      }
      if (state.vaultMismatch) warn(`The companion config syncs another vault: ${state.settings.vault_path}`);
      if (state.stage === 'error') warn(`The companion reported a problem: ${state.error}`);
      if (state.status?.service.installed === null) warn(`Cannot read the service state: ${state.status.service.detail}`);
      if (state.configExists && state.status?.vaultProblem) warn(`This vault cannot sync yet: ${state.status.vaultProblem}`);
    }

    renderToggle(root, state) {
      const enabled = state.status?.service.enabled === true;
      const known = state.stage === 'ready' && state.status.service.enabled !== null;
      new Setting(root)
        .setName('Sync while Obsidian is closed')
        .setDesc('Controls the login service on this device. The companion defers to Obsidian Git while Obsidian is open.')
        .addToggle((toggle) => toggle
          .setValue(enabled)
          .setDisabled(!known || Boolean(this.busy))
          .onChange((value) => this.perform(
            value ? 'Enabling the service' : 'Disabling the service',
            () => plugin.companion.setEnabled(value),
          )));
    }

    renderStatus(root, state) {
      new Setting(root).setName('Status').setHeading();
      const companion = { missing: 'Not installed', outdated: 'Installed, update required', error: 'Installed, not responding', ready: 'Installed' };
      const service = state.status?.service;
      const facts = [
        ['Companion', companion[state.stage]],
        ['Obsidian Git', state.gitActive ? 'Enabled' : 'Disabled'],
      ];
      if (service) {
        const installed = service.installed === null ? 'Unknown' : service.installed ? 'Installed' : 'Not installed';
        const enabled = service.enabled === null ? 'unknown' : service.enabled ? 'enabled' : 'disabled';
        facts.push(
          [`Service (${plugin.companion.serviceManager()})`, `${installed}, ${enabled}`],
          ['Daemon', state.status.pid ? `${state.status.daemon} (PID ${state.status.pid})` : state.status.daemon],
          ['Last sync', state.status.lastSync ?? 'None in the current log'],
          ['Last log line', state.status.lastLog ?? 'No log yet'],
        );
      }
      if (state.configPath) facts.push(['Config file', state.configPath]);
      const list = root.createEl('dl', { cls: 'osc-status' });
      for (const [name, value] of facts) {
        list.createEl('dt', { text: name });
        list.createEl('dd', { text: value });
      }
    }

    renderInstall(root, state) {
      if (state.stage !== 'missing' && state.stage !== 'outdated') return;
      const missing = state.stage === 'missing';
      const heading = missing ? 'Install the companion' : 'Update the companion';
      new Setting(root).setName(heading).setHeading();
      if (!missing) {
        root.createEl('p', { text: 'This version has no plugin support. Updating stops the service; turn it back on here afterwards.' });
      }
      const { command, prerequisite } = state.install;
      if (prerequisite) {
        root.createEl('p', { text: 'uv is required. Install it from a terminal, then return here:' });
        root.createEl('pre', { cls: 'osc-command', text: prerequisite });
      }
      root.createEl('pre', { cls: 'osc-command', text: command });
      new Setting(root)
        .setName('Run this command')
        .setDesc(prerequisite ? 'Available once uv is installed.' : 'Installs for this user account. Downloads from GitHub.')
        .addButton((button) => button
          .setButtonText(missing ? 'Install' : 'Update')
          .setCta()
          .setDisabled(Boolean(prerequisite) || Boolean(this.busy))
          .onClick(() => this.perform('Installing the companion', async () => {
            await plugin.companion.installCompanion();
            this.form = null;
          })));
    }

    text(root, name, desc, field) {
      new Setting(root).setName(name).setDesc(desc).addText((text) => text
        .setValue(this.form[field])
        .onChange((value) => { this.form[field] = value; }));
    }

    renderForm(root) {
      new Setting(root).setName('Sync settings').setHeading();
      this.text(root, 'Sync interval', 'Seconds between sync cycles.', 'interval');
      this.text(root, 'Remote', 'Git remote name.', 'remote');
      this.text(root, 'Branch', 'Git branch name.', 'branch');
      new Setting(root)
        .setName('Pull strategy')
        .setDesc('Match the method used by Obsidian Git.')
        .addDropdown((dropdown) => dropdown
          .addOptions({ merge: 'merge', rebase: 'rebase', 'ff-only': 'ff-only' })
          .setValue(this.form.pullStrategy)
          .onChange((value) => { this.form.pullStrategy = value; }));
      this.text(root, 'Commit message template', 'Supports {{date}}, {{hostname}}, {{numFiles}} and {{files}}.', 'template');

      new Setting(root).setName('Retry after failures').setHeading();
      this.text(root, 'First retry delay', 'Seconds. 0 uses the sync interval.', 'backoffInitial');
      this.text(root, 'Retry multiplier', 'Applied after each consecutive failure. 2 doubles the delay, 1 keeps it constant.', 'backoffMultiplier');
      this.text(root, 'Longest retry delay', 'Seconds. The sync interval applies when it is longer.', 'backoffMax');

      new Setting(root)
        .setDesc('Backs up the config file first, then restarts the service when it is enabled.')
        .addButton((button) => button
          .setButtonText('Save settings')
          .setCta()
          .setDisabled(Boolean(this.busy))
          .onClick(() => this.perform('Saving settings', async () => {
            const result = await plugin.companion.apply(this.form);
            if (!result.changed) return { message: 'No settings changed.' };
            this.form = null;
            const pending = !result.restarted && this.state.status.service.enabled;
            return { message: pending ? 'Saved. Takes effect when the service next starts.' : 'Settings saved.' };
          })));
    }

    renderService(root, state) {
      new Setting(root).setName('Service').setHeading();
      new Setting(root)
        .setName('Recent log')
        .addButton((button) => button.setButtonText('Show').setDisabled(Boolean(this.busy)).onClick(() => {
          this.perform('Reading the log', async () => { this.log = await plugin.companion.readLog(); });
        }));
      if (this.log !== null) root.createEl('pre', { cls: 'osc-log', text: this.log });
      if (!state.status.service.installed) return;
      new Setting(root)
        .setName('Remove the login service')
        .setDesc('Keeps the companion, its config, its logs and the vault. A customized service definition is deleted; turning sync on again creates the standard one.')
        .addButton((button) => button.setButtonText('Remove').setWarning().setDisabled(Boolean(this.busy)).onClick(() => {
          new ConfirmModal(this.app, `Remove the ${plugin.companion.serviceManager()} service for this account?`, () => {
            this.perform('Removing the service', () => plugin.companion.removeService());
          }).open();
        }));
    }
  };
};
