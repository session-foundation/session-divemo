"""Transport-agnostic monitor: Session store releases vs. GitHub releases.

For each configured platform it periodically fetches the latest published
GitHub release and the latest store release, then:

  * warns (tagging configured users) when the store is *ahead* of GitHub — or,
    for a strict_sync platform (the APT repo we control), whenever the two
    diverge in *either* direction, since it must always match GitHub;
  * repeats the warning every warning_reminder_hours while it stays unresolved;
  * sends an all-clear once they are back in sync;
  * posts an informational update when GitHub advances (not yet in the store);
  * posts an informational update when the store version advances.

State is persisted in SQLite so warnings fire once, reminders are paced, and
all-clears only follow a real warning, even across restarts.

Each alert is posted to every target: a coroutine taking ``content``, ``embed``
and ``allowed_mentions`` keyword arguments, which both ``TextChannel.send`` and
``Webhook.send`` accept, so bot channels and webhooks mix freely.
"""

import asyncio
import logging
import time

import discord

from sources import STORE_DISPLAY_NAMES, fetch_github_latest, fetch_store
from state import (
    ALL_CLEAR,
    INFO_GITHUB,
    INFO_STORE,
    WARNING,
    WARNING_REMINDER,
    WARNING_UPDATE,
    StateStore,
    evaluate,
)
from version_utils import compare_versions

log = logging.getLogger("divemo")

COLOR_WARNING = 0xE74C3C
COLOR_ALL_CLEAR = 0x2ECC71
COLOR_INFO = 0x3498DB


class Monitor:
    """Polls every configured platform and posts alerts to ``targets``.

    ``targets`` maps a log label to a send coroutine. With no targets it is a
    dry run: platforms are fetched and evaluated and the state is recorded, but
    nothing is posted.
    """

    def __init__(self, config, targets):
        self.platforms = config["platforms"]
        self.github_token = config.get("github", {}).get("token") or None
        self.interval = int(config.get("check_interval_seconds", 300))
        self.reminder_interval = float(config.get("warning_reminder_hours", 12)) * 3600
        self.state = StateStore(config.get("state_db", "divemo.db"))
        self.targets = targets

    async def run_forever(self):
        while True:
            await self.check_all()
            await asyncio.sleep(self.interval)

    async def check_all(self):
        # Cache GitHub results per cycle so a repo shared by several platforms
        # (e.g. session-android backs both the Play and F-Droid checks) is only
        # fetched once, keeping us under the unauthenticated 60 req/h limit.
        github_cache = {}
        for name, pcfg in self.platforms.items():
            try:
                await self.check_platform(name, pcfg, github_cache)
            except Exception:  # keep the loop alive across per-platform errors
                log.exception("Version check failed for platform %s", name)

    async def check_platform(self, name, pcfg, github_cache):
        """Fetch, evaluate, announce and persist one platform.

        Returns ``(github, store, result)``.
        """
        repo = pcfg["github_repo"]
        github = github_cache.get(repo)
        if github is None:
            github = await asyncio.to_thread(
                fetch_github_latest, repo, self.github_token
            )
            github_cache[repo] = github
        store = await asyncio.to_thread(fetch_store, pcfg["store"])
        log.info(
            "%s: github=%s store=%s", name, github.version, store.version
        )

        prev = self.state.get(name)
        result = evaluate(
            prev, github.version, store.version, compare_versions,
            strict_sync=bool(pcfg.get("strict_sync", False)),
            now=time.time(),
            reminder_interval=self.reminder_interval,
        )
        if self.targets:
            for event in result.events:
                await self._announce(name, pcfg, event, github, store)
        self.state.upsert(
            name, github.version, store.version,
            result.warning_active, result.last_warned_at, result.store_high,
        )
        return github, store, result

    async def _announce(self, name, pcfg, event, github, store):
        display = pcfg.get("display_name", name)
        store_name = STORE_DISPLAY_NAMES.get(pcfg["store"]["store"], "store")

        content = ""
        allowed = discord.AllowedMentions.none()
        embed = discord.Embed()
        embed.add_field(name=f"{store_name} version", value=event.store_version, inline=True)
        embed.add_field(name="GitHub version", value=event.github_version, inline=True)
        if store.url:
            embed.add_field(name=f"{store_name} link", value=store.url, inline=False)
        if github.url:
            embed.add_field(name="GitHub release", value=github.url, inline=False)

        if event.kind in (WARNING, WARNING_UPDATE, WARNING_REMINDER):
            embed.color = COLOR_WARNING
            ids = [int(uid) for uid in pcfg.get("tag_user_ids", [])]
            content = " ".join(f"<@{uid}>" for uid in ids)
            allowed = discord.AllowedMentions(users=[discord.Object(id=uid) for uid in ids])

            # Direction-aware: the store may be ahead of GitHub or (for the APT
            # repo, which must stay in sync) behind it.
            if compare_versions(event.store_version, event.github_version) > 0:
                direction = f"{store_name} is ahead of GitHub"
                gap = (
                    f"The {store_name} has published **{event.store_version}**, but the "
                    f"latest GitHub release is only **{event.github_version}**."
                )
            else:
                direction = f"{store_name} is behind GitHub"
                gap = (
                    f"GitHub has published **{event.github_version}**, but the "
                    f"{store_name} is still at **{event.store_version}**."
                )

            if event.kind == WARNING:
                first = " (first check)" if event.initial else ""
                embed.title = f"⚠️ {display}: {direction}{first}"
                embed.description = gap
            elif event.kind == WARNING_UPDATE:
                embed.title = f"⚠️ {display}: {direction} — divergence grew"
                embed.description = gap
            else:  # WARNING_REMINDER
                hours = int(self.reminder_interval // 3600)
                embed.title = f"⚠️ {display}: {direction} — still unresolved"
                embed.description = f"{gap}\nStill out of sync after ~{hours}h."

        elif event.kind == ALL_CLEAR:
            embed.color = COLOR_ALL_CLEAR
            embed.title = f"✅ {display}: resolved — back in sync"
            embed.description = (
                f"{store_name} and GitHub are back in sync at **{event.github_version}**."
            )

        elif event.kind == INFO_GITHUB:
            embed.color = COLOR_INFO
            embed.title = f"ℹ️ {display}: new GitHub release"
            embed.description = (
                f"GitHub advanced to **{event.github_version}** "
                f"(the {store_name} is at **{event.store_version}**)."
            )

        elif event.kind == INFO_STORE:
            embed.color = COLOR_INFO
            embed.title = f"ℹ️ {display}: {store_name} updated"
            embed.description = (
                f"The {store_name} advanced to **{event.store_version}** "
                f"(GitHub is at **{event.github_version}**)."
            )
        else:
            log.warning("Unknown event kind: %s", event.kind)
            return

        await self._deliver(content=content, embed=embed, allowed_mentions=allowed)
        log.info(
            "Posted %s for %s (store=%s github=%s)",
            event.kind, name, event.store_version, event.github_version,
        )

    async def _deliver(self, **message):
        # A failing target (e.g. a webhook deleted in someone else's server) is
        # logged but must not block or duplicate the alert on the others, so the
        # event only counts as undelivered -- raising, so state isn't advanced
        # and it is retried next cycle -- when *every* target failed.
        labels = list(self.targets)
        results = await asyncio.gather(
            *(self.targets[label](**message) for label in labels),
            return_exceptions=True,
        )
        failures = [
            (label, r) for label, r in zip(labels, results) if isinstance(r, Exception)
        ]
        for label, exc in failures:
            log.error("Failed to post to %s", label, exc_info=exc)
        if len(failures) == len(labels):
            raise RuntimeError("alert could not be posted to any target") from failures[0][1]
