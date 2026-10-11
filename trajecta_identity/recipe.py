"""Profiles as recipes.

A profile can be given as:

- a name (``example``) looked up in the profile search path;
- a folder containing ``profile.json``;
- a ``.json`` file;
- an ``http(s)://`` URL to a ``profile.json``. It is fetched once and cached
  under ``<data dir>/profiles/<name>/`` so it keeps working offline.

When no profile is given, the last one used is reused, then ``example``.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Any

from .paths import data_dir, safe_fs_name
from .profile import IdentityProfile, load_profile, validate_core

ENV_PROFILE = "TRAJECTA_IDENTITY_PROFILE"
DEFAULT_PROFILE = "example"
LAST_FILE = ".last-profile"
MAX_PROFILE_BYTES = 256 * 1024


def _last_path(env: dict[str, str] | None = None) -> Path:
    return data_dir(env=env) / LAST_FILE


def last_profile(env: dict[str, str] | None = None) -> str | None:
    path = _last_path(env)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def remember(spec: str, env: dict[str, str] | None = None) -> None:
    path = _last_path(env)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(spec + "\n", encoding="utf-8", newline="\n")
    except OSError:
        pass  # remembering is a convenience, never a failure


def _check(data: Any, origin: str) -> dict[str, Any]:
    if not isinstance(data, dict) or not str(data.get("name", "")).strip():
        raise ValueError(f"{origin}: a profile needs a non-empty 'name'")
    errors = validate_core(data.get("core") or {})
    if errors:
        raise ValueError(f"{origin}: " + "; ".join(errors))
    return data


def _install(data: dict[str, Any], env: dict[str, str] | None = None) -> str:
    """Copy a profile into the data dir and return its name."""

    name = str(data["name"]).strip()
    target = data_dir(env=env) / "profiles" / safe_fs_name(name)
    target.mkdir(parents=True, exist_ok=True)
    (target / "profile.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return name


def fetch(url: str, *, timeout: float = 20.0) -> dict[str, Any]:
    if not url.startswith(("https://", "http://")):
        raise ValueError("only http(s) profile URLs are supported")
    request = urllib.request.Request(url, headers={"User-Agent": "trajecta-identity-memory"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (scheme checked)
        raw = response.read(MAX_PROFILE_BYTES + 1)
    if len(raw) > MAX_PROFILE_BYTES:
        raise ValueError("profile is larger than 256 KiB")
    return _check(json.loads(raw.decode("utf-8")), url)


def resolve(
    spec: str | None = None,
    *,
    env: dict[str, str] | None = None,
    remember_choice: bool = True,
) -> IdentityProfile:
    """Turn a profile spec into a loaded profile (see module docstring)."""

    environ = dict(os.environ if env is None else env)
    chosen = (spec or "").strip() or environ.get(ENV_PROFILE, "").strip() or last_profile(env) or DEFAULT_PROFILE
    if chosen.startswith(("https://", "http://")):
        name = _install(fetch(chosen), env)
        profile = load_profile(name)
    else:
        path = Path(chosen).expanduser()
        if path.is_file() and path.suffix.lower() == ".json":
            data = _check(json.loads(path.read_text(encoding="utf-8")), str(path))
            profile = load_profile(_install(data, env))
        else:
            profile = load_profile(chosen)
    if remember_choice:
        remember(chosen, env)
    return profile
