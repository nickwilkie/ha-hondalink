"""Polling should end immediately on known terminal failure statuses
(verified in HondaLink Android 5.5.4 smali; see
docs/hondalink-protocol/android-crosscheck.md §E)."""
from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_api_module():
    if "hondalink_under_test" not in sys.modules:
        pkg = types.ModuleType("hondalink_under_test")
        pkg.__path__ = [str(ROOT)]
        sys.modules["hondalink_under_test"] = pkg
    return importlib.import_module("hondalink_under_test.api")


api_module = _load_api_module()
HondaLinkAPI = api_module.HondaLinkAPI
HondaLinkCommandError = api_module.HondaLinkCommandError


class _StubAPI(HondaLinkAPI):
    def __init__(self, responses):
        self.session = None
        self.vin = "VIN"
        self._responses = list(responses)
        self.calls = 0

    async def _request_api(self, method, path, **kwargs):
        self.calls += 1
        return self._responses.pop(0)


def _run(coro):
    import asyncio

    return asyncio.run(coro)


@pytest.mark.parametrize(
    "status",
    [
        "MAXIMUM_START_REQUESTS_EXCEEDED",
        "TOO_MANY_REQUESTS",
        "SERVICE_NOT_AVAILABLE",
        "SERVICE_NOT_EXCECUTED",
        "SERVICE_MISSING_PERMISSION",
        "SERVICE_DISABLED",
        "SERVICE_VERSION_UPDATE_REQUIRED",
    ],
)
def test_terminal_failure_ends_polling_early(status, monkeypatch):
    # keep the fake clock advancing only on sleep so the 75 s timeout can't win
    clock = {"t": 0.0}
    monkeypatch.setattr(api_module.time, "monotonic", lambda: clock["t"])

    async def fake_sleep(interval: float) -> None:
        clock["t"] += interval

    monkeypatch.setattr(api_module.asyncio, "sleep", fake_sleep)

    api = _StubAPI(
        [
            {"status": "inProgress"},
            {"status": "inProgress"},
            {"status": status},
            {"status": "success"},  # must never be reached
        ]
    )
    with pytest.raises(HondaLinkCommandError):
        _run(api._poll_cig_result("eng", "req-1", timeout=75, poll_interval=3))
    assert api.calls == 3


def test_in_flight_statuses_keep_polling(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(api_module.time, "monotonic", lambda: clock["t"])

    async def fake_sleep(interval: float) -> None:
        clock["t"] += interval

    monkeypatch.setattr(api_module.asyncio, "sleep", fake_sleep)

    api = _StubAPI(
        [
            {"status": "IN_PROGRESS"},
            {"status": "PENDING"},
            {"status": "SERVICE_UPDATING"},  # ambiguous: must NOT end polling
            {"status": "SUCCESS"},
        ]
    )
    result = _run(api._poll_cig_result("eng", "req-1", timeout=75, poll_interval=3))
    assert result["status"] == "SUCCESS"
    assert api.calls == 4
