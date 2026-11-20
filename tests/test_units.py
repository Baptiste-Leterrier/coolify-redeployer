import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from redeployer import config
from redeployer.coolify import Resource
from redeployer.cron import CronError, CronExpression
from redeployer.registry import parse_image
from redeployer.selectors import SelectorError, parse_selectors

PARIS = ZoneInfo("Europe/Paris")


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


class CronTest(unittest.TestCase):
    def test_daily_midnight_paris(self):
        c = CronExpression("0 0 * * *")
        nxt = c.next_after(utc(2026, 11, 19, 12, 0), PARIS)
        self.assertEqual(nxt.astimezone(PARIS).isoformat(), "2026-11-20T00:00:00+01:00")

    def test_steps_ranges_names(self):
        c = CronExpression("*/15 9-17 * * MON-FRI")
        # Saturday 2026-11-21 -> Monday 09:00 Paris
        nxt = c.next_after(utc(2026, 11, 21, 10, 0), PARIS)
        self.assertEqual(nxt.astimezone(PARIS).isoformat(), "2026-11-23T09:00:00+01:00")
        nxt = c.next_after(nxt, PARIS)
        self.assertEqual(nxt.astimezone(PARIS).strftime("%H:%M"), "09:15")

    def test_dom_dow_or_rule(self):
        c = CronExpression("0 3 1 * 0")  # 1st of month OR Sunday
        nxt = c.next_after(utc(2026, 11, 19, 0, 0), PARIS)
        self.assertEqual(nxt.astimezone(PARIS).date().isoformat(), "2026-11-22")  # Sunday

    def test_sunday_as_7_and_macro(self):
        self.assertEqual(CronExpression("0 0 * * 7").dows, {0})
        self.assertEqual(CronExpression("@daily").minutes, {0})

    def test_dst_spring_forward_skips_missing_time(self):
        c = CronExpression("30 2 * * *")  # 2026-03-29 02:30 does not exist in Paris
        nxt = c.next_after(utc(2026, 3, 28, 12, 0), PARIS)
        self.assertEqual(nxt.astimezone(PARIS).isoformat(), "2026-03-30T02:30:00+02:00")

    def test_dst_fall_back_fires_once(self):
        c = CronExpression("30 2 * * *")  # 2026-10-25 02:30 happens twice in Paris
        first = c.next_after(utc(2026, 10, 24, 12, 0), PARIS)
        second = c.next_after(first, PARIS)
        self.assertEqual(first.astimezone(PARIS).isoformat(), "2026-10-25T02:30:00+02:00")
        self.assertEqual(second.astimezone(PARIS).isoformat(), "2026-10-26T02:30:00+01:00")

    def test_errors(self):
        for bad in ("* * * *", "0 0 0 * * *", "61 * * * *", "0 25 * * *", "*/0 * * * *", "5-1 * * * *", "x * * * *"):
            with self.assertRaises(CronError, msg=bad):
                CronExpression(bad)


class SelectorTest(unittest.TestCase):
    def res(self, **kw):
        base = dict(uuid="abc123", name="shop-api", type="application", project="Shop", project_uuid="p1",
                    environment="staging", environment_uuid="e11")
        base.update(kw)
        return Resource(**base)

    def test_kinds(self):
        r = self.res()
        cases = {"abc123": True, "name:shop-*": True, "name:SHOP-API": True, "project:shop": True,
                 "project:p1": True, "env:staging": True, "env:Shop/staging": True, "env:Shop/prod*": False,
                 "env:*/staging": True, "type:standalone-*": False, "type:application": True, "zzz": False}
        for spec, expected in cases.items():
            self.assertEqual(parse_selectors(spec)[0].matches(r), expected, spec)

    def test_separators_and_spaces_in_names(self):
        sels = parse_selectors("project:My Project, env:staging\nabc123")
        self.assertEqual([str(s) for s in sels], ["project:My Project", "env:staging", "abc123"])

    def test_invalid(self):
        for bad in ("foo bar", "colour:red", "name:"):
            with self.assertRaises(SelectorError, msg=bad):
                parse_selectors(bad)


class ImageRefTest(unittest.TestCase):
    def test_parse(self):
        r = parse_image("nginx")
        self.assertEqual((r.registry, r.repository, r.tag), ("registry-1.docker.io", "library/nginx", "latest"))
        r = parse_image("ghcr.io/acme/app:1.2")
        self.assertEqual((r.registry, r.repository, r.tag), ("ghcr.io", "acme/app", "1.2"))
        r = parse_image("localhost:5000/x/y")
        self.assertEqual((r.registry, r.repository, r.tag), ("localhost:5000", "x/y", "latest"))
        r = parse_image("louislam/uptime-kuma:1@sha256:" + "a" * 64)
        self.assertEqual(r.digest, "sha256:" + "a" * 64)
        self.assertIsNone(parse_image("${IMAGE:-nginx}"))


class ConfigTest(unittest.TestCase):
    base = {"TOKEN": "1|abcdef"}

    def test_defaults(self):
        c = config.load(dict(self.base))
        self.assertEqual(c.resource_types, ["application", "service"])
        self.assertEqual(c.cron_raw, "0 0 * * *")
        self.assertEqual(c.notify_on, ["upgrade", "failure"])
        self.assertFalse(c.api_enabled)

    def test_errors_are_collected(self):
        with self.assertRaises(config.ConfigError) as ctx:
            config.load({"UUIDS": "a,b c!", "CRON_SCHEDULE": "* * *", "TRIGGER_TOKEN": "short",
                         "NOVU_API_KEY": "x", "RESOURCE_TYPES": "app"})
        msg = str(ctx.exception)
        for part in ("TOKEN is required", "UUIDS", "5 fields", "TRIGGER_TOKEN", "NOVU_WORKFLOW_ID", "RESOURCE_TYPES"):
            self.assertIn(part, msg)

    def test_schedule_off_and_alerts(self):
        c = config.load(self.base | {"CRON_SCHEDULE": "off", "NTFY_URL": "https://ntfy.sh/topic",
                                     "NOVU_API_KEY": "k", "NOVU_WORKFLOW_ID": "w", "NOVU_SUBSCRIBER_ID": "s"})
        self.assertIsNone(c.cron)
        self.assertEqual(c.ntfy.url, "https://ntfy.sh/topic")
        self.assertEqual(c.novu.api_url, "https://api.novu.co")


if __name__ == "__main__":
    unittest.main()
