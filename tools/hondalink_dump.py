#!/usr/bin/env python3
"""Dump what the HondaLink cloud API returns for your car.

Reuses the ha-hondalink integration's own api.py, so the login and requests are
exactly what Home Assistant sends. Read-only by default: nothing is sent to the
car unless you pass --wake or --start-climate.

Writes, into ./hondalink_dump_<timestamp>/ :
  vehicles.json         GET /REST/NGT/MyVehicle/1.0           (vehicle list, may include feature flags)
  vehicle.json          GET /REST/NGT/MyVehicle/1.0/{VIN}     (single vehicle record)
  profile.json          GET /REST/NGT/myProfile/1.0/{VIN}     (account / subscription profile)
  sync_settings.json    GET ws.hondaweb.com/REST/NGT/Settings/SyncSettings/1.0/user/profile
                        (settings the app syncs, including its saved remote-climate choices)
  climate_settings.json those saved climate choices, and the climate block a remote start
                        would carry for them
  dashboard.json        POST /REST/NGT/CIG/dbd/latest/{VIN}   (the status data the integration polls)
  wake_result.json      only with --wake (POST /REST/NGT/CIG/dbd/async, asks the car for fresh data)
  probe_<name>.json     only with --probe: read-only endpoints found in the iOS app (see
                        docs/hondalink-protocol/) that answered with JSON
  probe_summary.txt     only with --probe: every URL tried, its HTTP status and what came back
  probe_errors.json     only with --probe: Honda's JSON error replies, which often say what was wrong
  start_climate.json    only with --start-climate: the request sent, Honda's replies, and the
                        car's status afterwards
  fields.txt            every leaf field from all of the above as "path = value", easiest to read
  errors.txt            any request that failed, with the error message

Everything is redacted (tokens, PIN, password, email, VIN, GPS, names, addresses,
phone numbers) unless you pass --raw, which ALSO writes unredacted copies to a
raw/ subfolder. Never share the raw/ folder.

Usage:
  pip install aiohttp
  python3 hondalink_dump.py --integration-dir /path/to/hondalink
  python3 hondalink_dump.py --integration-dir ... --vin <VIN> --wake
  python3 hondalink_dump.py --integration-dir ... --probe
  python3 hondalink_dump.py --integration-dir ... --start-climate defrost
  python3 hondalink_dump.py --integration-dir ... --start-climate 72 --defrost-front on

--probe tries 18 read-only endpoints on Honda's servers, using the HTTP method the app
uses where it is known. None of them is a vehicle command: nothing reaches the car.
Paths and methods come from the app, but the host and VIN placement are guesses, so
expect most attempts to fail; probe_summary.txt shows which ones didn't.

--start-climate REMOTE-STARTS THE CAR with a climate block attached, in the form the
HondaLink app sends (docs/hondalink-protocol/android-crosscheck.md section M). Only run it
with the car outdoors and where you can see it. Give it:
  defrost   the block the app sends with the defroster chosen
  67|72|77  the block the app sends with that temperature (F) chosen
  saved     whatever is saved from the app's Climate Settings
  plain     no climate block: the start the integration has always sent
--start-extra JSON merges more fields into the request, to try other body shapes without
editing code, e.g.
  --start-climate plain --start-extra '{"vehicleControl": {"acSetting": {"acTempVal": "05"}}}'
You are shown the exact request and must type START to send it. If Honda refuses it, the
script offers to send the plain start as a control, and it offers to stop the engine
afterwards. It needs the remote PIN (HONDALINK_PIN, or you are prompted).

Credentials come from HONDALINK_EMAIL / HONDALINK_PASSWORD (and optionally
HONDALINK_VIN), or you are prompted. The PIN is not needed for the reads.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib
import json
import os
import re
import sys
import types
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

DEFAULT_DIRS = [
    "/config/custom_components/hondalink",  # HA terminal / container
    "/homeassistant/custom_components/hondalink",  # Advanced SSH app on HAOS
    "./custom_components/hondalink",
    "./ha-hondalink",  # a git clone of the repo
    ".",
]

# Keys whose values are replaced outright, matched case-insensitively.
# Substring match (safe because these words don't hide inside useful field names):
SECRET_KEY_PARTS = (
    "token", "password", "passwd", "secret", "client_reg_key", "clientregkey",
    "authorization", "cookie", "session", "hidas", "ident", "userid", "user_id",
    "email", "phone", "mobile", "address", "street", "postal", "zipcode",
    "firstname", "lastname", "first_name", "last_name", "fullname", "nickname",
    "latitude", "longitude", "device", "plate", "license",
)
# Exact match only ("city" would otherwise hit velocity/batteryCapacity, "pin" hits spinner):
SECRET_KEYS_EXACT = {"pin", "vin", "lat", "lon", "lng", "city", "zip", "state_code", "name", "spguid"}
# Seen in the enrollment reply and vehicle record; caught by neither rule above.
SECRET_KEYS_EXACT |= {
    "homecity", "mailingcity", "homestate", "mailingstate", "vinheader", "ahmglobalcode",
    "agreementid", "correlationid", "solddate", "associationdate",
}
# Keys that contain a secret-looking part but are safe and useful to keep.
SAFE_KEYS = {
    "plugstatus", "lightstate", "openstate", "closestate", "lockstate",
    "pressuredata", "deviceid_type", "commandstatus",
}
# Secret-looking keys that hold a list/dict worth looking inside (each field in it is still
# redacted by the rules above). The whole myProfile reply sits under "devices".
CONTAINER_KEYS = {"devices"}
# Not \b: the settings sync has keys like "NPS_RC_EXECUTED_<VIN>", and "_" is a word character.
VIN_RE = re.compile(r"(?<![A-Za-z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Za-z0-9])")
# Account profile IDs that appear inside the settings sync's status messages.
PROFILE_ID_RE = re.compile(r"\b[A-Z]{3}\d{3}[0-9a-f]{32}\b")

class Probe(NamedTuple):
    name: str
    path: str  # appended to each of PROBE_HOSTS, unless it is a full URL
    method: str = "GET"  # from the app's operation class (COMGet*/COMPost*), deep-dive.md section 7
    post_on_405: bool = False  # method unknown: also try POST if the server refuses GET
    vin_style: str = "path"  # how to add the VIN: "path" (/VIN) or "query" (?vin=VIN)
    query: str = ""  # extra query string, e.g. a parameter an error reply said was required


# Read-only endpoints from the HondaLink iOS app (docs/hondalink-protocol/) that could give
# this integration something new. The app's tables don't pin down which host each path is
# on, so each is tried on both Honda web-service hosts, with and without the VIN.
PROBE_HOSTS = ("https://wsc.hondaweb.com", "https://ws.hondaweb.com")
PROBES: tuple[Probe, ...] = (
    Probe("feature_list", "/REST/NGT/FeatureList/1.0/"),
    Probe("feature_list_cbo", "/REST/NGT/CBO/FeatureList/1.0/"),
    Probe("vehicle_entitlement", "/REST/NGT/Enrollment/VehicleEntitlement/2.0", "POST"),
    Probe("customer_entitlement", "/REST/NGT/EnrollmentCustomerEntitlement/1.0", "POST"),
    Probe("telematics_profile", "/REST/NGT/VTP/telematics/profile"),
    Probe("account_status", "/REST/NGT/CSAccountStatus/1.0"),
    Probe("trip_scores", "/REST/NGT/CVP/TripScores/1.0"),
    Probe("driving_scores", "/REST/NGT/CVP/DrivingScores/1.0"),
    # Its error reply: "Required BigDecimal parameter 'maxCount' is not present".
    Probe("activity_log", "/REST/NGT/ActivityLog/1.0/", query="maxCount=20"),
    # Inbox: Maintenance Minder, warning and recall messages (deep-dive.md section 5).
    Probe("vehicle_messages", "/REST/NGT/VehicleMessage/Details/1.0/"),
    Probe("vehicle_messages_query", "/REST/NGT/VehicleMessage/Details/1.0/", vin_style="query"),
    Probe("vehicle_location", "/REST/NGT/VDS/vehicle/location", post_on_405=True),
    Probe("fota_campaigns", "/REST/NGT/FOTAvehiclesGetVincampaigns/1.0"),
    Probe("stolen_vehicle_status", "/REST/NGT/SVLGetStatus/1.0"),
    Probe("parts_health", "/REST/SPT/VehiclePartsHealth/1.0", post_on_405=True),
    # Its error reply without a VIN: "Invalid VIN format."
    Probe("parts_health_query", "/REST/SPT/VehiclePartsHealth/1.0", vin_style="query"),
    # Not "garage_devices": a name containing "device" gets redacted in probe_errors.json.
    Probe("garage", "/REST/GDO/devices/1.0"),
    # Honda's public US recall lookup (service maintenanceRecallUs); no login needed.
    # GET ?vin= returns recalls_response.Response.Recall (a POST gets no recall data).
    Probe("recalls", "https://content.services.honda.com/recalls/GetRecalls.ashx", vin_style="query"),
)
# Probes whose "name" fields are feature/campaign names, not people, so worth keeping.
PROBES_KEEP_NAME = {"feature_list", "feature_list_cbo", "vehicle_entitlement", "fota_campaigns", "telematics_profile"}


def load_api(integration_dir: Path):
    """Import const.py and api.py as a package WITHOUT running __init__.py.

    __init__.py imports homeassistant, which isn't installed on a laptop; api.py
    and const.py only need aiohttp.
    """
    for name in ("api.py", "const.py"):
        if not (integration_dir / name).is_file():
            raise SystemExit(f"{integration_dir} has no {name}. Point --integration-dir at the hondalink folder.")
    pkg = types.ModuleType("hondalink_dump_pkg")
    pkg.__path__ = [str(integration_dir)]
    sys.modules["hondalink_dump_pkg"] = pkg
    return importlib.import_module("hondalink_dump_pkg.api")


def load_util():
    """The integration's util.py (no Home Assistant imports); call after load_api."""
    return importlib.import_module("hondalink_dump_pkg.util")


def find_integration_dir(arg: str | None) -> Path:
    candidates = [arg] if arg else DEFAULT_DIRS
    for cand in candidates:
        path = Path(cand).expanduser().resolve()
        if (path / "api.py").is_file() and (path / "const.py").is_file():
            return path
    raise SystemExit(
        "Couldn't find the integration's api.py. Pass --integration-dir pointing at the folder "
        "that contains api.py and const.py (e.g. /config/custom_components/hondalink, or a git "
        "clone of https://github.com/daviddelahoz/ha-hondalink)."
    )


def is_secret_key(key: str) -> bool:
    k = key.lower().replace("-", "").replace(".", "")
    if k in SAFE_KEYS:
        return False
    return k in SECRET_KEYS_EXACT or any(part in k for part in SECRET_KEY_PARTS)


def scrub(text: str, vins: set[str]) -> str:
    for vin in vins:
        text = text.replace(vin, "VIN-REDACTED")
    return PROFILE_ID_RE.sub("PROFILE-ID-REDACTED", VIN_RE.sub("VIN-REDACTED", text))


def redact(value: Any, vins: set[str], keep: frozenset[str] = frozenset()) -> Any:
    if isinstance(value, dict):
        return {scrub(str(k), vins): (
                    "**REDACTED**" if is_secret_key(str(k)) and str(k) not in keep and v not in (None, "", [], {})
                    and not (str(k).lower() in CONTAINER_KEYS and isinstance(v, (dict, list)))
                    else redact(v, vins, keep))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, vins, keep) for v in value]
    if isinstance(value, str):
        return scrub(value, vins)
    return value


def leaf_lines(value: Any, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        if not value:
            return [f"{prefix} = {{}}"]
        lines: list[str] = []
        for k, v in value.items():
            lines += leaf_lines(v, f"{prefix}.{k}" if prefix else str(k))
        return lines
    if isinstance(value, list):
        if not value:
            return [f"{prefix} = []"]
        lines = []
        for i, v in enumerate(value):
            lines += leaf_lines(v, f"{prefix}[{i}]")
        return lines
    return [f"{prefix} = {json.dumps(value, ensure_ascii=False)}"]


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")


def _body_summary(payload: Any) -> str:
    if isinstance(payload, dict):
        status = payload.get("status") or (payload.get("Header") or {}).get("ErrorCode")
        keys = ", ".join(list(payload)[:8])
        return f"status={status!r} keys=[{keys}]" if status is not None else f"keys=[{keys}]"
    if isinstance(payload, list):
        return f"list of {len(payload)}"
    return type(payload).__name__


def _failed(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    status = str(payload.get("status") or "").lower()
    return status in ("failure", "failed", "error") or bool((payload.get("Header") or {}).get("ErrorCode"))


async def probe_endpoints(session, api, vin: str | None, hosts=None, probes=PROBES, delay: float = 0.3,
                          error_replies: dict[str, list[Any]] | None = None):
    """Try each read-only endpoint; return ({name: response}, [summary lines], {name: raw text}).

    If error_replies is given, JSON replies with an HTTP error status are collected in it,
    since their error descriptions often say what the endpoint expected.
    """
    hosts = hosts or PROBE_HOSTS
    found: dict[str, Any] = {}
    raw_text: dict[str, str] = {}
    lines: list[str] = []
    for name, path, first_method, post_on_405, vin_style, query in probes:
        hit = False
        for host in ("",) if path.startswith("http") else hosts:
            for suffix in ("", vin) if vin else ("",):
                if not suffix:
                    url = host + path
                elif vin_style == "query":
                    url = f"{host}{path}?vin={suffix}"
                else:
                    url = f"{host}{path.rstrip('/')}/{suffix}"
                if query:
                    url += ("&" if "?" in url else "?") + query
                shown = url.replace(vin, "{VIN}") if vin else url
                methods = [first_method]
                while methods:
                    method = methods.pop(0)
                    await api.async_ensure_login()
                    body = {"device": vin, "vin": vin} if method == "POST" and vin else None
                    try:
                        async with session.request(method, url, headers=api._api_headers(), json=body) as resp:
                            text = await resp.text()
                            ctype = resp.headers.get("Content-Type", "")
                            status = resp.status
                    except Exception as err:  # noqa: BLE001  (DNS errors etc. are just a miss)
                        lines.append(f"{name:22} {method:4} {shown} -> {type(err).__name__}")
                        continue
                    try:
                        payload: Any = json.loads(text) if text else None
                    except json.JSONDecodeError:
                        payload = None
                    if payload is not None:
                        info = _body_summary(payload)
                    else:
                        title = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
                        info = f"{ctype or 'no content-type'}, {len(text)} chars"
                        if title:
                            info += f", title={title.group(1).strip()[:60]!r}"
                    lines.append(f"{name:22} {method:4} {shown} -> HTTP {status} {info}")
                    if status == 405 and method == "GET" and post_on_405:
                        methods.append("POST")
                    if status >= 400 and payload is not None and error_replies is not None:
                        error_replies.setdefault(name, []).append(
                            {"method": method, "url": shown, "http_status": status, "response": payload}
                        )
                    if 200 <= status < 300 and text:
                        record = {"method": method, "url": shown, "http_status": status, "content_type": ctype}
                        if payload is not None:
                            record["response"] = payload
                        else:
                            # Non-JSON (probably a web page): keep it out of the shareable dump.
                            record["response_chars"] = len(text)
                            raw_text[name] = text
                        if name not in found or (_failed(found[name].get("response")) and not _failed(payload)):
                            found[name] = record
                        hit = hit or (payload is not None and not _failed(payload))
                    await asyncio.sleep(delay)
                if hit:
                    break
            if hit:
                break
    return found, lines, raw_text


def climate_summary(util_mod, sync_settings: Any) -> dict[str, Any]:
    """The climate choices saved from the app and the block a remote start would carry."""
    saved = util_mod.saved_climate_settings(sync_settings)
    mode = util_mod.saved_climate_mode(saved)
    return {
        "saved_settings": saved,
        "mode": mode,
        "start_would_send_vehicleControl": util_mod.remote_start_vehicle_control(mode),
        "note": "saved_settings is what the HondaLink app's Climate Settings screen last saved.",
    }


def start_vehicle_control(choice: str, util_mod, summary: dict[str, Any] | None) -> dict[str, Any] | None:
    """The vehicleControl block for --start-climate; exits with a message if it can't be built."""
    choice = choice.strip().lower()
    if choice == "plain":
        return None
    if choice == "saved":
        if not summary:
            raise SystemExit("--start-climate saved: the saved climate settings could not be read.")
        return summary["start_would_send_vehicleControl"]
    try:
        return util_mod.remote_start_vehicle_control(choice)
    except ValueError:
        raise SystemExit("--start-climate takes defrost, 67, 72, 77, saved or plain.") from None


def merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """base with extra laid over it; dicts are merged key by key."""
    out = dict(base)
    for key, value in extra.items():
        out[key] = merge(out[key], value) if isinstance(out.get(key), dict) and isinstance(value, dict) else value
    return out


def start_request(vehicle_control: dict[str, Any] | None, extra: dict[str, Any] | None) -> dict[str, Any]:
    """The engine-start body without the VIN and PIN."""
    request: dict[str, Any] = {"extend": False}
    if vehicle_control:
        request["vehicleControl"] = vehicle_control
    return merge(request, extra or {})


def send_start(api, request: dict[str, Any]):
    return api._async_cig_command("eng", "srt", {"device": api._vin(), "pin": api._pin(), **request})


def confirm(question: str, word: str) -> bool:
    try:
        return input(f"{question}\nType {word} to send it, anything else to skip: ").strip() == word
    except EOFError:
        return False


def status_snapshot(util_mod, dashboard: Any) -> dict[str, Any]:
    body = util_mod.status_body(dashboard) if isinstance(dashboard, dict) else {}
    return {
        "remote_engine_status": util_mod.remote_engine_status(body),
        "remote_start": util_mod.remote_start_attributes(body),
        "cabin_temperature": util_mod.cabin_temperature(body),
        "cabin_temperature_unit": util_mod.cabin_temperature_unit(body),
        "evStatus": util_mod.get_path(body, "evStatus"),
    }


async def send_command(api, util_mod, label: str, request: dict[str, Any], send, *, offer_stop: bool,
                       control=None) -> dict[str, Any]:
    """Send one car command and record the reply, the car's status afterwards, and the stop.

    control, if given, is a plain command offered when the first one fails, to tell a
    refused request body from a car that can't be started right now.
    """
    record: dict[str, Any] = {"request": request}

    async def refreshed_status() -> dict[str, Any]:
        try:
            await api.async_request_dashboard_update(api.vin)
        except Exception as err:  # noqa: BLE001  (a failed refresh still leaves the last status)
            print(f"  refresh failed ({type(err).__name__}); reading the last status instead")
        return status_snapshot(util_mod, await api.async_get_dashboard_latest(api.vin))

    print(f"Sending {label}; waiting for the car to answer (up to 75 seconds)…")
    try:
        result = await send()
        record["result"] = {"request_id": result.request_id, "response": result.response}
        print(f"  ok    {label}")
    except Exception as err:  # noqa: BLE001  (the error text is the finding)
        record["error"] = f"{type(err).__name__}: {err}"
        print(f"  FAIL  {label}: {type(err).__name__}: {err}")
        if control and confirm(
            "Send the plain start (no climate block) as a control? If it works, Honda refused the "
            "climate block; if it fails too, the car can't be remote-started right now.", "START"
        ):
            try:
                result = await control()
                record["control_result"] = {"request_id": result.request_id, "response": result.response}
                print("  ok    plain start (control)")
            except Exception as err2:  # noqa: BLE001
                record["control_error"] = f"{type(err2).__name__}: {err2}"
                print(f"  FAIL  plain start (control): {type(err2).__name__}: {err2}")
    try:
        record["status_after"] = await refreshed_status()
        print(f"  status after: {json.dumps(record['status_after'].get('remote_engine_status'))}")
    except Exception as err:  # noqa: BLE001
        record["status_after_error"] = f"{type(err).__name__}: {err}"
    if offer_stop:
        try:
            observed = input(
                "Look at the car now. What do you see (engine on? defroster on? temperature shown)?\n"
                "Type a note for the record, then Enter: "
            ).strip()
        except EOFError:
            observed = ""
        record["observed"] = observed
        if confirm("Stop the engine now?", "STOP"):
            try:
                stopped = await api.async_stop_engine()
                record["stop_result"] = {"request_id": stopped.request_id, "response": stopped.response}
                print("  ok    engine stop")
            except Exception as err:  # noqa: BLE001
                record["stop_error"] = f"{type(err).__name__}: {err}"
                print(f"  FAIL  engine stop: {type(err).__name__}: {err}  (stop it from the HondaLink app or the key)")
        else:
            print("  Engine left running; it stops by itself after the remote-start time.")
    return record


async def run(args: argparse.Namespace) -> int:
    integration_dir = find_integration_dir(args.integration_dir)
    api_mod = load_api(integration_dir)
    util_mod = load_util()
    sends_command = bool(args.start_climate)
    if sends_command and not hasattr(util_mod, "remote_start_vehicle_control"):
        raise SystemExit(f"{integration_dir} is an older copy of the integration without climate support.")
    import aiohttp  # noqa: PLC0415  (imported late so a missing integration dir errors first)

    email = os.environ.get("HONDALINK_EMAIL") or input("HondaLink email: ").strip()
    password = os.environ.get("HONDALINK_PASSWORD") or getpass.getpass("HondaLink password: ")
    vin = args.vin or os.environ.get("HONDALINK_VIN") or None
    pin = None
    if sends_command:
        pin = os.environ.get("HONDALINK_PIN") or getpass.getpass("HondaLink remote PIN: ").strip()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out or f"hondalink_dump_{stamp}")
    out.mkdir(parents=True, exist_ok=True)
    raw_dir = out / "raw"
    if args.raw:
        raw_dir.mkdir(exist_ok=True)

    results: dict[str, Any] = {}
    errors: list[str] = []
    probe_lines: list[str] = []
    probe_raw_text: dict[str, str] = {}

    async with aiohttp.ClientSession() as session:
        api = api_mod.HondaLinkAPI(session, email=email, password=password, pin=pin, vin=vin)

        async def grab(name: str, coro_fn):
            try:
                results[name] = await coro_fn()
                print(f"  ok    {name}")
            except Exception as err:  # noqa: BLE001  (we want every failure recorded, not raised)
                errors.append(f"{name}: {type(err).__name__}: {err}")
                print(f"  FAIL  {name}: {type(err).__name__}")

        print("Logging in…")
        try:
            await api.async_login()
        except Exception as err:  # noqa: BLE001
            print(f"Login failed: {type(err).__name__}: {redact(str(err), set())}")
            return 1

        await grab("vehicles", api.async_get_vehicles)
        if not vin:
            vehicles = results.get("vehicles") or []
            vins = [v.get("vin") or v.get("VIN") for v in vehicles if isinstance(v, dict)]
            vins = [v for v in vins if v]
            if len(vins) == 1:
                vin = vins[0]
            elif len(vins) > 1:
                print(f"Found {len(vins)} vehicles; using the first. Pass --vin to choose another.")
                vin = vins[0]
            else:
                print("No VIN discovered. Pass --vin (or HONDALINK_VIN) and run again.")
        if vin:
            api.vin = vin
            await grab("vehicle", lambda: api.async_get_vehicle_by_vin(vin))
            await grab("profile", lambda: api.async_get_profile(vin))
            summary = None
            if hasattr(api, "async_get_sync_settings") and hasattr(util_mod, "saved_climate_settings"):
                await grab("sync_settings", api.async_get_sync_settings)
                if "sync_settings" in results:
                    summary = climate_summary(util_mod, results["sync_settings"])
                    results["climate_settings"] = summary
                    print(f"  saved climate settings: {json.dumps(summary['saved_settings'])}")
            if args.start_climate:
                try:
                    request = start_request(start_vehicle_control(args.start_climate, util_mod, summary), args.start_extra)
                    problem = None
                except SystemExit as err:  # keep going so the read-only dump is still written
                    request, problem = None, str(err)
                plain = request == start_request(None, None)
                if problem:
                    print(f"\nNot sending a remote start: {problem}")
                    results["start_climate"] = {"not_sent": problem}
                else:
                    print("\nRemote start with this request (plus VIN and PIN):")
                    print("  " + json.dumps(request))
                    if confirm("This STARTS THE CAR. Only continue if it is outdoors and you can see it.", "START"):
                        results["start_climate"] = await send_command(
                            api, util_mod, "plain remote start" if plain else "remote start with climate", request,
                            lambda: send_start(api, request), offer_stop=True,
                            control=None if plain else lambda: send_start(api, start_request(None, None)),
                        )
                    else:
                        print("  Skipped.")
            if args.wake:
                print("Asking the car for fresh data (--wake), this can take up to a minute…")

                async def wake():
                    res = await api.async_request_dashboard_update(vin)
                    return {"request_id": res.request_id, "response": res.response}

                await grab("wake_result", wake)
            await grab("dashboard", lambda: api.async_get_dashboard_latest(vin))
        if args.probe:
            print("Probing read-only endpoints from the app (--probe), about a minute…")
            try:
                error_replies: dict[str, list[Any]] = {}
                found, probe_lines, probe_raw_text = await probe_endpoints(
                    session, api, vin, error_replies=error_replies
                )
            except Exception as err:  # noqa: BLE001
                errors.append(f"probe: {type(err).__name__}: {err}")
                print(f"  FAIL  probe: {type(err).__name__}")
            else:
                for name, record in found.items():
                    results[f"probe_{name}"] = record
                    print(f"  ok    probe_{name}  (HTTP {record['http_status']} {record['method']} {record['url']})")
                if error_replies:
                    results["probe_errors"] = error_replies
                print(f"  {len(found)} of {len(PROBES)} probed endpoints answered; see probe_summary.txt")

    vins_to_hide = {vin} if vin else set()
    all_lines: list[str] = []
    for name, data in results.items():
        keep = frozenset({"name"}) if name.removeprefix("probe_") in PROBES_KEEP_NAME and name.startswith("probe_") else frozenset()
        safe = redact(data, vins_to_hide, keep)
        write_json(out / f"{name}.json", safe)
        all_lines.append(f"# ===== {name} =====")
        all_lines += leaf_lines(safe)
        all_lines.append("")
        if args.raw:
            write_json(raw_dir / f"{name}.json", data)
    (out / "fields.txt").write_text("\n".join(all_lines) + "\n", encoding="utf-8")
    if args.probe:
        (out / "probe_summary.txt").write_text(
            "\n".join(str(redact(line, vins_to_hide)) for line in probe_lines) + "\n", encoding="utf-8"
        )
        if args.raw:
            for name, text in probe_raw_text.items():
                (raw_dir / f"probe_{name}.html").write_text(text, encoding="utf-8")
    if errors:
        (out / "errors.txt").write_text("\n".join(str(redact(e, vins_to_hide)) for e in errors) + "\n", encoding="utf-8")

    print(f"\nSaved to {out.resolve()}")
    print("  Start with fields.txt. Values of null, \"\" or \"--\" are fields Honda knows about but isn't filling for your car.")
    if args.raw:
        print("  raw/ holds UNREDACTED copies (VIN, GPS, account details). Don't share it.")
    return 0 if results else 1


def json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as err:
        raise argparse.ArgumentTypeError(f"not valid JSON: {err}") from None
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("must be a JSON object, e.g. '{\"isChanged\": true}'")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Dump HondaLink API responses (read-only by default).")
    parser.add_argument("--integration-dir", help="Folder containing the integration's api.py and const.py")
    parser.add_argument("--vin", help="VIN to query (otherwise discovered from your account)")
    parser.add_argument("--wake", action="store_true",
                        help="Ask the car to report fresh data first (same as the Refresh button; pings the car)")
    parser.add_argument("--probe", action="store_true",
                        help="Also try read-only endpoints found in the iOS app (no vehicle commands)")
    parser.add_argument("--start-climate", metavar="defrost|67|72|77|saved|plain",
                        help="STARTS THE CAR, with the climate block for that choice (plain: none). "
                             "Asks before sending")
    parser.add_argument("--start-extra", metavar="JSON", type=json_object,
                        help="With --start-climate: a JSON object merged into the start request")
    parser.add_argument("--raw", action="store_true", help="Also write unredacted copies to raw/")
    parser.add_argument("--out", help="Output folder (default: hondalink_dump_<timestamp>)")
    args = parser.parse_args()
    if args.start_extra and not args.start_climate:
        parser.error("--start-extra only applies with --start-climate")
    if args.start_climate and args.start_climate.strip().lower() not in ("defrost", "67", "72", "77", "saved", "plain"):
        parser.error("--start-climate takes defrost, 67, 72, 77, saved or plain")
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
