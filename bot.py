"""Discord bot that watches Session store releases vs. GitHub releases.

For each configured platform it periodically fetches the latest published
GitHub release and the latest store release, then:

  * warns (tagging configured users) when the store is *ahead* of GitHub;
  * sends an all-clear once GitHub catches back up;
  * posts an informational update when GitHub advances (not yet in the store);
  * posts an informational update when the store version advances.

State is persisted in SQLite so warnings fire once and all-clears only follow a
real warning, even across restarts.
"""

import argparse
import asyncio
import logging
import os
import sys

import discord
import yaml
from discord import app_commands
from discord.ext import tasks

from sources import STORE_DISPLAY_NAMES, fetch_github_latest, fetch_store
from state import (
    ALL_CLEAR,
    INFO_GITHUB,
    INFO_STORE,
    WARNING,
    WARNING_UPDATE,
    StateStore,
    evaluate,
)
from version_utils import compare_versions

log = logging.getLogger("divemo")

COLOR_WARNING = 0xE74C3C
COLOR_ALL_CLEAR = 0x2ECC71
COLOR_INFO = 0x3498DB


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    if not config or "discord" not in config or "platforms" not in config:
        raise ValueError(f"{path}: config must define 'discord' and 'platforms'")
    return config


class VersionMonitorBot(discord.Client):
    # Only slash commands and message-posting are used, so a plain Client with a
    # CommandTree is sufficient (and avoids the message-content intent that
    # commands.Bot's prefix handling would warn about).
    def __init__(self, config):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.config = config
        self.platforms = config["platforms"]
        self.channel_id = int(config["discord"]["channel_id"])
        self.guild_id = config["discord"].get("guild_id")
        self.github_token = config.get("github", {}).get("token") or None
        self.interval = int(config.get("check_interval_seconds", 3600))
        self.state = StateStore(config.get("state_db", "divemo.db"))
        self._channel = None

    # -- lifecycle -----------------------------------------------------------

    async def setup_hook(self):
        self.check_versions.change_interval(seconds=self.interval)
        self.check_versions.start()
        self.tree.add_command(self._versions_command())
        if self.guild_id:
            guild = discord.Object(id=int(self.guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)

    async def get_target_channel(self):
        if self._channel is None:
            self._channel = self.get_channel(self.channel_id) or await self.fetch_channel(
                self.channel_id
            )
        return self._channel

    # -- monitoring loop -----------------------------------------------------

    @tasks.loop(seconds=3600)
    async def check_versions(self):
        for name, pcfg in self.platforms.items():
            try:
                await self._check_platform(name, pcfg)
            except Exception:  # keep the loop alive across per-platform errors
                log.exception("Version check failed for platform %s", name)

    @check_versions.before_loop
    async def _before_loop(self):
        await self.wait_until_ready()

    async def _check_platform(self, name, pcfg):
        github = await asyncio.to_thread(
            fetch_github_latest, pcfg["github_repo"], self.github_token
        )
        store = await asyncio.to_thread(fetch_store, pcfg["store"])
        log.info(
            "%s: github=%s store=%s", name, github.version, store.version
        )

        prev = self.state.get(name)
        result = evaluate(prev, github.version, store.version, compare_versions)
        for event in result.events:
            await self._announce(name, pcfg, event, github, store)
        self.state.upsert(name, github.version, store.version, result.warning_active)

    # -- messaging -----------------------------------------------------------

    async def _announce(self, name, pcfg, event, github, store):
        display = pcfg.get("display_name", name)
        store_name = STORE_DISPLAY_NAMES.get(pcfg["store"]["store"], "store")
        channel = await self.get_target_channel()

        content = ""
        allowed = discord.AllowedMentions.none()
        embed = discord.Embed()
        embed.add_field(name=f"{store_name} version", value=event.store_version, inline=True)
        embed.add_field(name="GitHub version", value=event.github_version, inline=True)
        if store.url:
            embed.add_field(name=f"{store_name} link", value=store.url, inline=False)
        if github.url:
            embed.add_field(name="GitHub release", value=github.url, inline=False)

        if event.kind in (WARNING, WARNING_UPDATE):
            embed.color = COLOR_WARNING
            mentions = " ".join(f"<@{uid}>" for uid in pcfg.get("tag_user_ids", []))
            allowed = discord.AllowedMentions(
                users=[discord.Object(id=int(uid)) for uid in pcfg.get("tag_user_ids", [])]
            )
            if event.kind == WARNING:
                seen = " (first check)" if event.initial else ""
                embed.title = f"⚠️ {display}: {store_name} is ahead of GitHub{seen}"
                embed.description = (
                    f"The {store_name} has published **{event.store_version}**, "
                    f"but the latest GitHub release is only **{event.github_version}**."
                )
            else:
                embed.title = f"⚠️ {display}: {store_name} advanced further ahead of GitHub"
                embed.description = (
                    f"The {store_name} moved on to **{event.store_version}** while "
                    f"GitHub is still at **{event.github_version}**."
                )
            content = mentions

        elif event.kind == ALL_CLEAR:
            embed.color = COLOR_ALL_CLEAR
            embed.title = f"✅ {display}: resolved — GitHub caught up"
            embed.description = (
                f"GitHub is now at **{event.github_version}**, no longer behind the "
                f"{store_name} (**{event.store_version}**)."
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

        await channel.send(content=content, embed=embed, allowed_mentions=allowed)

    # -- slash command -------------------------------------------------------

    def _versions_command(self):
        @app_commands.command(
            name="versions",
            description="Show the latest published versions for each Session platform.",
        )
        async def versions(interaction: discord.Interaction):
            lines = []
            for name, pcfg in self.platforms.items():
                st = self.state.get(name)
                display = pcfg.get("display_name", name)
                if st is None:
                    lines.append(f"**{display}**: no data yet")
                    continue
                store_name = STORE_DISPLAY_NAMES.get(pcfg["store"]["store"], "store")
                flag = " ⚠️" if st.warning_active else ""
                lines.append(
                    f"**{display}**: {store_name} `{st.store_version}` / "
                    f"GitHub `{st.github_version}`{flag}"
                )
            # Not ephemeral: the reply is visible to the channel so it can be
            # referenced in conversation.
            await interaction.response.send_message("\n".join(lines))

        return versions


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="session-divemo: Discord version monitor for Session releases"
    )
    parser.add_argument(
        "-c",
        "--config",
        default=os.environ.get("DIVEMO_CONFIG", "config.yaml"),
        help="path to config file (default: config.yaml or $DIVEMO_CONFIG)",
    )
    parser.add_argument(
        "--check-once",
        action="store_true",
        help="run a single check cycle without connecting to Discord (prints results)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = load_config(args.config)

    if args.check_once:
        _run_check_once(config)
        return

    token = config["discord"].get("token") or os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        sys.exit("No Discord bot token in config ('discord.token') or $DISCORD_BOT_TOKEN")

    bot = VersionMonitorBot(config)
    bot.run(token, log_handler=None)


def _run_check_once(config):
    """Offline dry-run: fetch and evaluate every platform, print the events."""
    store = StateStore(config.get("state_db", "divemo.db"))
    github_token = config.get("github", {}).get("token") or None
    for name, pcfg in config["platforms"].items():
        try:
            github = fetch_github_latest(pcfg["github_repo"], github_token)
            store_info = fetch_store(pcfg["store"])
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"{name}: ERROR {exc}")
            continue
        prev = store.get(name)
        result = evaluate(prev, github.version, store_info.version, compare_versions)
        events = ", ".join(e.kind for e in result.events) or "none"
        print(
            f"{name}: github={github.version} store={store_info.version} "
            f"warning_active={result.warning_active} events=[{events}]"
        )
        store.upsert(name, github.version, store_info.version, result.warning_active)
    store.close()


if __name__ == "__main__":
    main()
