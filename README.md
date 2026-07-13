# session-divemo

**di**scord **ve**rsion **mo**nitor — a Discord bot that watches, for each
Session platform, the **latest published store release** against the **latest
published GitHub release**, and posts to a Discord channel when they diverge.

| Platform         | GitHub repo       | Store source                                            |
| ---------------- | ----------------- | ------------------------------------------------------- |
| Session Android  | `session-android` | Google Play listing (scraped — no official API exists)  |
| Session iOS      | `session-ios`     | App Store, via the public iTunes lookup API             |
| Session Desktop  | `session-desktop` | `deb.session.foundation` apt repo (`sid` `Packages.gz`) |

## What it posts

For each platform, on every poll it compares the store version `S` with the
GitHub version `G`:

- **⚠️ Warning** (`S > G`) — the store is **ahead** of GitHub (a public release
  whose source hasn't been published yet). The configured users for that
  platform are tagged. Sent once per warning episode.
- **✅ All-clear** — after a warning, once GitHub catches up (`G >= S`). Only
  sent if a warning was actually outstanding.
- **ℹ️ GitHub advanced** — GitHub moved forward and is not yet in the store.
  Informational, no tags.
- **ℹ️ Store advanced** — the store version moved forward (without going ahead
  of GitHub). Informational, no tags.

If the store is already ahead the first time a platform is seen, the warning
fires immediately; otherwise the first observation just seeds the baseline
silently. Warning state is latched in SQLite, so warnings fire once and
all-clears only follow a real warning — even across bot restarts.

## Configuration

Copy the sample and edit it. **`config.yaml` is git-ignored and must not be
committed** — it holds the bot token and the per-platform lists of users to
tag. Only `config.sample.yaml` is tracked.

```sh
cp config.sample.yaml config.yaml
$EDITOR config.yaml
```

Key fields (see `config.sample.yaml` for the full, commented example):

- `discord.token` — bot token (or set `$DISCORD_BOT_TOKEN` instead).
- `discord.channel_id` — channel to post into.
- `discord.guild_id` — optional; syncs the `/versions` slash command to that
  server instantly (global sync otherwise takes ~1h).
- `check_interval_seconds` — poll interval (default `3600`).
- `state_db` — SQLite path (default `divemo.db`, git-ignored).
- `platforms.<name>.tag_user_ids` — Discord user IDs to tag in that platform's
  warnings. Lists can differ per platform.

The bot needs permission to send messages (and use application commands) in the
target channel. No privileged intents are required.

## Running

Dependencies are standard packages (see `requirements.txt`); on Python 3.13+
you need `discord.py >= 2.5`. Install them however you manage Python packages,
e.g.:

```sh
pip install -r requirements.txt   # or use your system packages
```

Then:

```sh
python3 bot.py -c config.yaml
```

### Dry run (no Discord connection)

Fetch and evaluate every platform once, printing what it found and which events
*would* be sent, then update the state DB:

```sh
python3 bot.py -c config.yaml --check-once
```

### Slash command

`/versions` posts (visibly, so it can be referenced in the conversation) the
latest-seen versions per platform, labelled with each platform's real source —
**Play Store** / **App Store** / **APT repo** vs **GitHub** — and flags any
platform currently in a warning state with ⚠️.

## Running as a service (systemd)

`divemo.service` is a ready-to-adapt **systemd user service** template (autostart
on boot, restart on failure, logs to journald). Adjust the paths if you don't
clone to `~/session-divemo`, then:

```sh
mkdir -p ~/.config/systemd/user
cp divemo.service ~/.config/systemd/user/divemo.service
systemctl --user daemon-reload
systemctl --user enable --now divemo.service
sudo loginctl enable-linger "$USER"   # keep it running while logged out
```

Follow the logs with `journalctl --user -u divemo -f`.

## Tests

Pure comparison/transition logic and the SQLite round-trip are unit tested:

```sh
python3 -m unittest discover tests
```

## Layout

| File                 | Purpose                                                     |
| -------------------- | ----------------------------------------------------------- |
| `bot.py`             | Discord bot, poll loop, message formatting, CLI entry point |
| `sources.py`         | Version fetchers (GitHub, App Store, Play, deb repo)        |
| `state.py`           | SQLite state store + the pure transition state machine      |
| `version_utils.py`   | Version normalization and comparison                        |
| `config.sample.yaml` | Sample config (copy to the git-ignored `config.yaml`)       |
| `divemo.service`     | systemd user-service template                               |
| `tests/`             | Unit + integration tests                                    |

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE).
