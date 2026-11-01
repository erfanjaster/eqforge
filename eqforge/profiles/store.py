"""Profile storage: builtin presets + user profiles in XDG dirs.

Resolution order for an id: user profile shadows builtin. Profiles are one
JSON file each; the filename is cosmetic (id inside the file wins).
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from eqforge import paths
from eqforge.errors import ProfileError
from eqforge.log import get_logger
from eqforge.profiles.model import Profile
from eqforge.profiles.schema import validate_or_raise
from eqforge.profiles.versioning import migrate, needs_migration

log = get_logger("profiles.store")

BUILTIN_DIR = Path(__file__).parent / "presets"


class ProfileStore:
    def __init__(self, user_dir: Path | None = None,
                 builtin_dir: Path | None = None):
        self.user_dir = user_dir or paths.profiles_dir()
        self.builtin_dir = builtin_dir or BUILTIN_DIR

    # ---------------- discovery ----------------
    def _scan(self, directory: Path, builtin: bool) -> dict[str, Path]:
        out: dict[str, Path] = {}
        if not directory.exists():
            return out
        for p in sorted(directory.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                pid = str(d.get("id") or p.stem)
                out[pid] = p
            except (json.JSONDecodeError, OSError) as e:
                log.warning("skipping broken profile %s: %s", p, e)
        return out

    def user_profiles(self) -> dict[str, Path]:
        return self._scan(self.user_dir, False)

    def builtin_profiles(self) -> dict[str, Path]:
        return self._scan(self.builtin_dir, True)

    def list_ids(self) -> list[dict]:
        """[{id, name, builtin, path, tags}] — user shadows builtin."""
        entries: dict[str, dict] = {}
        for builtin, src in ((True, self.builtin_profiles()),
                             (False, self.user_profiles())):
            for pid, path in src.items():
                d = self._load_raw(path)
                entries[pid] = {
                    "id": pid,
                    "name": d.get("name", pid),
                    "builtin": builtin,
                    "path": str(path),
                    "tags": d.get("tags", []),
                    "description": d.get("description", ""),
                    "extends": d.get("extends", []),
                    "modified": d.get("modified", 0),
                }
        return sorted(entries.values(), key=lambda e: (e["builtin"], e["id"]))

    # ---------------- load / save ----------------
    def _load_raw(self, path: Path) -> dict:
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ProfileError(f"profile file not found: {path}")
        except json.JSONDecodeError as e:
            raise ProfileError(f"profile {path.name} is not valid JSON: {e}",
                               hint="fix the file or delete it")
        if needs_migration(d):
            d = migrate(d)
        return d

    def load_raw(self, profile_id: str) -> dict:
        """Raw (unresolved, migrated) profile dict by id."""
        for src in (self.user_profiles(), self.builtin_profiles()):
            if profile_id in src:
                return self._load_raw(src[profile_id])
        # maybe the caller passed a path
        p = Path(profile_id)
        if p.exists() and p.suffix == ".json":
            return self._load_raw(p)
        raise ProfileError(f"no such profile: {profile_id!r}",
                           hint="run `eqforge list-profiles` to see available ids")

    def load(self, profile_id: str) -> Profile:
        return Profile.from_dict(self.load_raw(profile_id))

    def resolve(self, profile_id: str) -> dict:
        from eqforge.profiles.resolve import resolve_profile
        return resolve_profile(profile_id, self.load_raw)

    def resolve_any(self, ref: str) -> dict:
        """Resolve from an id *or* a direct file path."""
        p = Path(ref)
        if p.exists() and p.suffix == ".json":
            d = self._load_raw(p)
            validate_or_raise(d)
            if d.get("extends"):
                from eqforge.profiles.resolve import resolve_profile
                pid = str(d.get("id") or "__inline__")
                cache = {pid: d}

                def loader(i: str) -> dict:
                    if i in cache:
                        return cache[i]
                    return self.load_raw(i)

                resolved = resolve_profile(pid, loader)
            else:
                resolved = d
            validate_or_raise(resolved)
            return resolved
        d = self.resolve(ref)
        validate_or_raise(d)
        return d

    def save(self, profile: Profile, migrate_on_save: bool = True,
             _tmp_id: str | None = None) -> Path:
        paths.ensure_dirs()
        d = profile.to_dict()
        if migrate_on_save:
            validate_or_raise(d)
        d["modified"] = time.time()
        pid = _tmp_id or profile.id
        path = self.user_dir / f"{pid.replace('/', '_')}.json"
        path.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
        return path
    def save_raw(self, d: dict, pid: str | None = None) -> Path:
        """Save a raw dict (used by importers) after validation."""
        if needs_migration(d):
            d = migrate(d)
        validate_or_raise(d)
        prof = Profile.from_dict(d)
        return self.save(prof)

    def delete(self, profile_id: str, missing_ok: bool = False) -> None:
        src = self.user_profiles()
        if profile_id not in src:
            if missing_ok:
                return
            raise ProfileError(f"no user profile {profile_id!r} "
                               "(builtins cannot be deleted)")
        src[profile_id].unlink()

    def duplicate(self, src_id: str, new_id: str) -> Path:
        d = self.load_raw(src_id)
        d["id"] = new_id
        d["name"] = f"{d.get('name', src_id)} (copy)"
        d["builtin"] = False
        d.pop("created", None)
        return self.save_raw(d, new_id)

    def export(self, profile_id: str, dest: Path) -> Path:
        d = self.resolve(profile_id)  # export fully-resolved, self-contained
        d.pop("extends", None)
        d["exported_from"] = profile_id
        d["exported_at"] = time.time()
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
        return dest

    def import_file(self, src: Path, new_id: str | None = None) -> Profile:
        """Import an eqforge profile JSON (or delegate to format importers)."""
        from eqforge.importers.detect import detect_and_import
        return detect_and_import(Path(src), store=self, new_id=new_id)

    # ---------------- config (active profile etc.) ----------------
    def config(self) -> dict:
        paths.ensure_dirs()
        p = paths.config_file()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                backup = p.with_suffix(".broken.bak")
                shutil.copy2(p, backup)
                log.warning("config.json corrupt; backed up to %s", backup)
        return {}

    def save_config(self, cfg: dict) -> None:
        paths.ensure_dirs()
        tmp = paths.config_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        tmp.replace(paths.config_file())
