"""session-divemo entry point: runs the release monitor against Discord.

Alerts are posted to every configured target, any mix of:

  * at most one gateway bot (enabled by configuring a token), installed in a
    server: posts alerts to ``channel_id`` and serves the ``/versions`` slash
    command. Adding it to a server requires Manage Server there.
  * any number of channel webhooks (``webhook_urls``): alerts only, since a
    webhook can't receive commands. Creating one only needs Manage Webhooks on
    that channel.

See ``monitor.py`` for what gets posted and when.
"""

import argparse
import asyncio
import logging
import os
import sys

import aiohttp
import discord
import yaml
from discord import app_commands

from monitor import Monitor
from sources import STORE_DISPLAY_NAMES

log = logging.getLogger("divemo")


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    if not config or "discord" not in config or "platforms" not in config:
        raise ValueError(f"{path}: config must define 'discord' and 'platforms'")
    return config


class VersionMonitorBot(discord.Client):
    """The gateway bot: owns the monitor so polling starts once it is ready.

    ``webhooks`` are the other targets the monitor posts to alongside the
    bot's own channel.
    """

    # Only slash commands and message-posting are used, so a plain Client with a
    # CommandTree is sufficient (and avoids the message-content intent that
    # commands.Bot's prefix handling would warn about).
    def __init__(self, config, webhooks):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.channel_id = int(config["discord"]["channel_id"])
        self.guild_id = config["discord"].get("guild_id")
        self.monitor = Monitor(
            config, {**webhooks, f"bot channel {self.channel_id}": self._post}
        )
        self._channel = None
        self._monitor_task = None

    # -- lifecycle -----------------------------------------------------------

    async def setup_hook(self):
        self._monitor_task = asyncio.create_task(self._run_monitor())
        self.tree.add_command(self._versions_command())
        if self.guild_id:
            guild = discord.Object(id=int(self.guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)

    async def _run_monitor(self):
        await self.wait_until_ready()
        await self.monitor.run_forever()

    async def get_target_channel(self):
        if self._channel is None:
            self._channel = self.get_channel(self.channel_id) or await self.fetch_channel(
                self.channel_id
            )
        return self._channel

    async def _post(self, **message):
        channel = await self.get_target_channel()
        await channel.send(**message)

    # -- slash command -------------------------------------------------------

    def _versions_command(self):
        @app_commands.command(
            name="versions",
            description="Show the latest published versions for each Session platform.",
        )
        async def versions(interaction: discord.Interaction):
            lines = []
            for name, pcfg in self.monitor.platforms.items():
                st = self.monitor.state.get(name)
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


async def _open_webhooks(urls, session):
    """Map a log label to each webhook's send coroutine."""
    webhooks = {}
    for url in urls:
        webhook = discord.Webhook.from_url(url, session=session)
        # The label must not contain the URL: its token is the posting secret.
        label = f"webhook {webhook.id}"
        # An unreachable webhook is kept rather than fatal: a webhook deleted in
        # one server must not stop alerts to the others, and a network error
        # here may be transient. Posting failures are logged per alert.
        try:
            info = await webhook.fetch()
        except (discord.HTTPException, aiohttp.ClientError) as exc:
            log.error("Can't reach %s (%s); will still try to post to it", label, exc)
        else:
            log.info("Posting to %s %r (channel id=%s)", label, info.name, info.channel_id)
        webhooks[label] = webhook.send
    return webhooks


async def run(config, token, webhook_urls):
    async with aiohttp.ClientSession() as session:
        webhooks = await _open_webhooks(webhook_urls, session)
        if token is None:
            await Monitor(config, webhooks).run_forever()
        else:
            async with VersionMonitorBot(config, webhooks) as bot:
                await bot.start(token)


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
        asyncio.run(_run_check_once(config))
        return

    dcfg = config["discord"]
    token = dcfg.get("token") or os.environ.get("DISCORD_BOT_TOKEN") or None
    webhook_urls = dcfg.get("webhook_urls") or []
    if token and not dcfg.get("channel_id"):
        sys.exit("A bot token is configured but 'discord.channel_id' is not set")
    if not token and not webhook_urls:
        sys.exit(
            "Nothing to post to: configure a bot token ('discord.token' or "
            "$DISCORD_BOT_TOKEN) and/or 'discord.webhook_urls'"
        )
    try:
        asyncio.run(run(config, token, webhook_urls))
    except KeyboardInterrupt:
        pass


async def _run_check_once(config):
    """Offline dry-run: fetch and evaluate every platform, print the events."""
    monitor = Monitor(config, targets={})
    github_cache = {}
    try:
        for name, pcfg in monitor.platforms.items():
            try:
                github, store, result = await monitor.check_platform(
                    name, pcfg, github_cache
                )
            except Exception as exc:  # noqa: BLE001 - report and continue
                print(f"{name}: ERROR {exc}")
                continue
            events = ", ".join(e.kind for e in result.events) or "none"
            print(
                f"{name}: github={github.version} store={store.version} "
                f"warning_active={result.warning_active} events=[{events}]"
            )
    finally:
        monitor.state.close()


if __name__ == "__main__":
    main()
