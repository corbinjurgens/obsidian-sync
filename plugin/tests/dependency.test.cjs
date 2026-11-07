'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { isObsidianGitActive, obsidianGitPullStrategy } = require('../lib/dependency');

function app(enabled, loaded) {
  return { plugins: { enabledPlugins: new Set(enabled), plugins: Object.fromEntries(loaded.map((id) => [id, {}])) } };
}

test('Obsidian Git counts as active only when enabled and loaded', () => {
  assert.equal(isObsidianGitActive(app(['obsidian-git'], ['obsidian-git'])), true);
  assert.equal(isObsidianGitActive(app([], [])), false);
  assert.equal(isObsidianGitActive(app(['obsidian-git'], [])), false, 'Restricted Mode keeps the list');
  assert.equal(isObsidianGitActive(app(['other'], ['other'])), false);
});

test('a missing plugin registry reads as inactive', () => {
  assert.equal(isObsidianGitActive({}), false);
  assert.equal(isObsidianGitActive(undefined), false);
});

test('only pull strategies the companion supports are taken from Obsidian Git', () => {
  assert.equal(obsidianGitPullStrategy('{"syncMethod":"merge"}'), 'merge');
  assert.equal(obsidianGitPullStrategy('{"syncMethod":"rebase"}'), 'rebase');
  assert.equal(obsidianGitPullStrategy('{"syncMethod":"reset"}'), null);
  assert.equal(obsidianGitPullStrategy('{}'), null);
  assert.equal(obsidianGitPullStrategy('not json'), null);
});
