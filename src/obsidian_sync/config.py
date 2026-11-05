"""YAML configuration loading with dataclass defaults for obsidian-sync."""

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path("~/.config/obsidian-sync/config.yaml")
DEFAULT_REPOS_ROOT = Path.home() / "git_repositories"


def default_repos_root() -> Path:
    """Return the default repo root for personal git clones."""
    raw_root = os.environ.get("REPOS_ROOT")
    if raw_root:
        return Path(raw_root).expanduser()
    return DEFAULT_REPOS_ROOT


def default_vault_path() -> str:
    """Return the default Obsidian vault path."""
    return str(default_repos_root() / "obsidian-vault")


@dataclass
class SyncSettings:
    """Settings controlling sync behavior."""

    interval_seconds: int = 60
    pull_strategy: str = "rebase"  # rebase | merge | ff-only
    remote: str = "origin"
    branch: str = "main"


@dataclass
class CommitSettings:
    """Settings for commit message generation."""

    template: str = (
        "{{date}} — Auto-sync completed"
        " [host={{hostname}}] [files={{numFiles}}] [changed={{files}}]"
    )
    date_format: str = "%Y-%m-%d %H:%M:%S"
    list_files_in_body: bool = True


@dataclass
class LogSettings:
    """Settings for log file management."""

    path: str = "~/.local/state/obsidian-sync/sync.log"
    max_size_mb: int = 10


@dataclass
class SyncConfig:
    """Top-level configuration for obsidian-sync."""

    vault_path: str = field(default_factory=default_vault_path)
    sync: SyncSettings = field(default_factory=SyncSettings)
    commit: CommitSettings = field(default_factory=CommitSettings)
    log: LogSettings = field(default_factory=LogSettings)
    lock_path: str = "~/.local/state/obsidian-sync/daemon.lock"


def _merge_dataclass(cls, defaults, overrides):
    """Merge a dict of overrides onto a dataclass, returning a new instance."""
    if overrides is None:
        return defaults
    merged = {}
    for f in defaults.__dataclass_fields__:
        if f in overrides:
            merged[f] = overrides[f]
        else:
            merged[f] = getattr(defaults, f)
    return cls(**merged)


def load_config(path: Path | None = None, *, required: bool = False) -> SyncConfig:
    """Load config from YAML, merging over defaults.

    If path is None, uses DEFAULT_CONFIG_PATH. If the file doesn't exist,
    returns a SyncConfig with all defaults.
    """
    if path is None:
        path = DEFAULT_CONFIG_PATH.expanduser()
    if not path.exists():
        if required:
            raise ValueError(f"Config not found: {path}")
        return SyncConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("Config must be a YAML mapping")
    if required:
        if not isinstance(raw.get("vault_path"), str) or not raw["vault_path"].strip():
            raise ValueError("Installed config requires an explicit vault_path")
        for section in ("sync", "commit", "log"):
            if section in raw and not isinstance(raw[section], dict):
                raise ValueError(f"Config {section} must be a mapping")
        for section, defaults in (
            (None, SyncConfig()),
            ("sync", SyncSettings()),
            ("commit", CommitSettings()),
            ("log", LogSettings()),
        ):
            values = raw if section is None else raw.get(section, {})
            for name, value in values.items():
                if section is None and name in ("sync", "commit", "log"):
                    continue
                if hasattr(defaults, name) and type(value) is not type(getattr(defaults, name)):
                    raise ValueError(f"Invalid config value: {section or 'config'}.{name}")
        if raw.get("sync", {}).get("interval_seconds", 60) <= 0:
            raise ValueError("sync.interval_seconds must be positive")
        if raw.get("sync", {}).get("pull_strategy", "rebase") not in ("rebase", "merge", "ff-only"):
            raise ValueError("Invalid sync.pull_strategy")
    defaults = SyncConfig()
    return SyncConfig(
        vault_path=raw.get("vault_path", defaults.vault_path),
        sync=_merge_dataclass(SyncSettings, defaults.sync, raw.get("sync")),
        commit=_merge_dataclass(CommitSettings, defaults.commit, raw.get("commit")),
        log=_merge_dataclass(LogSettings, defaults.log, raw.get("log")),
        lock_path=raw.get("lock_path", defaults.lock_path),
    )


def expand_paths(config: SyncConfig) -> SyncConfig:
    """Expand ~ in all path fields. Returns a new config with expanded paths."""
    return SyncConfig(
        vault_path=str(Path(config.vault_path).expanduser()),
        sync=config.sync,
        commit=config.commit,
        log=LogSettings(
            path=str(Path(config.log.path).expanduser()),
            max_size_mb=config.log.max_size_mb,
        ),
        lock_path=str(Path(config.lock_path).expanduser()),
    )


def save_config(config: SyncConfig, path: Path) -> None:
    """Save config to YAML file, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(config)
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")
