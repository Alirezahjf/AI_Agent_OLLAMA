"""Update checks against GitHub releases.

The desktop app is distributed as a single ``.exe``, so there is no
package manager to lean on.  Instead we poll the GitHub Releases API
for the newest tag and compare it with the running version.

Everything here degrades quietly: no network, a rate-limited API, or a
malformed tag all produce "no update available" rather than an error
dialog in the user's face.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

from ..core.logging_setup import get_logger


logger = get_logger("desktop.updater")


GITHUB_REPO = "Alirezahjf/AI_Agent_OLLAMA"
RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

#: Minimum seconds between two network checks (24h).
CHECK_INTERVAL_SECONDS = 24 * 60 * 60

_VERSION_RE = re.compile(
    r"^\s*v?(?P<major>\d+)(?:\.(?P<minor>\d+))?(?:\.(?P<patch>\d+))?"
    r"(?:[-.]?(?P<pre>[A-Za-z][0-9A-Za-z.]*))?\s*$"
)

_PRE_ORDER = {"dev": 0, "alpha": 1, "a": 1, "beta": 2, "b": 2, "rc": 3, "pre": 3}


# ---------------------------------------------------------------------------
# Version handling
# ---------------------------------------------------------------------------


def parse_version(value: str) -> tuple[int, int, int, int, str] | None:
    """Parse ``v1.2.3-beta2`` into a comparable tuple.

    Returns ``None`` when the string is not a recognisable version.  The
    tuple is ``(major, minor, patch, pre_rank, pre_label)`` where
    ``pre_rank`` is ``4`` for final releases so that ``1.0.0`` sorts
    after ``1.0.0-rc1``.
    """
    if not value:
        return None
    match = _VERSION_RE.match(str(value))
    if not match:
        return None
    major = int(match.group("major"))
    minor = int(match.group("minor") or 0)
    patch = int(match.group("patch") or 0)
    pre = (match.group("pre") or "").lower()
    if not pre:
        return (major, minor, patch, 4, "")
    label = re.match(r"[a-z]+", pre)
    rank = _PRE_ORDER.get(label.group(0) if label else "", 0)
    return (major, minor, patch, rank, pre)


def compare_versions(left: str, right: str) -> int:
    """Return -1, 0 or 1 for ``left`` vs ``right``.

    Unparseable versions sort *below* parseable ones so that garbage
    never triggers a spurious update prompt.
    """
    a = parse_version(left)
    b = parse_version(right)
    if a is None and b is None:
        return 0
    if a is None:
        return -1
    if b is None:
        return 1
    return (a > b) - (a < b)


def is_newer(candidate: str, current: str) -> bool:
    """True when ``candidate`` is a strictly newer version than ``current``."""
    return compare_versions(candidate, current) > 0


# ---------------------------------------------------------------------------
# Release model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Release:
    """A GitHub release, reduced to what the updater needs."""

    version: str
    name: str
    url: str
    notes: str = ""
    published_at: str = ""
    assets: tuple[str, ...] = ()

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "Release":
        tag = str(payload.get("tag_name") or payload.get("name") or "")
        assets: Iterable[dict[str, Any]] = payload.get("assets") or []
        return cls(
            version=tag,
            name=str(payload.get("name") or tag),
            url=str(payload.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases"),
            notes=str(payload.get("body") or "")[:4000],
            published_at=str(payload.get("published_at") or ""),
            assets=tuple(
                str(a.get("browser_download_url", ""))
                for a in assets
                if a.get("browser_download_url")
            ),
        )

    @property
    def installer_url(self) -> str | None:
        """The first Windows installer/exe asset, when present."""
        for url in self.assets:
            if url.lower().endswith((".exe", ".msi")):
                return url
        return None


@dataclass(frozen=True)
class UpdateCheck:
    """Outcome of a single update check."""

    current_version: str
    available: bool
    release: Release | None = None
    error: str | None = None
    checked_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_version": self.current_version,
            "available": self.available,
            "error": self.error,
            "checked_at": self.checked_at,
            "release": None
            if self.release is None
            else {
                "version": self.release.version,
                "name": self.release.name,
                "url": self.release.url,
                "notes": self.release.notes,
                "installer_url": self.release.installer_url,
            },
        }


# ---------------------------------------------------------------------------
# Updater
# ---------------------------------------------------------------------------


class Updater:
    """Checks GitHub for a newer release, with an on-disk cooldown."""

    def __init__(
        self,
        current_version: str,
        *,
        data_dir: Path | str | None = None,
        url: str = RELEASES_URL,
        timeout: float = 8.0,
    ) -> None:
        self.current_version = str(current_version)
        self.data_dir = Path(data_dir) if data_dir else None
        self.url = url
        self.timeout = timeout

    # ----------------------------------------------------------- state

    @property
    def state_path(self) -> Path | None:
        return (self.data_dir / "update-check.json") if self.data_dir else None

    def _load_state(self) -> dict[str, Any]:
        path = self.state_path
        if path is None or not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        path = self.state_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.debug("could not persist update state: %s", exc)

    def should_check(self, *, now: float | None = None) -> bool:
        """False while the last check is still inside the cooldown window."""
        state = self._load_state()
        last = float(state.get("checked_at") or 0)
        current = now if now is not None else time.time()
        return (current - last) >= CHECK_INTERVAL_SECONDS

    # ----------------------------------------------------------- check

    def fetch_latest(self) -> Release | None:
        """Fetch the newest release, or ``None`` when unavailable."""
        try:
            response = requests.get(
                self.url,
                timeout=self.timeout,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "local-agent-desktop",
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.info("update check failed: %s", exc)
            return None
        if not isinstance(payload, dict):
            return None
        release = Release.from_api(payload)
        return release if release.version else None

    def check(self, *, force: bool = False) -> UpdateCheck:
        """Run an update check, honouring the cooldown unless ``force``."""
        now = time.time()
        if not force and not self.should_check(now=now):
            state = self._load_state()
            cached = state.get("release") or None
            release = (
                Release(
                    version=str(cached.get("version", "")),
                    name=str(cached.get("name", "")),
                    url=str(cached.get("url", "")),
                    notes=str(cached.get("notes", "")),
                )
                if isinstance(cached, dict) and cached.get("version")
                else None
            )
            available = bool(release and is_newer(release.version, self.current_version))
            return UpdateCheck(
                current_version=self.current_version,
                available=available,
                release=release if available else None,
                checked_at=float(state.get("checked_at") or 0),
            )

        release = self.fetch_latest()
        if release is None:
            self._save_state({"checked_at": now})
            return UpdateCheck(
                current_version=self.current_version,
                available=False,
                error="could not reach GitHub",
                checked_at=now,
            )

        available = is_newer(release.version, self.current_version)
        self._save_state({
            "checked_at": now,
            "release": {
                "version": release.version,
                "name": release.name,
                "url": release.url,
                "notes": release.notes[:1000],
            },
        })
        if available:
            logger.info("update available: %s -> %s", self.current_version, release.version)
        return UpdateCheck(
            current_version=self.current_version,
            available=available,
            release=release if available else None,
            checked_at=now,
        )
