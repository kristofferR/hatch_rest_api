import unittest
from datetime import UTC, datetime, time, tzinfo
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from aiohttp import ClientError

from hatch_rest_api.hatch import Hatch
from hatch_rest_api.restore_iot import RestoreIot
from hatch_rest_api.restore_v4 import RestoreV4
from hatch_rest_api.restore_v5 import RestoreV5
from hatch_rest_api.scheduled_routine import (
    SCHEDULED_ROUTINE_ALARM_PRODUCTS,
    ScheduledRoutineAlarmMixin,
    alarm_weekdays_update_payload,
    alarm_update_payload,
    alarm_wake_time_update_payload,
    days_of_week_label,
    days_of_week_to_weekdays,
    weekdays_to_days_of_week,
)


class FakeResponse:
    status = 200
    headers = {}

    def __init__(self, url: str, payload: dict):
        self.url = url
        self._payload = payload

    async def json(self):
        return self._payload

    async def text(self):
        return str(self._payload)


class FakeSession:
    def __init__(self, fail_confirm=False, fail_fetch=False):
        self.calls = []
        self.last_mrds = []
        self.fail_confirm = fail_confirm
        self.fail_fetch = fail_fetch

    async def get(self, url: str, headers: dict, params: dict):
        self.calls.append(("GET", url, headers, params))
        if self.fail_fetch:
            raise ClientError("fetch failed")
        return FakeResponse(
            url,
            {
                "status": "success",
                "payload": [
                    {
                        "id": 2,
                        "name": "Wake",
                        "active": True,
                        "enabled": True,
                        "type": "alarm",
                        "macAddress": "AA:BB:CC",
                        "displayOrder": 1,
                    }
                ],
            },
        )

    async def post(self, url: str, json: dict, headers: dict):
        self.calls.append(("POST", url, headers, json))
        if self.fail_confirm and url.endswith("/service/app/v2/dataVersion"):
            raise ClientError("confirm failed")
        if url.endswith("/service/app/routine/v2/editMultiple"):
            self.last_mrds = json["mrds"]
            item = {
                "id": json["mrds"][0]["id"],
                "name": json["mrds"][0]["name"],
                "active": json["mrds"][0]["active"],
                "enabled": json["mrds"][0]["enabled"],
                "type": "alarm",
                "macAddress": "AA:BB:CC",
                "startTime": json["mrds"][0]["startTime"],
                "endTime": json["mrds"][0]["endTime"],
            }
            if "daysOfWeek" in json["mrds"][0]:
                item["daysOfWeek"] = json["mrds"][0]["daysOfWeek"]
            return FakeResponse(
                url,
                {
                    "status": "success",
                    "payload": {
                        "confirmDataVersion": True,
                        "item": [item],
                        "dataVersion": "version-1",
                    },
                },
            )
        item = {
            "id": self.last_mrds[0]["id"],
            "name": self.last_mrds[0]["name"],
            "active": self.last_mrds[0]["active"],
            "enabled": self.last_mrds[0]["enabled"],
            "type": "alarm",
            "macAddress": "AA:BB:CC",
            "startTime": self.last_mrds[0]["startTime"],
            "endTime": self.last_mrds[0]["endTime"],
        }
        if "daysOfWeek" in self.last_mrds[0]:
            item["daysOfWeek"] = self.last_mrds[0]["daysOfWeek"]
        return FakeResponse(
            url,
            {
                "status": "success",
                "payload": [
                    {"id": 5, "type": "routine", "enabled": True},
                    item,
                ],
            },
        )


class EmptyAlarmUpdateApi:
    def __init__(self):
        self.calls = []

    async def update_scheduled_routine_alarm_enabled(
        self,
        auth_token: str,
        mac: str,
        alarm: dict,
        enabled: bool,
        device_timezone: tzinfo | None = None,
    ):
        self.calls.append((auth_token, mac, alarm["id"], enabled))
        return []

    async def update_scheduled_routine_alarm_weekdays(
        self,
        auth_token: str,
        mac: str,
        alarm: dict,
        weekdays,
        device_timezone: tzinfo | None = None,
    ):
        self.calls.append((auth_token, mac, alarm["id"], list(weekdays)))
        return []

    async def update_scheduled_routine_alarm_wake_time(
        self,
        auth_token: str,
        mac: str,
        alarm: dict,
        wake_time: time,
        device_timezone: tzinfo | None = None,
    ):
        self.calls.append((auth_token, mac, alarm["id"], wake_time))
        return []


class RefreshAlarmApi:
    def __init__(self, alarms: list[dict]):
        self.alarms = alarms
        self.calls = []

    async def scheduled_routines(
        self,
        auth_token: str,
        mac: str,
        types: list[str],
    ):
        self.calls.append((auth_token, mac, types))
        return list(self.alarms)


class FakeAlarmDevice(ScheduledRoutineAlarmMixin):
    mac = "AA:BB:CC"
    device_name = "Restore"

    def __init__(self):
        self.publish_count = 0

    def publish_updates(self):
        self.publish_count += 1


class ScheduledRoutineAlarmTest(unittest.TestCase):
    def test_clearing_weekdays_rolls_only_enabled_alarms(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                alarm = {
                    "id": 2,
                    "enabled": enabled,
                    "startTime": "2000-01-01T07:00:00",
                    "endTime": "2000-01-01T07:30:00",
                    "daysOfWeek": 62,
                }

                payload = alarm_weekdays_update_payload(
                    alarm, [], now=datetime(2026, 5, 8, 8, 0, 0)
                )

                expected_date = "2026-05-09" if enabled else "2000-01-01"
                self.assertEqual(payload["daysOfWeek"], 0)
                self.assertIs(payload["enabled"], enabled)
                self.assertEqual(payload["startTime"], f"{expected_date}T07:00:00")
                self.assertEqual(payload["endTime"], f"{expected_date}T07:30:00")
                self.assertEqual(alarm["daysOfWeek"], 62)

    def test_alarm_products_include_all_restore_generations(self):
        self.assertEqual(
            SCHEDULED_ROUTINE_ALARM_PRODUCTS,
            {"restoreIot", "restoreV4", "restoreV5"},
        )

    def test_alarm_update_payload_preserves_active_and_toggles_enabled(self):
        alarm = {
            "id": 2,
            "name": "Wake",
            "active": False,
            "enabled": True,
            "displayOrder": 7,
            "startTime": "2026-05-08T07:30:00",
            "endTime": "2026-05-08T08:00:00",
            "daysOfWeek": 127,
        }

        payload = alarm_update_payload(alarm, False)

        self.assertEqual(payload["id"], 2)
        self.assertEqual(payload["name"], "Wake")
        self.assertIs(payload["active"], False)
        self.assertIs(payload["enabled"], False)
        self.assertEqual(payload["displayOrder"], 7)
        self.assertEqual(payload["startTime"], "2026-05-08T07:30:00")
        self.assertEqual(payload["endTime"], "2026-05-08T08:00:00")

    def test_weekdays_to_days_of_week_converts_weekday_lists_to_bitmask(self):
        self.assertEqual(
            weekdays_to_days_of_week(["monday", "tuesday", "friday"]),
            38,
        )
        self.assertEqual(weekdays_to_days_of_week([]), 0)
        self.assertEqual(
            weekdays_to_days_of_week([" Sunday ", "SATURDAY"]),
            65,
        )

    def test_days_of_week_to_weekdays_and_label_convert_bitmask(self):
        self.assertEqual(
            days_of_week_to_weekdays(38),
            ["monday", "tuesday", "friday"],
        )
        self.assertEqual(
            days_of_week_to_weekdays(127),
            [
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            ],
        )
        self.assertEqual(days_of_week_to_weekdays(0), [])
        self.assertEqual(days_of_week_label(0), "Once")
        self.assertEqual(days_of_week_label(62), "Weekdays")
        self.assertEqual(days_of_week_label(65), "Weekends")
        self.assertEqual(days_of_week_label(127), "Every day")
        self.assertEqual(days_of_week_label(42), "Mon, Wed, Fri")

    def test_alarm_weekdays_update_payload_preserves_fields_and_updates_days(self):
        alarm = {
            "id": 2,
            "name": "Wake",
            "active": False,
            "enabled": True,
            "displayOrder": 7,
            "startTime": "2026-05-08T07:30:00",
            "endTime": "2026-05-08T08:00:00",
            "daysOfWeek": 127,
        }

        payload = alarm_weekdays_update_payload(alarm, ["monday", "friday"])

        self.assertEqual(payload["id"], 2)
        self.assertEqual(payload["name"], "Wake")
        self.assertIs(payload["active"], False)
        self.assertIs(payload["enabled"], True)
        self.assertEqual(payload["displayOrder"], 7)
        self.assertEqual(payload["startTime"], "2026-05-08T07:30:00")
        self.assertEqual(payload["endTime"], "2026-05-08T08:00:00")
        self.assertEqual(payload["daysOfWeek"], 34)

    def test_alarm_weekdays_update_payload_allows_one_time_alarm(self):
        alarm = {
            "id": 2,
            "name": "Wake",
            "active": True,
            "enabled": True,
            "startTime": "2026-05-08T07:30:00",
            "endTime": "2026-05-08T08:00:00",
            "daysOfWeek": 127,
        }

        payload = alarm_weekdays_update_payload(
            alarm, [], now=datetime(2026, 5, 8, 7, 0, 0)
        )

        self.assertEqual(payload["daysOfWeek"], 0)
        self.assertIs(payload["enabled"], True)
        self.assertEqual(payload["startTime"], "2026-05-08T07:30:00")
        self.assertEqual(payload["endTime"], "2026-05-08T08:00:00")

    def test_alarm_weekdays_update_payload_rejects_invalid_weekday(self):
        alarm = {"id": 2}

        with self.assertRaisesRegex(ValueError, "Unsupported alarm weekday"):
            alarm_weekdays_update_payload(alarm, ["funday"])

    def test_one_time_alarm_enable_rolls_wake_time_to_tomorrow_when_time_passed(self):
        alarm = {
            "id": 2,
            "name": "Wake",
            "active": True,
            "enabled": False,
            "startTime": "2026-05-08T07:00:00",
            "endTime": "2026-05-08T07:30:00",
            "daysOfWeek": 0,
        }

        payload = alarm_update_payload(
            alarm,
            True,
            now=datetime(2026, 5, 8, 8, 0, 0),
        )

        self.assertEqual(payload["startTime"], "2026-05-09T07:00:00")
        self.assertEqual(payload["endTime"], "2026-05-09T07:30:00")

    def test_one_time_alarm_enable_rolls_wake_time_to_today_when_time_is_future(self):
        alarm = {
            "id": 2,
            "name": "Wake",
            "active": True,
            "enabled": False,
            "startTime": "2026-05-07T07:00:00",
            "endTime": "2026-05-07T07:30:00",
            "daysOfWeek": 0,
        }

        payload = alarm_update_payload(
            alarm,
            True,
            now=datetime(2026, 5, 8, 6, 0, 0),
        )

        self.assertEqual(payload["startTime"], "2026-05-08T07:00:00")
        self.assertEqual(payload["endTime"], "2026-05-08T07:30:00")

    def test_wake_time_update_preserves_sunrise_lead_for_repeating_alarm(self):
        alarm = {
            "id": 2,
            "name": "Wake",
            "active": True,
            "enabled": False,
            "displayOrder": 3,
            "startTime": "2026-05-08T07:00:00",
            "endTime": "2026-05-08T07:30:00",
            "daysOfWeek": 127,
        }

        payload = alarm_wake_time_update_payload(alarm, time(8, 15))

        self.assertEqual(payload["id"], 2)
        self.assertEqual(payload["name"], "Wake")
        self.assertIs(payload["active"], True)
        self.assertIs(payload["enabled"], False)
        self.assertEqual(payload["displayOrder"], 3)
        self.assertEqual(payload["startTime"], "2026-05-08T07:45:00")
        self.assertEqual(payload["endTime"], "2026-05-08T08:15:00")

    def test_wake_time_update_rolls_one_time_alarm_to_next_occurrence(self):
        alarm = {
            "id": 2,
            "name": "Wake",
            "active": True,
            "enabled": False,
            "startTime": "2026-05-08T07:00:00",
            "endTime": "2026-05-08T07:30:00",
            "daysOfWeek": 0,
        }

        payload = alarm_wake_time_update_payload(
            alarm,
            time(6, 45),
            now=datetime(2026, 5, 8, 8, 0, 0),
        )

        self.assertEqual(payload["startTime"], "2026-05-09T06:15:00")
        self.assertEqual(payload["endTime"], "2026-05-09T06:45:00")

    def test_wake_time_update_uses_sunrise_duration_when_end_time_is_missing(self):
        alarm = {
            "id": 2,
            "name": "Wake",
            "active": True,
            "enabled": False,
            "startTime": "2026-05-08T10:45:00",
            "endTime": None,
            "daysOfWeek": 62,
            "steps": [
                {
                    "name": "Sunrise",
                    "enabled": True,
                    "color": {"duration": 2700, "ignore": False},
                }
            ],
        }

        payload = alarm_wake_time_update_payload(alarm, time(11, 15))

        self.assertEqual(payload["startTime"], "2026-05-08T10:30:00")
        self.assertIsNone(payload["endTime"])
        self.assertIs(payload["enabled"], False)

    def test_one_time_alarm_enable_uses_sunrise_duration_when_end_time_is_missing(self):
        alarm = {
            "id": 2,
            "name": "Wake",
            "active": True,
            "enabled": False,
            "startTime": "2026-05-08 10:45:00",
            "endTime": None,
            "daysOfWeek": 0,
            "steps": [
                {
                    "name": "Sunrise",
                    "enabled": True,
                    "color": {"duration": 2700, "ignore": False},
                }
            ],
        }

        payload = alarm_update_payload(
            alarm,
            True,
            now=datetime(2026, 5, 8, 12, 0, 0),
        )

        self.assertEqual(payload["startTime"], "2026-05-09 10:45:00")
        self.assertIsNone(payload["endTime"])

    def test_wake_time_update_requires_sunrise_duration_when_end_time_is_missing(self):
        alarm = {
            "id": 2,
            "name": "Wake",
            "active": True,
            "enabled": False,
            "startTime": "2026-05-08T10:45:00",
            "endTime": None,
            "daysOfWeek": 62,
        }

        with self.assertRaisesRegex(ValueError, "valid sunrise duration"):
            alarm_wake_time_update_payload(alarm, time(11, 15))

    def test_wake_time_update_requires_valid_start_time(self):
        alarm = {
            "id": 2,
            "name": "Wake",
            "active": True,
            "enabled": False,
            "startTime": "not-a-date",
            "endTime": "2026-05-08T07:30:00",
            "daysOfWeek": 127,
        }

        with self.assertRaisesRegex(ValueError, "valid startTime"):
            alarm_wake_time_update_payload(alarm, time(6, 45))

    def test_wake_time_update_requires_valid_end_time_when_end_time_is_present(self):
        alarm = {
            "id": 2,
            "name": "Wake",
            "active": True,
            "enabled": False,
            "startTime": "2026-05-08T07:00:00",
            "endTime": "not-a-date",
            "daysOfWeek": 127,
        }

        with self.assertRaisesRegex(ValueError, "valid endTime"):
            alarm_wake_time_update_payload(alarm, time(6, 45))


class HatchScheduledRoutineApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_time_updates_use_device_local_date_and_time(self):
        for zone, instant, expected_date in (
            ("America/New_York", datetime(2026, 5, 8, 10, tzinfo=UTC), "2026-05-08"),
            ("Asia/Tokyo", datetime(2026, 5, 8, 0, 30, tzinfo=UTC), "2026-05-09"),
            ("America/New_York", datetime(2026, 3, 8, 10, tzinfo=UTC), "2026-03-08"),
        ):
            for operation in ("enabled", "wake_time", "weekdays"):
                with self.subTest(zone=zone, instant=instant, operation=operation):
                    session = FakeSession()
                    api = Hatch(client_session=session)
                    kwargs = {
                        "auth_token": "token",
                        "mac": "AA:BB:CC",
                        "alarm": {
                            "id": 2,
                            "enabled": True,
                            "startTime": "2000-01-01T07:00:00",
                            "endTime": "2000-01-01T07:30:00",
                            "daysOfWeek": 62 if operation == "weekdays" else 0,
                        },
                        "device_timezone": ZoneInfo(zone),
                    }
                    with patch("hatch_rest_api.hatch.datetime", wraps=datetime) as clock:
                        clock.now.side_effect = lambda tz=None: (
                            instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
                        )
                        if operation == "enabled":
                            await api.update_scheduled_routine_alarm_enabled(**kwargs, enabled=True)
                        elif operation == "wake_time":
                            await api.update_scheduled_routine_alarm_wake_time(**kwargs, wake_time=time(7, 30))
                        else:
                            await api.update_scheduled_routine_alarm_weekdays(**kwargs, weekdays=[])

                    self.assertEqual(session.last_mrds[0]["startTime"], f"{expected_date}T07:00:00")
                    self.assertEqual(session.last_mrds[0]["endTime"], f"{expected_date}T07:30:00")

    async def test_confirm_failure_refetches_authoritative_alarms(self):
        session = FakeSession(fail_confirm=True)
        api = Hatch(client_session=session)

        with self.assertLogs("hatch_rest_api.hatch", level="WARNING"):
            updated = await api.update_scheduled_routine_alarm_enabled(
                auth_token="token",
                mac="AA:BB:CC",
                alarm={"id": 2},
                enabled=False,
            )

        self.assertIs(updated[0]["enabled"], True)
        self.assertEqual(
            [(method, url.rsplit("/", 1)[-1]) for method, url, _, _ in session.calls],
            [("POST", "editMultiple"), ("POST", "dataVersion"), ("GET", "fetch")],
        )
        self.assertEqual(session.calls[-1][3], {
            "macAddress": "AA:BB:CC", "types": ["alarm"],
        })

    async def test_confirm_failure_propagates_refetch_failure(self):
        session = FakeSession(fail_confirm=True, fail_fetch=True)
        api = Hatch(client_session=session)

        with (
            self.assertLogs("hatch_rest_api.hatch", level="WARNING"),
            self.assertRaisesRegex(ClientError, "fetch failed"),
        ):
            await api.update_scheduled_routine_alarm_enabled(
                auth_token="token",
                mac="AA:BB:CC",
                alarm={"id": 2},
                enabled=False,
            )

        self.assertEqual(
            [(method, url.rsplit("/", 1)[-1]) for method, url, _, _ in session.calls],
            [("POST", "editMultiple"), ("POST", "dataVersion"), ("GET", "fetch")],
        )

    async def test_scheduled_routine_fetch_uses_v3_alarm_query(self):
        session = FakeSession()
        api = Hatch(client_session=session)

        routines = await api.scheduled_routines(
            auth_token="token",
            mac="AA:BB:CC",
            types=["alarm"],
        )

        self.assertEqual(routines[0]["id"], 2)
        method, url, headers, params = session.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/service/app/routine/v3/fetch"))
        self.assertEqual(headers["X-HatchBaby-Auth"], "token")
        self.assertEqual(params, {"macAddress": "AA:BB:CC", "types": ["alarm"]})

    async def test_alarm_update_uses_edit_multiple_and_confirms_data_version(self):
        session = FakeSession()
        api = Hatch(client_session=session)
        alarm = {
            "id": 2,
            "name": "Wake",
            "active": True,
            "enabled": True,
            "type": "alarm",
            "macAddress": "AA:BB:CC",
            "displayOrder": 1,
            "startTime": "2026-05-08T07:30:00",
            "endTime": "2026-05-08T08:00:00",
            "daysOfWeek": 127,
        }

        updated = await api.update_scheduled_routine_alarm_enabled(
            auth_token="token",
            mac="AA:BB:CC",
            alarm=alarm,
            enabled=False,
        )

        self.assertEqual(updated, [
            {
                "id": 2,
                "name": "Wake",
                "active": True,
                "enabled": False,
                "type": "alarm",
                "macAddress": "AA:BB:CC",
                "startTime": "2026-05-08T07:30:00",
                "endTime": "2026-05-08T08:00:00",
            }
        ])
        _, edit_url, _, edit_body = session.calls[0]
        self.assertTrue(edit_url.endswith("/service/app/routine/v2/editMultiple"))
        self.assertEqual(edit_body["type"], "alarm")
        self.assertEqual(edit_body["mrds"][0]["id"], 2)
        self.assertIs(edit_body["mrds"][0]["active"], True)
        self.assertIs(edit_body["mrds"][0]["enabled"], False)
        self.assertEqual(edit_body["mrds"][0]["startTime"], "2026-05-08T07:30:00")
        self.assertEqual(edit_body["mrds"][0]["endTime"], "2026-05-08T08:00:00")

        _, confirm_url, _, confirm_body = session.calls[1]
        self.assertTrue(confirm_url.endswith("/service/app/v2/dataVersion"))
        self.assertEqual(confirm_body, {
            "dataVersion": "version-1",
            "macAddress": "AA:BB:CC",
            "success": True,
            "returnAllRoutines": True,
        })

    async def test_alarm_wake_time_update_uses_edit_multiple_and_preserves_enabled(self):
        session = FakeSession()
        api = Hatch(client_session=session)
        alarm = {
            "id": 2,
            "name": "Wake",
            "active": True,
            "enabled": False,
            "type": "alarm",
            "macAddress": "AA:BB:CC",
            "displayOrder": 1,
            "startTime": "2026-05-08T07:30:00",
            "endTime": "2026-05-08T08:00:00",
            "daysOfWeek": 127,
        }

        updated = await api.update_scheduled_routine_alarm_wake_time(
            auth_token="token",
            mac="AA:BB:CC",
            alarm=alarm,
            wake_time=time(8, 15),
        )

        self.assertEqual(updated, [
            {
                "id": 2,
                "name": "Wake",
                "active": True,
                "enabled": False,
                "type": "alarm",
                "macAddress": "AA:BB:CC",
                "startTime": "2026-05-08T07:45:00",
                "endTime": "2026-05-08T08:15:00",
            }
        ])
        _, edit_url, _, edit_body = session.calls[0]
        self.assertTrue(edit_url.endswith("/service/app/routine/v2/editMultiple"))
        self.assertEqual(edit_body["type"], "alarm")
        self.assertIs(edit_body["mrds"][0]["active"], True)
        self.assertIs(edit_body["mrds"][0]["enabled"], False)
        self.assertEqual(edit_body["mrds"][0]["startTime"], "2026-05-08T07:45:00")
        self.assertEqual(edit_body["mrds"][0]["endTime"], "2026-05-08T08:15:00")

    async def test_alarm_weekdays_update_uses_edit_multiple_and_confirms_data_version(self):
        session = FakeSession()
        api = Hatch(client_session=session)
        alarm = {
            "id": 2,
            "name": "Wake",
            "active": True,
            "enabled": False,
            "type": "alarm",
            "macAddress": "AA:BB:CC",
            "displayOrder": 1,
            "startTime": "2026-05-08T07:30:00",
            "endTime": "2026-05-08T08:00:00",
            "daysOfWeek": 127,
        }

        updated = await api.update_scheduled_routine_alarm_weekdays(
            auth_token="token",
            mac="AA:BB:CC",
            alarm=alarm,
            weekdays=["monday", "tuesday", "friday"],
        )

        self.assertEqual(updated, [
            {
                "id": 2,
                "name": "Wake",
                "active": True,
                "enabled": False,
                "type": "alarm",
                "macAddress": "AA:BB:CC",
                "startTime": "2026-05-08T07:30:00",
                "endTime": "2026-05-08T08:00:00",
                "daysOfWeek": 38,
            }
        ])
        _, edit_url, _, edit_body = session.calls[0]
        self.assertTrue(edit_url.endswith("/service/app/routine/v2/editMultiple"))
        self.assertEqual(edit_body["type"], "alarm")
        self.assertIs(edit_body["mrds"][0]["active"], True)
        self.assertIs(edit_body["mrds"][0]["enabled"], False)
        self.assertEqual(edit_body["mrds"][0]["startTime"], "2026-05-08T07:30:00")
        self.assertEqual(edit_body["mrds"][0]["endTime"], "2026-05-08T08:00:00")
        self.assertEqual(edit_body["mrds"][0]["daysOfWeek"], 38)

        _, confirm_url, _, confirm_body = session.calls[1]
        self.assertTrue(confirm_url.endswith("/service/app/v2/dataVersion"))
        self.assertEqual(confirm_body, {
            "dataVersion": "version-1",
            "macAddress": "AA:BB:CC",
            "success": True,
            "returnAllRoutines": True,
        })


class ScheduledRoutineAlarmMixinTest(unittest.IsolatedAsyncioTestCase):
    async def test_device_shadow_timezone_reaches_alarm_updates_and_fallbacks(self):
        instant = datetime(2026, 5, 8, 10, tzinfo=UTC)
        for device_type in (RestoreIot, RestoreV4, RestoreV5):
            for fallback in (False, True):
                for operation in ("enabled", "wake_time", "weekdays"):
                    with self.subTest(device=device_type, fallback=fallback, operation=operation):
                        session = FakeSession()
                        api = EmptyAlarmUpdateApi() if fallback else Hatch(client_session=session)
                        device = object.__new__(device_type)
                        device.mac = "AA:BB:CC"
                        device.device_name = "Restore"
                        device.thing_name = "restore"
                        device.publish_updates = Mock()
                        device.configure_alarm_api(api=api, auth_token="token", alarms=[{
                            "id": 2,
                            "enabled": True,
                            "startTime": "2000-01-01T07:00:00",
                            "endTime": "2000-01-01T07:30:00",
                            "daysOfWeek": 62 if operation == "weekdays" else 0,
                        }])
                        device._update_local_state({"timezone": "America/New_York"})
                        device._update_local_state({"connected": True})
                        clock_path = "scheduled_routine" if fallback else "hatch"
                        with patch(f"hatch_rest_api.{clock_path}.datetime", wraps=datetime) as clock:
                            clock.now.side_effect = lambda tz=None: (
                                instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
                            )
                            if operation == "enabled":
                                await device.set_alarm_enabled(2, True)
                            elif operation == "wake_time":
                                await device.set_alarm_wake_time(2, time(7, 30))
                            else:
                                await device.set_alarm_weekdays(2, [])

                        self.assertEqual(device.alarms[0]["startTime"], "2026-05-08T07:00:00")
                        self.assertEqual(device.alarms[0]["endTime"], "2026-05-08T07:30:00")

    async def test_partial_update_preserves_cached_alarm_fields(self):
        device = FakeAlarmDevice()
        alarm = {
            "id": 2,
            "name": "Wake",
            "active": True,
            "enabled": True,
            "type": "alarm",
            "macAddress": "AA:BB:CC",
            "startTime": "2000-01-01T07:00:00",
            "endTime": "2000-01-01T07:30:00",
            "daysOfWeek": 62,
            "steps": [{"name": "Sunrise", "color": {"duration": 1800}}],
        }
        other_alarm = {"id": 3, "name": "Other"}
        device.configure_alarm_api(
            api=Hatch(client_session=FakeSession()),
            auth_token="token",
            alarms=[alarm, other_alarm],
        )

        await device.set_alarm_enabled(2, False)
        await device.set_alarm_enabled(2, True)

        self.assertEqual(device.alarms, [alarm, other_alarm])
        self.assertEqual(device.publish_count, 2)
        device._replace_alarms([{"id": 2, "name": "Updated"}, {"id": 4}])
        self.assertEqual(device.alarms, [{**alarm, "name": "Updated"}, other_alarm, {"id": 4}])

    async def test_set_alarm_wake_time_fallback_preserves_alarm_state(self):
        api = EmptyAlarmUpdateApi()
        device = FakeAlarmDevice()
        alarm = {
            "id": 2,
            "enabled": True,
            "startTime": "2000-01-01T07:00:00",
            "endTime": "2000-01-01T07:30:00",
            "daysOfWeek": 0,
        }
        other_alarm = {"id": 3, "name": "Other"}
        device.configure_alarm_api(api=api, auth_token="token", alarms=[alarm, other_alarm])
        before_update = datetime.now()

        await device.set_alarm_wake_time(2, time(8, 15))

        self.assertEqual(api.calls, [("token", "AA:BB:CC", 2, time(8, 15))])
        self.assertEqual(device.publish_count, 1)
        self.assertEqual(device.alarms[1], other_alarm)
        updated = device.alarms[0]
        self.assertEqual(updated["id"], 2)
        self.assertEqual(updated["daysOfWeek"], 0)
        self.assertIs(updated["enabled"], True)
        start = datetime.fromisoformat(updated["startTime"])
        end = datetime.fromisoformat(updated["endTime"])
        self.assertGreater(end, before_update)
        self.assertEqual(end.time(), time(8, 15))
        self.assertEqual((end - start).total_seconds(), 1800)

    async def test_set_alarm_weekdays_accepts_single_string(self):
        api = EmptyAlarmUpdateApi()
        device = FakeAlarmDevice()
        device.configure_alarm_api(api=api, auth_token="token", alarms=[{"id": 2}])

        await device.set_alarm_weekdays(2, "monday")

        self.assertEqual(api.calls, [("token", "AA:BB:CC", 2, ["monday"])])
        self.assertEqual(device.alarms[0]["daysOfWeek"], 2)
        self.assertEqual(device.publish_count, 1)

    async def test_failed_initial_fetch_remains_alarm_capable_for_refresh(self):
        api = RefreshAlarmApi(
            [
                {
                    "id": 2,
                    "name": "Wake",
                    "type": "alarm",
                    "macAddress": "AA:BB:CC",
                }
            ]
        )
        device = FakeAlarmDevice()

        device.configure_alarm_api(api=api, auth_token="token", alarms=None)

        self.assertIs(device.alarm_capable, True)
        self.assertIs(device.alarms_loaded, False)
        self.assertEqual(device.alarms, [])

        refreshed = await device.refresh_alarms()

        self.assertEqual(api.calls, [("token", "AA:BB:CC", ["alarm"])])
        self.assertIs(device.alarms_loaded, True)
        self.assertEqual(refreshed, api.alarms)
        self.assertEqual(device.alarms, api.alarms)
        self.assertEqual(device.publish_count, 1)

    async def test_set_alarm_enabled_fallback_rolls_one_time_alarm_times(self):
        api = EmptyAlarmUpdateApi()
        device = FakeAlarmDevice()
        device.configure_alarm_api(
            api=api,
            auth_token="token",
            alarms=[
                {
                    "id": 2,
                    "name": "Wake",
                    "active": True,
                    "enabled": False,
                    "type": "alarm",
                    "macAddress": "AA:BB:CC",
                    "startTime": "2000-01-01T07:00:00",
                    "endTime": "2000-01-01T07:30:00",
                    "daysOfWeek": 0,
                }
            ],
        )
        before_update = datetime.now()

        await device.set_alarm_enabled(2, True)

        self.assertEqual(api.calls, [("token", "AA:BB:CC", 2, True)])
        self.assertEqual(device.publish_count, 1)
        updated_alarm = device.alarms[0]
        self.assertIs(updated_alarm["enabled"], True)
        self.assertEqual(updated_alarm["daysOfWeek"], 0)
        self.assertNotEqual(updated_alarm["startTime"], "2000-01-01T07:00:00")
        self.assertNotEqual(updated_alarm["endTime"], "2000-01-01T07:30:00")
        updated_start = datetime.fromisoformat(updated_alarm["startTime"])
        updated_end = datetime.fromisoformat(updated_alarm["endTime"])
        self.assertGreater(updated_end, before_update)
        self.assertEqual((updated_end - updated_start).total_seconds(), 1800)


    async def test_set_alarm_weekdays_accepts_generator_input(self):
        api = EmptyAlarmUpdateApi()
        device = FakeAlarmDevice()
        device.configure_alarm_api(
            api=api,
            auth_token="token",
            alarms=[
                {
                    "id": 2,
                    "name": "Wake",
                    "active": True,
                    "enabled": True,
                    "type": "alarm",
                    "macAddress": "AA:BB:CC",
                    "startTime": "2026-05-08T07:30:00",
                    "endTime": "2026-05-08T08:00:00",
                    "daysOfWeek": 0,
                }
            ],
        )

        await device.set_alarm_weekdays(
            2,
            (weekday for weekday in ["monday", "wednesday", "friday"]),
        )

        self.assertEqual(
            api.calls,
            [("token", "AA:BB:CC", 2, ["monday", "wednesday", "friday"])],
        )
        self.assertEqual(device.publish_count, 1)
        updated_alarm = device.alarms[0]
        self.assertEqual(updated_alarm["daysOfWeek"], 42)


if __name__ == "__main__":
    unittest.main()
