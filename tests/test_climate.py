"""Remote-start climate: the helpers in util.py, the start request in api.py and the dump
tool's car-command path. The request shape is from a capture of the HondaLink app
(docs/hondalink-protocol/android-crosscheck.md section M)."""
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


# What the app sent with the defroster chosen, minus the VIN and PIN.
APP_DEFROST_CONTROL = {
    "acSetting": {"acDefSetting": "autoOn", "acTempVal": "04"},
    "acTemporarySetting": {"acDefSetting": "manualOn"},
}
SYNC_SETTINGS = json.loads((ROOT / "tests" / "fixtures" / "sync_settings_2025_crv_hybrid.json").read_text())


# The climate block


def test_remote_start_vehicle_control() -> None:
    assert util.remote_start_vehicle_control("off") is None
    assert util.remote_start_vehicle_control(None) is None
    assert util.remote_start_vehicle_control("defrost") == APP_DEFROST_CONTROL
    assert util.remote_start_vehicle_control("72") == {"acSetting": {"acDefSetting": "autoOn", "acTempVal": "04"}}
    assert util.remote_start_vehicle_control("67")["acSetting"]["acTempVal"] == "05"
    assert util.remote_start_vehicle_control("77")["acSetting"]["acTempVal"] == "03"
    with pytest.raises(ValueError):
        util.remote_start_vehicle_control("70")


def test_home_assistant_offers_only_confirmed_modes() -> None:
    assert util.REMOTE_START_CLIMATE_OPTIONS == ("off", "defrost")
    for option in util.REMOTE_START_CLIMATE_OPTIONS:
        util.remote_start_vehicle_control(option)


# The settings saved from the app


def test_saved_climate_settings_from_the_app() -> None:
    saved = util.saved_climate_settings(SYNC_SETTINGS)
    assert saved == {
        "unit": "F", "acPassengerHeater": "Off", "acDriverHeated": "Off", "acHeaterSteering": "Off",
        "acDefrosterFront": "On", "acDefrosterRear": "Off", "temperatureValue": "",
    }
    assert util.saved_climate_mode(saved) == "defrost"


def test_saved_climate_settings_takes_the_newest_entry() -> None:
    def detail(order: int, temperature: str) -> dict[str, Any]:
        return {"SPOrder": order, "SPValues": {"temperatureValue": temperature, "acDefrosterFront": "Off", "vin": "X"}}

    profile = {"profile": {"settings": [
        {"SID": "USERDATA", "SDetails": [{"SPOrder": 9, "SPValues": {"temperatureValue": "wrong section"}}]},
        {"SID": "REMOTECLIMATESETTINGS", "SDetails": [detail(2, "77"), detail(1, "67")]},
    ]}}
    saved = util.saved_climate_settings(profile)
    assert saved == {"temperatureValue": "77", "acDefrosterFront": "Off"}
    assert util.saved_climate_mode(saved) == "77"


@pytest.mark.parametrize("reply", [None, {}, {"profile": {}}, {"profile": {"settings": [{"SID": "USERDATA"}]}}])
def test_saved_climate_settings_missing(reply: Any) -> None:
    assert util.saved_climate_settings(reply) == {}
    assert util.saved_climate_mode({}) == "off"
    assert util.saved_climate_mode({"acDefrosterFront": "Off", "temperatureValue": ""}) == "off"


# The start request


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


ACCEPTED = {"status": "Success", "responseBody": {"cigServiceRequestId": "req-1"}}
DONE = {"status": "SUCCESS"}


def test_start_engine_body_unchanged_without_climate() -> None:
    api = _StubAPI([ACCEPTED, DONE, ACCEPTED, DONE])
    asyncio.run(api.async_start_engine())
    asyncio.run(api.async_start_engine(extend=True, vehicle_control=None))
    assert api.requests[0] == (
        "POST", "/REST/NGT/CIG/eng/async/srt", {"device": "VIN", "pin": "1234", "extend": False},
    )
    assert api.requests[1] == ("GET", "/REST/NGT/CIG/eng/results/req-1", None)
    assert api.requests[2][2] == {"device": "VIN", "pin": "1234", "extend": True}


def test_start_engine_with_defrost_matches_the_app() -> None:
    api = _StubAPI([ACCEPTED, DONE])
    asyncio.run(api.async_start_engine(vehicle_control=util.remote_start_vehicle_control("defrost")))
    assert api.requests[0] == (
        "POST",
        "/REST/NGT/CIG/eng/async/srt",
        {"device": "VIN", "pin": "1234", "extend": False, "vehicleControl": APP_DEFROST_CONTROL},
    )


# The dump tool


def _args(**overrides: Any) -> argparse.Namespace:
    values = {"start_climate": None, "start_extra": None}
    return argparse.Namespace(**{**values, **overrides})


def test_tool_start_vehicle_control() -> None:
    summary = dump.climate_summary(util, SYNC_SETTINGS)
    assert summary["mode"] == "defrost"
    assert dump.start_vehicle_control("saved", util, summary) == APP_DEFROST_CONTROL
    assert dump.start_vehicle_control("defrost", util, None) == APP_DEFROST_CONTROL
    assert dump.start_vehicle_control("plain", util, summary) is None
    assert dump.start_vehicle_control("77", util, None) == {"acSetting": {"acDefSetting": "autoOn", "acTempVal": "03"}}
    for bad in ("saved", "warm"):
        with pytest.raises(SystemExit):
            dump.start_vehicle_control(bad, util, None)


def test_tool_redaction_hides_a_vin_inside_a_key() -> None:
    vin = "1HGCM82633A004352"
    data = {
        "SPGUID": "658E813E-0000", "SPValues": {f"NPS_RC_EXECUTED_{vin}": "Y", "vin": vin, "temperatureValue": "72"},
        "errorMessage": "Profile USA001143b34f2785642cd86b01aa3b3c0f52c created.",
        "devices": [{"deviceId": "ABC", "settings": {"temperatureValue": "22"}, "firstName": "N"}], "device": "X",
    }
    assert dump.redact(data, set()) == {
        "SPGUID": "**REDACTED**",
        "SPValues": {"NPS_RC_EXECUTED_VIN-REDACTED": "Y", "vin": "**REDACTED**", "temperatureValue": "72"},
        "errorMessage": "Profile PROFILE-ID-REDACTED created.",
        "devices": [{"deviceId": "**REDACTED**", "settings": {"temperatureValue": "22"}, "firstName": "**REDACTED**"}],
        "device": "**REDACTED**",
    }


class _FakeToolAPI:
    """Stands in for HondaLinkAPI in the dump tool; records every car command."""

    commands: list[tuple[str, Any]] = []
    refuse_climate = False

    def __init__(self, session, *, email, password, pin, vin=None) -> None:
        self.vin, self.pin = vin, pin

    def _vin(self):
        return self.vin

    def _pin(self):
        return self.pin

    async def async_login(self) -> None:
        pass

    async def async_get_vehicles(self):
        return [{"vin": "1HGCM82633A004352"}]

    async def async_get_vehicle_by_vin(self, vin):
        return {"vin": vin}

    async def async_get_profile(self, vin):
        return {"devices": [{"id": vin}]}

    async def async_get_sync_settings(self):
        reply = json.loads(json.dumps(SYNC_SETTINGS).replace("NPS_RC_EXECUTED_VIN-REDACTED", f"NPS_RC_EXECUTED_{self.vin}"))
        return reply

    async def async_request_dashboard_update(self, vin):
        self.commands.append(("refresh", None))

    async def async_get_dashboard_latest(self, vin):
        return {"responseBody": {"temperature": {"cabin": {"value": "21", "unit": "c"}}}}

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
    assert summary["saved_settings"]["acDefrosterFront"] == "On"
    assert summary["start_would_send_vehicleControl"] == APP_DEFROST_CONTROL
    assert not (out / "start_climate.json").exists()
    for written in out.iterdir():
        assert "1HGCM82633A004352" not in written.read_text(), written.name


def test_tool_start_needs_typed_confirmation(monkeypatch, tmp_path) -> None:
    out = _run_tool(monkeypatch, tmp_path, ["yes"], start_climate="defrost")
    assert _FakeToolAPI.commands == []
    assert not (out / "start_climate.json").exists()


def test_tool_start_sends_and_stops(monkeypatch, tmp_path) -> None:
    out = _run_tool(monkeypatch, tmp_path, ["START", "engine on, front defrost lit", "STOP"], start_climate="saved")
    assert _FakeToolAPI.commands == [
        ("start", {"extend": False, "vehicleControl": APP_DEFROST_CONTROL}),
        ("refresh", None),
        ("stop", None),
    ]
    text = (out / "start_climate.json").read_text()
    record = json.loads(text)
    assert record["request"]["vehicleControl"] == APP_DEFROST_CONTROL
    assert record["observed"] == "engine on, front defrost lit"
    assert record["status_after"]["cabin_temperature"] == 21
    assert "9876" not in text and "1HGCM82633A004352" not in text


def test_tool_plain_start_and_extra_fields(monkeypatch, tmp_path) -> None:
    _run_tool(monkeypatch, tmp_path, ["START", "", "no"], start_climate="plain")
    assert _FakeToolAPI.commands[0] == ("start", {"extend": False})
    extra = {"vehicleControl": {"acSetting": {"acTempVal": "05"}}}
    _run_tool(monkeypatch, tmp_path, ["START", "", "no"], start_climate="defrost", start_extra=extra)
    assert _FakeToolAPI.commands[0] == ("start", {"extend": False, "vehicleControl": {
        "acSetting": {"acDefSetting": "autoOn", "acTempVal": "05"},
        "acTemporarySetting": {"acDefSetting": "manualOn"},
    }})


def test_tool_offers_plain_start_as_control_after_a_refusal(monkeypatch, tmp_path) -> None:
    out = _run_tool(monkeypatch, tmp_path, ["START", "START", "engine on", "STOP"], start_climate="72",
                    refuse_climate=True)
    assert [command for command, _ in _FakeToolAPI.commands] == ["start", "start", "refresh", "stop"]
    assert _FakeToolAPI.commands[1] == ("start", {"extend": False})
    record = json.loads((out / "start_climate.json").read_text())
    assert "HTTP 500" in record["error"]
    assert record["control_result"]["request_id"] == "req-1"
