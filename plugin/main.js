'use strict';

const obsidian = require('obsidian');
const path = require('node:path');

class SyncCompanionPlugin extends obsidian.Plugin {
  onload() {
    const vaultPath = this.app.vault.adapter.getBasePath();
    const lib = (name) => require(path.join(vaultPath, this.manifest.dir, 'lib', `${name}.js`));
    const { Companion } = lib('companion');
    this.companion = new Companion(this.app, vaultPath);
    this.addSettingTab(new (lib('settings-tab')(obsidian, this))(this.app));
    this.gitWasActive = true;
    this.app.workspace.onLayoutReady(() => {
      this.watchObsidianGit();
      this.registerInterval(window.setInterval(() => this.watchObsidianGit(), 60_000));
    });
  }

  // Asks the companion only when Obsidian Git stops being active, not on every load.
  async watchObsidianGit() {
    const active = this.companion.gitActive();
    const lost = this.gitWasActive && !active;
    this.gitWasActive = active;
    if (!lost) return;
    const state = await this.companion.refresh();
    if (state.status?.service.enabled) {
      new obsidian.Notice('Obsidian Git is disabled, but the sync companion is still enabled.', 12_000);
    }
  }
}

module.exports = SyncCompanionPlugin;
