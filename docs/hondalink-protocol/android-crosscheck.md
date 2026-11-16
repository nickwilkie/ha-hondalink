# Android 5.5.4 cross-check (HondaLink APK 114.6 MB)

Everything here is from the **Android** app (`hondalink-5-5-4.apk`), decompiled
with apktool 2.11 (smali) + androguard. It matters because the Android binary
keeps endpoints in `@GET/@POST` annotations and request/response fields as
plaintext class fields, so it resolves several things the iOS binary only hints
at. Each section says whether it **confirms**, **corrects**, or **adds to** the
iOS findings in `deep-dive.md`.

## A. Endpoint catalog (adds)

`android-endpoints.txt` (this folder) is the complete machine-extracted set of
173 Retrofit annotations: HTTP verb, literal path, and parameter kinds
(`path:`, `query:`, `body:`, `header:`). Notable absolute paths:

```
POST   /REST/CIG/ClimateAsync/1.0/{command}
GET    /REST/NGT/getClimateResult/1.0/{command}/{cigServiceRequestId}
GET    /REST/NGT/getClimateStatus/1.0/{vin}
POST   /REST/CIG/ClimateSetting/1.0                      (scheduled climate)
POST   /REST/NGT/CIG/{eng}/async/{command}
GET    /REST/NGT/CIG/{eng}/results/{cigServiceRequestId}
POST   /REST/NGT/CIG/dbd/async        (dashboard)
POST   /REST/NGT/CIG/dbd/latest/{device}
POST   /REST/NGT/vehiclelocaterAsync/1.0                 (car finder — sic)
GET    /REST/NGT/CIG/gefe/results/{device}
GET    /REST/NGT/CIG/spal/results/{device}
GET    /REST/NGT/CIGContactsResults/1.0/{device}
GET    /REST/NGT/VehicleMessage/Details/1.0/{VIN}
POST   /REST/NGT/VehicleMessage/Status/1.0
GET    /REST/NGT/FeatureList/1.0/{VIN}
GET    /REST/NGT/CVP/TripScores/1.0/{VIN}
GET    /REST/NGT/ActivityLog/1.0/{device}
GET    /REST/GDO/devices/1.0    POST /REST/GDO/remoteCommand_async/1.0
POST   /recalls/GetRecalls.ashx  (query+body)
GET    /VinRecall/VinRecallServiceREST.svc/{VIN}          (second recall source)
GET    /poi/private                                        (POI send, query-heavy GET)
POST   /ha/exchangeToken                                   (home-assistant token exchange!)
```

`/ha/exchangeToken` + `/REST/NGT/AuthChallenge/1.0` appear to be a first-party
token-exchange flow (possibly the official HA/cloud bridge) — worth probing.

## B. Engine command URL anatomy (confirms iOS)

`POST /REST/NGT/CIG/{eng}/async/{command}` — call sites in `ak/q.smali` pass:

- `{eng}` = `eng` (engine start/stop), with `{command}` = `srt` / `sop`
- lock/unlock uses `{eng}` = `lk` + `alk`/`dulk` (in `mb0/e.smali`,
  alongside the literal log strings `"lock post"`/`"unlock post"`)
- results: `GET /REST/NGT/CIG/{eng}/results/{cigServiceRequestId}`

This **confirms** the iOS inline-string recovery exactly (`/async/srt`,
`/async/sop`, `alk`, `dulk` fragments) — two independent decompilation
approaches, same tokens.

## C. Car finder (corrects)

Car finder is **not** `/CIG/cfl/async/{role}`. The Android app posts to its
own NGT endpoint:

    POST /REST/NGT/vehiclelocaterAsync/1.0      (Honda's own typo: "locater")

The `/cfl/1.0` substring exists but only inside an OkHttp interceptor doing
`encodedPath().contains(...)` checks (auth/header logic), not as a command
URL. **The reviewer's ask "give me the car-finder role code" dissolves: there
is no role suffix on 5.5.4.** The old `cfl` naming is legacy from earlier app
generations.

## D. Climate Settings "Update" (adds; full detail in deep-dive §11)

    POST /REST/CIG/ClimateAsync/1.0/temperature
    {"device": "<VIN>", "pin": "<optional>",
     "vehicleControl": {"acSetting":
        {"acTempVal": "67"|"72"|"77"|"", "acTempUnit": "F",
         "acDurationSetting": "", "acDefSetting": ""|"autoOn"|"autoOff"}}}

- 67/72/77 are hardcoded picker tiers; slider bounds 57–87 shown as `Lo`/`Hi`
  are UI-only.
- Response is the standard `CigRemoteCommandPostResponseBody`
  (`status` + `responseBody`) → poll
  `GET /REST/NGT/getClimateResult/1.0/temperature/{cigServiceRequestId}`.
- Current settings render from the dashboard's `EvStatus2Gx`
  (`acStatus`, `acTempVal`, `acTempUnit`, `acDurationSetting`, `acDefStatus`,
  `acDefAutoSetting`, `acDefManualSetting`, `acTotalTime`, `acTimerSettings`).
- **Correction to earlier notes:** `ClimateAsync` was filed as "OnStar-only,
  ignore" — for MY21/2G-X (the 2025 CR-V Hybrid platform, package
  `model.twogx`) it is the *proper* climate-settings setter.
- Engine-start `acSetting` wire object (`model.cigmodel.AcSetting`):
  `acTempVal`, `acTempUnit`, `acDefSetting`, `acDefFSetting`, `acDefRSetting`,
  `seatHeaterDrSetting`, `seatHeaterAsSetting`, `strHeaterSetting` (all
  strings); engine-start `CabinTemp`: `{temp: int, unit: str}`.

## E. Command result status vocabulary (corrects the correction)

Android polls for these literal statuses:

- Success: `SUCCESS` (dominant; 90+ call sites), `COMPLETED` (14)
- Failure: `FAILED` (73), plus `SERVICE_NOT_AVAILABLE`,
  `SERVICE_VERSION_UPDATE_REQUIRED`, `SERVICE_DISABLED`, `SERVICE_UPDATING`,
  `SERVICE_NOT_EXCECUTED` (sic), `SERVICE_MISSING_PERMISSION`
- Rate limits: `MAXIMUM_START_REQUESTS_EXCEEDED`, `TOO_MANY_REQUESTS`
- In flight: `PENDING`, `IN_PROGRESS`

`SERVICE_COMPLETED` exists (3 sites) but is **not** the main command-poll
success token on Android — the poller compares `SUCCESS`/`COMPLETED`. This
matches the iOS placement finding (SERVICE_COMPLETED = Send-to-Car polling
manager). For the HA integration: the repo's existing success set
(`success|completed|complete|ok`) already matches; the *missing* part is the
terminal error set — `FAILED`, `MAXIMUM_START_REQUESTS_EXCEEDED`,
`TOO_MANY_REQUESTS`, and the `SERVICE_*` family should end polling
immediately with an error instead of burning the full timeout.

## F. The disputed "not found" fields (settles it)

`lockState`, `openState`, `resTime`, `resStartCounter` are plaintext
const-strings in the **Android** DEX (4/3/6/6 occurrences) — the reviewer was
right that they are Honda's own wire names, read by the Android app. The
whole-`__text` MOVZ scan of the iOS binary (positive controls: `vin`×64,
`device`×10) still shows iOS never references them; iOS uses the DigitalTwin
names (`doorStatus`, `windowStatus`, …). Both facts stand; the earlier
"Android names" inference was correct, the "not on the wire" implication was not.

## G. Vehicle messages / FeatureList / recalls / misc (confirms, adds)

- Inbox: `GET /REST/NGT/VehicleMessage/Details/1.0/{VIN}` (VIN is a **path**
  param, not query), ack via `POST /REST/NGT/VehicleMessage/Status/1.0`.
- FeatureList: `GET /REST/NGT/FeatureList/1.0/{VIN}` (iOS: CBO variant).
- Recalls: main app uses `POST /recalls/GetRecalls.ashx` (query + JSON body);
  a legacy second source `GET /VinRecall/VinRecallServiceREST.svc/{VIN}`.
  The unauthenticated GET with `?vin=` works (live-verified) — good sensor.
- Home location: `POST /REST/NGT/setHomeLocation/1.0`,
  `removeHomeLocation`, `GET .../getHomeLocationStatus/1.0`.
- Stolen-vehicle locator (DTCU): separate module (`vehiclelocatorDtcu`, 629
  refs) — SVL address/state endpoints, distinct from car finder.

## H. Method notes

- `apktool d -r` → smali; Retrofit verbs extracted from
  `Lretrofit2/http/{GET,POST,PUT,DELETE};` annotations (`retrofit_paths.py`).
- androguard DEX field dumps confirm wire models
  (`twogx.request.temperatureSettings.*`, `cigmodel.AcSetting`,
  `dashboard.response.cigdashboard.EvStatus2Gx`).
- Obfuscation: business logic in single-letter packages (`xu/b`, `tm/j`,
  `wo/a`, `ak/q`, `mb0/e`); `tm/j` = dashboard drawer fragment holding the
  67/72/77 handlers.

## I. Review round 2 — open items resolved from the smali

### I.1 The defroster, pinned

The *ClimateAsync/1.0/temperature* body never carries a defroster value: at
every 2G-X call site (`xu/b.u()` via `wo/b`, `tm/j`) `acDefSetting` is passed
as `""`; only the scheduled variant uses `autoOn`/`autoOff`. The defroster is
expressed elsewhere, in two places depending on generation:

1. **2G-X (the 2025 CR-V Hybrid):** the defroster choice is persisted in the
   saved climate settings (`ClimateSettingCommandDataInfo.acDefrosterFront /
   acDefrosterRear`, `Y`/`N`) and mirrored into the **engine-start** body at
   command build time: `ak/q.k()` copies `getAcDefrosterFront()` into
   `acSetting.acDefFSetting` (rear the same way, `l()`), behind a per-VIN
   feature check (`jk/g.t(vin, 2)`). So on your car: set the defroster once in
   settings; it rides every remote start in `acSetting.acDefFSetting/RSetting`.
2. **Legacy CIG (pre-2GX):** the UI presets are *named levels*, not degrees.
   `ak/p.k()` maps the chosen preset to `acSetting.acTempVal`:
   `normal→"04"`, `cool→"05"`, `warm→"03"`, `defrost→""`, and when the
   preset is `defrost` it additionally sends
   `vehicleControl.acTemporarySetting.acDefSetting = "manualOn"` with
   `command = "defrost"`. This is exactly the "three tiers + defroster"
   preset stack seen on iOS — it is the legacy vocabulary, distinct from
   67/72/77 target-degrees.

### I.2 iOS vs Android field names — reconciled

Two different layers, no contradiction:

- **Wire names (authoritative): Android Gson fields** — `acTempVal`,
  `acTempUnit`, `acDefSetting`, `acDurationSetting`. Gson serializes field
  names verbatim, so these *are* the JSON keys.
- **iOS names** (`acTempValue`, `acDefSet`, `Lo`/`Hi`) came from Swift
  *reflection* metadata (`__swift5_fieldmd`) — those are Swift property names
  of iOS UI/storage models (the analogue of Android's Realm
  `ClimateSettingInfoModel`), not proven wire keys. `Lo`/`Hi` in both apps are
  slider *labels* (bounds 57–87), never sent.

When they disagree, trust Android. deep-dive §10.1's table should be read as
"iOS internal model", §11.1 as "wire format".

### I.3 Read-back claim — downgraded

The live 2025 CR-V Hybrid fixtures show `evStatus.vehicleInfo.acStatus =
"unknown"` and empty `intTemp` (even engine-running). `EvStatus2Gx`
*contains* the climate-setting fields, but this car's dashboard replies do not
currently populate them. Until a capture shows otherwise: an HA climate entity
can only remember what it last sent; it cannot read the car's stored setting
from the dashboard.

### I.4 `/ha/exchangeToken` — guess retracted

Context found: the token feeds `VmSxmCustomerIdentity`
(`exchangeTokenExpiresAtInMilliSec`) — it is a **SiriusXM/Castlabs media
identity exchange**, nothing to do with Home Assistant.

### I.5 Host for `/REST/CIG/...`

The Android Retrofit interfaces are built on `https://wsc.hondaweb.com`
(the same constant the integration already uses), and the absolute URL
`https://wsc.hondaweb.com/REST/NGT/getClimateStatus/1.0/{vin}` appears in
smali. Statically, `/REST/CIG/ClimateAsync/1.0/temperature` is the same host,
different prefix. Still untested live — one probe needed.

### I.6 Implemented: terminal failure statuses

`api.py` now stops command polling immediately on `MAXIMUM_START_REQUESTS_EXCEEDED`,
`TOO_MANY_REQUESTS`, `SERVICE_NOT_AVAILABLE`, `SERVICE_NOT_EXCECUTED` (sic),
`SERVICE_MISSING_PERMISSION`, `SERVICE_DISABLED`,
`SERVICE_VERSION_UPDATE_REQUIRED` (tests/test_api_poll.py; PR #7).
`SERVICE_UPDATING` deliberately remains in-flight.
