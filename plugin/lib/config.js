'use strict';

const fs = require('node:fs');
const path = require('node:path');

const FIELDS = [
  'sync.interval_seconds',
  'sync.pull_strategy',
  'sync.remote',
  'sync.branch',
  'sync.backoff_initial_seconds',
  'sync.backoff_multiplier',
  'sync.backoff_max_seconds',
  'commit.template',
];

function readField(config, field) {
  return field.split('.').reduce((value, key) => value?.[key], config);
}

// Windows installs may capture a custom config; every other command must follow it.
function resolveConfigPath({ platform, home, readText = (file) => fs.readFileSync(file, 'utf8') }) {
  const directory = path.join(home, '.config', 'obsidian-sync');
  if (platform === 'win32') {
    try {
      const captured = JSON.parse(readText(path.join(directory, 'windows-task.json'))).config_path;
      if (typeof captured === 'string' && captured) return captured;
    } catch (error) {
      if (error.code !== 'ENOENT' && !(error instanceof SyntaxError)) throw error;
    }
  }
  return path.join(directory, 'config.yaml');
}

function scalar(value) {
  // A JSON string is a valid YAML double-quoted scalar.
  return typeof value === 'number' ? String(value) : JSON.stringify(value);
}

function indentOf(line) {
  return line.length - line.trimStart().length;
}

function isContent(line) {
  const trimmed = line.trim();
  return trimmed !== '' && !trimmed.startsWith('#');
}

function keyPattern(key, indent) {
  return new RegExp(`^ {${indent}}${key}:(\\s|$)`);
}

// Replaces one key and the deeper lines that continue a wrapped scalar.
function setLine(lines, index, indent, key, value) {
  let end = index + 1;
  while (end < lines.length && isContent(lines[end]) && indentOf(lines[end]) > indent) end++;
  lines.splice(index, end - index, `${' '.repeat(indent)}${key}: ${scalar(value)}`);
}

function setTopLevel(lines, key, value) {
  const index = lines.findIndex((line) => keyPattern(key, 0).test(line));
  if (index >= 0) setLine(lines, index, 0, key, value);
  else lines.unshift(`${key}: ${scalar(value)}`);
}

function setNested(lines, section, key, value) {
  const header = lines.findIndex((line) => keyPattern(section, 0).test(line));
  if (header < 0) {
    while (lines.length && lines.at(-1).trim() === '') lines.pop();
    lines.push(`${section}:`, `  ${key}: ${scalar(value)}`);
    return;
  }
  const remainder = lines[header].slice(section.length + 1).trim();
  if (isContent(remainder)) {
    throw new Error(`Cannot edit "${section}" safely: only block-style YAML sections are supported.`);
  }
  let end = header + 1;
  while (end < lines.length && (!isContent(lines[end]) || indentOf(lines[end]) > 0)) end++;
  const first = lines.slice(header + 1, end).find(isContent);
  const indent = first ? indentOf(first) : 2;
  for (let index = header + 1; index < end; index++) {
    if (keyPattern(key, indent).test(lines[index])) {
      setLine(lines, index, indent, key, value);
      return;
    }
  }
  let last = header;
  for (let index = header + 1; index < end; index++) if (isContent(lines[index])) last = index;
  lines.splice(last + 1, 0, `${' '.repeat(indent)}${key}: ${scalar(value)}`);
}

// Updates only the named keys, so comments and fields the plugin does not manage survive.
function patchConfigText(text, updates) {
  const newline = text.includes('\r\n') ? '\r\n' : '\n';
  const lines = text === '' ? [] : text.replace(/\r?\n$/, '').split(/\r?\n/);
  for (const [field, value] of Object.entries(updates)) {
    const parts = field.split('.');
    if (parts.length === 1) setTopLevel(lines, parts[0], value);
    else setNested(lines, parts[0], parts[1], value);
  }
  return `${lines.join(newline)}${newline}`;
}

function backupPath(configPath, now) {
  const stamp = now.toISOString().replace(/[-:.]/g, '');
  return `${configPath}.bak-${stamp}`;
}

// Atomic in the config directory; the backup is kept beside the config and never rotated.
function writeConfig(configPath, updates, { now = new Date(), fileSystem = fs } = {}) {
  let current = '';
  let backup = null;
  try {
    current = fileSystem.readFileSync(configPath, 'utf8');
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  const next = patchConfigText(current, updates);
  fileSystem.mkdirSync(path.dirname(configPath), { recursive: true });
  if (current !== '') {
    backup = backupPath(configPath, now);
    fileSystem.copyFileSync(configPath, backup, fs.constants.COPYFILE_EXCL);
  }
  const temporary = `${configPath}.tmp-${process.pid}`;
  fileSystem.writeFileSync(temporary, next, 'utf8');
  fileSystem.renameSync(temporary, configPath);
  return { backup, created: current === '' };
}

function restoreConfig(configPath, written, { fileSystem = fs } = {}) {
  if (written.backup) fileSystem.copyFileSync(written.backup, configPath);
  else if (written.created) fileSystem.rmSync(configPath);
}

function positiveInteger(text, label, errors, minimum = 1) {
  const value = Number(text);
  if (!Number.isInteger(value) || value < minimum) {
    errors.push(`${label} must be a whole number of at least ${minimum}.`);
  }
  return value;
}

// Form values are text. Returns config fields, or the reasons they cannot be saved.
function validateForm(form) {
  const errors = [];
  const values = {
    'sync.interval_seconds': positiveInteger(form.interval, 'Sync interval', errors),
    'sync.pull_strategy': form.pullStrategy,
    'sync.remote': form.remote.trim(),
    'sync.branch': form.branch.trim(),
    'sync.backoff_initial_seconds': positiveInteger(form.backoffInitial, 'First retry delay', errors, 0),
    'sync.backoff_multiplier': Number(form.backoffMultiplier),
    'sync.backoff_max_seconds': positiveInteger(form.backoffMax, 'Longest retry delay', errors),
    'commit.template': form.template,
  };
  if (!(values['sync.backoff_multiplier'] >= 1) || !Number.isFinite(values['sync.backoff_multiplier'])) {
    errors.push('Retry multiplier must be a number of at least 1.');
  }
  if (!['rebase', 'merge', 'ff-only'].includes(form.pullStrategy)) {
    errors.push('Pull strategy must be rebase, merge or ff-only.');
  }
  // A remote is a name. Refusing URLs keeps tokens embedded in one out of the config.
  if (!/^[A-Za-z0-9_][A-Za-z0-9._-]*$/.test(values['sync.remote'])) {
    errors.push('Remote must be a Git remote name such as origin, not a URL.');
  }
  if (!/^[^\s~^:?*[\\-][^\s~^:?*[\\]*$/.test(values['sync.branch'])) {
    errors.push('Branch must be a single Git branch name.');
  }
  if (values['commit.template'].trim() === '' || /[\r\n]/.test(values['commit.template'])) {
    errors.push('Commit message template must be one non-empty line.');
  }
  return { values, errors };
}

function formFromConfig(config) {
  return {
    interval: String(readField(config, 'sync.interval_seconds')),
    pullStrategy: readField(config, 'sync.pull_strategy'),
    remote: readField(config, 'sync.remote'),
    branch: readField(config, 'sync.branch'),
    backoffInitial: String(readField(config, 'sync.backoff_initial_seconds')),
    backoffMultiplier: String(readField(config, 'sync.backoff_multiplier')),
    backoffMax: String(readField(config, 'sync.backoff_max_seconds')),
    template: readField(config, 'commit.template'),
  };
}

module.exports = {
  FIELDS,
  backupPath,
  formFromConfig,
  patchConfigText,
  readField,
  resolveConfigPath,
  restoreConfig,
  validateForm,
  writeConfig,
};
