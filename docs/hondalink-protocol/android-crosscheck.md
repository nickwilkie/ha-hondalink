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

## J. Engine-start body, complete — the last open points

All from `ak/q` (the 2G-X CIG command builder) and the Gson models it feeds,
`smali_classes4/ak/q.smali` lines cited at verdict time.

### J.1 Full request model (`CIGPostRequest`)

`POST /REST/NGT/CIG/{eng}/async/{command}` with the CIG verb in the path
(`srt` start, `sop` stop — also `alk/dulk/lgt/hrn/dbd`). Body:

```json
{
  "command": "Start",            // human name; also "Settings". Verb code is the URL path param.
  "commandStatus": null,
  "device": "<VIN>",
  "extend": false,               // Boolean: allow run-time extension
  "filters": [],                 // String[]
  "isChanged": false,            // Boolean
  "pin": "<pin>",
  "vehicleControl": {
    "acSetting":          { ...cigmodel.AcSetting... },
    "acTemporarySetting": { ...cigmodel.AcTemporarySetting... }
  }
}
```

### J.2 What the app actually puts in `acSetting` at start time (ak/q)

| wire field | source (verified smali) | value |
|---|---|---|
| `acTempVal` | saved setting via `ak/d.i(int,String)` | **temperature DOES ride the start** |
| `acTempUnit` | `equalsIgnoreCase` on saved unit | `"F"` or `"C"` |
| `acDefFSetting` | `getAcDefrosterFront().toLowerCase(Locale)` | saved value lowercased; **not** `Y`/`N` — see J.3 |
| `acDefRSetting` | same, rear | as above |
| `seatHeaterDrSetting` | `getAcDriverHeated()` | saved value |
| `seatHeaterAsSetting` | `getAcPassengerHeater()` | saved value |
| `strHeaterSetting` | `getAcHeaterSteering().toLowerCase()` | saved value lowercased |

Unset fields are set to `null` (`const/4 0x0`) and Gson omits them. The
def/seat block is behind per-VIN feature checks (`jk/g.t(vin, 2)` etc.).

**Correction to §I:** the saved defroster strings are **not** `Y`/`N`. The app
initializes settings with the literal `"Off"` (`tm/j`), so the engine-start
copy yields lowercase wire values — `"off"`, and `"on"` when enabled
(the enabled literal still needs one live CPC read to confirm).

### J.3 Where saved climate settings live: server-side CPC

The settings the start builder reads come from the **CPC (customer/profile)
API**, not phone-only storage:

- Read: `GET /REST/NGT/myProfile/1.0/{VIN}` → profile attribute map. The
  mapper (`ek/d`) reads literal keys: `temperatureValue`, `acDefrosterFront`,
  `acDefrosterRear`, `acDriverHeated`, `acPassengerHeater`, `acHeaterSteering`.
- Write: `POST /REST/NGT/usersProfile/1.0/` (log string "CPC Put Api Called").
- The app mirrors these into local Realm (`ClimateSettingInfoModel` via
  `gk/d`) purely as a cache; the authoritative copy is the server profile.
- iOS agrees: `api-map.txt` has `myProfile` GET and `userProfiles` POST/modify.

So HA can read the user's saved climate settings itself and build the same
`acSetting` block — a remote start from HA **can** defrost, pre-heat seats etc.
without the phone in the loop, by copying what the app does: GET myProfile →
lowercase the defroster/heater values → put them in `vehicleControl.acSetting`
on the `srt` call.

### J.4 Consequence for the HA integration

- Omitting `vehicleControl` from an HA start is *not* equivalent to the app's
  start: the app always sends the saved `acSetting` (feature-gated). HA should
  add an optional `acSetting` block (temperature + defroster + heaters), default
  = current behavior (unchanged) or optionally mirror CPC.
- `ClimateAsync/temperature` only stores the *setting* (and starts the
  immediate climate command); it does not alter what a *later* start carries —
  that comes from CPC at command-build time.

### J.5 Still needs one live probe each

- Exact CPC value strings when defroster enabled (`"On"`? `"on"`?).
- Whether `usersProfile` PUT is per-key or whole-profile replace.
- One real `srt` body from the app (Charles/Fiddler) to confirm optional-field
  omission matches Gson behavior.

## K. `acTempVal` — the exact math (ak/d fully decoded)

`ak/q` sets the temperature as:

```java
String saved = settings.getTemperatureValue();   // CPC value, e.g. "22"
String unit  = settings.getUnit();               // "F" or "C"
if (saved != null && !saved.isEmpty())
    acTempVal = String.valueOf( ak/d.i(Integer.parseInt(saved), unit) );
```

`ak/d.i` branches on the **vehicle's country code** (from the VIN's market):

- **US vehicles** (`ak/d.d`): if `unit` contains `C`/`c` → value passed through
  unchanged. Otherwise the value is converted **°C→°F**:
  `round( x * 9/5 + 32 )`.
- **CA vehicles** (`ak/d.c`): if `unit` contains `F`/`f` → converted
  **°F→°C**: `round( (x − 32) * 5/9 )`. Otherwise passed through.

Consequences for a US car (Nick's):

1. `acTempVal` is always a plain decimal integer string — never `"03"`-style
   level codes (those are legacy-preset only) and never a float.
2. For the US path to produce sane wire numbers in the displayed unit, the
   **CPC-stored `temperatureValue` must be Celsius** when `unit` is `"F"`:
   stored `"22"` + `unit "F"` → wire `acTempVal="72"`, `acTempUnit="F"`.
   (If CPC instead stored `"72"`, the app would send `"166"` — nonsense — so
   either CPC stores Celsius, or the US path is never fed an F-unit value;
   `profile.json` decides which, see below.)
3. CA vehicles always put Celsius on the wire.

Supporting evidence that CPC values are bare numeric strings: the profile
mapper parses `temperatureValue` with `Double.parseDouble` for min-value
validation, and stores settings as `VmSDetail.getsPValues()` — a
`Map<String,String>` keyed by `temperatureValue`, `geoFenceRadius`, etc.

### K.1 Defroster enabled-string — the honest state

Every defroster literal in the APK is `"Off"` (default init in `tm/j`, `vm/a`)
or a case-insensitive comparison of the stored server string against it. The
*enabled* literal is **never present as an app constant** — the app treats
whatever the server returns as the value and only `.toLowerCase()`s it for
engine start. So the enabled string is a server property, most likely `"On"`
(mirroring `"Off"`), which would put `"on"` on the start wire. One read of
`profile.json` settles it.

### K.2 Clarifying §J.4 on ClimateAsync (review point)

Two separate things, now stated precisely:

- `POST /REST/CIG/ClimateAsync/1.0/temperature` = an **immediate** climate
  command (precondition now); its body carries the temp the user picked.
- `POST /REST/NGT/usersProfile/1.0/` = **stores** profile settings
  (`temperatureValue`, `acDefrosterFront`, …) that the *next engine start*
  embeds via the builder in §J.2.

Whether the target-temperature screen's Update button also writes CPC is not
pinned in smali. It does not matter for HA: an HA start that carries its own
`vehicleControl.acSetting` needs neither call.
