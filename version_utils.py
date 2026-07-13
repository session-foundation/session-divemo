"""Version normalization and comparison helpers.

Every version we deal with (GitHub tags, App Store, Play Store, Debian package
versions) is reduced to a comparable upstream version string and compared with
:mod:`packaging`'s version ordering, falling back to a best-effort string
comparison if a value cannot be parsed as a PEP 440 / semver-ish version.
"""

import logging
import re

from packaging.version import InvalidVersion, Version

log = logging.getLogger(__name__)

# Matches a Debian version's optional "epoch:" prefix and "-revision" suffix so
# we can recover the plain upstream version (e.g. "1:1.17.17-2" -> "1.17.17").
_DEB_EPOCH_RE = re.compile(r"^\d+:")
_DEB_REVISION_RE = re.compile(r"-[^-]+$")


def normalize_version(value):
    """Strip a leading v/V and surrounding whitespace from a version string."""
    if value is None:
        return ""
    return re.sub(r"^[vV]", "", value.strip())


def deb_upstream_version(value):
    """Reduce a Debian package version to its upstream portion.

    Drops the epoch ("1:") and Debian revision ("-2") so it can be compared
    against the upstream version published on GitHub / the app stores.
    """
    value = value.strip()
    value = _DEB_EPOCH_RE.sub("", value)
    # Only strip a revision if what remains still looks like it has one; the
    # upstream versions we track never contain a hyphen, so this is safe.
    if "-" in value:
        value = _DEB_REVISION_RE.sub("", value)
    return normalize_version(value)


def compare_versions(a, b):
    """Return -1, 0 or 1 for a<b, a==b, a>b.

    Uses PEP 440 ordering when both parse, otherwise falls back to a plain
    string comparison (and logs) so a weird value can never crash the monitor.
    """
    try:
        va, vb = Version(a), Version(b)
    except InvalidVersion:
        log.warning("Falling back to string comparison for %r vs %r", a, b)
        if a == b:
            return 0
        return -1 if a < b else 1
    if va == vb:
        return 0
    return -1 if va < vb else 1
