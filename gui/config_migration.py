"""One-time import of user GUI settings; never import packaged defaults.

Existing profiles always win automatic discovery. Explicit restoration requires
--replace-existing, backs up both files, and copies the entire validated source.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile

logger = logging.getLogger(__name__)
MARKER_NAME = "gui-migration-v1.json"


def _digest(content):
    return hashlib.sha256(content).hexdigest()


def _publish(path, content, *, replace=False):
    """Publish a complete fsynced file, refusing to clobber by default."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            # Unlike a check followed by replace(), link() cannot overwrite a
            # configuration created by another process during migration.
            os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _backup(target, role, content):
    path = target.parent / "migration-backups" / f"{role}-{_digest(content)}.json"
    try:
        _publish(path, content)
    except FileExistsError:
        if path.read_bytes() != content:
            raise OSError(f"Configuration backup differs: {path}")
    return str(path)


def migrate_legacy_gui_config(target: Path, sources, *, replace_existing=False):
    target = Path(target).resolve()
    marker = target.with_name(MARKER_NAME)
    if marker.exists() and not replace_existing:
        # Completed and conflict decisions are stable across restarts/updates.
        return json.loads(marker.read_text(encoding="utf-8"))
    source = next((Path(p).resolve() for p in sources
                   if Path(p).resolve() != target and Path(p).is_file()), None)
    if source is None:
        return {"status": "no_legacy", "target": str(target)}

    from gui.config import ConfigStore
    content = source.read_bytes()
    # Use exactly the GUI's existing validation, without checking or launching
    # the emulator executable. A temporarily missing drive must not erase it.
    try:
        ConfigStore(source).load()
    except ValueError as exc:
        logger.warning("Legacy GUI config is invalid; left untouched: %s (%s)", source, exc)
        return {"status": "invalid_legacy", "source": str(source), "target": str(target)}
    if source.read_bytes() != content:
        raise OSError("Legacy GUI config changed while being read; retry migration")

    result = {"version": 1, "source": str(source), "target": str(target),
              "source_sha256": _digest(content),
              "source_backup": _backup(target, "legacy", content)}
    if target.exists():
        existing = target.read_bytes()
        result["target_backup"] = _backup(target, "existing", existing)
        if existing == content:
            result["status"] = "already_present"
        elif not replace_existing:
            result["status"] = "conflict"
        else:
            if target.read_bytes() != existing:
                raise OSError("Current GUI config changed during backup; restoration cancelled")
            _publish(target, content, replace=True)
            result["status"] = "restored"
    else:
        try:
            _publish(target, content)
            result["status"] = "migrated"
        except FileExistsError:
            # A competing launch created a profile. Preserve it and report it.
            result["target_backup"] = _backup(target, "existing", target.read_bytes())
            result["status"] = "conflict"
    encoded = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if replace_existing:
        _publish(marker, encoded, replace=True)
    else:
        try:
            _publish(marker, encoded)
        except FileExistsError:
            pass
    logger.info("GUI configuration migration %s: %s -> %s; report: %s",
                result["status"], source, target, marker)
    if result["status"] == "conflict":
        logger.warning("Existing GUI profile preserved; legacy import requires explicit restoration")
    return result


def main(argv=None):
    from app_paths import GUI_CONFIG_PATH, legacy_gui_candidates
    parser = argparse.ArgumentParser(description="Import legacy GUI settings without starting the GUI or devices")
    parser.add_argument("--source", type=Path, help="Legacy config/gui.json outside the distribution resources")
    parser.add_argument("--replace-existing", action="store_true", help="Explicitly restore all legacy settings after backing up both profiles")
    args = parser.parse_args(argv)
    if args.replace_existing and args.source is None:
        parser.error("--replace-existing requires an explicit --source")
    if args.source is not None and not args.source.is_file():
        parser.error(f"Legacy file does not exist: {args.source}")
    result = migrate_legacy_gui_config(GUI_CONFIG_PATH,
                                      [args.source] if args.source else legacy_gui_candidates(),
                                      replace_existing=args.replace_existing)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"migrated", "restored", "already_present", "no_legacy"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
