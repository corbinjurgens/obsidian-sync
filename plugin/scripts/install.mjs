import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const pluginId = 'obsidian-sync-companion';
const source = fileURLToPath(new URL('../', import.meta.url));
// Mirrored code only. Anything else in the installed directory is left in place.
const owned = ['main.js', 'manifest.json', 'styles.css', 'lib'];

function exists(file) {
  try { return fs.lstatSync(file); }
  catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}

function refuseLinks(file) {
  const stat = exists(file);
  if (!stat) return;
  if (stat.isSymbolicLink()) throw new Error(`Refusing symbolic link: ${file}`);
  if (stat.isDirectory()) for (const name of fs.readdirSync(file)) refuseLinks(path.join(file, name));
}

let changed = 0;
function mirror(from, to) {
  if (fs.statSync(from).isDirectory()) {
    fs.mkdirSync(to, { recursive: true });
    const names = fs.readdirSync(from);
    for (const name of names) mirror(path.join(from, name), path.join(to, name));
    for (const name of fs.readdirSync(to)) {
      if (!names.includes(name)) {
        fs.rmSync(path.join(to, name), { recursive: true });
        changed++;
      }
    }
  } else if (!exists(to) || !fs.readFileSync(from).equals(fs.readFileSync(to))) {
    fs.copyFileSync(from, to);
    changed++;
  }
}

try {
  const args = process.argv.slice(2);
  if (args.length && (args.length !== 2 || args[0] !== '--vault')) {
    throw new Error('Usage: node scripts/install.mjs [--vault ABSOLUTE_VAULT_PATH]');
  }
  const vault = args[1] || process.env.WASABI_VAULT;
  if (!vault || !path.isAbsolute(vault) || !fs.statSync(vault).isDirectory()) {
    throw new Error('Set WASABI_VAULT or pass --vault with an existing absolute vault path.');
  }
  if (!exists(path.join(vault, '.obsidian'))?.isDirectory()) {
    throw new Error('The target is not an Obsidian vault: .obsidian is missing.');
  }
  const target = path.join(vault, '.obsidian', 'plugins', pluginId);
  for (const directory of [path.join(vault, '.obsidian', 'plugins'), target]) {
    const stat = exists(directory);
    if (stat?.isSymbolicLink() || (stat && !stat.isDirectory())) {
      throw new Error(`Refusing non-directory or symbolic link: ${directory}`);
    }
  }
  for (const name of owned) refuseLinks(path.join(target, name));
  fs.mkdirSync(target, { recursive: true });
  for (const name of owned) mirror(path.join(source, name), path.join(target, name));
  console.log(`Installed Sync Companion at ${target} (${changed} entries changed).`);
  console.log('Restart Obsidian or reload its window after code updates.');
} catch (error) {
  console.error(`install: ${error.message}`);
  process.exitCode = 1;
}
