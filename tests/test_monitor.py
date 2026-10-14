"""Monitor tests: polling and alert posting through a fake transport."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monitor import Monitor  # noqa: E402
from sources import VersionInfo  # noqa: E402


class FakeSender:
    def __init__(self, fail=False):
        self.messages = []
        self.fail = fail

    async def __call__(self, **message):
        if self.fail:
            raise ConnectionError("target down")
        self.messages.append(message)


class MonitorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config = {
            "state_db": os.path.join(self._tmp.name, "state.db"),
            "platforms": {
                "android": {
                    "display_name": "Session Android",
                    "github_repo": "session-android",
                    "store": {"store": "play", "app_id": "network.loki.messenger"},
                    "tag_user_ids": [111, 222],
                },
                "android_fdroid": {
                    "github_repo": "session-android",
                    "store": {"store": "fdroid", "base_url": "x", "app_id": "y"},
                },
            },
        }
        self.store_versions = {"play": "1.1.0", "fdroid": "1.0.0"}
        patch_github = mock.patch(
            "monitor.fetch_github_latest",
            return_value=VersionInfo("1.0.0", "https://github.example/r"),
        )
        patch_store = mock.patch(
            "monitor.fetch_store",
            side_effect=lambda scfg: VersionInfo(self.store_versions[scfg["store"]]),
        )
        self.fetch_github = patch_github.start()
        patch_store.start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        self._tmp.cleanup()

    async def test_warning_tags_platform_users(self):
        send = FakeSender()
        monitor = Monitor(self.config, {"fake": send})
        try:
            await monitor.check_all()
        finally:
            monitor.state.close()

        self.assertEqual(len(send.messages), 1)
        message = send.messages[0]
        self.assertEqual(message["content"], "<@111> <@222>")
        self.assertEqual([u.id for u in message["allowed_mentions"].users], [111, 222])
        self.assertIn("Session Android", message["embed"].title)
        self.assertIn("ahead of GitHub", message["embed"].title)

    async def test_every_target_gets_the_alert(self):
        senders = {"a": FakeSender(), "b": FakeSender(), "c": FakeSender()}
        monitor = Monitor(self.config, senders)
        try:
            await monitor.check_all()
        finally:
            monitor.state.close()
        for send in senders.values():
            self.assertEqual(len(send.messages), 1)

    async def test_partial_failure_still_delivers_and_advances_state(self):
        good = FakeSender()
        monitor = Monitor(self.config, {"down": FakeSender(fail=True), "up": good})
        try:
            with self.assertLogs("divemo", "ERROR"):
                await monitor.check_all()
            self.assertEqual(len(good.messages), 1)
            self.assertTrue(monitor.state.get("android").warning_active)
            # Not retried next cycle, so the working target isn't spammed.
            await monitor.check_all()
            self.assertEqual(len(good.messages), 1)
        finally:
            monitor.state.close()

    async def test_total_failure_retries_next_cycle(self):
        down = FakeSender(fail=True)
        monitor = Monitor(self.config, {"down": down})
        try:
            with self.assertLogs("divemo", "ERROR"):
                await monitor.check_all()
            self.assertIsNone(monitor.state.get("android"))
            down.fail = False
            await monitor.check_all()
            self.assertEqual(len(down.messages), 1)
            self.assertTrue(monitor.state.get("android").warning_active)
        finally:
            monitor.state.close()

    async def test_shared_repo_fetched_once_per_cycle(self):
        monitor = Monitor(self.config, {"fake": FakeSender()})
        try:
            await monitor.check_all()
            self.assertEqual(self.fetch_github.call_count, 1)
            await monitor.check_all()
            self.assertEqual(self.fetch_github.call_count, 2)
        finally:
            monitor.state.close()

    async def test_dry_run_records_state_without_sending(self):
        monitor = Monitor(self.config, targets={})
        try:
            _, _, result = await monitor.check_platform(
                "android", self.config["platforms"]["android"], {}
            )
            self.assertTrue(result.events)
            self.assertTrue(monitor.state.get("android").warning_active)
        finally:
            monitor.state.close()


if __name__ == "__main__":
    unittest.main()
