'use strict';

const OBSIDIAN_GIT_ID = 'obsidian-git';

// Enabled in the list and actually loaded: Restricted Mode keeps the list but loads nothing.
function isObsidianGitActive(app) {
  const plugins = app?.plugins;
  return Boolean(plugins?.enabledPlugins?.has(OBSIDIAN_GIT_ID) && plugins?.plugins?.[OBSIDIAN_GIT_ID]);
}

// Reads Obsidian Git's saved settings, never writes them. `reset` has no companion equivalent.
function obsidianGitPullStrategy(dataJson) {
  try {
    const method = JSON.parse(dataJson).syncMethod;
    return method === 'merge' || method === 'rebase' ? method : null;
  } catch {
    return null;
  }
}

module.exports = { OBSIDIAN_GIT_ID, isObsidianGitActive, obsidianGitPullStrategy };
