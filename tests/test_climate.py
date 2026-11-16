"""Remote-start climate: the helpers in util.py, the request bodies in api.py and the
dump tool's car-command path. Shapes are from the Android app
(docs/hondalink-protocol/android-crosscheck.md sections J and K)."""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import util

ROOT = Path(__file__).resolve().parent.parent


def _load_api_module():
    if "hondalink_under_test" not in sys.modules:
        pkg = types.ModuleType("hondalink_under_test")
        pkg.__path__ = [str(ROOT)]
        sys.modules["hondalink_under_test"] = pkg
    return importlib.import_module("hondalink_under_test.api")


def _load_dump_tool():
    spec = importlib.util.spec_from_file_location("hondalink_dump", ROOT / "tools" / "hondalink_dump.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["hondalink_dump"] = module
    spec.loader.exec_module(module)
    return module


api_module = _load_api_module()
dump = _load_dump_tool()


# Saved settings in the profile reply


@pytest.mark.parametrize(
    "profile",
    [
        {"responseBody": {"sPValues": {"temperatureValue": "22", "unit": "F", "acDefrosterFront": "On"}}},
        {"profile": [{"other": 1}, {"details": {"temperatureValue": "22", "tempUnit": "F", "acDefrosterFront": "On"}}]},
        {"settings": [
            {"name": "temperatureValue", "value": "22"},
            {"name": "acDefrosterFront", "sPValue": "On"},
            {"temperatureValue": "ignored: first one wins", "unit": "F"},
        ]},
    ],
)
def test_saved_climate_settings_found_in_any_layout(profile: dict[str, Any]) -> None:
    saved = util.saved_climate_settings(profile)
    assert saved["temperatureValue"] == "22"
    assert saved["acDefrosterFront"] == "On"
    assert saved["unit"] == "F"


def test_saved_climate_settings_ignores_unrelated_units() -> None:
    profile = {"odometer": {"value": "1", "unit": "mile"}, "geoFenceRadius": "5"}
    assert util.saved_climate_settings(profile) == {}
    assert util.saved_climate_settings(None) == {}


# What the app sends for a saved temperature (ak/d.i)


@pytest.mark.parametrize(
    ("saved", "unit", "country", "expected"),
    [
        ("22", "F", "US", "72"),  # US car: saved Celsius shown as F
        ("19", "F", "US", "66"),  # 66.2
        ("22", "C", "US", "22"),
        ("22", None, "US", "72"),
        ("72", "F", "US", "162"),  # if the profile holds F, the app's math gives nonsense
        ("72", "F", "CA", "22"),
        ("22", "C", "CA", "22"),
        ("", "F", "US", None),
        ("Lo", "F", "US", None),
    ],
)
def test_app_ac_temp_val(saved: Any, unit: Any, country: str, expected: str | None) -> None:
    assert util.app_ac_temp_val(saved, unit, country) == expected


def test_ac_setting_from_saved() -> None:
    saved = {
        "temperatureValue": "22", "unit": "F", "acDefrosterFront": "On", "acDefrosterRear": "Off",
        "acDriverHeated": "High", "acHeaterSteering": "On",
    }
    assert util.ac_setting_from_saved(saved) == {
        "acTempVal": "72", "acTempUnit": "F", "acDefFSetting": "on", "acDefRSetting": "off",
        "seatHeaterDrSetting": "High", "strHeaterSetting": "on",
    }
    assert util.ac_setting_from_saved({}) is None


def test_build_ac_setting() -> None:
    assert util.build_ac_setting() is None
    assert util.build_ac_setting(defrost_front="", defrost_rear=None) is None
    assert util.build_ac_setting(temperature=72) == {"acTempVal": "72", "acTempUnit": "F"}
    assert util.build_ac_setting(temperature="21.5", unit="c") == {"acTempVal": "22", "acTempUnit": "C"}
    assert util.build_ac_setting(defrost_front="On", defrost_rear="OFF") == {
        "acDefFSetting": "on", "acDefRSetting": "off",
    }


# Request bodies


class _StubAPI(api_module.HondaLinkAPI):
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.session = None
        self.vin = "VIN"
        self.pin = "1234"
        self._responses = list(responses)
        self.requests: list[tuple[str, str, Any]] = []

    async def _request_api(self, method, path, *, json_body=None, **kwargs):
        self.requests.append((method, path, json_body))
        return self._responses.pop(0)


ACCEPTED = {"status": "SUCCESS", "responseBody": {"cigServiceRequestId": "req-1"}}
DONE = {"status": "SUCCESS"}


def test_start_engine_body_unchanged_without_climate() -> None:
    api = _StubAPI([ACCEPTED, DONE, ACCEPTED, DONE])
    asyncio.run(api.async_start_engine())
    asyncio.run(api.async_start_engine(extend=True, ac_setting={}))
    assert api.requests[0] == (
        "POST", "/REST/NGT/CIG/eng/async/srt", {"device": "VIN", "pin": "1234", "extend": False},
    )
    assert api.requests[1] == ("GET", "/REST/NGT/CIG/eng/results/req-1", None)
    assert api.requests[2][2] == {"device": "VIN", "pin": "1234", "extend": True}


def test_start_engine_with_climate() -> None:
    api = _StubAPI([ACCEPTED, DONE])
    setting = {"acTempVal": "72", "acTempUnit": "F", "acDefFSetting": "on"}
    asyncio.run(api.async_start_engine(ac_setting=setting))
    assert api.requests[0] == (
        "POST",
        "/REST/NGT/CIG/eng/async/srt",
        {"device": "VIN", "pin": "1234", "extend": False, "vehicleControl": {"acSetting": setting}},
    )


def test_set_climate_temperature() -> None:
    api = _StubAPI([ACCEPTED, DONE])
    result = asyncio.run(api.async_set_climate_temperature(72))
    assert api.requests[0] == (
        "POST",
        "/REST/CIG/ClimateAsync/1.0/temperature",
        {
            "device": "VIN",
            "pin": "1234",
            "vehicleControl": {"acSetting": {
                "acTempVal": "72", "acTempUnit": "F", "acDurationSetting": "", "acDefSetting": "",
            }},
        },
    )
    assert api.requests[1] == ("GET", "/REST/NGT/getClimateResult/1.0/temperature/req-1", None)
    assert result.request_id == "req-1"


# The dump tool


def _args(**overrides: Any) -> argparse.Namespace:
    values = {"start_climate": None, "defrost_front": None, "defrost_rear": None, "climate_now": None,
              "start_extra": None}
    return argparse.Namespace(**{**values, **overrides})


def test_tool_start_climate_setting() -> None:
    summary = {"app_would_send_acSetting": {"acTempVal": "72", "acTempUnit": "F", "acDefFSetting": "off"}}
    assert dump.start_climate_setting(_args(start_climate="saved", defrost_front="On"), util, summary) == {
        "acTempVal": "72", "acTempUnit": "F", "acDefFSetting": "on",
    }
    assert dump.start_climate_setting(_args(start_climate="72"), util, None) == {"acTempVal": "72", "acTempUnit": "F"}
    assert dump.start_climate_setting(_args(start_climate="none", defrost_rear="on"), util, None) == {
        "acDefRSetting": "on",
    }
    for bad in (_args(start_climate="saved"), _args(start_climate="none"), _args(start_climate="warm")):
        with pytest.raises(SystemExit):
            dump.start_climate_setting(bad, util, {"app_would_send_acSetting": None})


def test_tool_temperature_problem() -> None:
    assert dump.temperature_problem({"acTempVal": "72", "acTempUnit": "F"}) is None
    assert dump.temperature_problem({"acTempVal": "22", "acTempUnit": "C"}) is None
    assert dump.temperature_problem({"acDefFSetting": "on"}) is None
    assert "outside" in dump.temperature_problem({"acTempVal": "162", "acTempUnit": "F"})
    assert "outside" in dump.temperature_problem({"acTempVal": "22", "acTempUnit": "F"})


class _FakeToolAPI:
    """Stands in for HondaLinkAPI in the dump tool; records every car command."""

    commands: list[tuple[str, Any]] = []
    country = "US"

    def __init__(self, session, *, email, password, pin, vin=None) -> None:
        self.vin, self.pin = vin, pin

    async def async_login(self) -> None:
        pass

    async def async_get_vehicles(self):
        return [{"vin": "1HGCM82633A004352"}]

    async def async_get_vehicle_by_vin(self, vin):
        return {"vin": vin}

    async def async_get_profile(self, vin):
        return {"responseBody": {"sPValues": {"temperatureValue": "22", "unit": "F", "acDefrosterFront": "On"}}}

    async def async_request_dashboard_update(self, vin):
        self.commands.append(("refresh", None))

    async def async_get_dashboard_latest(self, vin):
        return {"responseBody": {"temperature": {"cabin": {"value": "21", "unit": "c"}}}}

    refuse_climate = False

    def _vin(self):
        return self.vin

    def _pin(self):
        return self.pin

    async def _async_cig_command(self, engine, command, body):
        assert (engine, command) == ("eng", "srt")
        assert body["device"] == self.vin and body["pin"] == self.pin
        request = {key: value for key, value in body.items() if key not in ("device", "pin")}
        self.commands.append(("start", request))
        if self.refuse_climate and "vehicleControl" in request:
            raise api_module.HondaLinkCommandError("HondaLink request failed: HTTP 500")
        return api_module.HondaLinkCommandResult("req-1", {"status": "SUCCESS", "pin": self.pin})

    async def async_stop_engine(self):
        self.commands.append(("stop", None))
        return api_module.HondaLinkCommandResult("req-2", {"status": "SUCCESS"})

    async def async_set_climate_temperature(self, temperature, unit="F"):
        self.commands.append(("climate", (temperature, unit)))
        return api_module.HondaLinkCommandResult("req-3", {"status": "SUCCESS"})


def _run_tool(monkeypatch, tmp_path, answers: list[str], **overrides: Any) -> Path:
    _FakeToolAPI.commands = []
    _FakeToolAPI.refuse_climate = overrides.pop("refuse_climate", False)
    monkeypatch.setattr(dump, "load_api", lambda directory: types.SimpleNamespace(HondaLinkAPI=_FakeToolAPI))
    monkeypatch.setattr(dump, "load_util", lambda: util)
    monkeypatch.setattr(dump, "find_integration_dir", lambda arg: ROOT)
    for name, value in (("HONDALINK_EMAIL", "a@example.com"), ("HONDALINK_PASSWORD", "pw"), ("HONDALINK_PIN", "9876")):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("HONDALINK_VIN", raising=False)
    replies = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
    out = tmp_path / "dump"
    args = _args(integration_dir=None, vin=None, wake=False, probe=False, raw=False, out=str(out), **overrides)
    assert asyncio.run(dump.run(args)) == 0
    return out


def test_tool_reads_only_by_default(monkeypatch, tmp_path) -> None:
    out = _run_tool(monkeypatch, tmp_path, [])
    assert _FakeToolAPI.commands == []
    summary = json.loads((out / "climate_settings.json").read_text())
    assert summary["saved_settings"] == {"temperatureValue": "22", "acDefrosterFront": "On", "unit": "F"}
    assert summary["app_would_send_acSetting"] == {"acTempVal": "72", "acTempUnit": "F", "acDefFSetting": "on"}
    assert not (out / "start_climate.json").exists()


def test_tool_start_climate_needs_typed_confirmation(monkeypatch, tmp_path) -> None:
    out = _run_tool(monkeypatch, tmp_path, ["yes"], start_climate="saved")
    assert _FakeToolAPI.commands == []
    assert not (out / "start_climate.json").exists()


def test_tool_start_climate_sends_and_stops(monkeypatch, tmp_path) -> None:
    out = _run_tool(monkeypatch, tmp_path, ["START", "engine on, front defrost lit", "STOP"], start_climate="saved")
    assert _FakeToolAPI.commands == [
        ("start", {"extend": False, "vehicleControl": {"acSetting": {
            "acTempVal": "72", "acTempUnit": "F", "acDefFSetting": "on",
        }}}),
        ("refresh", None),
        ("stop", None),
    ]
    text = (out / "start_climate.json").read_text()
    record = json.loads(text)
    assert record["request"]["vehicleControl"]["acSetting"]["acDefFSetting"] == "on"
    assert record["observed"] == "engine on, front defrost lit"
    assert record["status_after"]["cabin_temperature"] == 21
    assert "9876" not in text and "1HGCM82633A004352" not in text


def test_tool_refuses_a_nonsense_temperature(monkeypatch, tmp_path) -> None:
    out = _run_tool(monkeypatch, tmp_path, [], start_climate="120")
    assert _FakeToolAPI.commands == []
    assert "outside" in json.loads((out / "start_climate.json").read_text())["not_sent"]


def test_tool_climate_now(monkeypatch, tmp_path) -> None:
    out = _run_tool(monkeypatch, tmp_path, ["SEND"], climate_now=72)
    assert _FakeToolAPI.commands == [("climate", (72, "F")), ("refresh", None)]
    assert (out / "climate_now.json").exists()


def test_tool_plain_start_and_extra_fields(monkeypatch, tmp_path) -> None:
    _run_tool(monkeypatch, tmp_path, ["START", "", "no"], start_climate="plain")
    assert _FakeToolAPI.commands[0] == ("start", {"extend": False})
    extra = {"command": "Start", "isChanged": True, "vehicleControl": {"acSetting": {"acDefFSetting": "on"}}}
    _run_tool(monkeypatch, tmp_path, ["START", "", "no"], start_climate="72", start_extra=extra)
    assert _FakeToolAPI.commands[0] == ("start", {
        "extend": False, "command": "Start", "isChanged": True,
        "vehicleControl": {"acSetting": {"acTempVal": "72", "acTempUnit": "F", "acDefFSetting": "on"}},
    })


def test_tool_offers_plain_start_as_control_after_a_refusal(monkeypatch, tmp_path) -> None:
    out = _run_tool(monkeypatch, tmp_path, ["START", "START", "engine on", "STOP"], start_climate="72",
                    refuse_climate=True)
    assert [command for command, _ in _FakeToolAPI.commands] == ["start", "start", "refresh", "stop"]
    assert _FakeToolAPI.commands[1] == ("start", {"extend": False})
    record = json.loads((out / "start_climate.json").read_text())
    assert "HTTP 500" in record["error"]
    assert record["control_result"]["request_id"] == "req-1"


def test_tool_shows_inside_the_profile_devices_list() -> None:
    profile = {"devices": [{"deviceId": "ABC", "settings": {"temperatureValue": "22"}, "firstName": "N"}], "device": "X"}
    assert dump.redact(profile, set()) == {
        "devices": [{"deviceId": "**REDACTED**", "settings": {"temperatureValue": "22"}, "firstName": "**REDACTED**"}],
        "device": "**REDACTED**",
    }
