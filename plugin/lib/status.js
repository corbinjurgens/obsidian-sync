'use strict';

function fact(value) {
  return typeof value === 'boolean' ? value : null;
}

function text(value) {
  return typeof value === 'string' && value !== '' ? value : null;
}

// Parses `obsidian-sync status --json`. A service fact stays null when it is unknown.
function parseStatus(stdout) {
  let raw;
  try {
    raw = JSON.parse(stdout);
  } catch {
    throw new Error('The companion returned unreadable status output.');
  }
  if (raw === null || typeof raw !== 'object' || typeof raw.daemon !== 'string') {
    throw new Error('The companion returned an incomplete status report.');
  }
  return {
    daemon: raw.daemon,
    pid: text(raw.pid),
    service: {
      installed: fact(raw.service?.installed),
      enabled: fact(raw.service?.enabled),
      detail: text(raw.service?.detail),
    },
    vault: text(raw.vault),
    lastSync: text(raw.last_sync),
    lastLog: text(raw.last_log),
  };
}

function isOutdatedFailure(error) {
  return /no such option:?\s*'?--json/i.test(String(error?.stderr ?? ''));
}

function describeFailure(error) {
  const detail = String(error?.stderr ?? '').trim().split(/\r?\n/).filter(Boolean).at(-1);
  if (error?.killed) return `Timed out: ${detail || error.message}`;
  return detail || error?.message || 'Unknown failure.';
}

function comparablePath(value, platform) {
  const unified = String(value).replace(/\\/g, '/').replace(/\/+$/, '');
  return platform === 'win32' ? unified.toLowerCase() : unified;
}

function samePath(left, right, platform) {
  return comparablePath(left, platform) === comparablePath(right, platform);
}

module.exports = { describeFailure, isOutdatedFailure, parseStatus, samePath };
