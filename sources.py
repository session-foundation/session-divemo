"""Fetchers for the "latest published version" of each Session release channel.

Each fetcher returns a :class:`VersionInfo` (an upstream-normalized version
string plus a human-facing URL) or raises :class:`SourceError`.

Channels:
  * GitHub    -- the latest published release of session-{android,ios,desktop}
  * App Store -- the iTunes lookup API (official, returns the live version)
  * Play Store-- scraped from the store listing page (no official API exists)
  * Debian    -- parsed from the apt repository's Packages index
  * F-Droid   -- parsed from an F-Droid repo's index-v1.json (merged or live)
"""

import gzip
import json
import logging
import re
from dataclasses import dataclass

import requests

from version_utils import deb_upstream_version, normalize_version

log = logging.getLogger(__name__)

_USER_AGENT = "session-version-monitor (+https://github.com/session-foundation)"
_GITHUB_LATEST = "https://api.github.com/repos/session-foundation/{repo}/releases/latest"
_ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
_PLAY_DETAILS = "https://play.google.com/store/apps/details"

# The Play listing embeds the current version inside its bootstrap JSON as
# `[[["1.2.3"]]`; this is the long-standing extraction point used by scrapers.
_PLAY_VERSION_RE = re.compile(r'\[\[\["([\d]+(?:\.[\d]+)+)"\]\]')


class SourceError(RuntimeError):
    """Raised when a version could not be determined from a source."""


@dataclass
class VersionInfo:
    version: str
    url: str = ""


def fetch_github_latest(repo, token=None, timeout=30):
    """Latest published (non-draft, non-prerelease) GitHub release version."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": _USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.get(_GITHUB_LATEST.format(repo=repo), headers=headers, timeout=timeout)
    if resp.status_code == 404:
        raise SourceError(f"no published release found for {repo}")
    resp.raise_for_status()
    data = resp.json()
    tag = data.get("tag_name")
    if not tag:
        raise SourceError(f"GitHub response for {repo} had no tag_name")
    return VersionInfo(normalize_version(tag), data.get("html_url", ""))


def fetch_appstore(bundle_id, country="us", timeout=30):
    """Live App Store version via the public iTunes lookup API."""
    resp = requests.get(
        _ITUNES_LOOKUP,
        params={"bundleId": bundle_id, "country": country},
        headers={"User-Agent": _USER_AGENT},
        timeout=timeout,
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        raise SourceError(f"App Store has no listing for bundle id {bundle_id}")
    item = results[0]
    version = item.get("version")
    if not version:
        raise SourceError(f"App Store listing for {bundle_id} had no version")
    return VersionInfo(normalize_version(version), item.get("trackViewUrl", ""))


def fetch_play(app_id, country="us", lang="en", timeout=30):
    """Current Play Store version, scraped from the listing page.

    Google publishes no official version API, so we extract it from the JSON
    the listing page bootstraps with.
    """
    url = f"{_PLAY_DETAILS}?id={app_id}"
    resp = requests.get(
        _PLAY_DETAILS,
        params={"id": app_id, "hl": lang, "gl": country},
        headers={"User-Agent": _USER_AGENT},
        timeout=timeout,
    )
    resp.raise_for_status()
    match = _PLAY_VERSION_RE.search(resp.text)
    if not match:
        raise SourceError(
            f"could not extract a version from the Play listing for {app_id} "
            "(page layout may have changed)"
        )
    return VersionInfo(normalize_version(match.group(1)), url)


def fetch_deb(base_url, package, suite="sid", component="main", arch="amd64", timeout=30):
    """Latest upstream version of a package in an apt (Debian) repository."""
    base = base_url.rstrip("/")
    index_url = f"{base}/dists/{suite}/{component}/binary-{arch}/Packages.gz"
    resp = requests.get(index_url, headers={"User-Agent": _USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    try:
        text = gzip.decompress(resp.content).decode("utf-8", "replace")
    except (OSError, EOFError) as exc:
        raise SourceError(f"could not decompress Packages index at {index_url}: {exc}") from exc

    version = _packages_version(text, package)
    if version is None:
        raise SourceError(f"package {package!r} not found in {index_url}")
    return VersionInfo(deb_upstream_version(version), base)


def _packages_version(packages_text, package):
    """Return the ``Version:`` of ``package`` from a Packages index, or None."""
    # Stanzas are separated by blank lines; find the one whose Package matches.
    for stanza in re.split(r"\n\s*\n", packages_text):
        name = version = None
        for line in stanza.splitlines():
            if line.startswith("Package:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("Version:"):
                version = line.split(":", 1)[1].strip()
        if name == package and version:
            return version
    return None


def fetch_fdroid(base_url, app_id, timeout=30):
    """Latest version of an app published in an F-Droid repository.

    Reads the repo's ``index-v1.json``; the same fetcher serves both the
    *merged* index committed to session-fdroid's ``main`` branch (via
    raw.githubusercontent.com) and the *live* index served to F-Droid clients
    at fdroid.getsession.org -- they differ only by ``base_url``.
    """
    base = base_url.rstrip("/")
    index_url = f"{base}/index-v1.json"
    resp = requests.get(index_url, headers={"User-Agent": _USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    try:
        index = resp.json()
    except json.JSONDecodeError as exc:
        raise SourceError(f"could not parse F-Droid index at {index_url}: {exc}") from exc

    version = _fdroid_version(index, app_id)
    if version is None:
        raise SourceError(f"app {app_id!r} not found in {index_url}")
    return VersionInfo(normalize_version(version), base)


def _fdroid_version(index, app_id):
    """Return the ``versionName`` of the newest build of ``app_id``, or None.

    ``index`` is the parsed ``index-v1.json``. Each app maps to a list of
    per-build entries (one per ABI split); the newest is the one with the
    highest integer ``versionCode``.
    """
    builds = (index.get("packages") or {}).get(app_id)
    if not builds:
        return None
    newest = max(builds, key=lambda b: b.get("versionCode", 0))
    return newest.get("versionName")


# Dispatch table so the bot can resolve a store version from platform config
# keyed by its "store" discriminator.
def fetch_store(store_config):
    """Fetch the store version for a platform config's ``store`` block."""
    store_type = store_config["store"]
    if store_type == "appstore":
        return fetch_appstore(
            store_config["bundle_id"], store_config.get("country", "us")
        )
    if store_type == "play":
        return fetch_play(
            store_config["app_id"],
            store_config.get("country", "us"),
            store_config.get("lang", "en"),
        )
    if store_type == "deb":
        return fetch_deb(
            store_config["base_url"],
            store_config["package"],
            store_config.get("suite", "sid"),
            store_config.get("component", "main"),
            store_config.get("arch", "amd64"),
        )
    if store_type == "fdroid":
        return fetch_fdroid(store_config["base_url"], store_config["app_id"])
    raise SourceError(f"unknown store type: {store_type!r}")


STORE_DISPLAY_NAMES = {
    "appstore": "App Store",
    "play": "Play Store",
    "deb": "APT repo",
    "fdroid": "F-Droid repo",
}
