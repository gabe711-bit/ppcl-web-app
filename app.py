from flask import Flask, render_template, request, jsonify, Response
import random
import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # picks up a local .env file if present; no-op if missing
except ImportError:
    pass

app = Flask(__name__)

# --- SHARED LOGIN (single username/password, distributed by a supervisor) ---
# Set APP_USERNAME / APP_PASSWORD as environment variables on the host. If
# either is unset (e.g. local development), the login gate is skipped
# entirely so testing on your own machine doesn't require setting them.
APP_USERNAME = os.getenv("APP_USERNAME", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
AUTH_ENABLED = bool(APP_USERNAME and APP_PASSWORD)

if not AUTH_ENABLED:
    print("[WARNING] APP_USERNAME / APP_PASSWORD not set -- the login gate is "
          "DISABLED. This is fine for local testing, but set both before "
          "deploying anywhere public.")


def _check_auth(username, password):
    return username == APP_USERNAME and password == APP_PASSWORD


def _auth_challenge():
    return Response(
        "Login required.", 401,
        {"WWW-Authenticate": 'Basic realm="PPCL Trainer"'}
    )


@app.before_request
def require_login():
    if not AUTH_ENABLED:
        return None
    auth = request.authorization
    if not auth or not _check_auth(auth.username, auth.password):
        return _auth_challenge()
    return None


# --- EXPANDED RANDOM SCENARIO GENERATOR ---
# Equipment-type system pools. Templates declare which pool(s) they're valid
# for via a "systems" key so a scenario never assigns e.g. an economizer
# damper sequence to a pump, or a discharge-air-temp alarm to a system that
# doesn't have ductwork. VAV is intentionally absent -- VAV boxes are
# controlled by a different system in the field, not PPCL.
CHILLER_SYSTEMS = ["CHLR"]
AHU_SYSTEMS = ["AHU01", "AHU02", "AHU03"]
CHWP_SYSTEMS = ["CHWP01", "CHWP02"]
HWP_SYSTEMS = ["HWP01", "HWP02"]
PUMP_SYSTEMS = CHWP_SYSTEMS + HWP_SYSTEMS
CT_SYSTEMS = ["CT01", "CT02"]
FCU_SYSTEMS = ["FCU101", "FCU102", "FCU201"]
EF_SYSTEMS = ["EF01", "EF02"]
MAU_SYSTEMS = ["MAU01"]
BOILER_SYSTEMS = ["BLR"]
CRAC_SYSTEMS = ["CRAC"]
AC_SYSTEMS = ["AC101"]

# Plant-level pump groups, used ONLY by lead-lag rotation templates. CHWP01/
# CHWP02/HWP01/HWP02 (above) each already denote one specific physical pump,
# so a lead-lag scenario that names its rotating pumps "CHWP02.P1"/"CHWP02.P3"
# reads as nonsense (pump 02's pump 1?). A lead-lag group is a plant-level
# concept -- P1/P2/(P3) are the individual pumps WITHIN one plant, e.g.
# "%X%CHW.P1.ENA" / "%X%CHW.P2.ENA" for the chilled water plant.
CHW_PLANT_SYSTEMS = ["CHW"]
HW_PLANT_SYSTEMS = ["HW"]
PUMP_PLANT_SYSTEMS = CHW_PLANT_SYSTEMS + HW_PLANT_SYSTEMS
PLANT_LABELS = {"CHW": "Chilled Water Pump", "HW": "Hot Water Pump"}

# Fallback pool for the rare template that doesn't declare its own "systems".
SYSTEMS = CHILLER_SYSTEMS + AHU_SYSTEMS + PUMP_SYSTEMS

# --- EQUIPMENT FOCUS CATEGORIES ---
# Lets a student pick "I want to practice on chillers" (or pumps, AHUs, etc.)
# instead of just a difficulty tier. Rather than hand-tagging every single
# template with its equipment type (error-prone as templates get added),
# categories are derived from the point-name prefixes already in each
# template's "systems" list -- so a template stays correctly categorized
# automatically even as new templates/equipment are added later.
EQUIPMENT_CATEGORIES = {
    "ahu": {"label": "Air Handling Units (AHU)", "prefixes": ("AHU",)},
    "mau": {"label": "Make-Up Air Units (MAU)", "prefixes": ("MAU",)},
    "fcu": {"label": "Fan Coil Units (FCU)", "prefixes": ("FCU",)},
    "exhaust_fan": {"label": "Exhaust Fans (EF)", "prefixes": ("EF",)},
    "chiller": {"label": "Chillers (CHLR)", "prefixes": ("CHLR",)},
    "pump": {"label": "Pumps (CHW/HW)", "prefixes": ("CHWP", "HWP", "CHW", "HW")},
    "cooling_tower": {"label": "Cooling Towers (CT)", "prefixes": ("CT",)},
    "boiler": {"label": "Boilers (BLR)", "prefixes": ("BLR",)},
    "crac": {"label": "CRAC Units", "prefixes": ("CRAC",)},
    "air_compressor": {"label": "Air Compressors (AC)", "prefixes": ("AC",)},
}


def classify_equipment(systems_list):
    """Return the set of equipment-category keys a template's systems list
    touches, e.g. ["AHU01", "MAU01"] -> {"ahu", "mau"}."""
    cats = set()
    for s in systems_list or []:
        for key, info in EQUIPMENT_CATEGORIES.items():
            if any(s.startswith(p) for p in info["prefixes"]):
                cats.add(key)
    return cats

# Every point below carries a "kind":
#   "physical" -- a real physical I/O point (AI/AO/DI/DO) or a genuinely
#                pre-existing, tech-adjustable database point (a setpoint,
#                high limit, gain). These already exist; use the exact
#                name given.
#   "virtual" -- an alarm flag, timer/accumulator, or internal status/delay
#                value that the PPCL program itself creates and drives.
#                Nothing pre-wires these -- you give them a name and your
#                own logic gives them meaning, the way alarms and timers
#                actually get built while writing a real sequence.
#   "local"   -- $LOC/$ARG scratchpad variables (not database points at all).
TEMPLATES = [
    # ---------------- EASY ----------------
    {
        "title": "Zone Cooling Valve Control by Room Temperature",
        "difficulty": "easy",
        "multiLine": False,
        "systems": FCU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Cooling Valve Temperature Control",
            "prompt": f"If Room Temperature (\"%X%{sys}.RMT\") is greater than Cooling Setpoint (\"%X%{sys}.RMT.SP\"), turn ON the Cooling Valve (\"%X%{sys}.CLG.VLV\"). Otherwise, turn it OFF.",
            "points": [
                {"name": f"%X%{sys}.RMT",
                    "desc": "Room Temperature Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.RMT.SP",
                    "desc": "Cooling Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.CLG.VLV",
                    "desc": "Cooling Valve Command", "type": "DO", "kind": "physical"}
            ],
            "help": "This is a single IF/THEN/ELSE built around one .GT. comparison between two analog points. Compare RMT directly to RMT.SP -- don't hardcode a number in place of the setpoint, since the whole point of a setpoint point is that it can be changed later without editing the program. Write the ELSE branch too: without it, the valve turns ON when the condition is met but never turns back OFF once RMT drops back down, because a point just holds whatever it was last written to.",
            "checks": [
                {"force": {f"%X%{sys}.RMT": 85, f"%X%{sys}.RMT.SP": 72}, "expect": {f"%X%{sys}.CLG.VLV": 1}, "note": "RMT above setpoint -- valve should be ON"},
                {"force": {f"%X%{sys}.RMT": 65, f"%X%{sys}.RMT.SP": 72}, "expect": {f"%X%{sys}.CLG.VLV": 0}, "note": "RMT below setpoint -- valve should be OFF"}
            ]
        }
    },
    {
        "title": "FCU Cycling Fan Control",
        "difficulty": "easy",
        "multiLine": False,
        "systems": FCU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Cycling Fan Control",
            "prompt": f"This FCU is a cycling unit -- instead of modulating a valve, the fan itself cycles on and off with zone temperature. If Room Temperature (\"%X%{sys}.RMT\") rises above the Cooling Setpoint (\"%X%{sys}.CLG.SP\") OR falls below the Heating Setpoint (\"%X%{sys}.HTG.SP\"), turn the Fan (\"%X%{sys}.FAN.ENA\") ON. When Room Temperature is between the two setpoints (in the deadband), turn the Fan OFF.",
            "points": [
                {"name": f"%X%{sys}.RMT",
                    "desc": "Room Temperature Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.CLG.SP",
                    "desc": "Cooling Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.HTG.SP",
                    "desc": "Heating Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.FAN.ENA",
                    "desc": "Fan Enable Command", "type": "DO", "kind": "physical"}
            ],
            "help": "Two comparisons joined by .OR.: RMT .GT. CLG.SP .OR. RMT .LT. HTG.SP. Both conditions call for the same action (fan ON), so combine them in one IF rather than writing two separate IF blocks. Anything that's neither above the cooling setpoint nor below the heating setpoint is sitting in the deadband and belongs in the ELSE -- FAN.ENA OFF.",
            "checks": [
                {"force": {f"%X%{sys}.RMT": 80, f"%X%{sys}.CLG.SP": 75, f"%X%{sys}.HTG.SP": 68}, "expect": {f"%X%{sys}.FAN.ENA": 1}, "note": "above cooling setpoint -- fan should be ON"},
                {"force": {f"%X%{sys}.RMT": 60, f"%X%{sys}.CLG.SP": 75, f"%X%{sys}.HTG.SP": 68}, "expect": {f"%X%{sys}.FAN.ENA": 1}, "note": "below heating setpoint -- fan should be ON"},
                {"force": {f"%X%{sys}.RMT": 70, f"%X%{sys}.CLG.SP": 75, f"%X%{sys}.HTG.SP": 68}, "expect": {f"%X%{sys}.FAN.ENA": 0}, "note": "in the deadband -- fan should be OFF"}
            ]
        }
    },
    {
        "title": "Outside Air Damper Command/Status Fault Check",
        "difficulty": "easy",
        "multiLine": False,
        "systems": AHU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Damper Fault Alarm",
            "prompt": f"If Outside Air Damper Command (\"%X%{sys}.OAD.CMD\") is ON (commanded open) and Outside Air Damper Status (\"%X%{sys}.OAD.S\") is OFF (not proven open), turn ON a Damper Fault Alarm (\"%X%{sys}.OAD.ALM\") -- a virtual point. Otherwise, turn it OFF.",
            "points": [
                {"name": f"%X%{sys}.OAD.CMD",
                    "desc": "Damper Open/Closed Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.OAD.S",
                    "desc": "Damper Proven-Open Status (proof for OAD.CMD)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.OAD.ALM",
                    "desc": "Damper Fault Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "One .AND. between two digital points: CMD is ON while S is OFF is the fault. Every other combination -- including CMD OFF regardless of what S is doing -- should fall through to the ELSE and clear the alarm. Write out both sides of the AND explicitly (e.g. OAD.CMD .EQ. 1 .AND. OAD.S .EQ. 0) rather than assuming a single point check covers it.",
            "checks": [
                {"force": {f"%X%{sys}.OAD.CMD": 1, f"%X%{sys}.OAD.S": 0}, "expect": {f"%X%{sys}.OAD.ALM": 1}, "note": "commanded open but not proven open -- alarm should be ON"},
                {"force": {f"%X%{sys}.OAD.CMD": 1, f"%X%{sys}.OAD.S": 1}, "expect": {f"%X%{sys}.OAD.ALM": 0}, "note": "commanded open and proven open -- no fault"},
                {"force": {f"%X%{sys}.OAD.CMD": 0, f"%X%{sys}.OAD.S": 0}, "expect": {f"%X%{sys}.OAD.ALM": 0}, "note": "not commanded open -- no fault regardless of status"}
            ]
        }
    },
    {
        "title": "Supply Fan Status Verification Alarm",
        "difficulty": "easy",
        "multiLine": False,
        "systems": AHU_SYSTEMS + MAU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Supply Fan Fail Alarm",
            "prompt": f"If Supply Fan Start/Stop Command (\"%X%{sys}.SF.SS\") is ON and Supply Fan Status (\"%X%{sys}.SF.S\") is OFF, turn ON a Supply Fan Fail Alarm (\"%X%{sys}.SF.ALM\") -- a virtual point. Otherwise, turn it OFF.",
            "points": [
                {"name": f"%X%{sys}.SF.SS",
                    "desc": "Supply Fan Start/Stop Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.SF.S",
                    "desc": "Supply Fan Running Status", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.SF.ALM",
                    "desc": "Supply Fan Fail Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Same shape as a command/status mismatch alarm: .AND. together SS ON with S OFF for the fault. Anything else -- including SS OFF -- clears the alarm in the ELSE. Watch out for writing the comparison backwards (S ON and SS OFF is NOT a fault condition here; the fan just isn't supposed to be running).",
            "checks": [
                {"force": {f"%X%{sys}.SF.SS": 1, f"%X%{sys}.SF.S": 0}, "expect": {f"%X%{sys}.SF.ALM": 1}, "note": "commanded to run but not proven running -- alarm should be ON"},
                {"force": {f"%X%{sys}.SF.SS": 1, f"%X%{sys}.SF.S": 1}, "expect": {f"%X%{sys}.SF.ALM": 0}, "note": "commanded to run and proven running -- no fault"},
                {"force": {f"%X%{sys}.SF.SS": 0, f"%X%{sys}.SF.S": 0}, "expect": {f"%X%{sys}.SF.ALM": 0}, "note": "not commanded to run -- no fault"}
            ]
        }
    },
    {
        "title": "Supply Fan VFD Trouble Alarm (Independent of Status)",
        "difficulty": "easy",
        "multiLine": False,
        "systems": AHU_SYSTEMS + MAU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} VFD Trouble Alarm",
            "prompt": f"Real field panels generate a VFD trouble alarm independent from the fan's start/stop status alarm -- a current switch confirms the fan is in the commanded state, but the VFD itself also has its own fault relay for drive-level problems (overcurrent, overtemp, etc.) that should trip the fan immediately regardless of what status says. If Supply Fan VFD Trouble (\"%X%{sys}.SF.VFD.FLT\") is ON, turn ON a VFD Trouble Alarm (\"%X%{sys}.SF.VFD.ALM\" -- a virtual point) and force Supply Fan Enable (\"%X%{sys}.SF.ENA\") OFF, overriding normal control. Otherwise, turn the alarm OFF.",
            "points": [
                {"name": f"%X%{sys}.SF.VFD.FLT",
                    "desc": "Supply Fan VFD Trouble Contact (fault relay from the VFD drive itself, separate from the fan's running-status feedback)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.SF.VFD.ALM",
                    "desc": "Supply Fan VFD Trouble Alarm (virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.SF.ENA",
                    "desc": "Supply Fan Enable Command (already exists -- your logic must write to this point)", "type": "DO", "kind": "physical"}
            ],
            "help": "Two writes belong in the same THEN block: set the alarm AND force SF.ENA OFF when VFD.FLT is ON. The ELSE only needs to turn the alarm back OFF -- the prompt doesn't ask you to do anything to SF.ENA when there's no fault, so leave it alone there.",
            "checks": [
                {"force": {f"%X%{sys}.SF.VFD.FLT": 1}, "expect": {f"%X%{sys}.SF.VFD.ALM": 1, f"%X%{sys}.SF.ENA": 0}, "note": "VFD trouble contact closed -- alarm ON and fan forced off"},
                {"force": {f"%X%{sys}.SF.VFD.FLT": 0}, "expect": {f"%X%{sys}.SF.VFD.ALM": 0}, "note": "no VFD trouble -- alarm should be OFF"}
            ]
        }
    },
    {
        "title": "Exhaust Fan Louver Interlock",
        "difficulty": "easy",
        "multiLine": False,
        "systems": EF_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Exhaust Fan Louver Interlock",
            "prompt": f"If Exhaust Fan Start Command (\"%X%{sys}.EF.SS\") is ON and Exhaust Louver Position Feedback (\"%X%{sys}.LVR.POS\") is ON (proven open), turn ON the Exhaust Fan Run Output (\"%X%{sys}.EF.RUN\"). Otherwise, turn it OFF -- the fan should never run with the louver unproven.",
            "points": [
                {"name": f"%X%{sys}.EF.SS",
                    "desc": "Exhaust Fan Start Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.LVR.POS",
                    "desc": "Exhaust Louver Position Feedback (proven open)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.EF.RUN",
                    "desc": "Exhaust Fan Run Output", "type": "DO", "kind": "physical"}
            ],
            "help": "AND together SS ON with POS ON before turning the fan on -- both conditions must be true. If either one drops out, the ELSE should turn RUN OFF immediately; there's no partial-open allowance here.",
            "checks": [
                {"force": {f"%X%{sys}.EF.SS": 1, f"%X%{sys}.LVR.POS": 1}, "expect": {f"%X%{sys}.EF.RUN": 1}, "note": "started and louver proven open -- fan should run"},
                {"force": {f"%X%{sys}.EF.SS": 1, f"%X%{sys}.LVR.POS": 0}, "expect": {f"%X%{sys}.EF.RUN": 0}, "note": "started but louver not proven -- fan must not run"},
                {"force": {f"%X%{sys}.EF.SS": 0, f"%X%{sys}.LVR.POS": 1}, "expect": {f"%X%{sys}.EF.RUN": 0}, "note": "not started -- fan should be off regardless of louver"}
            ]
        }
    },
    {
        "title": "Exhaust Fan Interlocked with AHU Mode",
        "difficulty": "easy",
        "multiLine": False,
        "systems": EF_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} AHU-Interlocked Exhaust Fan",
            "prompt": f"Exhaust Fan {sys} has no start/stop command of its own -- it's interlocked with AHU01 and simply follows whatever AHU01's supply fan is doing. If AHU01 Supply Fan Enable (\"%X%AHU01.SF.ENA\") is ON and Louver Position Feedback (\"%X%{sys}.LVR.POS\") is ON (proven open), turn ON the Exhaust Fan Run Output (\"%X%{sys}.EF.RUN\"). Otherwise, turn it OFF.",
            "points": [
                {"name": "%X%AHU01.SF.ENA",
                    "desc": "AHU01 Supply Fan Enable Command (already exists on the interlocked AHU -- read-only here)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.LVR.POS",
                    "desc": "Exhaust Louver Position Feedback (proven open)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.EF.RUN",
                    "desc": "Exhaust Fan Run Output", "type": "DO", "kind": "physical"}
            ],
            "help": "Same AND pattern as a normal fan/louver interlock, except the fan-enable half of the condition comes from a different unit entirely -- AHU01.SF.ENA, not anything on this EF's own point tree. Reference \"%X%AHU01.SF.ENA\" literally in your code; don't substitute this EF's own system prefix onto it.",
            "checks": [
                {"force": {"%X%AHU01.SF.ENA": 1, f"%X%{sys}.LVR.POS": 1}, "expect": {f"%X%{sys}.EF.RUN": 1}, "note": "AHU01 supply fan running and louver proven open -- EF should run"},
                {"force": {"%X%AHU01.SF.ENA": 1, f"%X%{sys}.LVR.POS": 0}, "expect": {f"%X%{sys}.EF.RUN": 0}, "note": "AHU01 running but louver not proven -- EF must not run"},
                {"force": {"%X%AHU01.SF.ENA": 0, f"%X%{sys}.LVR.POS": 1}, "expect": {f"%X%{sys}.EF.RUN": 0}, "note": "AHU01 supply fan not running -- EF should be off"}
            ]
        }
    },
    {
        "title": "Make-Up Air Unit Heating Valve Control",
        "difficulty": "easy",
        "multiLine": False,
        "systems": MAU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Heating Valve Control",
            "prompt": f"If Discharge Air Temp (\"%X%{sys}.DAT\") is less than Discharge Air Setpoint (\"%X%{sys}.DAT.SP\"), turn ON the Heating Valve (\"%X%{sys}.HTG.VLV\"). Otherwise, turn it OFF.",
            "points": [
                {"name": f"%X%{sys}.DAT",
                    "desc": "Discharge Air Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.DAT.SP",
                    "desc": "Discharge Air Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.HTG.VLV",
                    "desc": "Heating Valve Command", "type": "DO", "kind": "physical"}
            ],
            "help": "A single .LT. comparison -- the same shape as a cooling-valve-by-room-temp scenario, just flipped: colder than setpoint opens the valve instead of warmer than setpoint. Compare DAT to DAT.SP directly rather than a hardcoded number.",
            "checks": [
                {"force": {f"%X%{sys}.DAT": 60, f"%X%{sys}.DAT.SP": 68}, "expect": {f"%X%{sys}.HTG.VLV": 1}, "note": "DAT below setpoint -- valve should be ON"},
                {"force": {f"%X%{sys}.DAT": 75, f"%X%{sys}.DAT.SP": 68}, "expect": {f"%X%{sys}.HTG.VLV": 0}, "note": "DAT above setpoint -- valve should be OFF"}
            ]
        }
    },
    {
        "title": "Chiller Enable by Building Cooling Demand",
        "difficulty": "easy",
        "multiLine": False,
        "systems": CHILLER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Enable by Cooling Demand",
            "prompt": f"If Building Cooling Demand (\"%X%{sys}.DMD\") is ON, turn ON Chiller Plant Master Enable (\"%X%{sys}.ENA\"). Otherwise, turn it OFF.",
            "points": [
                {"name": f"%X%{sys}.DMD",
                    "desc": "Building Cooling Demand (virtual point, driven by a separate plant-load program)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.ENA",
                    "desc": "Chiller Plant Master Enable Command", "type": "BV", "kind": "physical"}
            ],
            "help": "As simple as PPCL gets -- DMD is already a clean ON/OFF flag, so just mirror it onto ENA with one IF/THEN/ELSE.",
            "checks": [
                {"force": {f"%X%{sys}.DMD": 1}, "expect": {f"%X%{sys}.ENA": 1}, "note": "demand present -- plant should enable"},
                {"force": {f"%X%{sys}.DMD": 0}, "expect": {f"%X%{sys}.ENA": 0}, "note": "no demand -- plant should be off"}
            ]
        }
    },
    {
        "title": "Pump Enable by System Demand",
        "difficulty": "easy",
        "multiLine": False,
        "systems": PUMP_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Enable by System Demand",
            "prompt": f"If System Demand (\"%X%{sys}.DMD\") is ON, turn ON Pump Enable (\"%X%{sys}.ENA\"). Otherwise, turn it OFF.",
            "points": [
                {"name": f"%X%{sys}.DMD",
                    "desc": "System Demand (virtual point, driven by a separate plant-load program)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.ENA",
                    "desc": "Pump Enable Command", "type": "DO", "kind": "physical"}
            ],
            "help": "Same pattern as the chiller demand scenario -- DMD is already a clean flag, mirror it straight onto ENA.",
            "checks": [
                {"force": {f"%X%{sys}.DMD": 1}, "expect": {f"%X%{sys}.ENA": 1}, "note": "demand present -- pump should enable"},
                {"force": {f"%X%{sys}.DMD": 0}, "expect": {f"%X%{sys}.ENA": 0}, "note": "no demand -- pump should be off"}
            ]
        }
    },
    {
        "title": "Cooling Tower Basin Heater Freeze Protection",
        "difficulty": "easy",
        "multiLine": False,
        "systems": CT_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Basin Heater Control",
            "prompt": f"If Basin Water Temp (\"%X%{sys}.BASIN.T\") is less than Freeze Protection Setpoint (\"%X%{sys}.BASIN.T.SP\"), turn ON the Basin Heater (\"%X%{sys}.BASIN.HTR\"). Otherwise, turn it OFF.",
            "points": [
                {"name": f"%X%{sys}.BASIN.T",
                    "desc": "Basin Water Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.BASIN.T.SP",
                    "desc": "Freeze Protection Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.BASIN.HTR",
                    "desc": "Basin Heater Command", "type": "DO", "kind": "physical"}
            ],
            "help": "Single .LT. comparison, same shape as the heating-valve scenario -- colder than the freeze-protection setpoint turns the heater ON.",
            "checks": [
                {"force": {f"%X%{sys}.BASIN.T": 35, f"%X%{sys}.BASIN.T.SP": 40}, "expect": {f"%X%{sys}.BASIN.HTR": 1}, "note": "basin water below freeze setpoint -- heater should be ON"},
                {"force": {f"%X%{sys}.BASIN.T": 55, f"%X%{sys}.BASIN.T.SP": 40}, "expect": {f"%X%{sys}.BASIN.HTR": 0}, "note": "basin water above freeze setpoint -- heater should be OFF"}
            ]
        }
    },
    {
        "title": "Boiler Enable by Firing Demand",
        "difficulty": "easy",
        "multiLine": False,
        "systems": BOILER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys}1 Enable by Firing Demand",
            "prompt": f"If Firing Demand (\"%X%{sys}1.DMD\") is ON, turn ON Boiler 1 Enable (\"%X%{sys}1.ENA\"). Otherwise, turn it OFF.",
            "points": [
                {"name": f"%X%{sys}1.DMD",
                    "desc": "Firing Demand (virtual point, driven by hot water temperature reset logic elsewhere in the program)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}1.ENA",
                    "desc": "Boiler 1 Enable Command", "type": "DO", "kind": "physical"}
            ],
            "help": "Same demand-to-enable mirroring pattern as the chiller and pump scenarios -- one IF/THEN/ELSE off the DMD flag.",
            "checks": [
                {"force": {f"%X%{sys}1.DMD": 1}, "expect": {f"%X%{sys}1.ENA": 1}, "note": "firing demand present -- boiler should enable"},
                {"force": {f"%X%{sys}1.DMD": 0}, "expect": {f"%X%{sys}1.ENA": 0}, "note": "no firing demand -- boiler should be off"}
            ]
        }
    },
    {
        "title": "CRAC Compressor Enable by Room Temperature",
        "difficulty": "easy",
        "multiLine": False,
        "systems": CRAC_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys}201 Compressor Enable",
            "prompt": f"If Room Temperature (\"%X%{sys}201.RMT\") is greater than Cooling Setpoint (\"%X%{sys}201.CLG.SP\"), turn ON Compressor Enable (\"%X%{sys}201.COMP.ENA\"). Otherwise, turn it OFF.",
            "points": [
                {"name": f"%X%{sys}201.RMT",
                    "desc": "Room Temperature Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}201.CLG.SP",
                    "desc": "Cooling Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}201.COMP.ENA",
                    "desc": "Compressor Enable Command", "type": "DO", "kind": "physical"}
            ],
            "help": "Same single .GT. comparison as the FCU cooling-valve scenario -- compare RMT to CLG.SP directly and don't forget the ELSE, otherwise the compressor never shuts back off once the room cools down.",
            "checks": [
                {"force": {f"%X%{sys}201.RMT": 78, f"%X%{sys}201.CLG.SP": 74}, "expect": {f"%X%{sys}201.COMP.ENA": 1}, "note": "RMT above setpoint -- compressor should enable"},
                {"force": {f"%X%{sys}201.RMT": 70, f"%X%{sys}201.CLG.SP": 74}, "expect": {f"%X%{sys}201.COMP.ENA": 0}, "note": "RMT below setpoint -- compressor should be off"}
            ]
        }
    },
    {
        "title": "Air Compressor Cooling Water High Temp Alarm",
        "difficulty": "easy",
        "multiLine": False,
        "systems": AC_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Cooling Water High Temp Alarm",
            "prompt": f"If Compressor Run Status (\"%X%{sys}.STAT\") is ON and Cooling Water Entering Temp (\"%X%{sys}.CHW.ET\") is greater than High Limit (\"%X%{sys}.CHW.ET.HI\"), turn ON a Cooling Water High Temp Alarm (\"%X%{sys}.CHW.ET.ALM\"). Otherwise, turn it OFF.",
            "points": [
                {"name": f"%X%{sys}.STAT",
                    "desc": "Compressor Run Status", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.CHW.ET",
                    "desc": "Cooling Water Entering Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.CHW.ET.HI",
                    "desc": "Cooling Water High Limit (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.CHW.ET.ALM",
                    "desc": "Cooling Water High Temp Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "AND together the run-status check with the high-temp comparison -- this alarm should only matter while the compressor is actually running. Checking cooling water temp on a stopped compressor doesn't tell you anything and would nuisance-alarm on a cooling-down unit.",
            "checks": [
                {"force": {f"%X%{sys}.STAT": 1, f"%X%{sys}.CHW.ET": 95, f"%X%{sys}.CHW.ET.HI": 85}, "expect": {f"%X%{sys}.CHW.ET.ALM": 1}, "note": "running and cooling water over limit -- alarm should be ON"},
                {"force": {f"%X%{sys}.STAT": 1, f"%X%{sys}.CHW.ET": 75, f"%X%{sys}.CHW.ET.HI": 85}, "expect": {f"%X%{sys}.CHW.ET.ALM": 0}, "note": "running and cooling water under limit -- no alarm"},
                {"force": {f"%X%{sys}.STAT": 0, f"%X%{sys}.CHW.ET": 95, f"%X%{sys}.CHW.ET.HI": 85}, "expect": {f"%X%{sys}.CHW.ET.ALM": 0}, "note": "compressor not running -- no alarm regardless of temp"}
            ]
        }
    },

    # ---------------- MEDIUM ----------------
    {
        "title": "High Discharge Air Temp Alarm with Time Delay",
        "difficulty": "medium",
        "multiLine": True,
        "systems": AHU_SYSTEMS + MAU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Discharge Air Temp Alarm (Delayed)",
            "prompt": f"Real Siemens panel dumps often write a delayed alarm as a GOTO branch rather than one big IF/THEN/ELSE. Build it that way here: if Discharge Air Temp (\"%X%{sys}.DAT\") is greater than High Limit (\"%X%{sys}.DAT.HI\"), GOTO a higher-numbered alarm-timer block. Otherwise (still on the lines right after that check), reset the Alarm Delay Timer (\"%X%{sys}.DAT.ALM.TMR\") to 0, turn the High Temp Alarm (\"%X%{sys}.DAT.ALM\") OFF, then GOTO past the alarm-timer block entirely so it can't run while the temperature is back in range. In the alarm-timer block itself, use SAMPLE(1) IF (\"%X%{sys}.DAT\" .GT. \"%X%{sys}.DAT.HI\") THEN ... to accumulate the Alarm Delay Timer by 1 each second, then turn ON the High Temp Alarm once the timer exceeds 60 seconds -- leave the alarm alone otherwise in that block, since dropping back below the limit (handled by the earlier GOTO branch) is what turns it back off. DAT.HI, DAT.ALM.TMR, and DAT.ALM are all virtual points. Remember: GOTO only ever jumps to a HIGHER line number than the line it's on.",
            "points": [
                {"name": f"%X%{sys}.DAT",
                    "desc": "Discharge Air Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.DAT.HI",
                    "desc": "High Temp Alarm Limit (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.DAT.ALM.TMR",
                    "desc": "Alarm Delay Timer Accumulator (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.DAT.ALM",
                    "desc": "High Discharge Temp Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "GOTO can only ever jump forward to a higher line number, never backward -- so lay your lines out top to bottom as: the main DAT.HI check, the normal-condition reset-and-skip branch, then the alarm-timer block last. If your timer never seems to move, check that you're using SAMPLE(1) IF (...) THEN inside the timer block rather than a bare IF -- SAMPLE gates the accumulation to once per scan rather than piling up every pass. The reset branch needs its own GOTO too, jumping past the timer block entirely, otherwise the timer block would still execute right after it on the same scan and immediately re-accumulate.",
            "checks": [
                {"force": {f"%X%{sys}.DAT": 60, f"%X%{sys}.DAT.HI": 55, f"%X%{sys}.DAT.ALM.TMR": 0}, "cycles": 65, "expect": {f"%X%{sys}.DAT.ALM": 1}, "note": "DAT stays over the high limit for more than 60 scans -- alarm should latch ON"},
                {"force": {f"%X%{sys}.DAT": 60, f"%X%{sys}.DAT.HI": 55, f"%X%{sys}.DAT.ALM.TMR": 0, f"%X%{sys}.DAT.ALM": 0}, "cycles": 5, "expect": {f"%X%{sys}.DAT.ALM": 0}, "note": "DAT over the limit but not yet 60 seconds -- alarm should NOT have tripped yet"},
                {"force": {f"%X%{sys}.DAT": 50, f"%X%{sys}.DAT.HI": 55}, "cycles": 10, "expect": {f"%X%{sys}.DAT.ALM": 0, f"%X%{sys}.DAT.ALM.TMR": 0}, "note": "DAT back under the limit -- timer resets to 0 and alarm stays OFF"}
            ]
        }
    },
    {
        "title": "FCU Occupied/Unoccupied Setpoint Strategy",
        "difficulty": "medium",
        "multiLine": False,
        "systems": FCU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Occupied/Unoccupied Setpoint Strategy",
            "prompt": f"The {sys} zone needs different cooling setpoints depending on building occupancy. Building occupancy is tracked by a single, building-wide Building State point (\"%X%BLDG.OCC.STATE\") where 0 = Occupied and 1 = Unoccupied -- there is no per-zone occupancy point. When Occupied, the Active Cooling Setpoint (\"%X%{sys}.CLG.SP.ACT\") should be 72 degrees. When Unoccupied, it should be 78 degrees. Use those two fixed values directly in your logic -- you don't need to create separate setpoint points for them, just drive Active Cooling Setpoint to the correct number for each building state.",
            "points": [
                {"name": "%X%BLDG.OCC.STATE",
                    "desc": "Building-wide Occupancy Mode (0 = Occupied, 1 = Unoccupied) -- a virtual point driven by a scheduling program, shared across all equipment", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.CLG.SP.ACT",
                    "desc": "Active Cooling Setpoint (virtual point -- calculated and written by your program logic)", "type": "AV", "kind": "virtual"}
            ],
            "help": "BLDG.OCC.STATE is an analog virtual point holding 0 or 1, not a digital ON/OFF point -- compare it with .EQ. 0 / .EQ. 1 rather than treating it like a DI. Use the fixed setpoint values (72, 78) directly in your THEN/ELSE; there's no separate setpoint database point to reference for either mode.",
            "checks": [
                {"force": {"%X%BLDG.OCC.STATE": 0}, "expect": {f"%X%{sys}.CLG.SP.ACT": 72}, "note": "occupied (state 0) -- active cooling setpoint should be 72"},
                {"force": {"%X%BLDG.OCC.STATE": 1}, "expect": {f"%X%{sys}.CLG.SP.ACT": 78}, "note": "unoccupied (state 1) -- active cooling setpoint should be 78"}
            ]
        }
    },
    {
        "title": "AHU Occupied/Unoccupied Mode Strategy",
        "difficulty": "medium",
        "multiLine": True,
        "systems": AHU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Occupied/Unoccupied Mode Strategy",
            "prompt": f"{sys} needs to change more than just its setpoint between occupied and unoccupied mode -- a full air handler typically shuts down overnight rather than just relaxing its setpoint like a single zone would. Building occupancy is tracked by a single, building-wide Building State point (\"%X%BLDG.OCC.STATE\") where 0 = Occupied and 1 = Unoccupied. When Occupied: turn ON Supply Fan Enable (\"%X%{sys}.SF.ENA\"), drive Active Cooling Setpoint (\"%X%{sys}.CLG.SP.ACT\") to 72 degrees, and open the Outside Air Damper (\"%X%{sys}.OAD.CMD\") for ventilation. When Unoccupied: turn OFF Supply Fan Enable, drive Active Cooling Setpoint to 78 degrees, and close the Outside Air Damper -- no ventilation air needed with nobody in the building. Use the fixed setpoint values directly in your logic.",
            "points": [
                {"name": "%X%BLDG.OCC.STATE",
                    "desc": "Building-wide Occupancy Mode (0 = Occupied, 1 = Unoccupied) -- a virtual point driven by a scheduling program, shared across all equipment", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.SF.ENA",
                    "desc": "Supply Fan Enable Command (already exists -- your logic must write to this point)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.CLG.SP.ACT",
                    "desc": "Active Cooling Setpoint (virtual point -- calculated and written by your program logic)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.OAD.CMD",
                    "desc": "Outside Air Damper Open/Closed Command (already exists -- your logic must write to this point)", "type": "DO", "kind": "physical"}
            ],
            "help": "Three separate writes belong in each branch of one IF/THEN/ELSE keyed on BLDG.OCC.STATE -- group all three occupied actions together in THEN and all three unoccupied actions together in ELSE (comma-separated or on separate lines), rather than writing three different IF blocks that each re-check occupancy.",
            "checks": [
                {"force": {"%X%BLDG.OCC.STATE": 0}, "expect": {f"%X%{sys}.SF.ENA": 1, f"%X%{sys}.CLG.SP.ACT": 72, f"%X%{sys}.OAD.CMD": 1}, "note": "occupied -- fan on, 72 setpoint, OA damper open"},
                {"force": {"%X%BLDG.OCC.STATE": 1}, "expect": {f"%X%{sys}.SF.ENA": 0, f"%X%{sys}.CLG.SP.ACT": 78, f"%X%{sys}.OAD.CMD": 0}, "note": "unoccupied -- fan off, 78 setpoint, OA damper closed"}
            ]
        }
    },
    {
        "title": "Warmup Mode Once-Per-Day Lockout",
        "difficulty": "medium",
        "multiLine": True,
        "systems": AHU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Warmup Mode Lockout",
            "prompt": f"{sys} is allowed to enter Warmup mode (preheating the space ahead of occupancy) at most once per day -- real sequences explicitly prevent re-triggering it repeatedly. If Room Temperature (\"%X%{sys}.RMT\") is below the Warmup Enable Setpoint (\"%X%{sys}.WU.SP\") and the Warmup Lockout flag (\"%X%{sys}.WU.LOCKOUT\") is OFF, turn ON Warmup Enable (\"%X%{sys}.WU.ENA\") and latch the Warmup Lockout flag ON so the mode can't retrigger again until reset -- once latched, Room Temperature dropping below setpoint again should NOT turn Warmup back on. Separately, a scheduling program pulses a Daily Reset point (\"%X%{sys}.WU.RST\") ON once every day; whenever that happens, turn Warmup Enable OFF and clear the Warmup Lockout flag back OFF, ready for the next cycle.",
            "points": [
                {"name": f"%X%{sys}.RMT",
                    "desc": "Room Temperature Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.WU.SP",
                    "desc": "Warmup Enable Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.WU.LOCKOUT",
                    "desc": "Warmup Lockout Flag (virtual point -- latches ON once Warmup has fired today)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.WU.ENA",
                    "desc": "Warmup Enable Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.WU.RST",
                    "desc": "Daily Reset Pulse (virtual point, driven by a separate scheduling program once per day)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Two independent checks, not one IF/THEN/ELSE: the warmup-trigger condition (RMT below setpoint AND lockout still clear) and the daily-reset condition (WU.RST pulsed) belong in two separate IF blocks. The lockout flag only ever gets set inside the trigger block, never cleared there -- once WU.LOCKOUT is ON, the AND condition is permanently false until the reset block clears it back OFF.",
            "checks": [
                {"force": {f"%X%{sys}.RMT": 60, f"%X%{sys}.WU.SP": 65, f"%X%{sys}.WU.LOCKOUT": 0, f"%X%{sys}.WU.RST": 0}, "cycles": 3, "expect": {f"%X%{sys}.WU.ENA": 1, f"%X%{sys}.WU.LOCKOUT": 1}, "note": "RMT below setpoint, not yet locked out -- warmup fires and stays latched across scans"},
                {"force": {f"%X%{sys}.RMT": 70, f"%X%{sys}.WU.SP": 65, f"%X%{sys}.WU.LOCKOUT": 0, f"%X%{sys}.WU.RST": 0}, "cycles": 1, "expect": {f"%X%{sys}.WU.LOCKOUT": 0}, "note": "RMT above setpoint -- warmup should not trigger or latch"},
                {"force": {f"%X%{sys}.RMT": 60, f"%X%{sys}.WU.SP": 65, f"%X%{sys}.WU.LOCKOUT": 1, f"%X%{sys}.WU.ENA": 1, f"%X%{sys}.WU.RST": 1}, "cycles": 1, "expect": {f"%X%{sys}.WU.ENA": 0, f"%X%{sys}.WU.LOCKOUT": 0}, "note": "daily reset pulse clears both the enable and the lockout flag"}
            ]
        }
    },
    {
        "title": "Continuously Operated Exhaust Fan with VOC Speed-Up",
        "difficulty": "medium",
        "multiLine": True,
        "systems": EF_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Continuous Operation VOC Speed-Up",
            "prompt": f"Exhaust Fan {sys} runs continuously at half speed once its louver is proven open -- per code, this fan is never allowed to shut off. If a VOC Detected alarm (\"%X%{sys}.VOC.DET\") trips ON, latch Full Speed Command (\"%X%{sys}.EF.FULL\") ON, driving the fan to full airflow -- it must stay latched even after the VOC alarm clears, since real code requires an operator to manually confirm the space is clear. It should only return to half speed when an operator pulses Manual Reset (\"%X%{sys}.EF.RST\") ON, which clears Full Speed Command back OFF.",
            "points": [
                {"name": f"%X%{sys}.VOC.DET",
                    "desc": "VOC Detected Alarm (from a duct-mounted VOC sensor)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.EF.FULL",
                    "desc": "Full Speed Command (virtual point -- latches ON until manually reset)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.EF.RST",
                    "desc": "Manual Reset Pulse (operator-facing)", "type": "BV", "kind": "physical"}
            ],
            "help": "Two independent IF blocks: one sets EF.FULL ON when VOC.DET trips and should NOT have an ELSE that clears it -- that's what makes it a latch. A separate block clears EF.FULL back OFF when EF.RST pulses. If the first block's ELSE turns EF.FULL off whenever VOC.DET is OFF, the latch breaks and the fan drops back to half speed the instant the sensor clears.",
            "checks": [
                {"force": {f"%X%{sys}.VOC.DET": 1, f"%X%{sys}.EF.FULL": 0, f"%X%{sys}.EF.RST": 0}, "expect": {f"%X%{sys}.EF.FULL": 1}, "note": "VOC detected -- full speed latches ON"},
                {"force": {f"%X%{sys}.VOC.DET": 0, f"%X%{sys}.EF.FULL": 1, f"%X%{sys}.EF.RST": 0}, "cycles": 3, "expect": {f"%X%{sys}.EF.FULL": 1}, "note": "VOC alarm cleared but not manually reset -- full speed stays latched across scans"},
                {"force": {f"%X%{sys}.VOC.DET": 0, f"%X%{sys}.EF.FULL": 1, f"%X%{sys}.EF.RST": 1}, "expect": {f"%X%{sys}.EF.FULL": 0}, "note": "manual reset pulsed -- clears full speed back OFF"}
            ]
        }
    },
    {
        "title": "Mixed Air Damper Economizer Sequence",
        "difficulty": "medium",
        "multiLine": True,
        "systems": AHU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Economizer Damper Sequence",
            "prompt": f"If Outside Air Temp (\"%X%{sys}.OAT\") is less than Return Air Temp (\"%X%{sys}.RAT\"), open the Outside Air Damper (\"%X%{sys}.OAD.CMD\") -- outside air is cooler than return air, so free cooling is available. If Outside Air Temp is greater than or equal to Return Air Temp, close the Outside Air Damper.",
            "points": [
                {"name": f"%X%{sys}.OAT",
                    "desc": "Outside Air Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.RAT",
                    "desc": "Return Air Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.OAD.CMD",
                    "desc": "Outside Air Damper Open/Closed Command", "type": "DO", "kind": "physical"}
            ],
            "help": "Single .LT. comparison IF/THEN/ELSE -- compare OAT to RAT directly. The prompt's \"greater than or equal to\" for the ELSE case is just the logical opposite of \"less than,\" so a plain ELSE (no separate .GE. check needed) covers it correctly.",
            "checks": [
                {"force": {f"%X%{sys}.OAT": 50, f"%X%{sys}.RAT": 75}, "expect": {f"%X%{sys}.OAD.CMD": 1}, "note": "OAT cooler than RAT -- damper should open for free cooling"},
                {"force": {f"%X%{sys}.OAT": 85, f"%X%{sys}.RAT": 75}, "expect": {f"%X%{sys}.OAD.CMD": 0}, "note": "OAT warmer than RAT -- damper should close"}
            ]
        }
    },
    {
        "title": "Duct Static Pressure High Limit Alarm",
        "difficulty": "medium",
        "multiLine": False,
        "systems": AHU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Duct Static Pressure Alarm",
            "prompt": f"If Duct Static Pressure (\"%X%{sys}.DSP\") is greater than High Limit (\"%X%{sys}.DSP.HI\"), turn ON a High Static Pressure Alarm (\"%X%{sys}.DSP.ALM\") and command Supply Fan Speed (\"%X%{sys}.SF.SPD\") down to a minimum speed of 30. Otherwise, turn the alarm OFF. DSP.HI and DSP.ALM are both virtual points.",
            "points": [
                {"name": f"%X%{sys}.DSP",
                    "desc": "Duct Static Pressure Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.DSP.HI",
                    "desc": "High Static Pressure Limit (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.DSP.ALM",
                    "desc": "High Static Pressure Alarm (virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.SF.SPD",
                    "desc": "Supply Fan Speed Command (0-100%)", "type": "AO", "kind": "physical"}
            ],
            "help": "Two writes in the THEN block (set the alarm AND clamp fan speed down to 30), but the ELSE only needs to clear the alarm -- the prompt doesn't say what to do with SF.SPD once pressure is back in range, so leave it under normal control there rather than writing a fixed value to it.",
            "checks": [
                {"force": {f"%X%{sys}.DSP": 3.5, f"%X%{sys}.DSP.HI": 3.0}, "expect": {f"%X%{sys}.DSP.ALM": 1, f"%X%{sys}.SF.SPD": 30}, "note": "static pressure over the limit -- alarm ON and fan speed clamped to 30"},
                {"force": {f"%X%{sys}.DSP": 2.0, f"%X%{sys}.DSP.HI": 3.0}, "expect": {f"%X%{sys}.DSP.ALM": 0}, "note": "static pressure under the limit -- alarm should be OFF"}
            ]
        }
    },
    {
        "title": "Chiller Plant Stage-Up Timer & Differential Pressure Check",
        "difficulty": "medium",
        "multiLine": True,
        "systems": CHILLER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Staging & Differential Pressure Enable",
            "prompt": f"If Chiller Enable (\"%X%{sys}.ENA\") is ON and System Chilled Water Differential Pressure (\"%X%{sys}.CHW.DP\") is greater than Setpoint (\"%X%{sys}.CHW.DP.SP\"), increment a Stage Up Timer (\"%X%{sys}.STGUP\") by 1 second using SAMPLE(1). Once the Stage Up Timer exceeds 300 seconds, enable Next Stage (\"%X%{sys}.STG2.ENA\"). Otherwise, reset the Stage Up Timer to 0. If Chiller Enable is ON and Differential Pressure remains below Setpoint, turn ON a Differential Pressure Alarm (\"%X%{sys}.ALM\"). CHW.DP.SP, STGUP, and ALM are all virtual points.",
            "points": [
                {"name": f"%X%{sys}.ENA",
                    "desc": "Chiller Plant Master Enable", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}.CHW.DP",
                 "desc": "Chilled Water Differential Pressure", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.CHW.DP.SP",
                 "desc": "Differential Pressure Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.STGUP",
                 "desc": "Stage Up Timer Accumulator (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.STG2.ENA",
                 "desc": "Stage 2 Enable Command", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}.ALM",
                 "desc": "Differential Pressure Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Two separate pieces sharing one general shape: a SAMPLE(1)-driven staging timer nested inside an IF that only accumulates while ENA is ON and DP is above setpoint (with an ELSE that resets the timer to 0 for any other combination -- ENA off, or DP at/below setpoint), and a second, independent IF that raises the DP alarm whenever ENA is ON and DP stays below setpoint. Keep the timer's reset condition broad -- it needs to fire whenever the accumulate-condition isn't true, not just when ENA drops out.",
            "checks": [
                {"force": {f"%X%{sys}.ENA": 1, f"%X%{sys}.CHW.DP": 10, f"%X%{sys}.CHW.DP.SP": 5, f"%X%{sys}.STGUP": 0}, "cycles": 301, "expect": {f"%X%{sys}.STG2.ENA": 1}, "note": "pressure stays above setpoint with chiller enabled past 300 seconds -- next stage should enable"},
                {"force": {f"%X%{sys}.ENA": 1, f"%X%{sys}.CHW.DP": 10, f"%X%{sys}.CHW.DP.SP": 5, f"%X%{sys}.STGUP": 0, f"%X%{sys}.STG2.ENA": 0}, "cycles": 50, "expect": {f"%X%{sys}.STG2.ENA": 0}, "note": "timer hasn't reached 300 seconds yet -- next stage should not have enabled"},
                {"force": {f"%X%{sys}.ENA": 1, f"%X%{sys}.CHW.DP": 3, f"%X%{sys}.CHW.DP.SP": 5}, "expect": {f"%X%{sys}.ALM": 1, f"%X%{sys}.STGUP": 0}, "note": "DP stays below setpoint while enabled -- alarm should trip and the stage timer resets"},
                {"force": {f"%X%{sys}.ENA": 0, f"%X%{sys}.CHW.DP": 10, f"%X%{sys}.CHW.DP.SP": 5}, "cycles": 5, "expect": {f"%X%{sys}.STGUP": 0}, "note": "chiller not enabled -- stage timer stays reset regardless of pressure"}
            ]
        }
    },
    {
        "title": "Chilled Water Sequence Change & Flag Management",
        "difficulty": "medium",
        "multiLine": True,
        "systems": CHILLER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Sequence Change & Flag Reset",
            "prompt": f"If a Sequence Alarm (\"%X%{sys}.SEQALM\") is equal to ON, latch the current Active Sequence Command (\"%X%{sys}.SEQCMD\") into local variable $LOC1 so its value at the moment of the fault is captured before anything changes it on the next scan. Separately, whenever Master Enable (\"%X%{sys}.ENA\") is OFF, reset the Stage Timer (\"%X%{sys}.STGUP\") to 0 so staging always starts from zero the next time the plant is enabled. SEQALM, SEQCMD, and STGUP are all virtual points.",
            "points": [
                {"name": f"%X%{sys}.SEQALM",
                    "desc": "Sequence Fault Alarm (virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.SEQCMD",
                    "desc": "Active Sequence Command (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.STGUP",
                    "desc": "Stage Timer Accumulator (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.ENA", "desc": "Chiller Master Enable", "type": "BV", "kind": "physical"},
                {"name": "$LOC1", "desc": "Local Variable", "type": "AV", "kind": "local"}
            ],
            "help": "Two unrelated IF blocks, not one combined condition -- capturing SEQCMD into $LOC1 when SEQALM trips has nothing to do with resetting STGUP when ENA is OFF. Neither one needs an ELSE: the prompt never says what should happen to $LOC1 when SEQALM is OFF, or to STGUP when ENA is ON, so leave those points untouched outside their own trigger condition.",
            "checks": [
                {"force": {f"%X%{sys}.SEQALM": 1, f"%X%{sys}.SEQCMD": 42, "$LOC1": 0}, "expect": {"$LOC1": 42}, "note": "sequence alarm trips -- current active sequence command should be captured into $LOC1"},
                {"force": {f"%X%{sys}.ENA": 0, f"%X%{sys}.STGUP": 150}, "expect": {f"%X%{sys}.STGUP": 0}, "note": "master enable OFF -- stage timer resets to 0"},
                {"force": {f"%X%{sys}.ENA": 1, f"%X%{sys}.STGUP": 150}, "expect": {f"%X%{sys}.STGUP": 150}, "note": "master enable ON -- stage timer left alone (only resets while ENA is OFF)"}
            ]
        }
    },
    {
        "title": "FCU Multi-Speed Fan Control",
        "difficulty": "medium",
        "multiLine": True,
        "systems": FCU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Multi-Speed Fan Control",
            "prompt": f"If Room Temperature (\"%X%{sys}.RMT\") exceeds Cooling Setpoint (\"%X%{sys}.RMT.SP\") by more than 3 degrees, run the fan on High Speed (\"%X%{sys}.FAN.HI\") and keep Low Speed (\"%X%{sys}.FAN.LO\") OFF. If it exceeds Setpoint by any smaller amount (up to 3 degrees), run on Low Speed and keep High Speed OFF. Otherwise, turn both speed outputs OFF.",
            "points": [
                {"name": f"%X%{sys}.RMT",
                    "desc": "Room Temperature Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.RMT.SP",
                    "desc": "Cooling Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.FAN.HI",
                    "desc": "Fan High Speed Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.FAN.LO",
                    "desc": "Fan Low Speed Command", "type": "DO", "kind": "physical"}
            ],
            "help": "A three-way split written as a chain: IF (RMT minus RMT.SP) .GT. 3 THEN high speed, ELSE IF (RMT minus RMT.SP) .GT. 0 THEN low speed, ELSE both off. Check the bigger threshold (greater than 3) first -- if you check .GT. 0 first, temperatures more than 3 degrees over setpoint would incorrectly fall into the low-speed branch too.",
            "checks": [
                {"force": {f"%X%{sys}.RMT": 80, f"%X%{sys}.RMT.SP": 75}, "expect": {f"%X%{sys}.FAN.HI": 1, f"%X%{sys}.FAN.LO": 0}, "note": "5 degrees over setpoint (more than 3) -- high speed"},
                {"force": {f"%X%{sys}.RMT": 77, f"%X%{sys}.RMT.SP": 75}, "expect": {f"%X%{sys}.FAN.LO": 1, f"%X%{sys}.FAN.HI": 0}, "note": "2 degrees over setpoint (within 3) -- low speed"},
                {"force": {f"%X%{sys}.RMT": 73, f"%X%{sys}.RMT.SP": 75}, "expect": {f"%X%{sys}.FAN.HI": 0, f"%X%{sys}.FAN.LO": 0}, "note": "at or under setpoint -- both speeds off"}
            ]
        }
    },
    {
        "title": "Cooling Tower Fan Enable by Condenser Water Temp",
        "difficulty": "medium",
        "multiLine": True,
        "systems": CT_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Fan Enable",
            "prompt": f"If Condenser Water Supply Temp (\"%X%{sys}.CWS.T\") is greater than High Limit (\"%X%{sys}.CWS.T.HI\"), turn ON Tower Fan Enable (\"%X%{sys}.FAN.ENA\") and set Tower Fan Speed Output (\"%X%{sys}.FAN.SPD\") to 100. Otherwise, turn OFF Tower Fan Enable and set Tower Fan Speed Output to 0. CWS.T.HI is a virtual point.",
            "points": [
                {"name": f"%X%{sys}.CWS.T",
                    "desc": "Condenser Water Supply Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.CWS.T.HI",
                    "desc": "Condenser Water High Limit (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.FAN.ENA",
                    "desc": "Tower Fan Enable", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}.FAN.SPD",
                    "desc": "Tower Fan Speed Output (0-100%)", "type": "AO", "kind": "physical"}
            ],
            "help": "Two writes belong in each branch: FAN.ENA and FAN.SPD move together, ON/100 in the THEN and OFF/0 in the ELSE. It's the same single .GT. comparison shape as the basin heater and cooling-valve scenarios, just driving two outputs instead of one.",
            "checks": [
                {"force": {f"%X%{sys}.CWS.T": 90, f"%X%{sys}.CWS.T.HI": 85}, "expect": {f"%X%{sys}.FAN.ENA": 1, f"%X%{sys}.FAN.SPD": 100}, "note": "condenser water over the limit -- fan on at full speed"},
                {"force": {f"%X%{sys}.CWS.T": 78, f"%X%{sys}.CWS.T.HI": 85}, "expect": {f"%X%{sys}.FAN.ENA": 0, f"%X%{sys}.FAN.SPD": 0}, "note": "condenser water under the limit -- fan off"}
            ]
        }
    },
    {
        "title": "Condenser Water High Temp Alarm with Time Delay (GOTO)",
        "difficulty": "medium",
        "multiLine": True,
        "systems": CT_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Condenser Water Alarm (Delayed)",
            "prompt": f"Write this the way a real Siemens panel dump does it: a GOTO branch instead of one big IF/THEN/ELSE. If Condenser Water Supply Temp (\"%X%{sys}.CWS.T\") is greater than High Limit (\"%X%{sys}.CWS.T.HI\"), GOTO a higher-numbered alarm-timer block. Otherwise (on the lines right after that check), reset an Alarm Delay Timer (\"%X%{sys}.CWS.T.ALM.TMR\") to 0, turn a High Condenser Water Temp Alarm (\"%X%{sys}.CWS.T.ALM\") OFF, then GOTO past the alarm-timer block entirely. In the alarm-timer block, use SAMPLE(1) IF (\"%X%{sys}.CWS.T\" .GT. \"%X%{sys}.CWS.T.HI\") THEN ... to accumulate the Alarm Delay Timer by 1 each second, then turn ON the alarm once the timer exceeds 45 seconds -- leave the alarm alone otherwise in that block, since dropping back below the limit (handled by the earlier GOTO branch) is what clears it. CWS.T.HI, CWS.T.ALM.TMR, and CWS.T.ALM are all virtual points. Remember: GOTO only ever jumps to a HIGHER line number than the line it's on.",
            "points": [
                {"name": f"%X%{sys}.CWS.T",
                    "desc": "Condenser Water Supply Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.CWS.T.HI",
                    "desc": "Condenser Water High Limit (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.CWS.T.ALM.TMR",
                    "desc": "Alarm Delay Timer Accumulator (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.CWS.T.ALM",
                    "desc": "High Condenser Water Temp Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Same GOTO/timer shape as the discharge-air-temp delayed alarm -- lay lines out top to bottom (main check, reset-and-skip branch, then the timer block last, since GOTO only jumps forward). Use SAMPLE(1) IF (...) THEN inside the timer block so it accumulates once per scan, and give the reset branch its own GOTO past the timer block so it doesn't immediately re-accumulate on the same scan.",
            "checks": [
                {"force": {f"%X%{sys}.CWS.T": 95, f"%X%{sys}.CWS.T.HI": 85, f"%X%{sys}.CWS.T.ALM.TMR": 0}, "cycles": 46, "expect": {f"%X%{sys}.CWS.T.ALM": 1}, "note": "over the limit for more than 45 scans -- alarm latches ON"},
                {"force": {f"%X%{sys}.CWS.T": 95, f"%X%{sys}.CWS.T.HI": 85, f"%X%{sys}.CWS.T.ALM.TMR": 0, f"%X%{sys}.CWS.T.ALM": 0}, "cycles": 5, "expect": {f"%X%{sys}.CWS.T.ALM": 0}, "note": "over the limit but under 45 seconds -- alarm hasn't tripped yet"},
                {"force": {f"%X%{sys}.CWS.T": 70, f"%X%{sys}.CWS.T.HI": 85}, "cycles": 10, "expect": {f"%X%{sys}.CWS.T.ALM": 0, f"%X%{sys}.CWS.T.ALM.TMR": 0}, "note": "back under the limit -- timer resets and alarm stays OFF"}
            ]
        }
    },
    {
        "title": "Air Compressor Header Pressure Load/Unload Control",
        "difficulty": "medium",
        "multiLine": True,
        "systems": AC_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Header Pressure Load/Unload Control",
            "prompt": f"{sys} uses classic load/unload staging to hold discharge header pressure in a band rather than cycling the whole compressor on and off. If Header Pressure (\"%X%{sys}.HDR.PSI\") drops below the Load Setpoint (\"%X%{sys}.HDR.PSI.LO\"), turn ON the Load Solenoid (\"%X%{sys}.LOAD.SOL\") so the compressor starts pumping air into the header again. If Header Pressure rises above the Unload Setpoint (\"%X%{sys}.HDR.PSI.HI\"), turn OFF the Load Solenoid so the compressor unloads (runs unloaded/idles rather than continuing to add air). Leave the Load Solenoid in whatever state it's already in while Header Pressure is between the two setpoints -- don't write to it at all in that band, since that's exactly what creates the deadband and keeps the compressor from short-cycling right at one threshold. HDR.PSI.LO and HDR.PSI.HI are virtual points.",
            "points": [
                {"name": f"%X%{sys}.HDR.PSI",
                    "desc": "Discharge Header Pressure Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.HDR.PSI.LO",
                    "desc": "Load Setpoint -- Header Pressure below this loads the compressor (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.HDR.PSI.HI",
                    "desc": "Unload Setpoint -- Header Pressure above this unloads the compressor (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.LOAD.SOL",
                    "desc": "Load Solenoid Command", "type": "DO", "kind": "physical"}
            ],
            "help": "This is a deadband, not an IF/THEN/ELSE -- write two separate IF blocks (one for the LO threshold turning the solenoid ON, one for the HI threshold turning it OFF), each with no ELSE at all. Adding an ELSE to either one would write to LOAD.SOL on every single scan even while pressure sits in the middle of the band, which defeats the point of having a deadband in the first place.",
            "checks": [
                {"force": {f"%X%{sys}.HDR.PSI": 60, f"%X%{sys}.HDR.PSI.LO": 70, f"%X%{sys}.HDR.PSI.HI": 90, f"%X%{sys}.LOAD.SOL": 0}, "expect": {f"%X%{sys}.LOAD.SOL": 1}, "note": "pressure below load setpoint -- solenoid should turn ON"},
                {"force": {f"%X%{sys}.HDR.PSI": 100, f"%X%{sys}.HDR.PSI.LO": 70, f"%X%{sys}.HDR.PSI.HI": 90, f"%X%{sys}.LOAD.SOL": 1}, "expect": {f"%X%{sys}.LOAD.SOL": 0}, "note": "pressure above unload setpoint -- solenoid should turn OFF"},
                {"force": {f"%X%{sys}.HDR.PSI": 80, f"%X%{sys}.HDR.PSI.LO": 70, f"%X%{sys}.HDR.PSI.HI": 90, f"%X%{sys}.LOAD.SOL": 1}, "expect": {f"%X%{sys}.LOAD.SOL": 1}, "note": "pressure inside the deadband -- solenoid should be left alone (stays ON)"},
                {"force": {f"%X%{sys}.HDR.PSI": 80, f"%X%{sys}.HDR.PSI.LO": 70, f"%X%{sys}.HDR.PSI.HI": 90, f"%X%{sys}.LOAD.SOL": 0}, "expect": {f"%X%{sys}.LOAD.SOL": 0}, "note": "pressure inside the deadband -- solenoid should be left alone (stays OFF)"}
            ]
        }
    },

    # ---------------- HARD ----------------
    {
        "title": "Filter Differential Pressure Subroutine Check",
        "difficulty": "medium",
        "multiLine": True,
        "systems": AHU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Filter DP Monitoring Routine",
            "prompt": f"Every 60 seconds (using SAMPLE), jump to Subroutine 5000 (GOSUB). In the subroutine, check if Filter Differential Pressure (\"%X%{sys}.FLT.DP\") is greater than High Threshold (\"%X%{sys}.FLT.DP.SP\"). If true, turn ON a Filter Alarm (\"%X%{sys}.FLT.ALM\") -- a virtual point -- store the current DP in local variable $LOC1, and return with RETURN.",
            "points": [
                {"name": f"%X%{sys}.FLT.DP",
                    "desc": "Filter Differential Pressure", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.FLT.DP.SP",
                    "desc": "Filter DP High Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.FLT.ALM",
                    "desc": "Dirty Filter Alarm (virtual point)", "type": "BV", "kind": "virtual"},
                {"name": "$LOC1", "desc": "Local Scratchpad Variable", "type": "AV", "kind": "local"}
            ],
            "help": "SAMPLE(60) IF (...) THEN GOSUB 5000 fires the subroutine call once every 60 scans, not every scan -- don't confuse this with the SAMPLE(1) timer-accumulator pattern used in delayed-alarm scenarios elsewhere. Inside the subroutine, end with RETURN so execution resumes right after the GOSUB line. The prompt never says to clear FLT.ALM on a good reading, so this alarm is a latch -- only write to it (and $LOC1) when the DP check comes back true.",
            "checks": [
                {"force": {f"%X%{sys}.FLT.DP": 6, f"%X%{sys}.FLT.DP.SP": 5, f"%X%{sys}.FLT.ALM": 0}, "cycles": 61, "expect": {f"%X%{sys}.FLT.ALM": 1, "$LOC1": 6}, "note": "DP over setpoint when the 60-second subroutine call fires -- alarm sets and DP is captured into $LOC1"},
                {"force": {f"%X%{sys}.FLT.DP": 3, f"%X%{sys}.FLT.DP.SP": 5, f"%X%{sys}.FLT.ALM": 0}, "cycles": 61, "expect": {f"%X%{sys}.FLT.ALM": 0}, "note": "DP under setpoint when the subroutine runs -- alarm should stay OFF"}
            ]
        }
    },
    {
        "title": "Variable Speed Pump Differential Pressure PID Loop",
        "difficulty": "expert",
        "multiLine": False,
        "systems": PUMP_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Pump Speed DP Control",
            "prompt": f"When Pump Enable (\"%X%{sys}.ENA\") is ON, execute a LOOP statement to modulate Pump Speed Output (\"%X%{sys}.SPD\") (0-100%) to maintain System Differential Pressure (\"%X%{sys}.DP\") at Setpoint (\"%X%{sys}.DP.SP\"). If System Differential Pressure falls below half of Setpoint, turn ON a Low Pressure Fault Alarm (\"%X%{sys}.ALM\") -- a virtual point.",
            "points": [
                {"name": f"%X%{sys}.ENA",
                    "desc": "Pump Enable Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.DP",
                    "desc": "System Differential Pressure", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.DP.SP",
                    "desc": "Differential Pressure Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.SPD",
                    "desc": "Pump Speed Control Output (0-100%)", "type": "AO", "kind": "physical"},
                {"name": f"%X%{sys}.ALM",
                    "desc": "Low Pressure Fault Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "A real LOOP call takes 12 positional arguments in this exact order: LOOP(type, pv, cv, sp, pg, ig, dg, st, bias, lo, hi, 0). Use: type=128 (reverse acting -- speed must increase as pressure falls below setpoint, the opposite direction of the process variable), pv=\"%X%{sys}.DP\", cv=\"%X%{sys}.SPD\", sp=\"%X%{sys}.DP.SP\", P Gain (pg)=5.0, I Gain (ig)=0.2, D Gain (dg)=0.0, sample time (st)=1, bias=50.0 (mid-range output when DP is exactly at setpoint), low limit (lo)=0, high limit (hi)=100, and the final parameter is always 0 (unused). Wrap the LOOP call in an IF on Pump Enable so it only runs while the pump is on. The fault check is a separate, ordinary IF/THEN comparing DP against half of DP.SP -- it doesn't need the LOOP at all.",
            "checks": [
                {"force": {f"%X%{sys}.ENA": 1, f"%X%{sys}.DP": 20, f"%X%{sys}.DP.SP": 100}, "expect": {f"%X%{sys}.ALM": 1}, "note": "DP has fallen below half of setpoint (20 < 50) -- low pressure fault should trip"},
                {"force": {f"%X%{sys}.ENA": 1, f"%X%{sys}.DP": 80, f"%X%{sys}.DP.SP": 100}, "expect": {f"%X%{sys}.ALM": 0}, "note": "DP is above half of setpoint -- no fault"}
            ]
        }
    },
    {
        "title": "Air Compressor PID Bypass Valve Control",
        "difficulty": "expert",
        "multiLine": False,
        "systems": AC_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Water-Cooled Bypass Valve Control",
            "prompt": f"Air Compressor {sys} is water-cooled: cooling water flows through the compressor's heat exchanger, and a three-way bypass valve mixes cooled return water with warm bypassed water to hold a stable Compressor Water Entering Temp (\"%X%{sys}.CHW.ET\") at Setpoint (\"%X%{sys}.CHW.ET.SP\") -- too cold risks condensation damage inside the compressor, too warm hurts cooling performance. Whenever the compressor is running (Compressor Run Status \"%X%{sys}.STAT\" is ON), execute a LOOP statement to modulate the Bypass Valve Output (\"%X%{sys}.BYP.VLV\") (0-100%) to hold Entering Temp at Setpoint. When the compressor is not running, hold the Bypass Valve Output at 0.",
            "points": [
                {"name": f"%X%{sys}.STAT",
                    "desc": "Compressor Run Status", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.CHW.ET",
                    "desc": "Compressor Cooling Water Entering Temp", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.CHW.ET.SP",
                    "desc": "Cooling Water Entering Temp Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.BYP.VLV",
                    "desc": "Three-Way Bypass Valve Control Output (0-100%)", "type": "AO", "kind": "physical"}
            ],
            "help": "A real LOOP call takes 12 positional arguments in this exact order: LOOP(type, pv, cv, sp, pg, ig, dg, st, bias, lo, hi, 0). Use: type=0 (direct acting -- valve output must increase as entering temp rises above setpoint, the same direction as the process variable), pv=\"%X%{sys}.CHW.ET\", cv=\"%X%{sys}.BYP.VLV\", sp=\"%X%{sys}.CHW.ET.SP\", P Gain (pg)=4.0, I Gain (ig)=0.3, D Gain (dg)=0.0, sample time (st)=1, bias=50.0 (mid-range output when Entering Temp is exactly at setpoint), low limit (lo)=0, high limit (hi)=100, and the final parameter is always 0 (unused). Wrap the LOOP call in an IF on STAT so it only runs while the compressor is running; the ELSE just needs to hold BYP.VLV at 0.",
            "checks": [
                {"force": {f"%X%{sys}.STAT": 0, f"%X%{sys}.CHW.ET": 75, f"%X%{sys}.CHW.ET.SP": 65}, "expect": {f"%X%{sys}.BYP.VLV": 0}, "note": "compressor off -- bypass valve forced to 0 regardless of temperature"},
                {"force": {f"%X%{sys}.STAT": 0, f"%X%{sys}.CHW.ET": 55, f"%X%{sys}.CHW.ET.SP": 65}, "expect": {f"%X%{sys}.BYP.VLV": 0}, "note": "compressor off -- bypass valve forced to 0 even if temp is below setpoint"}
            ]
        }
    },
    {
        "title": "Occupied/Unoccupied Fan Scheduling with Optimal Start",
        "difficulty": "medium",
        "multiLine": True,
        "systems": AHU_SYSTEMS + MAU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Occupied Fan Scheduling",
            "prompt": f"Supply Fan operation at {sys} should follow the building's occupancy mode, tracked by a single Building State point (\"%X%BLDG.OCC.STATE\") where 0 = Occupied and 1 = Unoccupied, with an allowance for optimal start: a separate scheduling program sets Optimal Start Command (\"%X%{sys}.OPT.START\") ON up to 2 hours before scheduled occupancy, telling this sequence to start the fan early to pre-condition the space. Drive Supply Fan Enable (\"%X%{sys}.SF.ENA\") ON whenever Building State is Occupied (0) OR Optimal Start Command is ON, and OFF otherwise.",
            "points": [
                {"name": "%X%BLDG.OCC.STATE",
                    "desc": "Building-wide Occupancy Mode (0 = Occupied, 1 = Unoccupied) -- a virtual point driven by a scheduling program", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.OPT.START",
                    "desc": "Optimal Start Command (driven by a separate scheduling program)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}.SF.ENA",
                    "desc": "Supply Fan Enable Command (already exists -- your logic must write to this point)", "type": "DO", "kind": "physical"}
            ],
            "help": "OR together the two conditions -- occupied OR optimal-start commanded -- in one IF, since both call for the same action (fan ON). BLDG.OCC.STATE is an analog point holding 0 or 1, so compare it with .EQ. 0 rather than treating it like a digital point.",
            "checks": [
                {"force": {"%X%BLDG.OCC.STATE": 0, f"%X%{sys}.OPT.START": 0}, "expect": {f"%X%{sys}.SF.ENA": 1}, "note": "occupied -- fan should be on"},
                {"force": {"%X%BLDG.OCC.STATE": 1, f"%X%{sys}.OPT.START": 1}, "expect": {f"%X%{sys}.SF.ENA": 1}, "note": "unoccupied but optimal start commanded -- fan should start early"},
                {"force": {"%X%BLDG.OCC.STATE": 1, f"%X%{sys}.OPT.START": 0}, "expect": {f"%X%{sys}.SF.ENA": 0}, "note": "unoccupied and no optimal start -- fan should be off"}
            ]
        }
    },
    {
        "title": "Hot Water Supply Temperature Reset",
        "difficulty": "expert",
        "multiLine": True,
        "systems": HWP_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Hot Water Supply Temperature Reset",
            "prompt": f"The Hot Water Supply Setpoint at {sys} should reset based on Outside Air Temp (\"%X%{sys}.OAT\") to save energy. Use these fixed boundaries directly in your logic: at 20 degrees OAT or colder, Hot Water Supply Setpoint (\"%X%{sys}.HW.SP\") should be 180 degrees. At 60 degrees OAT or warmer, it should be 140 degrees. Between 20 and 60 degrees, calculate a proportional (linear) reset between those two values based on where Outside Air Temp falls in that range.",
            "points": [
                {"name": f"%X%{sys}.OAT",
                    "desc": "Outside Air Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.HW.SP",
                    "desc": "Hot Water Supply Setpoint (already exists -- your logic must write to this point)", "type": "AV", "kind": "physical"}
            ],
            "help": "This is a linear reset curve between two fixed points (20 deg OAT to 180 deg HW.SP, and 60 deg OAT to 140 deg HW.SP) -- the real PPCL tool for this is the TABLE() function (see the Learn PPCL tab's TABLE() section for the exact syntax), which interpolates between breakpoints for you. If you'd rather hand-roll the math: clamp OAT to the 20-60 range first, then the slope between the two anchor points is (140-180)/(60-20) = -1 degree of setpoint per degree of OAT above 20. Whichever approach you use, all three cases -- below 20, above 60, and in between -- need to land on the right setpoint.",
            "checks": [
                {"force": {f"%X%{sys}.OAT": 10}, "expect": {f"%X%{sys}.HW.SP": 180}, "note": "OAT colder than 20 -- HW setpoint pinned at 180"},
                {"force": {f"%X%{sys}.OAT": 70}, "expect": {f"%X%{sys}.HW.SP": 140}, "note": "OAT warmer than 60 -- HW setpoint pinned at 140"},
                {"force": {f"%X%{sys}.OAT": 40}, "expect": {f"%X%{sys}.HW.SP": 160}, "tolerance": 1, "note": "OAT halfway between 20 and 60 -- HW setpoint should be linearly interpolated to 160"}
            ]
        }
    },
    {
        "title": "Lead-Lag Pump Rotation on Failure",
        "difficulty": "medium",
        "multiLine": True,
        "systems": PUMP_PLANT_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{PLANT_LABELS.get(sys, sys)} Lead-Lag Rotation",
            "prompt": f"Two pumps in the {sys} plant ({sys}.P1 and {sys}.P2) share standby duty: one is normally lead, the other lag (backup). If the lead pump develops a fault -- Pump 1 Fault (\"%X%{sys}.P1.FLT\") if {sys}.P1 is currently lead, or Pump 2 Fault (\"%X%{sys}.P2.FLT\") if {sys}.P2 is currently lead -- the other pump should automatically take over as lead. That state should persist (it should not automatically swap back once the fault clears, to avoid short-cycling equipment through a bouncing fault). Drive Pump 1 Enable (\"%X%{sys}.P1.ENA\") and Pump 2 Enable (\"%X%{sys}.P2.ENA\") accordingly, using a Lead-Lag Status point (\"%X%{sys}.LL.STAT\", 1 = Pump 1 lead, 2 = Pump 2 lead) to track which pump is currently lead.",
            "points": [
                {"name": f"%X%{sys}.P1.FLT",
                    "desc": "Pump 1 Fault (fault relay contact from the pump's own VFD/starter)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}.P2.FLT",
                    "desc": "Pump 2 Fault (fault relay contact from the pump's own VFD/starter)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}.P1.ENA",
                    "desc": "Pump 1 Enable Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.P2.ENA",
                    "desc": "Pump 2 Enable Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.LL.STAT",
                    "desc": "Lead-Lag Status Code (virtual point)", "type": "AV", "kind": "virtual"}
            ],
            "help": "Track lead/lag purely off LL.STAT, not off which fault is active -- when LL.STAT is 1 and P1.FLT trips, flip LL.STAT to 2 and swap the enables; when LL.STAT is 2 and P2.FLT trips, flip back to 1. Neither swap condition should also check the other pump's fault, and there's no condition anywhere that swaps LL.STAT back just because a fault clears -- that's what makes the rotation persistent instead of bouncing.",
            "checks": [
                {"force": {f"%X%{sys}.LL.STAT": 1, f"%X%{sys}.P1.FLT": 0, f"%X%{sys}.P2.FLT": 0}, "expect": {f"%X%{sys}.P1.ENA": 1, f"%X%{sys}.P2.ENA": 0, f"%X%{sys}.LL.STAT": 1}, "note": "P1 lead and healthy -- P1 runs, P2 stands by, status unchanged"},
                {"force": {f"%X%{sys}.LL.STAT": 1, f"%X%{sys}.P1.FLT": 1, f"%X%{sys}.P2.FLT": 0}, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}.P1.ENA": 0, f"%X%{sys}.P2.ENA": 1}, "note": "lead pump (P1) faults -- P2 takes over as lead"},
                {"force": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}.P1.FLT": 0, f"%X%{sys}.P2.FLT": 0, f"%X%{sys}.P1.ENA": 0, f"%X%{sys}.P2.ENA": 1}, "cycles": 3, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}.P2.ENA": 1, f"%X%{sys}.P1.ENA": 0}, "note": "P2 already lead with no active faults -- stays lead, no automatic swap back to P1"}
            ]
        }
    },
    {
        "title": "Lead-Lag Chiller Failure Rotation",
        "difficulty": "medium",
        "multiLine": True,
        "systems": CHILLER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Lead-Lag Chiller Failure Rotation",
            "prompt": f"Two chillers, {sys}1 and {sys}2, share standby duty: one is normally lead, the other lag (backup). If the Lead Chiller Fault Alarm (\"%X%{sys}.LEAD.FLT\") is ON, the lag chiller should immediately take over as lead: turn ON Chiller 2 Enable (\"%X%{sys}2.ENA\"), turn OFF Chiller 1 Enable (\"%X%{sys}1.ENA\"), and set Lead-Lag Status (\"%X%{sys}.LL.STAT\") to 2, resetting a Fault-Clear Timer (\"%X%{sys}.FLT.CLR.TMR\") to 0. While Lead-Lag Status is 2 and the Fault Alarm is OFF, accumulate the Fault-Clear Timer by 1 second using SAMPLE(1); once it exceeds 300 seconds (5 minutes proven clear), restore normal operation -- turn ON Chiller 1 Enable, turn OFF Chiller 2 Enable, and set Lead-Lag Status back to 1. This way the rotation doesn't bounce back the instant the fault first clears.",
            "points": [
                {"name": f"%X%{sys}.LEAD.FLT",
                    "desc": "Lead Chiller Fault Alarm (fault relay contact from whichever chiller is currently lead)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}1.ENA",
                    "desc": "Chiller 1 Enable", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}2.ENA",
                    "desc": "Chiller 2 Enable", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.LL.STAT",
                    "desc": "Lead-Lag Status Code (virtual point, 1 = Chiller 1 lead, 2 = Chiller 2 lead)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.FLT.CLR.TMR",
                    "desc": "Fault-Clear Timer Accumulator (virtual point)", "type": "AV", "kind": "virtual"}
            ],
            "help": "Three pieces: an immediate takeover IF (LEAD.FLT trips -> swap to chiller 2, reset the timer), a SAMPLE(1) timer that only accumulates while LL.STAT is 2 AND LEAD.FLT is OFF, and a restore-to-normal IF once that timer exceeds 300. Keep the timer's accumulate-condition and the initial takeover condition separate -- the timer should NOT accumulate while the fault is still active, only once it's been continuously clear.",
            "checks": [
                {"force": {f"%X%{sys}.LEAD.FLT": 1, f"%X%{sys}.LL.STAT": 1}, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}1.ENA": 0, f"%X%{sys}2.ENA": 1, f"%X%{sys}.FLT.CLR.TMR": 0}, "note": "lead chiller faults -- chiller 2 takes over immediately and the fault-clear timer resets"},
                {"force": {f"%X%{sys}.LEAD.FLT": 0, f"%X%{sys}.LL.STAT": 2, f"%X%{sys}.FLT.CLR.TMR": 0}, "cycles": 100, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}2.ENA": 1}, "note": "fault-clear timer running but hasn't reached 300 seconds yet -- stays on chiller 2"},
                {"force": {f"%X%{sys}.LEAD.FLT": 0, f"%X%{sys}.LL.STAT": 2, f"%X%{sys}.FLT.CLR.TMR": 0}, "cycles": 301, "expect": {f"%X%{sys}.LL.STAT": 1, f"%X%{sys}1.ENA": 1, f"%X%{sys}2.ENA": 0}, "note": "fault stayed clear for over 300 seconds -- rotation restores back to chiller 1"}
            ]
        }
    },
    {
        "title": "Boiler N+1 Standby Rotation on Failure",
        "difficulty": "medium",
        "multiLine": True,
        "systems": BOILER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Boiler Standby Rotation",
            "prompt": f"Two boilers, {sys}1 and {sys}2, provide N+1 redundancy -- normally only one runs, the other sits as a spare. If the Lead Boiler Fault Alarm (\"%X%{sys}.LEAD.FLT\") is ON, the standby boiler should immediately take over: turn ON Boiler 2 Enable (\"%X%{sys}2.ENA\"), turn OFF Boiler 1 Enable (\"%X%{sys}1.ENA\"), and set Lead-Lag Status (\"%X%{sys}.LL.STAT\") to 2. That state should persist -- it should not automatically swap back once the fault clears, to avoid short-cycling equipment through a bouncing fault.",
            "points": [
                {"name": f"%X%{sys}.LEAD.FLT",
                    "desc": "Lead Boiler Fault Alarm (fault relay contact from whichever boiler is currently running)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}1.ENA",
                    "desc": "Boiler 1 Enable", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}2.ENA",
                    "desc": "Boiler 2 Enable", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.LL.STAT",
                    "desc": "Lead-Lag Status Code (virtual point, 1 = Boiler 1 lead, 2 = Boiler 2 lead)", "type": "AV", "kind": "virtual"}
            ],
            "help": "One IF, no ELSE: only write to LL.STAT/BLR1.ENA/BLR2.ENA when LEAD.FLT trips. Leaving out the ELSE (rather than writing one that would swap back to boiler 1) is exactly what makes the rotation persist instead of bouncing back the instant the fault clears.",
            "checks": [
                {"force": {f"%X%{sys}.LEAD.FLT": 1, f"%X%{sys}.LL.STAT": 1, f"%X%{sys}1.ENA": 1, f"%X%{sys}2.ENA": 0}, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}1.ENA": 0, f"%X%{sys}2.ENA": 1}, "note": "lead boiler faults -- boiler 2 takes over, status flips to 2"},
                {"force": {f"%X%{sys}.LEAD.FLT": 0, f"%X%{sys}.LL.STAT": 2, f"%X%{sys}1.ENA": 0, f"%X%{sys}2.ENA": 1}, "cycles": 5, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}2.ENA": 1}, "note": "fault clears -- boiler 2 stays lead, no automatic swap back"}
            ]
        }
    },
    {
        "title": "CRAC Standby Unit Failover",
        "difficulty": "medium",
        "multiLine": True,
        "systems": CRAC_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys}201/202 CRAC Standby Failover",
            "prompt": f"{sys}201 and {sys}202 form an N+1 redundant pair serving the same room: normally one runs as lead and the other sits in standby. If {sys}201's Alarm (\"%X%{sys}201.ALM\") is ON while {sys}201 is currently lead, or {sys}202's Alarm (\"%X%{sys}202.ALM\") is ON while {sys}202 is currently lead, the other unit should take over: enable the healthy unit, disable the alarmed one, and update Lead-Lag Status (\"%X%{sys}.LL.STAT\", 1 = {sys}201 lead, 2 = {sys}202 lead) accordingly. That state should persist -- don't automatically swap back once the alarm clears.",
            "points": [
                {"name": f"%X%{sys}201.ALM",
                    "desc": "CRAC201 Alarm (high/low temp, high/low humidity, or airflow fault)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}202.ALM",
                    "desc": "CRAC202 Alarm (high/low temp, high/low humidity, or airflow fault)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}201.ENA",
                    "desc": "CRAC201 Enable Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}202.ENA",
                    "desc": "CRAC202 Enable Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.LL.STAT",
                    "desc": "Lead-Lag Status Code (virtual point)", "type": "AV", "kind": "virtual"}
            ],
            "help": "Each failover condition needs to check BOTH which unit is alarming AND which unit is currently lead -- 201's alarm should only trigger a swap if LL.STAT says 201 is lead; if 202 is already lead, 201's alarm doesn't matter since it's already the standby doing nothing. Mirror the same AND logic for the 202 condition.",
            "checks": [
                {"force": {f"%X%{sys}201.ALM": 1, f"%X%{sys}202.ALM": 0, f"%X%{sys}.LL.STAT": 1, f"%X%{sys}201.ENA": 1, f"%X%{sys}202.ENA": 0}, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}201.ENA": 0, f"%X%{sys}202.ENA": 1}, "note": "201 (currently lead) alarms -- 202 takes over"},
                {"force": {f"%X%{sys}201.ALM": 0, f"%X%{sys}202.ALM": 1, f"%X%{sys}.LL.STAT": 2, f"%X%{sys}201.ENA": 0, f"%X%{sys}202.ENA": 1}, "expect": {f"%X%{sys}.LL.STAT": 1, f"%X%{sys}201.ENA": 1, f"%X%{sys}202.ENA": 0}, "note": "202 (currently lead) alarms -- 201 takes over"},
                {"force": {f"%X%{sys}201.ALM": 1, f"%X%{sys}202.ALM": 0, f"%X%{sys}.LL.STAT": 2, f"%X%{sys}201.ENA": 0, f"%X%{sys}202.ENA": 1}, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}202.ENA": 1}, "note": "201 alarms but it isn't currently lead (202 is) -- no failover needed, nothing changes"}
            ]
        }
    },
    {
        "title": "Chiller Compressor Staging",
        "difficulty": "medium",
        "multiLine": True,
        "systems": CHILLER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Compressor Staging",
            "prompt": f"If Chilled Water Supply Temp (\"%X%{sys}.CHW.ST\") stays above Setpoint (\"%X%{sys}.CHW.ST.SP\") continuously, accumulate a Load Timer (\"%X%{sys}.LOAD.TMR\") by 1 second using SAMPLE(1). Once the Load Timer exceeds 300 seconds, stage ON Compressor 2 (\"%X%{sys}.COMP2.ENA\"). If Chilled Water Supply Temp drops more than 2 degrees below Setpoint, stage OFF Compressor 2 and reset the Load Timer to 0. CHW.ST.SP and LOAD.TMR are virtual points.",
            "points": [
                {"name": f"%X%{sys}.CHW.ST",
                    "desc": "Chilled Water Supply Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.CHW.ST.SP",
                    "desc": "Chilled Water Supply Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.LOAD.TMR",
                    "desc": "Compressor Load Timer Accumulator (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.COMP2.ENA",
                    "desc": "Compressor 2 Stage Enable", "type": "DO", "kind": "physical"}
            ],
            "help": "The staging timer only accumulates while CHW.ST stays above setpoint (SAMPLE(1) inside that IF); the stage-off condition is a separate IF checking for more than 2 degrees below setpoint. Between those two thresholds -- at or slightly below setpoint but not yet 2 degrees under -- neither condition should be true, so just leave the timer and COMP2.ENA alone in that narrow band.",
            "checks": [
                {"force": {f"%X%{sys}.CHW.ST": 60, f"%X%{sys}.CHW.ST.SP": 52, f"%X%{sys}.LOAD.TMR": 0, f"%X%{sys}.COMP2.ENA": 0}, "cycles": 301, "expect": {f"%X%{sys}.COMP2.ENA": 1}, "note": "supply temp stays above setpoint for over 300 seconds -- compressor 2 stages on"},
                {"force": {f"%X%{sys}.CHW.ST": 60, f"%X%{sys}.CHW.ST.SP": 52, f"%X%{sys}.LOAD.TMR": 0, f"%X%{sys}.COMP2.ENA": 0}, "cycles": 50, "expect": {f"%X%{sys}.COMP2.ENA": 0}, "note": "timer hasn't reached 300 seconds yet -- compressor 2 should not have staged on"},
                {"force": {f"%X%{sys}.CHW.ST": 48, f"%X%{sys}.CHW.ST.SP": 52, f"%X%{sys}.LOAD.TMR": 200, f"%X%{sys}.COMP2.ENA": 1}, "expect": {f"%X%{sys}.COMP2.ENA": 0, f"%X%{sys}.LOAD.TMR": 0}, "note": "supply temp drops more than 2 degrees below setpoint -- compressor 2 stages off and load timer resets"}
            ]
        }
    },
    {
        "title": "AHU Supply Fan VFD Speed Control by Duct Static Pressure",
        "difficulty": "expert",
        "multiLine": True,
        "systems": AHU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Supply Fan VFD Speed Control",
            "prompt": f"Using a LOOP statement, modulate Supply Fan Speed Output (\"%X%{sys}.SF.SPD\") (0-100%) to maintain Duct Static Pressure (\"%X%{sys}.DSP\") at Setpoint (\"%X%{sys}.DSP.SP\"). If Duct Static Pressure exceeds 150% of Setpoint, turn ON a High Static Fault (\"%X%{sys}.DSP.FLT\") -- a virtual point.",
            "points": [
                {"name": f"%X%{sys}.SF.SPD",
                    "desc": "Supply Fan Speed Output (0-100%)", "type": "AO", "kind": "physical"},
                {"name": f"%X%{sys}.DSP",
                    "desc": "Duct Static Pressure Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.DSP.SP",
                    "desc": "Duct Static Pressure Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.DSP.FLT",
                    "desc": "High Static Fault (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "A real LOOP call takes 12 positional arguments in this exact order: LOOP(type, pv, cv, sp, pg, ig, dg, st, bias, lo, hi, 0). Use: type=128 (reverse acting -- fan speed must increase as static pressure falls below setpoint, the opposite direction of the process variable), pv=\"%X%{sys}.DSP\", cv=\"%X%{sys}.SF.SPD\", sp=\"%X%{sys}.DSP.SP\", P Gain (pg)=3.0, I Gain (ig)=0.15, D Gain (dg)=0.0, sample time (st)=1, bias=50.0 (mid-range output when DSP is exactly at setpoint), low limit (lo)=0, high limit (hi)=100, and the final parameter is always 0 (unused). The fault check is a separate, ordinary IF/THEN comparing DSP against 1.5 times DSP.SP -- it sits outside the LOOP call and doesn't need an ELSE unless you want it to self-clear.",
            "checks": [
                {"force": {f"%X%{sys}.DSP": 8, f"%X%{sys}.DSP.SP": 5}, "expect": {f"%X%{sys}.DSP.FLT": 1}, "note": "DSP is 160% of setpoint (over 150%) -- high static fault should trip"},
                {"force": {f"%X%{sys}.DSP": 6, f"%X%{sys}.DSP.SP": 5}, "expect": {f"%X%{sys}.DSP.FLT": 0}, "note": "DSP is 120% of setpoint (under 150%) -- no fault"}
            ]
        }
    },
    {
        "title": "Cooling Tower Fan VFD Sequence",
        "difficulty": "medium",
        "multiLine": True,
        "systems": CT_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Fan VFD Sequence",
            "prompt": f"If Condenser Water Supply Temp (\"%X%{sys}.CWS.T\") is greater than Setpoint (\"%X%{sys}.CWS.T.SP\"), increase Tower Fan Speed Output (\"%X%{sys}.FAN.SPD\") by 5 every 10 seconds using SAMPLE(10), up to a maximum of 100. If Condenser Water Supply Temp is more than 2 degrees below Setpoint, decrease Tower Fan Speed Output by 5 every 10 seconds, down to a minimum of 20. CWS.T.SP is a virtual point.",
            "points": [
                {"name": f"%X%{sys}.CWS.T",
                    "desc": "Condenser Water Supply Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.CWS.T.SP",
                    "desc": "Condenser Water Supply Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.FAN.SPD",
                    "desc": "Tower Fan Speed Output (0-100%)", "type": "AO", "kind": "physical"}
            ],
            "help": "SAMPLE(10) IF (...) THEN gates the +/-5 step to fire once every 10 scans, not every scan -- that's what turns it into a gradual ramp instead of one big jump. Clamp the result (MIN()/MAX(), or an extra IF) so FAN.SPD never exceeds 100 on the way up or drops below 20 on the way down. Between the two trigger conditions -- within 2 degrees of setpoint -- neither ramp should fire; leave FAN.SPD wherever it already is.",
            "checks": [
                {"force": {f"%X%{sys}.CWS.T": 90, f"%X%{sys}.CWS.T.SP": 85, f"%X%{sys}.FAN.SPD": 50}, "cycles": 15, "expect": {f"%X%{sys}.FAN.SPD": 55}, "note": "CWS.T above setpoint -- after the 10-second SAMPLE fires once, fan speed steps up by 5"},
                {"force": {f"%X%{sys}.CWS.T": 90, f"%X%{sys}.CWS.T.SP": 85, f"%X%{sys}.FAN.SPD": 98}, "cycles": 15, "expect": {f"%X%{sys}.FAN.SPD": 100}, "note": "fan speed increase clamps at 100 even though the +5 step would overshoot"},
                {"force": {f"%X%{sys}.CWS.T": 80, f"%X%{sys}.CWS.T.SP": 85, f"%X%{sys}.FAN.SPD": 50}, "cycles": 15, "expect": {f"%X%{sys}.FAN.SPD": 45}, "note": "CWS.T more than 2 degrees below setpoint -- fan speed steps down by 5"},
                {"force": {f"%X%{sys}.CWS.T": 80, f"%X%{sys}.CWS.T.SP": 85, f"%X%{sys}.FAN.SPD": 22}, "cycles": 15, "expect": {f"%X%{sys}.FAN.SPD": 20}, "note": "fan speed decrease clamps at a minimum of 20"},
                {"force": {f"%X%{sys}.CWS.T": 84, f"%X%{sys}.CWS.T.SP": 85, f"%X%{sys}.FAN.SPD": 50}, "cycles": 15, "expect": {f"%X%{sys}.FAN.SPD": 50}, "note": "CWS.T within the 2-degree deadband -- fan speed stays unchanged"}
            ]
        }
    },
    {
        "title": "AHU Safety Interlock & Occupancy Dispatch (GOTO Decision Tree)",
        "difficulty": "expert",
        "multiLine": True,
        "systems": AHU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Safety Interlock & Occupancy Dispatch",
            "prompt": f"Real Siemens AHU programs open with a decision tree: check safety trips first, then dispatch to the block for the current operating mode via GOTO -- exactly the architecture used in production APOGEE sequences. Build it here. Remember GOTO can only jump forward to a higher line number, never backward, so lay the blocks out in this order. First, check safety: if Supply Duct Smoke Detector (\"%X%{sys}.SSD\") is ON, GOTO line 02000 immediately, skipping mode logic entirely -- safety always overrides schedule. Otherwise, check Building Occupancy State (\"%X%BLDG.OCC.STATE\", 0 = Occupied, 1 = Unoccupied): if Occupied, GOTO line 01100; if Unoccupied, GOTO line 01200. At line 01100 (the Occupied block), turn ON Supply Fan Enable (\"%X%{sys}.SF.ENA\"), then GOTO line 03000 to end the scan. At line 01200 (the Unoccupied block), turn OFF Supply Fan Enable, then GOTO line 03000. At line 02000 (the Safety block), turn OFF Supply Fan Enable, then GOTO line 03000. Line 03000 should be a comment marking the end of the scan, and it needs to be the LAST line in your program -- every block above it ends with its own GOTO there so none of them can fall straight through into whatever comes next in the file.",
            "points": [
                {"name": f"%X%{sys}.SSD",
                    "desc": "Supply Duct Smoke Detector Trip", "type": "DI", "kind": "physical"},
                {"name": "%X%BLDG.OCC.STATE",
                    "desc": "Building-wide Occupancy Mode (0 = Occupied, 1 = Unoccupied) -- a virtual point driven by a scheduling program, shared across all equipment", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.SF.ENA",
                    "desc": "Supply Fan Enable Command (already exists -- your logic must write to this point)", "type": "DO", "kind": "physical"}
            ],
            "help": "This is a GOTO dispatch tree, not nested IF/THEN/ELSE -- the safety check comes first and jumps straight to the safety block (line 02000) with no further evaluation of occupancy at all when it's true. Each of the three blocks (Occupied, Unoccupied, Safety) needs to end with its own GOTO 03000, otherwise a block would fall straight through into the next one's code and re-execute it.",
            "checks": [
                {"force": {f"%X%{sys}.SSD": 1, "%X%BLDG.OCC.STATE": 0}, "expect": {f"%X%{sys}.SF.ENA": 0}, "note": "smoke detected -- safety overrides occupancy, fan forced off"},
                {"force": {f"%X%{sys}.SSD": 0, "%X%BLDG.OCC.STATE": 0}, "expect": {f"%X%{sys}.SF.ENA": 1}, "note": "no smoke, occupied -- fan on"},
                {"force": {f"%X%{sys}.SSD": 0, "%X%BLDG.OCC.STATE": 1}, "expect": {f"%X%{sys}.SF.ENA": 0}, "note": "no smoke, unoccupied -- fan off"}
            ]
        }
    },
    {
        "title": "AHU Multi-Mode Dispatch: Warmup / Cooldown / Occupied / Unoccupied (GOTO Decision Tree)",
        "difficulty": "expert",
        "multiLine": True,
        "systems": AHU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Warmup/Cooldown/Occupied/Unoccupied Dispatch",
            "prompt": f"Real VAV AHU sequences run a four-way mode dispatch every scan: Unoccupied, Warmup, Cooldown, and normal Occupied, each its own block reached by GOTO. Remember GOTO can only jump forward to a higher line number, never backward, so lay the blocks out in that order. First, check occupancy: if Building Occupancy State (\"%X%BLDG.OCC.STATE\") equals 1 (Unoccupied), GOTO line 01300 immediately, skipping the mode checks below -- an unoccupied building never enters Warmup or Cooldown. Otherwise (still Occupied), check Room Temperature (\"%X%{sys}.RMT\") against the two mode setpoints: if RMT is less than the Warmup Setpoint (\"%X%{sys}.WU.SP\"), GOTO line 01100 (Warmup); if RMT is greater than the Cooldown Setpoint (\"%X%{sys}.CD.SP\"), GOTO line 01200 (Cooldown). If neither is true, fall through into the Normal Occupied block that follows immediately. At line 01030 (Normal Occupied), turn ON Supply Fan Enable (\"%X%{sys}.SF.ENA\"), turn OFF Warmup Enable (\"%X%{sys}.WU.ENA\"), turn OFF Cooldown Enable (\"%X%{sys}.CD.ENA\"), then GOTO line 01400 to end the scan. At line 01100 (Warmup), turn ON Supply Fan Enable, turn ON Warmup Enable, turn OFF Cooldown Enable, then GOTO line 01400. At line 01200 (Cooldown), turn ON Supply Fan Enable, turn OFF Warmup Enable, turn ON Cooldown Enable, then GOTO line 01400. At line 01300 (Unoccupied), turn OFF Supply Fan Enable, turn OFF Warmup Enable, turn OFF Cooldown Enable, then GOTO line 01400. Line 01400 should be a comment marking the end of the scan, and it needs to be the LAST line in your program -- every block above it ends with its own GOTO there so none of them can fall straight through into whatever comes next in the file.",
            "points": [
                {"name": "%X%BLDG.OCC.STATE",
                    "desc": "Building-wide Occupancy Mode (0 = Occupied, 1 = Unoccupied) -- a virtual point driven by a scheduling program, shared across all equipment", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.RMT",
                    "desc": "Room Temperature Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.WU.SP",
                    "desc": "Warmup Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.CD.SP",
                    "desc": "Cooldown Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.SF.ENA",
                    "desc": "Supply Fan Enable Command (already exists -- your logic must write to this point)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.WU.ENA",
                    "desc": "Warmup Enable Command (already exists -- your logic must write to this point)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.CD.ENA",
                    "desc": "Cooldown Enable Command (already exists -- your logic must write to this point)", "type": "DO", "kind": "physical"}
            ],
            "help": "Check occupancy first and dispatch Unoccupied straight to its own block, skipping the warmup/cooldown checks entirely. Only once you know the building is occupied do you compare RMT to the two mode setpoints -- and if neither Warmup nor Cooldown applies, just fall through into the Normal Occupied block right below (no GOTO needed to reach it, since it's already the next block in line). Every block, including Normal Occupied, still needs its own GOTO 01400 at the end so it doesn't fall into the block below it too.",
            "checks": [
                {"force": {"%X%BLDG.OCC.STATE": 1, f"%X%{sys}.RMT": 70, f"%X%{sys}.WU.SP": 65, f"%X%{sys}.CD.SP": 78}, "expect": {f"%X%{sys}.SF.ENA": 0, f"%X%{sys}.WU.ENA": 0, f"%X%{sys}.CD.ENA": 0}, "note": "unoccupied -- fan and both modes off regardless of RMT"},
                {"force": {"%X%BLDG.OCC.STATE": 0, f"%X%{sys}.RMT": 60, f"%X%{sys}.WU.SP": 65, f"%X%{sys}.CD.SP": 78}, "expect": {f"%X%{sys}.SF.ENA": 1, f"%X%{sys}.WU.ENA": 1, f"%X%{sys}.CD.ENA": 0}, "note": "occupied and RMT below warmup setpoint -- warmup mode"},
                {"force": {"%X%BLDG.OCC.STATE": 0, f"%X%{sys}.RMT": 82, f"%X%{sys}.WU.SP": 65, f"%X%{sys}.CD.SP": 78}, "expect": {f"%X%{sys}.SF.ENA": 1, f"%X%{sys}.WU.ENA": 0, f"%X%{sys}.CD.ENA": 1}, "note": "occupied and RMT above cooldown setpoint -- cooldown mode"},
                {"force": {"%X%BLDG.OCC.STATE": 0, f"%X%{sys}.RMT": 72, f"%X%{sys}.WU.SP": 65, f"%X%{sys}.CD.SP": 78}, "expect": {f"%X%{sys}.SF.ENA": 1, f"%X%{sys}.WU.ENA": 0, f"%X%{sys}.CD.ENA": 0}, "note": "occupied and RMT between the two setpoints -- normal occupied mode"}
            ]
        }
    },
    {
        "title": "FCU Freeze Protection & Occupancy Dispatch (GOTO Decision Tree)",
        "difficulty": "expert",
        "multiLine": True,
        "systems": FCU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Freeze Protection & Occupancy Dispatch",
            "prompt": f"Real FCU programs open with a decision tree just like AHUs do: check safety first, then dispatch to the current mode via GOTO. Build it here. Remember GOTO can only jump forward to a higher line number, never backward, so lay the blocks out in this order. First, check safety: if the Coil Freeze Stat (\"%X%{sys}.FRZ.STAT\") is ON (tripped -- a low-limit safety on the water coil), GOTO line 02000 immediately, skipping mode logic entirely -- freeze protection always overrides occupancy. Otherwise, check Building Occupancy State (\"%X%BLDG.OCC.STATE\", 0 = Occupied, 1 = Unoccupied): if Occupied, GOTO line 01100; if Unoccupied, GOTO line 01200. At line 01100 (the Occupied block), turn ON the Fan (\"%X%{sys}.FAN.ENA\"), then GOTO line 03000 to end the scan. At line 01200 (the Unoccupied block), turn OFF the Fan, then GOTO line 03000. At line 02000 (the Freeze Protection block), turn OFF the Fan and turn ON the Heating Valve (\"%X%{sys}.HTG.VLV\") to its fail-safe fully-open position to keep hot water moving through the coil, then GOTO line 03000. Line 03000 should be a comment marking the end of the scan, and it needs to be the LAST line in your program -- every block above it ends with its own GOTO there so none of them can fall straight through into whatever comes next in the file.",
            "points": [
                {"name": f"%X%{sys}.FRZ.STAT",
                    "desc": "Coil Freeze Stat Trip (low-limit safety, opens below roughly 38 degrees)", "type": "DI", "kind": "physical"},
                {"name": "%X%BLDG.OCC.STATE",
                    "desc": "Building-wide Occupancy Mode (0 = Occupied, 1 = Unoccupied) -- a virtual point driven by a scheduling program, shared across all equipment", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.FAN.ENA",
                    "desc": "Fan Enable Command (already exists -- your logic must write to this point)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.HTG.VLV",
                    "desc": "Heating Valve Command (already exists -- your logic must write to this point)", "type": "DO", "kind": "physical"}
            ],
            "help": "Same dispatch shape as the AHU safety interlock -- freeze protection is checked first and jumps straight to its own block, bypassing the occupancy check entirely when tripped. Only the Freeze Protection block writes to HTG.VLV; the Occupied/Unoccupied blocks only touch the fan, so leave the heating valve alone there.",
            "checks": [
                {"force": {f"%X%{sys}.FRZ.STAT": 1, "%X%BLDG.OCC.STATE": 0}, "expect": {f"%X%{sys}.FAN.ENA": 0, f"%X%{sys}.HTG.VLV": 1}, "note": "freeze stat tripped -- overrides occupancy, fan off and heating valve driven open"},
                {"force": {f"%X%{sys}.FRZ.STAT": 0, "%X%BLDG.OCC.STATE": 0}, "expect": {f"%X%{sys}.FAN.ENA": 1}, "note": "no freeze trip, occupied -- fan on"},
                {"force": {f"%X%{sys}.FRZ.STAT": 0, "%X%BLDG.OCC.STATE": 1}, "expect": {f"%X%{sys}.FAN.ENA": 0}, "note": "no freeze trip, unoccupied -- fan off"}
            ]
        }
    },
    {
        "title": "Exhaust Fan VFD Speed Control by Building Static Pressure",
        "difficulty": "expert",
        "multiLine": True,
        "systems": EF_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Building Static Pressure Control",
            "prompt": f"{sys}'s speed needs to modulate continuously to control Building Static Pressure rather than simply cycling on and off. Whenever the Exhaust Fan Start Command (\"%X%{sys}.EF.SS\") is ON, execute a LOOP statement to modulate Exhaust Fan Speed Output (\"%X%{sys}.EF.SPD\") (0-100%) to maintain Building Static Pressure (\"%X%{sys}.BSP\") at Setpoint (\"%X%{sys}.BSP.SP\"). When Exhaust Fan Start Command is OFF, hold Exhaust Fan Speed Output at 0.",
            "points": [
                {"name": f"%X%{sys}.EF.SS",
                    "desc": "Exhaust Fan Start Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.BSP",
                    "desc": "Building Static Pressure Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.BSP.SP",
                    "desc": "Building Static Pressure Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.EF.SPD",
                    "desc": "Exhaust Fan Speed Output (0-100%)", "type": "AO", "kind": "physical"}
            ],
            "help": "A real LOOP call takes 12 positional arguments in this exact order: LOOP(type, pv, cv, sp, pg, ig, dg, st, bias, lo, hi, 0). Use: type=0 (direct acting -- fan speed must increase as building pressure rises above setpoint, pulling more air out to bring it back down, the same direction as the process variable), pv=\"%X%{sys}.BSP\", cv=\"%X%{sys}.EF.SPD\", sp=\"%X%{sys}.BSP.SP\", P Gain (pg)=4.0, I Gain (ig)=0.25, D Gain (dg)=0.0, sample time (st)=1, bias=50.0 (mid-range output when Building Static Pressure is exactly at setpoint), low limit (lo)=20 (never let the fan fully stop while it's commanded to run), high limit (hi)=100, and the final parameter is always 0 (unused). Wrap the LOOP call in an IF on EF.SS; the ELSE just needs to hold EF.SPD at 0.",
            "checks": [
                {"force": {f"%X%{sys}.EF.SS": 0, f"%X%{sys}.BSP": 0.1, f"%X%{sys}.BSP.SP": 0.05}, "expect": {f"%X%{sys}.EF.SPD": 0}, "note": "fan not started -- speed output forced to 0"},
                {"force": {f"%X%{sys}.EF.SS": 0, f"%X%{sys}.BSP": 0.02, f"%X%{sys}.BSP.SP": 0.05}, "expect": {f"%X%{sys}.EF.SPD": 0}, "note": "fan not started -- speed output forced to 0 regardless of pressure"}
            ]
        }
    },
    {
        "title": "Chiller Plant Safety Shutdown Dispatch (GOTO Decision Tree)",
        "difficulty": "expert",
        "multiLine": True,
        "systems": CHILLER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Safety Shutdown Dispatch",
            "prompt": f"Chiller safeties always override normal staging -- real chiller plant programs check safety first via a GOTO decision tree, exactly like an AHU program does. Build it here. Remember GOTO can only jump forward to a higher line number, never backward, so lay the blocks out in this order. First, check safety: if Low Refrigerant Pressure Trip (\"%X%{sys}.LOP.TRIP\") is ON OR High Condenser Pressure Trip (\"%X%{sys}.HCP.TRIP\") is ON, GOTO line 02000 immediately, skipping normal staging entirely -- either safety trip shuts the whole plant down. Otherwise, check Chiller Plant Master Enable (\"%X%{sys}.ENA\"): if ON, GOTO line 01100 (Normal Run); if OFF, GOTO line 01200 (Standby). At line 01100 (Normal Run), turn ON Chiller 1 Enable (\"%X%{sys}1.ENA\"), then GOTO line 03000 to end the scan. At line 01200 (Standby), turn OFF Chiller 1 Enable, then GOTO line 03000. At line 02000 (the Safety Shutdown block), turn OFF Chiller 1 Enable and turn ON a Safety Shutdown Alarm (\"%X%{sys}.SAFE.ALM\") -- a virtual point -- then GOTO line 03000. Line 03000 should be a comment marking the end of the scan, and it needs to be the LAST line in your program -- every block above it ends with its own GOTO there so none of them can fall straight through into whatever comes next in the file.",
            "points": [
                {"name": f"%X%{sys}.LOP.TRIP",
                    "desc": "Low Refrigerant Pressure Safety Trip", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.HCP.TRIP",
                    "desc": "High Condenser Pressure Safety Trip", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.ENA",
                    "desc": "Chiller Plant Master Enable (already exists)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}1.ENA",
                    "desc": "Chiller 1 Enable", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.SAFE.ALM",
                    "desc": "Safety Shutdown Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "OR the two safety trips together in the first check -- either one alone should jump to the shutdown block. Only that block writes to SAFE.ALM; the Normal Run and Standby blocks don't touch it, so don't add logic there that clears it.",
            "checks": [
                {"force": {f"%X%{sys}.LOP.TRIP": 1, f"%X%{sys}.HCP.TRIP": 0, f"%X%{sys}.ENA": 1}, "expect": {f"%X%{sys}1.ENA": 0, f"%X%{sys}.SAFE.ALM": 1}, "note": "low refrigerant pressure trip -- safety shutdown overrides master enable"},
                {"force": {f"%X%{sys}.LOP.TRIP": 0, f"%X%{sys}.HCP.TRIP": 1, f"%X%{sys}.ENA": 1}, "expect": {f"%X%{sys}1.ENA": 0, f"%X%{sys}.SAFE.ALM": 1}, "note": "high condenser pressure trip -- safety shutdown overrides master enable"},
                {"force": {f"%X%{sys}.LOP.TRIP": 0, f"%X%{sys}.HCP.TRIP": 0, f"%X%{sys}.ENA": 1}, "expect": {f"%X%{sys}1.ENA": 1}, "note": "no trips, master enable on -- normal run"},
                {"force": {f"%X%{sys}.LOP.TRIP": 0, f"%X%{sys}.HCP.TRIP": 0, f"%X%{sys}.ENA": 0}, "expect": {f"%X%{sys}1.ENA": 0}, "note": "no trips, master enable off -- standby"}
            ]
        }
    },
    {
        "title": "Cooling Tower Fan VFD Speed Control PID Loop",
        "difficulty": "expert",
        "multiLine": True,
        "systems": CT_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Fan Speed PID Control",
            "prompt": f"Rather than staged on/off fan speed, {sys}'s fan should modulate continuously via LOOP to hold Condenser Water Supply Temp at setpoint. Whenever Tower Fan Enable (\"%X%{sys}.FAN.ENA\") is ON, execute a LOOP statement to modulate Tower Fan Speed Output (\"%X%{sys}.FAN.SPD\") (0-100%) to maintain Condenser Water Supply Temp (\"%X%{sys}.CWS.T\") at Setpoint (\"%X%{sys}.CWS.T.SP\"). When Tower Fan Enable is OFF, hold Tower Fan Speed Output at 0.",
            "points": [
                {"name": f"%X%{sys}.FAN.ENA",
                    "desc": "Tower Fan Enable (already exists -- your logic must write to this point)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}.CWS.T",
                    "desc": "Condenser Water Supply Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.CWS.T.SP",
                    "desc": "Condenser Water Supply Temp Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.FAN.SPD",
                    "desc": "Tower Fan Speed Output (0-100%)", "type": "AO", "kind": "physical"}
            ],
            "help": "A real LOOP call takes 12 positional arguments in this exact order: LOOP(type, pv, cv, sp, pg, ig, dg, st, bias, lo, hi, 0). Use: type=0 (direct acting -- fan speed must increase as condenser water temp rises above setpoint, the same direction as the process variable), pv=\"%X%{sys}.CWS.T\", cv=\"%X%{sys}.FAN.SPD\", sp=\"%X%{sys}.CWS.T.SP\", P Gain (pg)=4.0, I Gain (ig)=0.2, D Gain (dg)=0.0, sample time (st)=1, bias=50.0 (mid-range output when Condenser Water Supply Temp is exactly at setpoint), low limit (lo)=20 (never let the fan fully stop while enabled, to avoid basin freeze in cold weather), high limit (hi)=100, and the final parameter is always 0 (unused). Wrap the LOOP call in an IF on FAN.ENA; the ELSE just needs to hold FAN.SPD at 0.",
            "checks": [
                {"force": {f"%X%{sys}.FAN.ENA": 0, f"%X%{sys}.CWS.T": 90, f"%X%{sys}.CWS.T.SP": 85}, "expect": {f"%X%{sys}.FAN.SPD": 0}, "note": "fan not enabled -- speed forced to 0"},
                {"force": {f"%X%{sys}.FAN.ENA": 0, f"%X%{sys}.CWS.T": 80, f"%X%{sys}.CWS.T.SP": 85}, "expect": {f"%X%{sys}.FAN.SPD": 0}, "note": "fan not enabled -- speed forced to 0 regardless of temp"}
            ]
        }
    },
    {
        "title": "Boiler Low Water Cutoff Safety Shutdown Dispatch (GOTO Decision Tree)",
        "difficulty": "expert",
        "multiLine": True,
        "systems": BOILER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys}1 Low Water Cutoff Safety Dispatch",
            "prompt": f"Boiler safeties always override normal firing -- real boiler programs check safety first via a GOTO decision tree. Build it here. Remember GOTO can only jump forward to a higher line number, never backward, so lay the blocks out in this order. First, check safety: if the Low Water Cutoff (\"%X%{sys}1.LWCO\") is ON (tripped -- water level below the safe minimum), GOTO line 02000 immediately, skipping normal firing logic entirely -- a low water condition shuts the boiler down regardless of demand. Otherwise, check Boiler 1 Firing Demand (\"%X%{sys}1.DMD\", a virtual point driven by hot water temperature reset logic elsewhere in the program): if ON, GOTO line 01100 (Firing); if OFF, GOTO line 01200 (Standby). At line 01100 (Firing), turn ON Boiler 1 Enable (\"%X%{sys}1.ENA\"), then GOTO line 03000 to end the scan. At line 01200 (Standby), turn OFF Boiler 1 Enable, then GOTO line 03000. At line 02000 (the Safety Shutdown block), turn OFF Boiler 1 Enable and turn ON a Low Water Safety Alarm (\"%X%{sys}1.LWCO.ALM\") -- a virtual point -- then GOTO line 03000. Line 03000 should be a comment marking the end of the scan, and it needs to be the LAST line in your program -- every block above it ends with its own GOTO there so none of them can fall straight through into whatever comes next in the file.",
            "points": [
                {"name": f"%X%{sys}1.LWCO",
                    "desc": "Low Water Cutoff Safety Switch", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}1.DMD",
                    "desc": "Firing Demand (virtual point, driven by hot water temperature reset logic elsewhere in the program)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}1.ENA",
                    "desc": "Boiler 1 Enable", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}1.LWCO.ALM",
                    "desc": "Low Water Safety Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Same dispatch shape as the chiller and AHU safety trees -- check LWCO first and jump straight to the shutdown block, bypassing the demand check entirely when tripped. Only the shutdown block writes to LWCO.ALM.",
            "checks": [
                {"force": {f"%X%{sys}1.LWCO": 1, f"%X%{sys}1.DMD": 1}, "expect": {f"%X%{sys}1.ENA": 0, f"%X%{sys}1.LWCO.ALM": 1}, "note": "low water cutoff tripped -- shuts boiler down and alarms regardless of demand"},
                {"force": {f"%X%{sys}1.LWCO": 0, f"%X%{sys}1.DMD": 1}, "expect": {f"%X%{sys}1.ENA": 1}, "note": "no trip, firing demand present -- boiler enabled"},
                {"force": {f"%X%{sys}1.LWCO": 0, f"%X%{sys}1.DMD": 0}, "expect": {f"%X%{sys}1.ENA": 0}, "note": "no trip, no demand -- standby"}
            ]
        }
    },
    {
        "title": "CRAC Discharge Air Temp PID Loop with Compressor Staging",
        "difficulty": "expert",
        "multiLine": True,
        "systems": CRAC_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys}201 Discharge Air Temp PID Control & Staging",
            "prompt": f"{sys}201's cooling needs finer control than a simple on/off compressor -- real CRAC units modulate a hot-gas bypass or EEV to hold discharge air temp precisely, staging a second compressor only under heavy load. Whenever {sys}201 is enabled (\"%X%{sys}201.ENA\" is ON), execute a LOOP statement to modulate the Modulating Cooling Output (\"%X%{sys}201.CLG.MOD\") (0-100%) to maintain Discharge Air Temp (\"%X%{sys}201.DAT\") at Setpoint (\"%X%{sys}201.DAT.SP\"). Separately, if the Modulating Cooling Output stays above 90% continuously, accumulate a Stage-Up Timer (\"%X%{sys}201.STGUP\") by 1 second using SAMPLE(1); once it exceeds 180 seconds, stage ON a second compressor (\"%X%{sys}201.COMP2.ENA\") for extra capacity. If the Modulating Cooling Output drops below 60%, stage OFF Compressor 2 and reset the Stage-Up Timer to 0.",
            "points": [
                {"name": f"%X%{sys}201.ENA",
                    "desc": "CRAC201 Enable Command (already exists)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}201.DAT",
                    "desc": "Discharge Air Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}201.DAT.SP",
                    "desc": "Discharge Air Temp Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}201.CLG.MOD",
                    "desc": "Modulating Cooling Output (0-100%, hot-gas bypass/EEV)", "type": "AO", "kind": "physical"},
                {"name": f"%X%{sys}201.STGUP",
                    "desc": "Stage-Up Timer Accumulator (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}201.COMP2.ENA",
                    "desc": "Compressor 2 Stage Enable", "type": "DO", "kind": "physical"}
            ],
            "help": "A real LOOP call takes 12 positional arguments in this exact order: LOOP(type, pv, cv, sp, pg, ig, dg, st, bias, lo, hi, 0). Use: type=0 (direct acting -- cooling output must increase as discharge air temp rises above setpoint, the same direction as the process variable), pv=\"%X%{sys}201.DAT\", cv=\"%X%{sys}201.CLG.MOD\", sp=\"%X%{sys}201.DAT.SP\", P Gain (pg)=3.5, I Gain (ig)=0.2, D Gain (dg)=0.0, sample time (st)=1, bias=30.0 (mid-range output when Discharge Air Temp is exactly at setpoint), low limit (lo)=0, high limit (hi)=100, and the final parameter is always 0 (unused). Wrap the LOOP call in an IF on ENA. The staging logic is separate and doesn't touch the LOOP call at all -- it just watches CLG.MOD's output value: a SAMPLE(1) timer while it's above 90%, and an immediate stage-off with timer reset once it drops below 60%.",
            "checks": [
                {"force": {f"%X%{sys}201.CLG.MOD": 95, f"%X%{sys}201.STGUP": 0, f"%X%{sys}201.COMP2.ENA": 0}, "cycles": 181, "expect": {f"%X%{sys}201.COMP2.ENA": 1}, "note": "cooling output stays above 90% for over 180 seconds -- compressor 2 stages on"},
                {"force": {f"%X%{sys}201.CLG.MOD": 95, f"%X%{sys}201.STGUP": 0, f"%X%{sys}201.COMP2.ENA": 0}, "cycles": 30, "expect": {f"%X%{sys}201.COMP2.ENA": 0}, "note": "timer hasn't reached 180 seconds yet -- compressor 2 should not have staged on"},
                {"force": {f"%X%{sys}201.CLG.MOD": 40, f"%X%{sys}201.STGUP": 150, f"%X%{sys}201.COMP2.ENA": 1}, "expect": {f"%X%{sys}201.COMP2.ENA": 0, f"%X%{sys}201.STGUP": 0}, "note": "cooling output drops below 60% -- compressor 2 stages off and stage-up timer resets"}
            ]
        }
    },
    {
        "title": "MAU Freezestat Safety Dispatch (GOTO Decision Tree)",
        "difficulty": "expert",
        "multiLine": True,
        "systems": MAU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Freezestat Safety Dispatch",
            "prompt": f"Real Siemens MAU programs open with a decision tree just like AHUs do: check safety first, then dispatch to the current mode via GOTO. Build it here. Remember GOTO can only jump forward to a higher line number, never backward, so lay the blocks out in this order. First, check safety: if the Low-Limit Freezestat (\"%X%{sys}.FRZ.STAT\") is ON (tripped -- discharge air temp dropped below the coil freeze threshold), GOTO line 02000 immediately, skipping mode logic entirely -- since this unit is 100% outside air with no return air to temper it, freeze protection has to override everything else. Otherwise, check Building Occupancy State (\"%X%BLDG.OCC.STATE\", 0 = Occupied, 1 = Unoccupied): if Occupied, GOTO line 01100; if Unoccupied, GOTO line 01200. At line 01100 (the Occupied block), turn ON Supply Fan Enable (\"%X%{sys}.SF.ENA\") and turn ON the Heating Valve (\"%X%{sys}.HTG.VLV\") to satisfy Discharge Air Setpoint (keep that as a simple ON for this exercise), then GOTO line 03000 to end the scan. At line 01200 (the Unoccupied block), turn OFF Supply Fan Enable and turn OFF the Heating Valve, then GOTO line 03000. At line 02000 (the Freeze Protection block), turn OFF Supply Fan Enable, turn ON the Heating Valve fully open to keep hot water moving through the coil, and turn ON a Freeze Alarm (\"%X%{sys}.FRZ.ALM\") -- a virtual point -- then GOTO line 03000. Line 03000 should be a comment marking the end of the scan, and it needs to be the LAST line in your program -- every block above it ends with its own GOTO there so none of them can fall straight through into whatever comes next in the file.",
            "points": [
                {"name": f"%X%{sys}.FRZ.STAT",
                    "desc": "Low-Limit Freezestat Trip (discharge air side, no return air to temper this 100% OA unit)", "type": "DI", "kind": "physical"},
                {"name": "%X%BLDG.OCC.STATE",
                    "desc": "Building-wide Occupancy Mode (0 = Occupied, 1 = Unoccupied) -- a virtual point driven by a scheduling program, shared across all equipment", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.SF.ENA",
                    "desc": "Supply Fan Enable Command (already exists -- your logic must write to this point)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.HTG.VLV",
                    "desc": "Heating Valve Command (already exists -- your logic must write to this point)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.FRZ.ALM",
                    "desc": "Freeze Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Same dispatch shape as the other safety-first GOTO trees -- the freezestat check comes first and jumps straight to its own block, skipping the occupancy check entirely when tripped. Only the Freeze Protection block writes to FRZ.ALM, and it also drives the fan/valve to their fail-safe positions rather than mirroring what either Occupied or Unoccupied would normally do.",
            "checks": [
                {"force": {f"%X%{sys}.FRZ.STAT": 1, "%X%BLDG.OCC.STATE": 0}, "expect": {f"%X%{sys}.SF.ENA": 0, f"%X%{sys}.HTG.VLV": 1, f"%X%{sys}.FRZ.ALM": 1}, "note": "freezestat tripped -- overrides occupancy: fan off, heating valve driven open, alarm on"},
                {"force": {f"%X%{sys}.FRZ.STAT": 0, "%X%BLDG.OCC.STATE": 0}, "expect": {f"%X%{sys}.SF.ENA": 1, f"%X%{sys}.HTG.VLV": 1}, "note": "no freeze trip, occupied -- fan on, heating valve on"},
                {"force": {f"%X%{sys}.FRZ.STAT": 0, "%X%BLDG.OCC.STATE": 1}, "expect": {f"%X%{sys}.SF.ENA": 0, f"%X%{sys}.HTG.VLV": 0}, "note": "no freeze trip, unoccupied -- fan and heating valve off"}
            ]
        }
    }
]

# --- "EDIT EXISTING CODE" SCENARIOS ---
# Realistic Siemens commissioning framing: a programmer already wrote this
# code, and the student's job is to bring it into compliance with either an
# additional/changed device found in the field, or a gap between the as-built
# program and the approved sequence of operations -- the way PPCL punch-list
# work actually happens on a new-construction job. Each gives a working
# starter program plus a changed/new requirement; the student edits the
# starter code (instead of writing from a blank slate) to close the gap.
EDIT_TEMPLATES = [
    # ---------------- EASY ----------------
    {
        "title": "Additional Device: Second Cooling Valve Added",
        "difficulty": "medium",
        "systems": FCU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Second Cooling Valve Added",
            "original_prompt": f"If Room Temperature (\"%X%{sys}.RMT\") is greater than Cooling Setpoint (\"%X%{sys}.RMT.SP\"), turn ON the Cooling Valve (\"%X%{sys}.CLG.VLV\"). Otherwise, turn it OFF.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.RMT\" .GT. \"%X%{sys}.RMT.SP\") THEN ON (\"%X%{sys}.CLG.VLV\") ELSE OFF (\"%X%{sys}.CLG.VLV\")"
            ),
            "changed_prompt": f"The mechanical contractor field-installed a second, smaller Cooling Valve (\"%X%{sys}.CLG.VLV2\") in series with the original for finer capacity control. Update the code so that when Room Temperature exceeds Setpoint by more than 2 degrees, both valves turn ON; when it exceeds Setpoint by any smaller amount (up to 2 degrees), only the original valve turns ON; otherwise both valves turn OFF.",
            "points": [
                {"name": f"%X%{sys}.RMT",
                    "desc": "Room Temperature Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.RMT.SP",
                    "desc": "Cooling Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.CLG.VLV",
                    "desc": "Cooling Valve Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.CLG.VLV2",
                    "desc": "Second Cooling Valve Command (new, field-added)", "type": "DO", "kind": "physical"}
            ],
            "help": "Same three-way chain as the FCU multi-speed fan scenario -- check the bigger threshold (more than 2 degrees over) first, then the smaller range, then the else. Keep both valves' writes lined up with the same thresholds; VLV2 only ever turns on together with VLV, never alone.",
            "checks": [
                {"force": {f"%X%{sys}.RMT": 80, f"%X%{sys}.RMT.SP": 75}, "expect": {f"%X%{sys}.CLG.VLV": 1, f"%X%{sys}.CLG.VLV2": 1}, "note": "5 degrees over setpoint (more than 2) -- both valves on"},
                {"force": {f"%X%{sys}.RMT": 76, f"%X%{sys}.RMT.SP": 75}, "expect": {f"%X%{sys}.CLG.VLV": 1, f"%X%{sys}.CLG.VLV2": 0}, "note": "1 degree over setpoint (within 2) -- only original valve on"},
                {"force": {f"%X%{sys}.RMT": 73, f"%X%{sys}.RMT.SP": 75}, "expect": {f"%X%{sys}.CLG.VLV": 0, f"%X%{sys}.CLG.VLV2": 0}, "note": "under setpoint -- both off"}
            ]
        }
    },
    {
        "title": "Compliance Fix: Damper Alarm Auto-Clear on Proven Correct",
        "difficulty": "easy",
        "systems": AHU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Damper Alarm Auto-Clear",
            "original_prompt": f"If Outside Air Damper Command (\"%X%{sys}.OAD.CMD\") is ON (commanded open) and Outside Air Damper Status (\"%X%{sys}.OAD.S\") is OFF (not proven open), turn ON a Damper Fault Alarm (\"%X%{sys}.OAD.ALM\") -- a virtual point. Otherwise, turn it OFF.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.OAD.CMD\" .EQ. ON .AND. \"%X%{sys}.OAD.S\" .EQ. OFF) THEN ON (\"%X%{sys}.OAD.ALM\") ELSE OFF (\"%X%{sys}.OAD.ALM\")"
            ),
            "changed_prompt": f"The approved sequence calls for this alarm to behave differently: once tripped, it should auto-clear on its own once the fault condition has been proven correct again (Command and Status agreeing) for a sustained period -- not the instant it looks OK, to avoid chattering. Add a Proven-Correct Timer (\"%X%{sys}.OAD.ALM.TMR\") that accumulates by 1 second using SAMPLE(1) whenever the fault condition is NOT true, and resets to 0 whenever the fault condition IS true; once that timer exceeds 60 seconds, clear the alarm. Also add a new Alarm Reset point (\"%X%{sys}.OAD.ALM.RST\") an operator can pulse ON to clear the alarm immediately without waiting for the timer.",
            "points": [
                {"name": f"%X%{sys}.OAD.CMD",
                    "desc": "Damper Open/Closed Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.OAD.S",
                    "desc": "Damper Proven-Open Status (proof for OAD.CMD)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.OAD.ALM",
                    "desc": "Damper Fault Alarm (virtual point, driven by the starter code)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.OAD.ALM.TMR",
                    "desc": "Proven-Correct Timer Accumulator (new, virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.OAD.ALM.RST",
                    "desc": "Alarm Reset Command (new, operator-facing)", "type": "DO", "kind": "physical"}
            ],
            "help": "Keep the original fault-detection logic (set ALM ON immediately when CMD is ON and S is OFF) untouched -- you're adding an auto-clear path, not replacing the trip logic. The timer only accumulates while the fault condition is NOT true, and resets to 0 the instant it IS true, so a single momentary glitch back to faulted restarts the 60-second countdown from scratch. Check the manual reset independently -- if RST pulses, clear the alarm right then, no timer needed.",
            "checks": [
                {"force": {f"%X%{sys}.OAD.CMD": 1, f"%X%{sys}.OAD.S": 0, f"%X%{sys}.OAD.ALM": 0, f"%X%{sys}.OAD.ALM.TMR": 0, f"%X%{sys}.OAD.ALM.RST": 0}, "expect": {f"%X%{sys}.OAD.ALM": 1, f"%X%{sys}.OAD.ALM.TMR": 0}, "note": "fault condition true -- alarm sets and the proven-correct timer resets"},
                {"force": {f"%X%{sys}.OAD.CMD": 1, f"%X%{sys}.OAD.S": 1, f"%X%{sys}.OAD.ALM": 1, f"%X%{sys}.OAD.ALM.TMR": 0, f"%X%{sys}.OAD.ALM.RST": 0}, "cycles": 61, "expect": {f"%X%{sys}.OAD.ALM": 0}, "note": "fault condition proven correct for over 60 seconds -- alarm auto-clears"},
                {"force": {f"%X%{sys}.OAD.CMD": 1, f"%X%{sys}.OAD.S": 1, f"%X%{sys}.OAD.ALM": 1, f"%X%{sys}.OAD.ALM.TMR": 0, f"%X%{sys}.OAD.ALM.RST": 0}, "cycles": 10, "expect": {f"%X%{sys}.OAD.ALM": 1}, "note": "fault condition just cleared -- alarm stays latched until the 60-second timer completes"},
                {"force": {f"%X%{sys}.OAD.CMD": 1, f"%X%{sys}.OAD.S": 1, f"%X%{sys}.OAD.ALM": 1, f"%X%{sys}.OAD.ALM.TMR": 0, f"%X%{sys}.OAD.ALM.RST": 1}, "expect": {f"%X%{sys}.OAD.ALM": 0}, "note": "operator pulses manual reset -- alarm clears immediately without waiting for the timer"}
            ]
        }
    },
    {
        "title": "Additional Device: Return Fan Added",
        "difficulty": "easy",
        "systems": AHU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Return Fan Added",
            "original_prompt": f"If Supply Fan Start/Stop Command (\"%X%{sys}.SF.SS\") is ON, turn ON the Supply Fan Run Output (\"%X%{sys}.SF.RUN\"). Otherwise, turn it OFF.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.SF.SS\" .EQ. ON) THEN ON (\"%X%{sys}.SF.RUN\") ELSE OFF (\"%X%{sys}.SF.RUN\")"
            ),
            "changed_prompt": f"A Return Fan (\"%X%{sys}.RF.SS\") was added to the AHU for better building pressurization control. Update the code so the Return Fan's Start/Stop output runs whenever the Supply Fan is running, and stays off otherwise -- both outputs should track the same Start/Stop Command together.",
            "points": [
                {"name": f"%X%{sys}.SF.SS",
                    "desc": "Supply Fan Start/Stop Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.SF.RUN",
                    "desc": "Supply Fan Run Output", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.RF.SS",
                    "desc": "Return Fan Start/Stop Command (new, field-added)", "type": "DO", "kind": "physical"}
            ],
            "help": "RF.SS just needs to mirror the same condition already driving SF.RUN -- either add RF.SS to the existing IF/THEN/ELSE's two branches, or write a second IF checking SF.SS the same way. Either approach works as long as RF.SS ends up matching SF.SS/SF.RUN on every scan.",
            "checks": [
                {"force": {f"%X%{sys}.SF.SS": 1}, "expect": {f"%X%{sys}.SF.RUN": 1, f"%X%{sys}.RF.SS": 1}, "note": "supply fan running -- return fan runs too"},
                {"force": {f"%X%{sys}.SF.SS": 0}, "expect": {f"%X%{sys}.SF.RUN": 0, f"%X%{sys}.RF.SS": 0}, "note": "supply fan off -- return fan off too"}
            ]
        }
    },
    {
        "title": "Additional Device: Filter Status Switch Added",
        "difficulty": "easy",
        "systems": MAU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Filter Status Switch Added",
            "original_prompt": f"If Discharge Air Temp (\"%X%{sys}.DAT\") is less than Discharge Air Setpoint (\"%X%{sys}.DAT.SP\"), turn ON the Heating Valve (\"%X%{sys}.HTG.VLV\"). Otherwise, turn it OFF.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.DAT\" .LT. \"%X%{sys}.DAT.SP\") THEN ON (\"%X%{sys}.HTG.VLV\") ELSE OFF (\"%X%{sys}.HTG.VLV\")"
            ),
            "changed_prompt": f"A Filter Status Switch (\"%X%{sys}.FLT.STAT\") was added to the MAU's filter rack. Update the code so the Heating Valve only opens when Filter Status is proven OK (ON); if the filter switch indicates a dirty/blocked filter (OFF), keep the Heating Valve OFF regardless of temperature, to avoid overheating starved airflow across the coil.",
            "points": [
                {"name": f"%X%{sys}.DAT",
                    "desc": "Discharge Air Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.DAT.SP",
                    "desc": "Discharge Air Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.HTG.VLV",
                    "desc": "Heating Valve Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.FLT.STAT",
                    "desc": "Filter Status Switch (new, DI -- ON = clean/OK, OFF = dirty/blocked)", "type": "DI", "kind": "physical"}
            ],
            "help": "AND the filter check into the existing condition -- the valve should only open when BOTH the original DAT .LT. DAT.SP condition AND FLT.STAT is ON are true. A dirty filter needs to override, so this AND has to be part of the main condition, not a separate rule that could get overwritten.",
            "checks": [
                {"force": {f"%X%{sys}.DAT": 60, f"%X%{sys}.DAT.SP": 68, f"%X%{sys}.FLT.STAT": 1}, "expect": {f"%X%{sys}.HTG.VLV": 1}, "note": "cold, filter OK -- valve opens normally"},
                {"force": {f"%X%{sys}.DAT": 60, f"%X%{sys}.DAT.SP": 68, f"%X%{sys}.FLT.STAT": 0}, "expect": {f"%X%{sys}.HTG.VLV": 0}, "note": "cold, but filter dirty/blocked -- valve forced off regardless of temperature"},
                {"force": {f"%X%{sys}.DAT": 75, f"%X%{sys}.DAT.SP": 68, f"%X%{sys}.FLT.STAT": 1}, "expect": {f"%X%{sys}.HTG.VLV": 0}, "note": "warm, filter OK -- valve off as before"}
            ]
        }
    },
    {
        "title": "Additional Device: Exhaust Fan VFD Trouble Alarm Added",
        "difficulty": "easy",
        "systems": EF_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} VFD Trouble Alarm Added",
            "original_prompt": f"If Exhaust Fan Start Command (\"%X%{sys}.EF.SS\") is ON and Exhaust Louver Position Feedback (\"%X%{sys}.LVR.POS\") is ON (proven open), turn ON the Exhaust Fan Run Output (\"%X%{sys}.EF.RUN\"). Otherwise, turn it OFF -- the fan should never run with the louver unproven.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.EF.SS\" .EQ. ON .AND. \"%X%{sys}.LVR.POS\" .EQ. ON) THEN ON (\"%X%{sys}.EF.RUN\") ELSE OFF (\"%X%{sys}.EF.RUN\")"
            ),
            "changed_prompt": f"A VFD was added to the exhaust fan motor during a retrofit. If Exhaust Fan VFD Trouble (\"%X%{sys}.EF.VFD.FLT\") is ON, force Exhaust Fan Run Output OFF immediately -- independent of and overriding the louver-interlock logic above -- and turn ON a VFD Trouble Alarm (\"%X%{sys}.EF.VFD.ALM\", a virtual point).",
            "points": [
                {"name": f"%X%{sys}.EF.SS",
                    "desc": "Exhaust Fan Start Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.LVR.POS",
                    "desc": "Exhaust Louver Position Feedback (proven open)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.EF.RUN",
                    "desc": "Exhaust Fan Run Output", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.EF.VFD.FLT",
                    "desc": "Exhaust Fan VFD Trouble Contact (new, fault relay from the VFD drive itself)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.EF.VFD.ALM",
                    "desc": "Exhaust Fan VFD Trouble Alarm (new, virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "The VFD check needs to win over the original louver-interlock IF -- if you add it as a second, independent IF, make sure it writes EF.RUN OFF unconditionally when VFD.FLT is ON so it isn't immediately overwritten back ON by the original block re-evaluating the same scan.",
            "checks": [
                {"force": {f"%X%{sys}.EF.SS": 1, f"%X%{sys}.LVR.POS": 1, f"%X%{sys}.EF.VFD.FLT": 1}, "expect": {f"%X%{sys}.EF.RUN": 0, f"%X%{sys}.EF.VFD.ALM": 1}, "note": "VFD trouble -- fan forced off and alarm on, even though start command and louver would otherwise allow it to run"},
                {"force": {f"%X%{sys}.EF.SS": 1, f"%X%{sys}.LVR.POS": 1, f"%X%{sys}.EF.VFD.FLT": 0}, "expect": {f"%X%{sys}.EF.RUN": 1, f"%X%{sys}.EF.VFD.ALM": 0}, "note": "no VFD trouble -- original louver-interlock logic runs the fan normally"}
            ]
        }
    },
    {
        "title": "Compliance Fix: Cooling Tower Fan Freeze Lockout",
        "difficulty": "easy",
        "systems": CT_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Fan Freeze Lockout",
            "original_prompt": f"If Condenser Water Supply Temp (\"%X%{sys}.CWS.T\") is greater than High Limit (\"%X%{sys}.CWS.T.HI\"), turn ON Tower Fan Enable (\"%X%{sys}.FAN.ENA\") and set Tower Fan Speed Output (\"%X%{sys}.FAN.SPD\") to 100. Otherwise, turn OFF Tower Fan Enable and set Tower Fan Speed Output to 0.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.CWS.T\" .GT. \"%X%{sys}.CWS.T.HI\") THEN ON (\"%X%{sys}.FAN.ENA\"), SET(100,\"%X%{sys}.FAN.SPD\")\n"
                f"01010 IF (\"%X%{sys}.CWS.T\" .LE. \"%X%{sys}.CWS.T.HI\") THEN OFF (\"%X%{sys}.FAN.ENA\"), SET(0,\"%X%{sys}.FAN.SPD\")"
            ),
            "changed_prompt": f"Commissioning found a missing freeze protection lockout: the approved sequence requires the tower fan to never run when Outside Air Temp (\"%X%{sys}.OAT\") is below 35 degrees F, to prevent ice forming on the tower structure and basin. Add this check ahead of the existing logic -- when OAT is below 35, force Tower Fan Enable OFF and Tower Fan Speed Output to 0 regardless of condenser water temperature.",
            "points": [
                {"name": f"%X%{sys}.CWS.T",
                    "desc": "Condenser Water Supply Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.CWS.T.HI",
                    "desc": "Condenser Water High Limit (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.FAN.ENA",
                    "desc": "Tower Fan Enable", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}.FAN.SPD",
                    "desc": "Tower Fan Speed Output (0-100%)", "type": "AO", "kind": "physical"},
                {"name": f"%X%{sys}.OAT",
                    "desc": "Outside Air Temp Sensor (new)", "type": "AI", "kind": "physical"}
            ],
            "help": "Add the freeze check as its own IF ahead of (or otherwise winning over) the existing temperature logic -- when OAT is below 35, it needs to override regardless of what CWS.T says. Write the freeze-lockout writes so they aren't immediately overwritten by the original block re-evaluating the same scan.",
            "checks": [
                {"force": {f"%X%{sys}.CWS.T": 90, f"%X%{sys}.CWS.T.HI": 85, f"%X%{sys}.OAT": 20}, "expect": {f"%X%{sys}.FAN.ENA": 0, f"%X%{sys}.FAN.SPD": 0}, "note": "OAT below the freeze threshold -- fan locked out even though condenser water temp calls for it to run"},
                {"force": {f"%X%{sys}.CWS.T": 90, f"%X%{sys}.CWS.T.HI": 85, f"%X%{sys}.OAT": 50}, "expect": {f"%X%{sys}.FAN.ENA": 1, f"%X%{sys}.FAN.SPD": 100}, "note": "OAT above the freeze threshold -- normal condenser-water-temp control applies"}
            ]
        }
    },
    {
        "title": "Additional Device: Boiler Low Flow Interlock Added",
        "difficulty": "easy",
        "systems": BOILER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys}1 Low Flow Interlock Added",
            "original_prompt": f"If Boiler 1 Enable Command (\"%X%{sys}1.ENA\") is ON, turn ON Boiler 1 Run Output (\"%X%{sys}1.RUN\"). Otherwise, turn it OFF.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}1.ENA\" .EQ. ON) THEN ON (\"%X%{sys}1.RUN\") ELSE OFF (\"%X%{sys}1.RUN\")"
            ),
            "changed_prompt": f"A flow switch was added to protect the boiler from a low-flow condition. If Boiler 1 Flow Switch (\"%X%{sys}1.FLOW.SW\") is OFF (no flow proven), force Boiler 1 Run Output OFF immediately -- independent of and overriding the enable logic above -- and turn ON a Low Flow Alarm (\"%X%{sys}1.FLOW.ALM\", a virtual point).",
            "points": [
                {"name": f"%X%{sys}1.ENA",
                    "desc": "Boiler 1 Enable Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}1.RUN",
                    "desc": "Boiler 1 Run Output", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}1.FLOW.SW",
                    "desc": "Boiler 1 Flow Switch Feedback (new, proven flow through the boiler)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}1.FLOW.ALM",
                    "desc": "Boiler 1 Low Flow Alarm (new, virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "The flow interlock needs to override the original enable-based RUN logic, not just add to it -- when FLOW.SW is OFF, RUN has to end up OFF regardless of what ENA says. Make sure the flow-interlock write happens after (or otherwise wins over) the original IF so it isn't immediately overwritten back ON.",
            "checks": [
                {"force": {f"%X%{sys}1.ENA": 1, f"%X%{sys}1.FLOW.SW": 0}, "expect": {f"%X%{sys}1.RUN": 0, f"%X%{sys}1.FLOW.ALM": 1}, "note": "low flow condition -- boiler forced off and alarm on, overriding the enable command"},
                {"force": {f"%X%{sys}1.ENA": 1, f"%X%{sys}1.FLOW.SW": 1}, "expect": {f"%X%{sys}1.RUN": 1}, "note": "flow proven -- original enable logic runs the boiler normally"}
            ]
        }
    },
    {
        "title": "Additional Device: CRAC High Humidity Alarm Added",
        "difficulty": "easy",
        "systems": CRAC_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys}201 High Humidity Alarm Added",
            "original_prompt": f"If Room Temperature (\"%X%{sys}201.RMT\") is greater than Cooling Setpoint (\"%X%{sys}201.CLG.SP\"), turn ON Compressor Enable (\"%X%{sys}201.COMP.ENA\"). Otherwise, turn it OFF.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}201.RMT\" .GT. \"%X%{sys}201.CLG.SP\") THEN ON (\"%X%{sys}201.COMP.ENA\") ELSE OFF (\"%X%{sys}201.COMP.ENA\")"
            ),
            "changed_prompt": f"Facilities wants humidity monitored on this unit too. If Room Relative Humidity (\"%X%{sys}201.RMH\") rises above 65 percent, turn ON a High Humidity Alarm (\"%X%{sys}201.RMH.ALM\", a virtual point) -- this should be independent of the temperature/compressor logic above, not tied to it.",
            "points": [
                {"name": f"%X%{sys}201.RMT",
                    "desc": "Room Temperature Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}201.CLG.SP",
                    "desc": "Cooling Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}201.COMP.ENA",
                    "desc": "Compressor Enable Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}201.RMH",
                    "desc": "Room Relative Humidity Sensor (new)", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}201.RMH.ALM",
                    "desc": "High Humidity Alarm (new, virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "This is a brand new, standalone IF/THEN/ELSE -- it doesn't touch RMT, CLG.SP, or COMP.ENA at all, so just add it as its own block rather than trying to work it into the existing temperature logic.",
            "checks": [
                {"force": {f"%X%{sys}201.RMH": 70}, "expect": {f"%X%{sys}201.RMH.ALM": 1}, "note": "humidity above 65% -- alarm on"},
                {"force": {f"%X%{sys}201.RMH": 50}, "expect": {f"%X%{sys}201.RMH.ALM": 0}, "note": "humidity at or below 65% -- alarm off"}
            ]
        }
    },
    {
        "title": "Additional Device: Air Compressor High Discharge Temp Alarm Added",
        "difficulty": "easy",
        "systems": AC_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} High Discharge Temp Alarm Added",
            "original_prompt": f"If Compressor Run Status (\"%X%{sys}.STAT\") is ON and Cooling Water Entering Temp (\"%X%{sys}.CHW.ET\") is greater than High Limit (\"%X%{sys}.CHW.ET.HI\"), turn ON a Cooling Water High Temp Alarm (\"%X%{sys}.CHW.ET.ALM\"). Otherwise, turn it OFF.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.STAT\" .EQ. ON .AND. \"%X%{sys}.CHW.ET\" .GT. \"%X%{sys}.CHW.ET.HI\") THEN ON (\"%X%{sys}.CHW.ET.ALM\") ELSE OFF (\"%X%{sys}.CHW.ET.ALM\")"
            ),
            "changed_prompt": f"A discharge air temp sensor was added downstream of the compressor head. If Compressor Discharge Air Temp (\"%X%{sys}.DAT\") rises above 250 degrees F while the compressor is running, turn ON a High Discharge Temp Alarm (\"%X%{sys}.DAT.ALM\", a virtual point) -- this should be independent of and in addition to the existing cooling water alarm logic above, not a replacement for it.",
            "points": [
                {"name": f"%X%{sys}.STAT",
                    "desc": "Compressor Run Status", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.CHW.ET",
                    "desc": "Cooling Water Entering Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.CHW.ET.HI",
                    "desc": "Cooling Water High Limit (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.CHW.ET.ALM",
                    "desc": "Cooling Water High Temp Alarm (virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.DAT",
                    "desc": "Compressor Discharge Air Temp Sensor (new)", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.DAT.ALM",
                    "desc": "High Discharge Temp Alarm (new, virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "This is a new, separate IF/THEN/ELSE alongside the existing cooling-water alarm block -- don't fold it into the existing condition. AND the run-status check into it the same way the existing block does, so a stopped compressor doesn't nuisance-alarm on a stale sensor reading.",
            "checks": [
                {"force": {f"%X%{sys}.STAT": 1, f"%X%{sys}.DAT": 260}, "expect": {f"%X%{sys}.DAT.ALM": 1}, "note": "compressor running and discharge air temp over 250 -- new alarm trips"},
                {"force": {f"%X%{sys}.STAT": 1, f"%X%{sys}.DAT": 200}, "expect": {f"%X%{sys}.DAT.ALM": 0}, "note": "discharge air temp under 250 -- new alarm stays off"},
                {"force": {f"%X%{sys}.STAT": 0, f"%X%{sys}.DAT": 260}, "expect": {f"%X%{sys}.DAT.ALM": 0}, "note": "compressor not running -- no alarm even with a high discharge temp reading"}
            ]
        }
    },
    {
        "title": "Additional Device: FCU Dirty Filter Switch Added",
        "difficulty": "easy",
        "systems": FCU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Dirty Filter Switch Added",
            "original_prompt": f"If Room Temperature (\"%X%{sys}.RMT\") is greater than Cooling Setpoint (\"%X%{sys}.RMT.SP\"), turn ON the Cooling Valve (\"%X%{sys}.CLG.VLV\"). Otherwise, turn it OFF.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.RMT\" .GT. \"%X%{sys}.RMT.SP\") THEN ON (\"%X%{sys}.CLG.VLV\") ELSE OFF (\"%X%{sys}.CLG.VLV\")"
            ),
            "changed_prompt": f"A dirty filter switch was field-installed on this unit. If the Filter Status Switch (\"%X%{sys}.FLT.S\") is ON (indicating a dirty/clogged filter), turn ON a Dirty Filter Alarm (\"%X%{sys}.FLT.ALM\", a virtual point) -- this should be independent of the temperature/valve logic above, not tied to it.",
            "points": [
                {"name": f"%X%{sys}.RMT",
                    "desc": "Room Temperature Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.RMT.SP",
                    "desc": "Cooling Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.CLG.VLV",
                    "desc": "Cooling Valve Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.FLT.S",
                    "desc": "Filter Status Switch (new, ON = dirty/clogged)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.FLT.ALM",
                    "desc": "Dirty Filter Alarm (new, virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Brand new standalone IF/THEN/ELSE, independent of the RMT/valve logic -- just mirror FLT.S straight onto FLT.ALM.",
            "checks": [
                {"force": {f"%X%{sys}.FLT.S": 1}, "expect": {f"%X%{sys}.FLT.ALM": 1}, "note": "filter switch indicates dirty -- alarm on"},
                {"force": {f"%X%{sys}.FLT.S": 0}, "expect": {f"%X%{sys}.FLT.ALM": 0}, "note": "filter switch OK -- alarm off"}
            ]
        }
    },
    {
        "title": "Additional Device: Chiller Low Refrigerant Pressure Alarm Added",
        "difficulty": "easy",
        "systems": CHILLER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Low Refrigerant Pressure Alarm Added",
            "original_prompt": f"If Building Cooling Demand (\"%X%{sys}.DMD\") is ON, turn ON Chiller Plant Master Enable (\"%X%{sys}.ENA\"). Otherwise, turn it OFF.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.DMD\" .EQ. ON) THEN ON (\"%X%{sys}.ENA\") ELSE OFF (\"%X%{sys}.ENA\")"
            ),
            "changed_prompt": f"A low refrigerant pressure switch was field-installed on the compressor. If the Low Refrigerant Pressure Switch (\"%X%{sys}.LOP.SW\") is ON (tripped) while Chiller Plant Master Enable is ON, turn ON a Low Refrigerant Pressure Alarm (\"%X%{sys}.LOP.ALM\", a virtual point). Otherwise, turn it OFF -- this should be independent of and in addition to the demand/enable logic above, not a replacement for it.",
            "points": [
                {"name": f"%X%{sys}.DMD",
                    "desc": "Building Cooling Demand (virtual point, driven by a separate plant-load program)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.ENA",
                    "desc": "Chiller Plant Master Enable Command", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}.LOP.SW",
                    "desc": "Low Refrigerant Pressure Switch (new)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.LOP.ALM",
                    "desc": "Low Refrigerant Pressure Alarm (new, virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Add this as a second, independent IF/THEN/ELSE -- AND the ENA check into it just like the prompt describes, so the alarm doesn't fire on a tripped switch while the plant is sitting off anyway.",
            "checks": [
                {"force": {f"%X%{sys}.DMD": 1, f"%X%{sys}.ENA": 1, f"%X%{sys}.LOP.SW": 1}, "expect": {f"%X%{sys}.LOP.ALM": 1}, "note": "pressure switch tripped while plant enabled -- alarm on"},
                {"force": {f"%X%{sys}.DMD": 1, f"%X%{sys}.ENA": 1, f"%X%{sys}.LOP.SW": 0}, "expect": {f"%X%{sys}.LOP.ALM": 0}, "note": "pressure switch OK -- alarm off"},
                {"force": {f"%X%{sys}.DMD": 0, f"%X%{sys}.ENA": 0, f"%X%{sys}.LOP.SW": 1}, "expect": {f"%X%{sys}.LOP.ALM": 0}, "note": "plant not enabled -- no alarm even if the switch is tripped"}
            ]
        }
    },
    {
        "title": "Additional Device: Pump Fail Alarm Added",
        "difficulty": "easy",
        "systems": PUMP_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Fail Alarm Added",
            "original_prompt": f"If System Demand (\"%X%{sys}.DMD\") is ON, turn ON Pump Enable (\"%X%{sys}.ENA\"). Otherwise, turn it OFF.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.DMD\" .EQ. ON) THEN ON (\"%X%{sys}.ENA\") ELSE OFF (\"%X%{sys}.ENA\")"
            ),
            "changed_prompt": f"A run-status feedback contact was field-installed on the pump starter. If Pump Enable (\"%X%{sys}.ENA\") is ON and Pump Run Status (\"%X%{sys}.S\") is OFF, turn ON a Pump Fail Alarm (\"%X%{sys}.ALM\", a virtual point). Otherwise, turn it OFF -- this should be independent of and in addition to the demand/enable logic above, not a replacement for it.",
            "points": [
                {"name": f"%X%{sys}.DMD",
                    "desc": "System Demand (virtual point, driven by a separate plant-load program)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.ENA",
                    "desc": "Pump Enable Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.S",
                    "desc": "Pump Run Status Feedback (new)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.ALM",
                    "desc": "Pump Fail Alarm (new, virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Same command/status mismatch pattern used elsewhere -- add a second, independent IF/THEN/ELSE AND-ing ENA ON with S OFF.",
            "checks": [
                {"force": {f"%X%{sys}.DMD": 1, f"%X%{sys}.ENA": 1, f"%X%{sys}.S": 0}, "expect": {f"%X%{sys}.ALM": 1}, "note": "enabled but not proven running -- fail alarm on"},
                {"force": {f"%X%{sys}.DMD": 1, f"%X%{sys}.ENA": 1, f"%X%{sys}.S": 1}, "expect": {f"%X%{sys}.ALM": 0}, "note": "enabled and proven running -- no alarm"},
                {"force": {f"%X%{sys}.DMD": 0, f"%X%{sys}.ENA": 0, f"%X%{sys}.S": 0}, "expect": {f"%X%{sys}.ALM": 0}, "note": "not enabled -- no alarm even though status reads off"}
            ]
        }
    },

    # ---------------- MEDIUM ----------------
    {
        "title": "Add a Nuisance-Trip Delay to a Fault Alarm",
        "difficulty": "medium",
        "systems": AHU_SYSTEMS + MAU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Supply Fan Alarm Delay",
            "original_prompt": f"If Supply Fan Start/Stop Command (\"%X%{sys}.SF.SS\") is ON and Supply Fan Status (\"%X%{sys}.SF.S\") is OFF, turn ON a Supply Fan Fail Alarm (\"%X%{sys}.SF.ALM\"). Otherwise, turn it OFF.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.SF.SS\" .EQ. ON .AND. \"%X%{sys}.SF.S\" .EQ. OFF) THEN ON (\"%X%{sys}.SF.ALM\") ELSE OFF (\"%X%{sys}.SF.ALM\")"
            ),
            "changed_prompt": f"This alarm is tripping on momentary status glitches during fan startup. Add a 30-second delay: only turn ON the Supply Fan Fail Alarm after the fault condition (Start/Stop Command ON, Status OFF) has been continuously true for 30 seconds. Add a new Delay Timer (\"%X%{sys}.SF.ALM.TMR\"), incremented by 1 each second with SAMPLE(1) while the fault condition holds, and reset it to 0 whenever the fault condition is not true. Hard-code the 30-second threshold rather than expecting a pre-existing delay point.",
            "points": [
                {"name": f"%X%{sys}.SF.SS",
                    "desc": "Supply Fan Start/Stop Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.SF.S",
                    "desc": "Supply Fan Running Status", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.SF.ALM",
                    "desc": "Supply Fan Fail Alarm (virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.SF.ALM.TMR",
                    "desc": "Fault Delay Timer Accumulator (new, virtual point)", "type": "AV", "kind": "virtual"}
            ],
            "help": "Same pattern as the discharge-air-temp delayed alarm: a SAMPLE(1) timer accumulates only while the fault condition holds, resets to 0 the instant it doesn't, and the alarm itself only turns ON once that timer exceeds 30 -- so it still clears immediately (no delay) the moment the condition goes away, just gated by the extra timer check on the way up.",
            "checks": [
                {"force": {f"%X%{sys}.SF.SS": 1, f"%X%{sys}.SF.S": 0, f"%X%{sys}.SF.ALM.TMR": 0, f"%X%{sys}.SF.ALM": 0}, "cycles": 31, "expect": {f"%X%{sys}.SF.ALM": 1}, "note": "fault condition holds for over 30 seconds -- alarm trips"},
                {"force": {f"%X%{sys}.SF.SS": 1, f"%X%{sys}.SF.S": 0, f"%X%{sys}.SF.ALM.TMR": 0, f"%X%{sys}.SF.ALM": 0}, "cycles": 10, "expect": {f"%X%{sys}.SF.ALM": 0}, "note": "fault condition present but under 30 seconds -- alarm hasn't tripped yet"},
                {"force": {f"%X%{sys}.SF.SS": 1, f"%X%{sys}.SF.S": 1, f"%X%{sys}.SF.ALM.TMR": 20, f"%X%{sys}.SF.ALM": 1}, "expect": {f"%X%{sys}.SF.ALM": 0, f"%X%{sys}.SF.ALM.TMR": 0}, "note": "fault condition clears -- timer resets and alarm clears immediately (no delay on the way down)"}
            ]
        }
    },
    {
        "title": "Additional Device: Critical High-Limit Added",
        "difficulty": "medium",
        "systems": AHU_SYSTEMS + MAU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Critical Discharge Air Limit Added",
            "original_prompt": f"If Discharge Air Temp (\"%X%{sys}.DAT\") is greater than High Limit (\"%X%{sys}.DAT.HI\"), increment an Alarm Delay Timer (\"%X%{sys}.DAT.ALM.TMR\") by 1 second using SAMPLE(1). Once the timer exceeds 60 seconds, turn ON a High Temp Alarm (\"%X%{sys}.DAT.ALM\"). Otherwise, reset the timer to 0 and turn the alarm OFF.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.DAT\" .GT. \"%X%{sys}.DAT.HI\") THEN SAMPLE(1) \"%X%{sys}.DAT.ALM.TMR\" = \"%X%{sys}.DAT.ALM.TMR\" + 1\n"
                f"01010 IF (\"%X%{sys}.DAT.ALM.TMR\" .GT. 60) THEN ON (\"%X%{sys}.DAT.ALM\") ELSE OFF (\"%X%{sys}.DAT.ALM\")\n"
                f"01020 IF (\"%X%{sys}.DAT\" .LE. \"%X%{sys}.DAT.HI\") THEN \"%X%{sys}.DAT.ALM.TMR\" = 0"
            ),
            "changed_prompt": f"A Critical High Limit was added to the approved sequence for safety, but it should still use the SAME alarm point -- don't create a second, separate alarm point for it (real panels consolidate everything that can trip \"discharge air temp too high\" into one alarm the operator sees). Update the code so the existing High Temp Alarm (\"%X%{sys}.DAT.ALM\") also turns ON immediately, with no delay, if Discharge Air Temp exceeds 110 degrees -- in addition to the existing timed behavior for the original High Limit.",
            "points": [
                {"name": f"%X%{sys}.DAT",
                    "desc": "Discharge Air Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.DAT.HI",
                    "desc": "High Temp Alarm Limit (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.DAT.ALM.TMR",
                    "desc": "Alarm Delay Timer Accumulator (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.DAT.ALM",
                    "desc": "High Discharge Temp Alarm (virtual point -- the one shared alarm)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Add a new, separate IF that turns DAT.ALM ON immediately when DAT exceeds 110 -- don't touch the existing timer logic. Since both paths write to the same DAT.ALM point, make sure the new critical-limit IF doesn't have an ELSE that would turn the alarm back OFF on a scan where DAT is under 110 but the original timed path still says ON.",
            "checks": [
                {"force": {f"%X%{sys}.DAT": 115, f"%X%{sys}.DAT.HI": 90, f"%X%{sys}.DAT.ALM.TMR": 0, f"%X%{sys}.DAT.ALM": 0}, "expect": {f"%X%{sys}.DAT.ALM": 1}, "note": "discharge air temp over the 110 critical limit -- alarm trips immediately, no 60-second delay needed"},
                {"force": {f"%X%{sys}.DAT": 95, f"%X%{sys}.DAT.HI": 90, f"%X%{sys}.DAT.ALM.TMR": 0, f"%X%{sys}.DAT.ALM": 0}, "cycles": 61, "expect": {f"%X%{sys}.DAT.ALM": 1}, "note": "original high-limit path still works -- over DAT.HI for more than 60 seconds trips the same alarm"},
                {"force": {f"%X%{sys}.DAT": 80, f"%X%{sys}.DAT.HI": 90, f"%X%{sys}.DAT.ALM.TMR": 0, f"%X%{sys}.DAT.ALM": 0}, "expect": {f"%X%{sys}.DAT.ALM": 0}, "note": "under both limits -- alarm stays off"}
            ]
        }
    },
    {
        "title": "Additional Device: Condensate Overflow Switch Added",
        "difficulty": "medium",
        "systems": FCU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Condensate Overflow Switch Added",
            "original_prompt": f"If Room Temperature (\"%X%{sys}.RMT\") is greater than Cooling Setpoint (\"%X%{sys}.RMT.SP\"), turn ON the Cooling Valve (\"%X%{sys}.CLG.VLV\"). Otherwise, turn it OFF.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.RMT\" .GT. \"%X%{sys}.RMT.SP\") THEN ON (\"%X%{sys}.CLG.VLV\") ELSE OFF (\"%X%{sys}.CLG.VLV\")"
            ),
            "changed_prompt": f"A Condensate Overflow Switch (\"%X%{sys}.COND.SW\") was added to the FCU's drain pan. Update the code so the Cooling Valve is forced OFF whenever the switch indicates an overflow condition (OFF = tripped/overflow, ON = normal drainage), regardless of what Room Temperature calls for, to prevent water damage. Normal temperature-based control resumes once the switch is proven OK again.",
            "points": [
                {"name": f"%X%{sys}.RMT",
                    "desc": "Room Temperature Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.RMT.SP",
                    "desc": "Cooling Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.CLG.VLV",
                    "desc": "Cooling Valve Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.COND.SW",
                    "desc": "Condensate Overflow Switch (new, DI -- ON = normal, OFF = overflow)", "type": "DI", "kind": "physical"}
            ],
            "help": "The overflow check needs to override the original temperature logic, not just run alongside it -- when COND.SW is OFF, CLG.VLV has to end up OFF regardless of what RMT/RMT.SP say. Put the override check after the original block (or otherwise make sure it wins), so it isn't overwritten back ON.",
            "checks": [
                {"force": {f"%X%{sys}.RMT": 80, f"%X%{sys}.RMT.SP": 72, f"%X%{sys}.COND.SW": 0}, "expect": {f"%X%{sys}.CLG.VLV": 0}, "note": "overflow switch tripped -- valve forced off even though RMT calls for cooling"},
                {"force": {f"%X%{sys}.RMT": 80, f"%X%{sys}.RMT.SP": 72, f"%X%{sys}.COND.SW": 1}, "expect": {f"%X%{sys}.CLG.VLV": 1}, "note": "switch proven OK -- normal temperature control resumes"}
            ]
        }
    },
    {
        "title": "Additional Device: Exhaust Fan VOC Full-Speed Latch Added",
        "difficulty": "medium",
        "systems": EF_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} VOC Full-Speed Latch Added",
            "original_prompt": f"Exhaust Fan {sys} has no start/stop command of its own -- it's interlocked with AHU01 and simply follows whatever AHU01's supply fan is doing. If AHU01 Supply Fan Enable (\"%X%AHU01.SF.ENA\") is ON and Louver Position Feedback (\"%X%{sys}.LVR.POS\") is ON (proven open), turn ON the Exhaust Fan Run Output (\"%X%{sys}.EF.RUN\"). Otherwise, turn it OFF.",
            "starter_code": (
                f"01000 IF (\"%X%AHU01.SF.ENA\" .EQ. ON .AND. \"%X%{sys}.LVR.POS\" .EQ. ON) THEN ON (\"%X%{sys}.EF.RUN\") ELSE OFF (\"%X%{sys}.EF.RUN\")"
            ),
            "changed_prompt": f"A VOC sensor was field-installed to catch fume events that need more airflow than the AHU-interlocked run schedule alone provides. If a VOC Detected alarm (\"%X%{sys}.VOC.DET\") trips ON, latch Full Speed Command (\"%X%{sys}.EF.FULL\") ON and force the Exhaust Fan Run Output ON regardless of the AHU interlock above -- it must stay latched even after the VOC alarm clears, since code requires an operator to manually confirm the space is clear. It should only return to normal AHU-interlocked operation once an operator pulses Manual Reset (\"%X%{sys}.EF.RST\") ON, which clears Full Speed Command back OFF.",
            "points": [
                {"name": "%X%AHU01.SF.ENA",
                    "desc": "AHU01 Supply Fan Enable Command (already exists on the interlocked AHU -- read-only here)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.LVR.POS",
                    "desc": "Exhaust Louver Position Feedback (proven open)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.EF.RUN",
                    "desc": "Exhaust Fan Run Output", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.VOC.DET",
                    "desc": "VOC Detected Alarm (new)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.EF.FULL",
                    "desc": "Full Speed Command Latch (new, virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.EF.RST",
                    "desc": "Manual Reset Pulse (new, operator-facing)", "type": "BV", "kind": "physical"}
            ],
            "help": "Same latch pattern as a standalone VOC scenario, but this time it also has to override EF.RUN -- when EF.FULL is latched, force EF.RUN ON regardless of what the AHU-interlock IF wrote. Once EF.RST clears the latch, EF.RUN should fall back to being driven purely by the original AHU01.SF.ENA/LVR.POS interlock.",
            "checks": [
                {"force": {"%X%AHU01.SF.ENA": 0, f"%X%{sys}.LVR.POS": 0, f"%X%{sys}.VOC.DET": 1, f"%X%{sys}.EF.FULL": 0, f"%X%{sys}.EF.RST": 0}, "expect": {f"%X%{sys}.EF.FULL": 1, f"%X%{sys}.EF.RUN": 1}, "note": "VOC detected -- fan forced to run at full speed even though the AHU interlock alone wouldn't run it"},
                {"force": {"%X%AHU01.SF.ENA": 0, f"%X%{sys}.LVR.POS": 0, f"%X%{sys}.VOC.DET": 0, f"%X%{sys}.EF.FULL": 1, f"%X%{sys}.EF.RST": 0}, "cycles": 3, "expect": {f"%X%{sys}.EF.FULL": 1, f"%X%{sys}.EF.RUN": 1}, "note": "VOC alarm cleared but not manually reset -- stays latched and running"},
                {"force": {"%X%AHU01.SF.ENA": 0, f"%X%{sys}.LVR.POS": 0, f"%X%{sys}.VOC.DET": 0, f"%X%{sys}.EF.FULL": 1, f"%X%{sys}.EF.RST": 1}, "expect": {f"%X%{sys}.EF.FULL": 0, f"%X%{sys}.EF.RUN": 0}, "note": "manual reset pulsed -- clears the latch and returns to normal AHU-interlocked operation (AHU off and louver not proven, so the fan goes back off)"}
            ]
        }
    },
    {
        "title": "Compliance Fix: Add Minimum Run-Time Protection to Pump Lead-Lag Rotation",
        "difficulty": "medium",
        "systems": PUMP_PLANT_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{PLANT_LABELS.get(sys, sys)} Minimum Run-Time Protection",
            "original_prompt": f"Two pumps in the {sys} plant ({sys}.P1 and {sys}.P2) share standby duty: one is normally lead, the other lag (backup). If the lead pump develops a fault -- Pump 1 Fault (\"%X%{sys}.P1.FLT\") if {sys}.P1 is currently lead, or Pump 2 Fault (\"%X%{sys}.P2.FLT\") if {sys}.P2 is currently lead -- the other pump automatically takes over as lead. Drive Pump 1 Enable (\"%X%{sys}.P1.ENA\") and Pump 2 Enable (\"%X%{sys}.P2.ENA\") accordingly, using a Lead-Lag Status point (\"%X%{sys}.LL.STAT\", 1 = Pump 1 lead, 2 = Pump 2 lead) to track which pump is currently lead.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.P1.FLT\" .EQ. ON) THEN ON (\"%X%{sys}.P2.ENA\"), OFF (\"%X%{sys}.P1.ENA\"), \"%X%{sys}.LL.STAT\" = 2\n"
                f"01010 IF (\"%X%{sys}.P2.FLT\" .EQ. ON) THEN ON (\"%X%{sys}.P1.ENA\"), OFF (\"%X%{sys}.P2.ENA\"), \"%X%{sys}.LL.STAT\" = 1"
            ),
            "changed_prompt": f"Field techs report the plant is short-cycling on a flickering fault contact. Add minimum run-time protection: accumulate a Run Timer (\"%X%{sys}.RUN.TMR\") by 1 second using SAMPLE(1) whenever the currently-enabled pump is ON, and reset it to 0 whenever the enable/lead pump changes. Only allow a fault-triggered lead-lag swap (either of the two IF statements above) to actually execute once the Run Timer exceeds 300 seconds -- a fault on a pump that hasn't been running at least 5 minutes should be ignored rather than triggering an immediate swap.",
            "points": [
                {"name": f"%X%{sys}.P1.FLT",
                    "desc": "Pump 1 Fault (fault relay contact)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}.P2.FLT",
                    "desc": "Pump 2 Fault (fault relay contact)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}.P1.ENA",
                    "desc": "Pump 1 Enable Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.P2.ENA",
                    "desc": "Pump 2 Enable Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.LL.STAT",
                    "desc": "Lead-Lag Status Code (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.RUN.TMR",
                    "desc": "Run Timer Accumulator (new, virtual point)", "type": "AV", "kind": "virtual"}
            ],
            "help": "Gate BOTH fault-triggered swap IFs behind an additional AND check on RUN.TMR .GT. 300 -- the fault alone isn't enough anymore. The timer itself needs its own SAMPLE(1) accumulate-while-running logic, plus a reset whenever the lead pump changes, so a fresh swap always starts its own 5-minute clock from zero.",
            "checks": [
                {"force": {f"%X%{sys}.P1.FLT": 1, f"%X%{sys}.P2.FLT": 0, f"%X%{sys}.LL.STAT": 1, f"%X%{sys}.RUN.TMR": 0, f"%X%{sys}.P1.ENA": 1, f"%X%{sys}.P2.ENA": 0}, "expect": {f"%X%{sys}.LL.STAT": 1, f"%X%{sys}.P1.ENA": 1, f"%X%{sys}.P2.ENA": 0}, "note": "P1 faults immediately after startup (run timer at 0) -- swap should be ignored since the pump hasn't run 5 minutes yet"},
                {"force": {f"%X%{sys}.P1.FLT": 1, f"%X%{sys}.P2.FLT": 0, f"%X%{sys}.LL.STAT": 1, f"%X%{sys}.RUN.TMR": 301, f"%X%{sys}.P1.ENA": 1, f"%X%{sys}.P2.ENA": 0}, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}.P1.ENA": 0, f"%X%{sys}.P2.ENA": 1}, "note": "P1 faults after running more than 5 minutes -- swap goes through as before"},
                {"force": {f"%X%{sys}.P1.FLT": 0, f"%X%{sys}.P2.FLT": 0, f"%X%{sys}.LL.STAT": 1, f"%X%{sys}.RUN.TMR": 0, f"%X%{sys}.P1.ENA": 1, f"%X%{sys}.P2.ENA": 0}, "cycles": 5, "expect": {f"%X%{sys}.RUN.TMR": 5}, "note": "lead pump (P1) running with no fault -- run timer accumulates while the enabled pump is on"}
            ]
        }
    },
    {
        "title": "Additional Device: Cooling Tower Fan Belt Loss Alarm Added",
        "difficulty": "medium",
        "systems": CT_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Fan Belt Loss Alarm Added",
            "original_prompt": f"If Condenser Water Supply Temp (\"%X%{sys}.CWS.T\") is greater than High Limit (\"%X%{sys}.CWS.T.HI\"), turn ON Tower Fan Enable (\"%X%{sys}.FAN.ENA\") and set Tower Fan Speed Output (\"%X%{sys}.FAN.SPD\") to 100. Otherwise, turn OFF Tower Fan Enable and set Tower Fan Speed Output to 0.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.CWS.T\" .GT. \"%X%{sys}.CWS.T.HI\") THEN ON (\"%X%{sys}.FAN.ENA\"), SET(100,\"%X%{sys}.FAN.SPD\")\n"
                f"01010 IF (\"%X%{sys}.CWS.T\" .LE. \"%X%{sys}.CWS.T.HI\") THEN OFF (\"%X%{sys}.FAN.ENA\"), SET(0,\"%X%{sys}.FAN.SPD\")"
            ),
            "changed_prompt": f"A current sensing relay was field-installed to catch a broken fan belt (motor runs but the fan itself doesn't turn). Add belt-loss detection: accumulate a Belt Loss Timer (\"%X%{sys}.BELT.TMR\") by 1 second using SAMPLE(1) whenever Tower Fan Enable is ON and the Fan Current Switch (\"%X%{sys}.FAN.CUR\") is OFF (no current draw despite being commanded on). Once the timer exceeds 30 seconds, turn ON a Belt Loss Alarm (\"%X%{sys}.BELT.ALM\", a virtual point). Reset the timer to 0 whenever Fan Current Switch is ON or Tower Fan Enable is OFF.",
            "points": [
                {"name": f"%X%{sys}.CWS.T",
                    "desc": "Condenser Water Supply Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.CWS.T.HI",
                    "desc": "Condenser Water High Limit (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.FAN.ENA",
                    "desc": "Tower Fan Enable", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}.FAN.SPD",
                    "desc": "Tower Fan Speed Output (0-100%)", "type": "AO", "kind": "physical"},
                {"name": f"%X%{sys}.FAN.CUR",
                    "desc": "Fan Current Sensing Relay (new, ON = current detected/fan turning)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.BELT.TMR",
                    "desc": "Belt Loss Timer Accumulator (new, virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.BELT.ALM",
                    "desc": "Belt Loss Alarm (new, virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Same SAMPLE(1) timer-and-threshold pattern as the discharge-air-temp delayed alarm -- accumulate BELT.TMR only while FAN.ENA is ON AND FAN.CUR is OFF, reset it to 0 the instant either half of that condition isn't true, and set BELT.ALM once the timer exceeds 30. It's a two-condition AND driving the accumulate step, not just one.",
            "checks": [
                {"force": {f"%X%{sys}.FAN.ENA": 1, f"%X%{sys}.FAN.CUR": 0, f"%X%{sys}.BELT.TMR": 0, f"%X%{sys}.BELT.ALM": 0}, "cycles": 31, "expect": {f"%X%{sys}.BELT.ALM": 1}, "note": "fan commanded on but no current draw for over 30 seconds -- belt loss alarm trips"},
                {"force": {f"%X%{sys}.FAN.ENA": 1, f"%X%{sys}.FAN.CUR": 0, f"%X%{sys}.BELT.TMR": 0, f"%X%{sys}.BELT.ALM": 0}, "cycles": 10, "expect": {f"%X%{sys}.BELT.ALM": 0}, "note": "condition present but under 30 seconds -- alarm hasn't tripped yet"},
                {"force": {f"%X%{sys}.FAN.ENA": 1, f"%X%{sys}.FAN.CUR": 1, f"%X%{sys}.BELT.TMR": 20, f"%X%{sys}.BELT.ALM": 0}, "expect": {f"%X%{sys}.BELT.TMR": 0}, "note": "current is flowing (belt intact) -- timer resets"},
                {"force": {f"%X%{sys}.FAN.ENA": 0, f"%X%{sys}.FAN.CUR": 0, f"%X%{sys}.BELT.TMR": 20, f"%X%{sys}.BELT.ALM": 0}, "expect": {f"%X%{sys}.BELT.TMR": 0}, "note": "fan not commanded on -- timer resets regardless of current switch"}
            ]
        }
    },
    {
        "title": "Compliance Fix: Add Minimum Run-Time Protection to Boiler Standby Rotation",
        "difficulty": "medium",
        "systems": BOILER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Minimum Run-Time Protection",
            "original_prompt": f"Two boilers, {sys}1 and {sys}2, provide N+1 redundancy -- normally only one runs, the other sits as a spare. If the Lead Boiler Fault Alarm (\"%X%{sys}.LEAD.FLT\") is ON, the standby boiler takes over: turn ON Boiler 2 Enable (\"%X%{sys}2.ENA\"), turn OFF Boiler 1 Enable (\"%X%{sys}1.ENA\"), and set Lead-Lag Status (\"%X%{sys}.LL.STAT\") to 2.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.LEAD.FLT\" .EQ. ON) THEN ON (\"%X%{sys}2.ENA\"), OFF (\"%X%{sys}1.ENA\"), \"%X%{sys}.LL.STAT\" = 2"
            ),
            "changed_prompt": f"Field techs report the standby rotation is short-cycling on a flickering fault contact. Add minimum run-time protection: accumulate a Run Timer (\"%X%{sys}.RUN.TMR\") by 1 second using SAMPLE(1) whenever the currently-lead boiler is ON, and reset it to 0 whenever Lead-Lag Status changes. Only allow the fault-triggered standby swap above to actually execute once the Run Timer exceeds 300 seconds -- a fault on a boiler that hasn't been running at least 5 minutes should be ignored rather than triggering an immediate swap.",
            "points": [
                {"name": f"%X%{sys}.LEAD.FLT",
                    "desc": "Lead Boiler Fault Alarm (fault relay contact from whichever boiler is currently running)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}1.ENA",
                    "desc": "Boiler 1 Enable", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}2.ENA",
                    "desc": "Boiler 2 Enable", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.LL.STAT",
                    "desc": "Lead-Lag Status Code (virtual point, 1 = Boiler 1 lead, 2 = Boiler 2 lead)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.RUN.TMR",
                    "desc": "Run Timer Accumulator (new, virtual point)", "type": "AV", "kind": "virtual"}
            ],
            "help": "Gate the fault-triggered swap IF behind an additional AND check on RUN.TMR .GT. 300 -- the fault alone isn't enough anymore. The timer needs its own SAMPLE(1) accumulate-while-running logic, plus a reset whenever Lead-Lag Status changes, so a fresh swap always starts its own 5-minute clock from zero.",
            "checks": [
                {"force": {f"%X%{sys}.LEAD.FLT": 1, f"%X%{sys}.LL.STAT": 1, f"%X%{sys}.RUN.TMR": 0, f"%X%{sys}1.ENA": 1, f"%X%{sys}2.ENA": 0}, "expect": {f"%X%{sys}.LL.STAT": 1}, "note": "fault right after startup (timer at 0) -- swap ignored, hasn't run 5 minutes yet"},
                {"force": {f"%X%{sys}.LEAD.FLT": 1, f"%X%{sys}.LL.STAT": 1, f"%X%{sys}.RUN.TMR": 301, f"%X%{sys}1.ENA": 1, f"%X%{sys}2.ENA": 0}, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}1.ENA": 0, f"%X%{sys}2.ENA": 1}, "note": "fault after running more than 5 minutes -- swap goes through"},
                {"force": {f"%X%{sys}.LEAD.FLT": 0, f"%X%{sys}.LL.STAT": 1, f"%X%{sys}.RUN.TMR": 0, f"%X%{sys}1.ENA": 1, f"%X%{sys}2.ENA": 0}, "cycles": 5, "expect": {f"%X%{sys}.RUN.TMR": 5}, "note": "lead boiler running with no fault -- run timer accumulates"}
            ]
        }
    },
    {
        "title": "Compliance Fix: Add a Manual Lead Select Override to CRAC Failover",
        "difficulty": "medium",
        "systems": CRAC_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Manual Lead Select Override",
            "original_prompt": f"{sys}201 and {sys}202 form an N+1 redundant pair serving the same room: normally one runs as lead and the other sits in standby. If {sys}201's Alarm (\"%X%{sys}201.ALM\") is ON while {sys}201 is currently lead, or {sys}202's Alarm (\"%X%{sys}202.ALM\") is ON while {sys}202 is currently lead, the other unit takes over: enable the healthy unit, disable the alarmed one, and update Lead-Lag Status (\"%X%{sys}.LL.STAT\", 1 = {sys}201 lead, 2 = {sys}202 lead) accordingly.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}201.ALM\" .EQ. ON .AND. \"%X%{sys}.LL.STAT\" .EQ. 1) THEN ON (\"%X%{sys}202.ENA\"), OFF (\"%X%{sys}201.ENA\"), \"%X%{sys}.LL.STAT\" = 2\n"
                f"01010 IF (\"%X%{sys}202.ALM\" .EQ. ON .AND. \"%X%{sys}.LL.STAT\" .EQ. 2) THEN ON (\"%X%{sys}201.ENA\"), OFF (\"%X%{sys}202.ENA\"), \"%X%{sys}.LL.STAT\" = 1"
            ),
            "changed_prompt": f"Operations wants a manual override added on top of the existing automatic alarm-based failover: if Manual Lead Select (\"%X%{sys}.MAN.LEAD\") is ON, force {sys}201 Enable ON and {sys}202 Enable OFF, ignoring the alarm-based logic entirely. Only fall back to the existing automatic failover when Manual Lead Select is OFF.",
            "points": [
                {"name": f"%X%{sys}201.ALM",
                    "desc": "CRAC201 Alarm (high/low temp, high/low humidity, or airflow fault)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}202.ALM",
                    "desc": "CRAC202 Alarm (high/low temp, high/low humidity, or airflow fault)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}201.ENA",
                    "desc": "CRAC201 Enable Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}202.ENA",
                    "desc": "CRAC202 Enable Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.LL.STAT",
                    "desc": "Lead-Lag Status Code (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.MAN.LEAD",
                    "desc": "Manual Lead Select Override (new, operator-facing)", "type": "BV", "kind": "physical"}
            ],
            "help": "Check Manual Lead Select first -- if it's ON, write 201.ENA/202.ENA directly and skip (or override) the two automatic-failover IFs entirely for that scan. When MAN.LEAD is OFF, the original two IFs should behave exactly as before.",
            "checks": [
                {"force": {f"%X%{sys}.MAN.LEAD": 1, f"%X%{sys}201.ALM": 1, f"%X%{sys}202.ALM": 0, f"%X%{sys}.LL.STAT": 2, f"%X%{sys}201.ENA": 0, f"%X%{sys}202.ENA": 1}, "expect": {f"%X%{sys}201.ENA": 1, f"%X%{sys}202.ENA": 0}, "note": "manual override active -- forces 201 lead regardless of alarms or LL.STAT"},
                {"force": {f"%X%{sys}.MAN.LEAD": 0, f"%X%{sys}201.ALM": 1, f"%X%{sys}202.ALM": 0, f"%X%{sys}.LL.STAT": 1, f"%X%{sys}201.ENA": 1, f"%X%{sys}202.ENA": 0}, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}201.ENA": 0, f"%X%{sys}202.ENA": 1}, "note": "manual override off -- original automatic failover still works"}
            ]
        }
    },
    {
        "title": "Additional Device: Air Compressor High Header Pressure Safety Relief Added",
        "difficulty": "medium",
        "systems": AC_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} High Header Pressure Safety Relief Added",
            "original_prompt": f"{sys} uses load/unload staging to hold discharge header pressure in a band. If Header Pressure (\"%X%{sys}.HDR.PSI\") drops below the Load Setpoint (\"%X%{sys}.HDR.PSI.LO\"), turn ON the Load Solenoid (\"%X%{sys}.LOAD.SOL\"). If Header Pressure rises above the Unload Setpoint (\"%X%{sys}.HDR.PSI.HI\"), turn OFF the Load Solenoid.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.HDR.PSI\" .LT. \"%X%{sys}.HDR.PSI.LO\") THEN ON (\"%X%{sys}.LOAD.SOL\")\n"
                f"01010 IF (\"%X%{sys}.HDR.PSI\" .GT. \"%X%{sys}.HDR.PSI.HI\") THEN OFF (\"%X%{sys}.LOAD.SOL\")"
            ),
            "changed_prompt": f"A hard safety limit needs to be added on top of the normal load/unload band above. If Header Pressure ever rises above a Safety Relief Limit (\"%X%{sys}.HDR.PSI.SAFE\", well above the normal Unload Setpoint), force the Load Solenoid OFF regardless of the load/unload logic above, and turn ON a High Header Pressure Safety Alarm (\"%X%{sys}.HDR.PSI.ALM\", a virtual point). The alarm should clear and normal load/unload control should resume once Header Pressure drops back below the Safety Relief Limit.",
            "points": [
                {"name": f"%X%{sys}.HDR.PSI",
                    "desc": "Discharge Header Pressure Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.HDR.PSI.LO",
                    "desc": "Load Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.HDR.PSI.HI",
                    "desc": "Unload Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.LOAD.SOL",
                    "desc": "Load Solenoid Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.HDR.PSI.SAFE",
                    "desc": "Safety Relief Limit (new, virtual point, well above the normal Unload Setpoint)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.HDR.PSI.ALM",
                    "desc": "High Header Pressure Safety Alarm (new, virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "The safety check needs to override the normal load/unload band -- when HDR.PSI exceeds HDR.PSI.SAFE, force LOAD.SOL off and set the alarm, and make sure that write happens after (or otherwise wins over) the original two IFs so it isn't overwritten. Unlike the load/unload band itself, this alarm needs a real ELSE that clears it once pressure drops back under the safety limit -- it isn't meant to be a manual-reset latch.",
            "checks": [
                {"force": {f"%X%{sys}.HDR.PSI": 150, f"%X%{sys}.HDR.PSI.LO": 70, f"%X%{sys}.HDR.PSI.HI": 90, f"%X%{sys}.HDR.PSI.SAFE": 140, f"%X%{sys}.LOAD.SOL": 1}, "expect": {f"%X%{sys}.LOAD.SOL": 0, f"%X%{sys}.HDR.PSI.ALM": 1}, "note": "pressure over the safety relief limit -- solenoid forced off and safety alarm trips, overriding normal load/unload"},
                {"force": {f"%X%{sys}.HDR.PSI": 80, f"%X%{sys}.HDR.PSI.LO": 70, f"%X%{sys}.HDR.PSI.HI": 90, f"%X%{sys}.HDR.PSI.SAFE": 140, f"%X%{sys}.HDR.PSI.ALM": 1}, "expect": {f"%X%{sys}.HDR.PSI.ALM": 0}, "note": "pressure back below the safety limit -- alarm clears and normal load/unload control resumes"}
            ]
        }
    },

    # ---------------- HARD ----------------
    {
        "title": "Sequence Compliance: Economizer Damper Freeze Protection Interlock",
        "difficulty": "medium",
        "systems": AHU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Economizer Freeze Protection Compliance",
            "original_prompt": f"If Outside Air Temp (\"%X%{sys}.OAT\") is less than Return Air Temp (\"%X%{sys}.RAT\"), open the Outside Air Damper (\"%X%{sys}.OAD.CMD\") for free cooling. Otherwise, close it.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.OAT\" .LT. \"%X%{sys}.RAT\") THEN ON (\"%X%{sys}.OAD.CMD\") ELSE OFF (\"%X%{sys}.OAD.CMD\")"
            ),
            "changed_prompt": f"Commissioning flagged a missing safety interlock: the approved sequence requires the Outside Air Damper to close immediately and stay closed whenever the Low-Limit Freeze Stat (\"%X%{sys}.FRZ.STAT\") trips ON, overriding the free-cooling calculation entirely -- a tripped freeze stat means near-freezing air is hitting the coil and outside air must be shut out regardless of the OAT/RAT comparison. Add this check ahead of the existing logic.",
            "points": [
                {"name": f"%X%{sys}.OAT",
                    "desc": "Outside Air Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.RAT",
                    "desc": "Return Air Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.OAD.CMD",
                    "desc": "Outside Air Damper Open/Closed Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.FRZ.STAT",
                    "desc": "Low-Limit Freeze Stat Trip (new)", "type": "DI", "kind": "physical"}
            ],
            "help": "Add the freeze check ahead of the existing OAT/RAT IF, and make sure it wins -- when FRZ.STAT is ON, OAD.CMD has to end up OFF regardless of the temperature comparison below it.",
            "checks": [
                {"force": {f"%X%{sys}.OAT": 40, f"%X%{sys}.RAT": 75, f"%X%{sys}.FRZ.STAT": 1}, "expect": {f"%X%{sys}.OAD.CMD": 0}, "note": "freeze stat tripped -- damper forced closed even though OAT is well below RAT and would normally call for free cooling"},
                {"force": {f"%X%{sys}.OAT": 40, f"%X%{sys}.RAT": 75, f"%X%{sys}.FRZ.STAT": 0}, "expect": {f"%X%{sys}.OAD.CMD": 1}, "note": "no freeze trip -- normal OAT/RAT economizer logic applies"}
            ]
        }
    },
    {
        "title": "Compliance Fix: Add a Standby Mode to Setpoint Selection",
        "difficulty": "medium",
        "systems": AHU_SYSTEMS + FCU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Add Standby Setpoint Mode",
            "original_prompt": f"If Building Occupancy State (\"%X%BLDG.OCC.STATE\") is equal to 0 (Occupied), set Active Cooling Setpoint (\"%X%{sys}.CLG.SP.ACT\") equal to Occupied Cooling Setpoint (\"%X%{sys}.CLG.SP.OCC\"). Otherwise, set Active Cooling Setpoint equal to Unoccupied Cooling Setpoint (\"%X%{sys}.CLG.SP.UNOCC\").",
            "starter_code": (
                f"01000 IF (\"%X%BLDG.OCC.STATE\" .EQ. 0) THEN \"%X%{sys}.CLG.SP.ACT\" = \"%X%{sys}.CLG.SP.OCC\" ELSE \"%X%{sys}.CLG.SP.ACT\" = \"%X%{sys}.CLG.SP.UNOCC\""
            ),
            "changed_prompt": f"This program was handed off from another programmer using the building-wide occupancy state and occupied/unoccupied setpoint values that were already established for this zone. The approved sequence now also calls for a Standby mode: if Standby Status (\"%X%{sys}.STBY.STAT\") is ON, the Active Cooling Setpoint should use the Standby Cooling Setpoint (\"%X%{sys}.CLG.SP.STBY\") regardless of building occupancy state. Otherwise, keep the existing occupied/unoccupied logic unchanged.",
            "points": [
                {"name": "%X%BLDG.OCC.STATE",
                    "desc": "Building-wide Occupancy Mode (0 = Occupied, 1 = Unoccupied) -- a virtual point driven by a scheduling program", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.CLG.SP.ACT",
                    "desc": "Active Cooling Setpoint (virtual point -- calculated and written by your program logic)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.CLG.SP.OCC",
                    "desc": "Occupied Cooling Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.CLG.SP.UNOCC",
                    "desc": "Unoccupied Cooling Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.STBY.STAT",
                    "desc": "Standby Mode Status (new, operator-facing)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}.CLG.SP.STBY",
                    "desc": "Standby Cooling Setpoint (new, virtual point)", "type": "AV", "kind": "virtual"}
            ],
            "help": "Check STBY.STAT first, and if it's ON, write CLG.SP.ACT from CLG.SP.STBY and skip (or override) the existing occupancy IF/ELSE entirely for that scan. When STBY.STAT is OFF, the original occupied/unoccupied logic should behave exactly as before.",
            "checks": [
                {"force": {"%X%BLDG.OCC.STATE": 0, f"%X%{sys}.CLG.SP.OCC": 72, f"%X%{sys}.CLG.SP.UNOCC": 78, f"%X%{sys}.STBY.STAT": 1, f"%X%{sys}.CLG.SP.STBY": 65}, "expect": {f"%X%{sys}.CLG.SP.ACT": 65}, "note": "standby active -- setpoint forced to the standby value regardless of occupancy state"},
                {"force": {"%X%BLDG.OCC.STATE": 0, f"%X%{sys}.CLG.SP.OCC": 72, f"%X%{sys}.CLG.SP.UNOCC": 78, f"%X%{sys}.STBY.STAT": 0, f"%X%{sys}.CLG.SP.STBY": 65}, "expect": {f"%X%{sys}.CLG.SP.ACT": 72}, "note": "standby off, occupied -- original occupied setpoint logic applies"},
                {"force": {"%X%BLDG.OCC.STATE": 1, f"%X%{sys}.CLG.SP.OCC": 72, f"%X%{sys}.CLG.SP.UNOCC": 78, f"%X%{sys}.STBY.STAT": 0, f"%X%{sys}.CLG.SP.STBY": 65}, "expect": {f"%X%{sys}.CLG.SP.ACT": 78}, "note": "standby off, unoccupied -- original unoccupied setpoint logic applies"}
            ]
        }
    },
    {
        "title": "Compliance Fix: Add a Manual Override to Lead-Lag Rotation",
        "difficulty": "medium",
        "systems": CHILLER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Lead-Lag Manual Override",
            "original_prompt": f"If the Lead Chiller Fault Alarm (\"%X%{sys}.LEAD.FLT\") is ON, the lag chiller immediately takes over as lead: turn ON Chiller 2 Enable (\"%X%{sys}2.ENA\"), turn OFF Chiller 1 Enable (\"%X%{sys}1.ENA\"), and set Lead-Lag Status (\"%X%{sys}.LL.STAT\") to 2, resetting a Fault-Clear Timer (\"%X%{sys}.FLT.CLR.TMR\") to 0. While Lead-Lag Status is 2 and the Fault Alarm is OFF, the Fault-Clear Timer accumulates by 1 second using SAMPLE(1); once it exceeds 300 seconds proven clear, normal operation is restored (Chiller 1 back to lead, Lead-Lag Status = 1).",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.LEAD.FLT\" .EQ. ON) THEN ON (\"%X%{sys}2.ENA\"), OFF (\"%X%{sys}1.ENA\"), \"%X%{sys}.LL.STAT\" = 2, \"%X%{sys}.FLT.CLR.TMR\" = 0\n"
                f"01010 IF (\"%X%{sys}.LL.STAT\" .EQ. 2 .AND. \"%X%{sys}.LEAD.FLT\" .EQ. OFF) THEN SAMPLE(1) \"%X%{sys}.FLT.CLR.TMR\" = \"%X%{sys}.FLT.CLR.TMR\" + 1\n"
                f"01020 IF (\"%X%{sys}.FLT.CLR.TMR\" .GT. 300) THEN ON (\"%X%{sys}1.ENA\"), OFF (\"%X%{sys}2.ENA\"), \"%X%{sys}.LL.STAT\" = 1"
            ),
            "changed_prompt": f"Operations wants a manual override added on top of the existing automatic fault-based rotation: if Manual Lead Select (\"%X%{sys}.MAN.LEAD\") is ON, force Chiller 1 Enable (\"%X%{sys}1.ENA\") ON and Chiller 2 Enable (\"%X%{sys}2.ENA\") OFF, ignoring the fault/timer logic entirely. Only fall back to the existing automatic rotation when Manual Lead Select is OFF.",
            "points": [
                {"name": f"%X%{sys}.LEAD.FLT",
                    "desc": "Lead Chiller Fault Alarm (fault relay contact from whichever chiller is currently lead)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}1.ENA",
                    "desc": "Chiller 1 Enable", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}2.ENA",
                    "desc": "Chiller 2 Enable", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.LL.STAT",
                    "desc": "Lead-Lag Status Code (virtual point, 1 = Chiller 1 lead, 2 = Chiller 2 lead)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.FLT.CLR.TMR",
                    "desc": "Fault-Clear Timer Accumulator (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.MAN.LEAD",
                    "desc": "Manual Lead Select Override (new, operator-facing)", "type": "BV", "kind": "physical"}
            ],
            "help": "Same shape as the CRAC manual override -- check MAN.LEAD first, and if it's ON, write CHLR1.ENA/CHLR2.ENA directly and skip the fault/timer logic entirely for that scan (or make the manual write happen after it and win). When MAN.LEAD is OFF, the original three-line rotation should behave exactly as before.",
            "checks": [
                {"force": {f"%X%{sys}.MAN.LEAD": 1, f"%X%{sys}.LEAD.FLT": 1, f"%X%{sys}.LL.STAT": 2, f"%X%{sys}1.ENA": 0, f"%X%{sys}2.ENA": 1, f"%X%{sys}.FLT.CLR.TMR": 0}, "expect": {f"%X%{sys}1.ENA": 1, f"%X%{sys}2.ENA": 0}, "note": "manual override active -- forces chiller 1 lead regardless of fault or timer state"},
                {"force": {f"%X%{sys}.MAN.LEAD": 0, f"%X%{sys}.LEAD.FLT": 1, f"%X%{sys}.LL.STAT": 1, f"%X%{sys}1.ENA": 1, f"%X%{sys}2.ENA": 0}, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}1.ENA": 0, f"%X%{sys}2.ENA": 1}, "note": "manual override off -- original automatic fault-based rotation still works"}
            ]
        }
    },
    {
        "title": "Additional Device: Third Pump Added to Lead-Lag Rotation",
        "difficulty": "expert",
        "systems": PUMP_PLANT_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{PLANT_LABELS.get(sys, sys)} Third Pump Added to Rotation",
            "original_prompt": f"If the lead pump develops a fault -- Pump 1 Fault (\"%X%{sys}.P1.FLT\") if Pump 1 is currently lead, or Pump 2 Fault (\"%X%{sys}.P2.FLT\") if Pump 2 is currently lead -- the other pump automatically takes over as lead, tracked with a Lead-Lag Status point (\"%X%{sys}.LL.STAT\", 1 = Pump 1 lead, 2 = Pump 2 lead).",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.P1.FLT\" .EQ. ON) THEN ON (\"%X%{sys}.P2.ENA\"), OFF (\"%X%{sys}.P1.ENA\"), \"%X%{sys}.LL.STAT\" = 2\n"
                f"01010 IF (\"%X%{sys}.P2.FLT\" .EQ. ON) THEN ON (\"%X%{sys}.P1.ENA\"), OFF (\"%X%{sys}.P2.ENA\"), \"%X%{sys}.LL.STAT\" = 1"
            ),
            "changed_prompt": f"A third pump (\"%X%{sys}.P3.ENA\" enable, \"%X%{sys}.P3.FLT\" fault) was added to the plant during construction. Update the rotation logic so that whenever the currently-enabled pump faults, the program picks one of the OTHER TWO pumps (whichever is not itself faulted) to become the new lead, and updates Lead-Lag Status (\"%X%{sys}.LL.STAT\", now 1/2/3) to track which pump is currently lead. Only one pump should be enabled at a time -- this is trickier than a simple two-pump swap since the program has to decide which of two remaining pumps to switch to.",
            "points": [
                {"name": f"%X%{sys}.P1.FLT",
                    "desc": "Pump 1 Fault (fault relay contact)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}.P2.FLT",
                    "desc": "Pump 2 Fault (fault relay contact)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}.P3.FLT",
                    "desc": "Pump 3 Fault (new, fault relay contact)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}.P1.ENA",
                    "desc": "Pump 1 Enable Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.P2.ENA",
                    "desc": "Pump 2 Enable Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.P3.ENA",
                    "desc": "Pump 3 Enable Command (new)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.LL.STAT",
                    "desc": "Lead-Lag Status Code (virtual point)", "type": "AV", "kind": "virtual"}
            ],
            "help": "The tricky part is that P1 faulting could hand off to EITHER P2 or P3 -- pick a consistent rule (e.g. always prefer the lower-numbered healthy pump) and apply it the same way no matter which pump is currently lead. Double check exactly one pump ends up enabled in every case -- turn the old lead OFF in the same block where you turn the new one ON, for all combinations, not just the P1-faults-so-swap-to-P2 case.",
            "checks": [
                {"force": {f"%X%{sys}.LL.STAT": 1, f"%X%{sys}.P1.FLT": 1, f"%X%{sys}.P2.FLT": 1, f"%X%{sys}.P3.FLT": 0, f"%X%{sys}.P1.ENA": 1, f"%X%{sys}.P2.ENA": 0, f"%X%{sys}.P3.ENA": 0}, "expect": {f"%X%{sys}.LL.STAT": 3, f"%X%{sys}.P3.ENA": 1, f"%X%{sys}.P1.ENA": 0, f"%X%{sys}.P2.ENA": 0}, "note": "lead pump (P1) faults and P2 is also faulted -- P3 is the only healthy option, so it must become the new lead"},
                {"force": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}.P1.FLT": 0, f"%X%{sys}.P2.FLT": 1, f"%X%{sys}.P3.FLT": 1, f"%X%{sys}.P1.ENA": 0, f"%X%{sys}.P2.ENA": 1, f"%X%{sys}.P3.ENA": 0}, "expect": {f"%X%{sys}.LL.STAT": 1, f"%X%{sys}.P1.ENA": 1, f"%X%{sys}.P2.ENA": 0, f"%X%{sys}.P3.ENA": 0}, "note": "lead pump (P2) faults and P3 is also faulted -- P1 is the only healthy option"},
                {"force": {f"%X%{sys}.LL.STAT": 1, f"%X%{sys}.P1.FLT": 0, f"%X%{sys}.P2.FLT": 0, f"%X%{sys}.P3.FLT": 0, f"%X%{sys}.P1.ENA": 1, f"%X%{sys}.P2.ENA": 0, f"%X%{sys}.P3.ENA": 0}, "expect": {f"%X%{sys}.LL.STAT": 1, f"%X%{sys}.P1.ENA": 1}, "note": "lead pump healthy -- no swap, stays as-is"}
            ]
        }
    },
    {
        "title": "Compliance Fix: Add Alarm Silence Override to GOTO Alarm Logic",
        "difficulty": "expert",
        "systems": AHU_SYSTEMS + MAU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Alarm Silence Override",
            "original_prompt": f"Real Siemens panel dumps write a delayed alarm as a GOTO branch: if Discharge Air Temp (\"%X%{sys}.DAT\") is greater than High Limit (\"%X%{sys}.DAT.HI\"), GOTO the alarm-timer block. Otherwise, reset the Alarm Delay Timer (\"%X%{sys}.DAT.ALM.TMR\") to 0, turn the alarm OFF, and GOTO past the alarm-timer block. In the alarm-timer block, SAMPLE(1) accumulates the timer while still above the limit, and the alarm turns ON once the timer exceeds 60 seconds.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.DAT\" .GT. \"%X%{sys}.DAT.HI\") THEN GOTO 01080\n"
                f"01010 SET(0,\"%X%{sys}.DAT.ALM.TMR\")\n"
                f"01020 OFF (\"%X%{sys}.DAT.ALM\")\n"
                f"01030 GOTO 01110\n"
                f"01080 SAMPLE(1) IF (\"%X%{sys}.DAT\" .GT. \"%X%{sys}.DAT.HI\") THEN \"%X%{sys}.DAT.ALM.TMR\" = \"%X%{sys}.DAT.ALM.TMR\" + 1\n"
                f"01090 IF (\"%X%{sys}.DAT.ALM.TMR\" .GT. 60) THEN ON (\"%X%{sys}.DAT.ALM\") ELSE OFF (\"%X%{sys}.DAT.ALM\")\n"
                f"01110 C --- END ---"
            ),
            "changed_prompt": f"Operators want an Alarm Silence override added: if Alarm Silence Command (\"%X%{sys}.DAT.ALM.SIL\") is ON, force the alarm OFF and hold the Alarm Delay Timer at 0 regardless of Discharge Air Temp. Add this as a new check that GOTOs past ALL of the existing logic below whenever Alarm Silence is active. Since it has to run BEFORE everything it needs to skip past, and GOTO can only ever jump to a HIGHER line number than the line it's on, you'll need to renumber the existing lines to make room for it at the top of the program.",
            "points": [
                {"name": f"%X%{sys}.DAT",
                    "desc": "Discharge Air Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.DAT.HI",
                    "desc": "High Temp Alarm Limit (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.DAT.ALM.TMR",
                    "desc": "Alarm Delay Timer Accumulator (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.DAT.ALM",
                    "desc": "High Discharge Temp Alarm (virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.DAT.ALM.SIL",
                    "desc": "Alarm Silence Command (new, operator-facing)", "type": "BV", "kind": "physical"}
            ],
            "help": "The silence check needs to be the very first thing evaluated, and its GOTO target has to skip past the original safety-check IF, the reset branch, AND the alarm-timer block -- all the way to the END comment line. Since this new check runs before everything it needs to jump over, and GOTO can only target higher line numbers, renumber every existing line to leave room for it at the top (e.g. shift everything down by 10-20 line numbers).",
            "checks": [
                {"force": {f"%X%{sys}.DAT": 150, f"%X%{sys}.DAT.HI": 90, f"%X%{sys}.DAT.ALM.SIL": 1, f"%X%{sys}.DAT.ALM.TMR": 50, f"%X%{sys}.DAT.ALM": 1}, "expect": {f"%X%{sys}.DAT.ALM": 0, f"%X%{sys}.DAT.ALM.TMR": 0}, "note": "alarm silenced -- alarm forced off and timer held at 0 even though DAT is way over the limit"},
                {"force": {f"%X%{sys}.DAT": 150, f"%X%{sys}.DAT.HI": 90, f"%X%{sys}.DAT.ALM.SIL": 0, f"%X%{sys}.DAT.ALM.TMR": 0, f"%X%{sys}.DAT.ALM": 0}, "cycles": 61, "expect": {f"%X%{sys}.DAT.ALM": 1}, "note": "silence off -- original delayed alarm logic still works after 60+ seconds over the limit"}
            ]
        }
    },
    {
        "title": "Compliance Fix: Add Manual Override to a PID Loop",
        "difficulty": "expert",
        "systems": PUMP_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Pump Speed Manual Override",
            "original_prompt": f"When Pump Enable (\"%X%{sys}.ENA\") is ON, a LOOP statement modulates Pump Speed Output (\"%X%{sys}.SPD\") to maintain System Differential Pressure (\"%X%{sys}.DP\") at Setpoint (\"%X%{sys}.DP.SP\"). If Differential Pressure falls below half of Setpoint, a Low Pressure Fault Alarm (\"%X%{sys}.ALM\") turns ON.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.ENA\" .EQ. ON) THEN LOOP(128,\"%X%{sys}.DP\",\"%X%{sys}.SPD\",\"%X%{sys}.DP.SP\",5.0,0.2,0.0,1,50.0,0,100,0)\n"
                f"01010 IF (\"%X%{sys}.DP\" .LT. \"%X%{sys}.DP.SP\" / 2) THEN ON (\"%X%{sys}.ALM\") ELSE OFF (\"%X%{sys}.ALM\")"
            ),
            "changed_prompt": f"Commissioning wants a manual/auto override added: if Manual Override (\"%X%{sys}.SPD.MAN\") is ON, skip the LOOP entirely and drive Pump Speed Output directly to Manual Speed Command (\"%X%{sys}.SPD.MAN.VAL\") instead -- ignore the PID and the low-pressure alarm check while in manual. When Manual Override is OFF, run the existing automatic control unchanged.",
            "points": [
                {"name": f"%X%{sys}.ENA",
                    "desc": "Pump Enable Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.DP",
                    "desc": "System Differential Pressure", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.DP.SP",
                    "desc": "Differential Pressure Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.SPD",
                    "desc": "Pump Speed Control Output (0-100%)", "type": "AO", "kind": "physical"},
                {"name": f"%X%{sys}.ALM",
                    "desc": "Low Pressure Fault Alarm (virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.SPD.MAN",
                    "desc": "Manual Override Command (new, operator-facing)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.SPD.MAN.VAL",
                    "desc": "Manual Speed Command (new, field-adjustable by the technician)", "type": "AV", "kind": "physical"}
            ],
            "help": "Check SPD.MAN first -- if it's ON, write SPD from SPD.MAN.VAL directly and skip the LOOP call and the low-pressure alarm IF entirely for that scan. When SPD.MAN is OFF, both original lines should run exactly as before.",
            "checks": [
                {"force": {f"%X%{sys}.ENA": 1, f"%X%{sys}.DP": 20, f"%X%{sys}.DP.SP": 100, f"%X%{sys}.SPD.MAN": 1, f"%X%{sys}.SPD.MAN.VAL": 45, f"%X%{sys}.ALM": 0}, "expect": {f"%X%{sys}.SPD": 45, f"%X%{sys}.ALM": 0}, "note": "manual override active -- speed driven directly to the manual value, and the low-pressure alarm check is skipped even though DP is below half of setpoint"},
                {"force": {f"%X%{sys}.ENA": 1, f"%X%{sys}.DP": 20, f"%X%{sys}.DP.SP": 100, f"%X%{sys}.SPD.MAN": 0, f"%X%{sys}.SPD.MAN.VAL": 45, f"%X%{sys}.ALM": 0}, "expect": {f"%X%{sys}.ALM": 1}, "note": "manual override off -- original low-pressure alarm logic still runs"}
            ]
        }
    },
    {
        "title": "Compliance Fix: Add Emergency Heat Boost to Hot Water Reset",
        "difficulty": "expert",
        "systems": HWP_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Emergency Heat Boost",
            "original_prompt": f"The Hot Water Supply Setpoint at {sys} resets based on Outside Air Temp (\"%X%{sys}.OAT\"): at 20 degrees OAT or colder, Hot Water Supply Setpoint (\"%X%{sys}.HW.SP\") is 180 degrees. At 60 degrees OAT or warmer, it's 140 degrees. Between 20 and 60 degrees, it's a proportional (linear) reset between those two values.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.OAT\" .LE. 20) THEN \"%X%{sys}.HW.SP\" = 180\n"
                f"01010 IF (\"%X%{sys}.OAT\" .GE. 60) THEN \"%X%{sys}.HW.SP\" = 140\n"
                f"01020 IF (\"%X%{sys}.OAT\" .GT. 20 .AND. \"%X%{sys}.OAT\" .LT. 60) THEN \"%X%{sys}.HW.SP\" = 180 - ((\"%X%{sys}.OAT\" - 20) / 40) * 40"
            ),
            "changed_prompt": f"Operations wants an Emergency Heat Boost added: if Emergency Heat Demand (\"%X%{sys}.EHT.DMD\") is ON, force Hot Water Supply Setpoint to 190 regardless of the outside air reset curve above. Add this check so it overrides the reset curve whenever active, and falls back to the existing reset logic unchanged when Emergency Heat Demand is OFF.",
            "points": [
                {"name": f"%X%{sys}.OAT",
                    "desc": "Outside Air Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.HW.SP",
                    "desc": "Hot Water Supply Setpoint (already exists -- your logic must write to this point)", "type": "AV", "kind": "physical"},
                {"name": f"%X%{sys}.EHT.DMD",
                    "desc": "Emergency Heat Demand Command (new, operator-facing)", "type": "BV", "kind": "physical"}
            ],
            "help": "Check EHT.DMD first -- if it's ON, write HW.SP = 190 and make sure that write happens after (or otherwise wins over) the three existing reset-curve IFs so it isn't overwritten. When EHT.DMD is OFF, the original reset curve should behave exactly as before.",
            "checks": [
                {"force": {f"%X%{sys}.OAT": 40, f"%X%{sys}.EHT.DMD": 1}, "expect": {f"%X%{sys}.HW.SP": 190}, "note": "emergency heat demand active -- setpoint forced to 190 regardless of the reset curve"},
                {"force": {f"%X%{sys}.OAT": 40, f"%X%{sys}.EHT.DMD": 0}, "expect": {f"%X%{sys}.HW.SP": 160}, "tolerance": 1, "note": "emergency heat off -- original reset curve still applies (OAT=40 is the midpoint, so 160)"}
            ]
        }
    },
    {
        "title": "Compliance Fix: Add High Discharge Temp Safety Shutdown to Air Compressor Bypass Valve Control",
        "difficulty": "expert",
        "systems": AC_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Discharge Temp Safety Shutdown",
            "original_prompt": f"Air Compressor {sys} is water-cooled: whenever the compressor is running (\"%X%{sys}.STAT\" is ON), a LOOP statement modulates the Bypass Valve Output (\"%X%{sys}.BYP.VLV\") (0-100%) to hold Compressor Cooling Water Entering Temp (\"%X%{sys}.CHW.ET\") at Setpoint (\"%X%{sys}.CHW.ET.SP\"). When the compressor is not running, the Bypass Valve Output is held at 0.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.STAT\" .EQ. ON) THEN LOOP(0,\"%X%{sys}.CHW.ET\",\"%X%{sys}.BYP.VLV\",\"%X%{sys}.CHW.ET.SP\",4.0,0.3,0.0,1,50.0,0,100,0)\n"
                f"01010 IF (\"%X%{sys}.STAT\" .EQ. OFF) THEN SET(0,\"%X%{sys}.BYP.VLV\")"
            ),
            "changed_prompt": f"A high discharge temp sensor was field-installed on the compressor head. Add a safety shutdown: if Compressor Discharge Air Temp (\"%X%{sys}.DAT\") rises above 260 degrees F, force the Bypass Valve Output fully open (100) and turn ON a Discharge Temp Safety Shutdown Alarm (\"%X%{sys}.DAT.SAFE.ALM\") -- skip the LOOP entirely while this condition is active, since driving the bypass fully open for maximum cooling takes priority over trying to hold setpoint. Return to the normal LOOP control above once Discharge Air Temp drops back below 260.",
            "points": [
                {"name": f"%X%{sys}.STAT",
                    "desc": "Compressor Run Status", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.CHW.ET",
                    "desc": "Compressor Cooling Water Entering Temp", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.CHW.ET.SP",
                    "desc": "Cooling Water Entering Temp Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.BYP.VLV",
                    "desc": "Three-Way Bypass Valve Control Output (0-100%)", "type": "AO", "kind": "physical"},
                {"name": f"%X%{sys}.DAT",
                    "desc": "Compressor Discharge Air Temp Sensor (already exists on this unit)", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.DAT.SAFE.ALM",
                    "desc": "Discharge Temp Safety Shutdown Alarm (new, virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Check DAT first -- if it's over 260, force BYP.VLV to 100 and set the alarm, skipping the LOOP call for that scan (put this check ahead of, or otherwise make it win over, the original STAT-based IF). When DAT is at or under 260, the original two lines should behave exactly as before.",
            "checks": [
                {"force": {f"%X%{sys}.STAT": 1, f"%X%{sys}.CHW.ET": 70, f"%X%{sys}.CHW.ET.SP": 65, f"%X%{sys}.DAT": 270, f"%X%{sys}.BYP.VLV": 50}, "expect": {f"%X%{sys}.BYP.VLV": 100, f"%X%{sys}.DAT.SAFE.ALM": 1}, "note": "discharge air temp over 260 -- bypass valve forced fully open and safety alarm trips, skipping the LOOP"},
                {"force": {f"%X%{sys}.STAT": 0, f"%X%{sys}.CHW.ET": 70, f"%X%{sys}.CHW.ET.SP": 65, f"%X%{sys}.DAT": 200}, "expect": {f"%X%{sys}.BYP.VLV": 0}, "note": "discharge temp normal, compressor off -- original behavior (valve held at 0) still applies"}
            ]
        }
    },
    {
        "title": "Compliance Fix: Add Condensate Overflow Priority Check to FCU Dispatch",
        "difficulty": "expert",
        "systems": FCU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Condensate Overflow Priority Check",
            "original_prompt": f"Real FCU programs open with a decision tree: check safety first, then dispatch to the current mode via GOTO. If the Coil Freeze Stat (\"%X%{sys}.FRZ.STAT\") is ON, GOTO line 02000 (Freeze Protection), skipping mode logic entirely. Otherwise, check Building Occupancy State (\"%X%BLDG.OCC.STATE\", 0 = Occupied, 1 = Unoccupied): if Occupied, GOTO line 01100; if Unoccupied, GOTO line 01200. At line 01100 (Occupied), turn ON the Fan (\"%X%{sys}.FAN.ENA\"), then GOTO line 03000. At line 01200 (Unoccupied), turn OFF the Fan, then GOTO line 03000. At line 02000 (Freeze Protection), turn OFF the Fan and turn ON the Heating Valve (\"%X%{sys}.HTG.VLV\") fully open, then GOTO line 03000. Line 03000 is a comment marking the end of the scan and is the last line in the program.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.FRZ.STAT\" .EQ. ON) THEN GOTO 02000\n"
                f"01010 IF (\"%X%BLDG.OCC.STATE\" .EQ. 0) THEN GOTO 01100\n"
                f"01020 IF (\"%X%BLDG.OCC.STATE\" .EQ. 1) THEN GOTO 01200\n"
                f"01100 ON (\"%X%{sys}.FAN.ENA\")\n"
                f"01110 GOTO 03000\n"
                f"01200 OFF (\"%X%{sys}.FAN.ENA\")\n"
                f"01210 GOTO 03000\n"
                f"02000 OFF (\"%X%{sys}.FAN.ENA\"), ON (\"%X%{sys}.HTG.VLV\")\n"
                f"02010 GOTO 03000\n"
                f"03000 C --- END ---"
            ),
            "changed_prompt": f"A condensate overflow switch was field-installed on the drain pan, and it needs to shut the unit down even faster than a freeze trip does -- standing water is an immediate problem, not a seasonal one. Add a new check for Condensate Overflow Switch (\"%X%{sys}.COND.SW\", OFF = overflow) that must run BEFORE the freeze check, forcing the Fan OFF and the Cooling Valve (\"%X%{sys}.CLG.VLV\") OFF whenever it trips, then GOTO straight to line 03000 -- skipping the freeze check and every mode block below it. Since this new check has to run before everything it needs to skip past, and GOTO can only ever jump to a HIGHER line number than the line it's on, you'll need to renumber the existing lines to make room for it at the very top of the program.",
            "points": [
                {"name": f"%X%{sys}.FRZ.STAT",
                    "desc": "Coil Freeze Stat Trip (low-limit safety)", "type": "DI", "kind": "physical"},
                {"name": "%X%BLDG.OCC.STATE",
                    "desc": "Building-wide Occupancy Mode (0 = Occupied, 1 = Unoccupied) -- a virtual point driven by a scheduling program, shared across all equipment", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.FAN.ENA",
                    "desc": "Fan Enable Command (already exists)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.HTG.VLV",
                    "desc": "Heating Valve Command (already exists)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.COND.SW",
                    "desc": "Condensate Overflow Switch (new, DI -- ON = normal, OFF = overflow)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.CLG.VLV",
                    "desc": "Cooling Valve Command (already exists)", "type": "DO", "kind": "physical"}
            ],
            "help": "The overflow check has to be the very first thing evaluated and its GOTO has to skip past the freeze check, both occupancy blocks, AND the freeze-protection block -- straight to the END line. Since it runs before everything it needs to jump over, renumber the whole program to make room for it at the top, same as the alarm-silence exercise.",
            "checks": [
                {"force": {f"%X%{sys}.COND.SW": 0, f"%X%{sys}.FRZ.STAT": 1, "%X%BLDG.OCC.STATE": 0}, "expect": {f"%X%{sys}.FAN.ENA": 0, f"%X%{sys}.CLG.VLV": 0}, "note": "condensate overflow tripped -- fan and cooling valve forced off immediately, overriding even the freeze check"},
                {"force": {f"%X%{sys}.COND.SW": 1, f"%X%{sys}.FRZ.STAT": 1, "%X%BLDG.OCC.STATE": 0}, "expect": {f"%X%{sys}.FAN.ENA": 0, f"%X%{sys}.HTG.VLV": 1}, "note": "no overflow -- original freeze-protection dispatch still runs when tripped"},
                {"force": {f"%X%{sys}.COND.SW": 1, f"%X%{sys}.FRZ.STAT": 0, "%X%BLDG.OCC.STATE": 0}, "expect": {f"%X%{sys}.FAN.ENA": 1}, "note": "no overflow, no freeze trip, occupied -- original occupied dispatch still runs"}
            ]
        }
    },
    {
        "title": "Compliance Fix: Add Manual Override to Exhaust Fan Speed PID Loop",
        "difficulty": "expert",
        "systems": EF_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Manual Override",
            "original_prompt": f"Whenever the Exhaust Fan Start Command (\"%X%{sys}.EF.SS\") is ON, a LOOP statement modulates Exhaust Fan Speed Output (\"%X%{sys}.EF.SPD\") to maintain Building Static Pressure (\"%X%{sys}.BSP\") at Setpoint (\"%X%{sys}.BSP.SP\"). When Exhaust Fan Start Command is OFF, Exhaust Fan Speed Output is held at 0.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.EF.SS\" .EQ. ON) THEN LOOP(0,\"%X%{sys}.BSP\",\"%X%{sys}.EF.SPD\",\"%X%{sys}.BSP.SP\",4.0,0.25,0.0,1,50.0,20,100,0)\n"
                f"01010 IF (\"%X%{sys}.EF.SS\" .EQ. OFF) THEN SET(0,\"%X%{sys}.EF.SPD\")"
            ),
            "changed_prompt": f"Commissioning wants a manual/auto override added: if Manual Override (\"%X%{sys}.SPD.MAN\") is ON, skip the LOOP entirely and drive Exhaust Fan Speed Output directly to Manual Speed Command (\"%X%{sys}.SPD.MAN.VAL\") instead -- ignore the building static pressure loop while in manual. When Manual Override is OFF, run the existing automatic control unchanged.",
            "points": [
                {"name": f"%X%{sys}.EF.SS",
                    "desc": "Exhaust Fan Start Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.BSP",
                    "desc": "Building Static Pressure Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.BSP.SP",
                    "desc": "Building Static Pressure Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.EF.SPD",
                    "desc": "Exhaust Fan Speed Output (0-100%)", "type": "AO", "kind": "physical"},
                {"name": f"%X%{sys}.SPD.MAN",
                    "desc": "Manual Override Command (new, operator-facing)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.SPD.MAN.VAL",
                    "desc": "Manual Speed Command (new, field-adjustable by the technician)", "type": "AV", "kind": "physical"}
            ],
            "help": "Same pattern as a pump-speed manual override -- check SPD.MAN first, and if it's ON, write EF.SPD from SPD.MAN.VAL directly, skipping the LOOP call entirely for that scan. When SPD.MAN is OFF, the original two lines should behave exactly as before.",
            "checks": [
                {"force": {f"%X%{sys}.EF.SS": 1, f"%X%{sys}.BSP": 0.1, f"%X%{sys}.BSP.SP": 0.05, f"%X%{sys}.SPD.MAN": 1, f"%X%{sys}.SPD.MAN.VAL": 40}, "expect": {f"%X%{sys}.EF.SPD": 40}, "note": "manual override active -- speed driven directly to the manual value, ignoring the static pressure loop"},
                {"force": {f"%X%{sys}.EF.SS": 0, f"%X%{sys}.SPD.MAN": 0, f"%X%{sys}.SPD.MAN.VAL": 40}, "expect": {f"%X%{sys}.EF.SPD": 0}, "note": "manual override off, fan not started -- original behavior (speed held at 0) still applies"}
            ]
        }
    },
    {
        "title": "Compliance Fix: Add Low Water Flow Safety Trip to Chiller Safety Dispatch",
        "difficulty": "expert",
        "systems": CHILLER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Low Water Flow Safety Trip",
            "original_prompt": f"Chiller safeties override normal staging via a GOTO decision tree. If Low Refrigerant Pressure Trip (\"%X%{sys}.LOP.TRIP\") is ON OR High Condenser Pressure Trip (\"%X%{sys}.HCP.TRIP\") is ON, GOTO line 02000 (Safety Shutdown), skipping normal staging entirely. Otherwise, check Chiller Plant Master Enable (\"%X%{sys}.ENA\"): if ON, GOTO line 01100 (Normal Run); if OFF, GOTO line 01200 (Standby). At line 01100, turn ON Chiller 1 Enable (\"%X%{sys}1.ENA\"), then GOTO line 03000. At line 01200, turn OFF Chiller 1 Enable, then GOTO line 03000. At line 02000, turn OFF Chiller 1 Enable and turn ON a Safety Shutdown Alarm (\"%X%{sys}.SAFE.ALM\"), then GOTO line 03000. Line 03000 is a comment marking the end of the scan and is the last line in the program.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.LOP.TRIP\" .EQ. ON .OR. \"%X%{sys}.HCP.TRIP\" .EQ. ON) THEN GOTO 02000\n"
                f"01010 IF (\"%X%{sys}.ENA\" .EQ. ON) THEN GOTO 01100\n"
                f"01020 IF (\"%X%{sys}.ENA\" .EQ. OFF) THEN GOTO 01200\n"
                f"01100 ON (\"%X%{sys}1.ENA\")\n"
                f"01110 GOTO 03000\n"
                f"01200 OFF (\"%X%{sys}1.ENA\")\n"
                f"01210 GOTO 03000\n"
                f"02000 OFF (\"%X%{sys}1.ENA\"), ON (\"%X%{sys}.SAFE.ALM\")\n"
                f"02010 GOTO 03000\n"
                f"03000 C --- END ---"
            ),
            "changed_prompt": f"A chilled water flow switch was field-installed on the evaporator, and low flow is just as dangerous to the chiller as low refrigerant pressure or high condenser pressure -- it can freeze and split the evaporator tubes. Extend the safety check at line 01000 to also GOTO 02000 whenever the Chilled Water Flow Switch (\"%X%{sys}.CHW.FLOW\") is OFF (no flow proven), using .OR. to add it alongside the two existing trip conditions. No renumbering is needed for this one -- you're extending the existing condition, not inserting a new block.",
            "points": [
                {"name": f"%X%{sys}.LOP.TRIP",
                    "desc": "Low Refrigerant Pressure Safety Trip", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.HCP.TRIP",
                    "desc": "High Condenser Pressure Safety Trip", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.ENA",
                    "desc": "Chiller Plant Master Enable (already exists)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}1.ENA",
                    "desc": "Chiller 1 Enable", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.SAFE.ALM",
                    "desc": "Safety Shutdown Alarm (virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.CHW.FLOW",
                    "desc": "Chilled Water Flow Switch (new, DI -- OFF = no flow proven)", "type": "DI", "kind": "physical"}
            ],
            "help": "Just add .OR. \"%X%{sys}.CHW.FLOW\" .EQ. OFF to the existing condition at line 01000 -- the rest of the program (the Normal Run/Standby/Safety Shutdown blocks) doesn't need to change at all, since they're already reached by GOTO from that one combined check.",
            "checks": [
                {"force": {f"%X%{sys}.LOP.TRIP": 0, f"%X%{sys}.HCP.TRIP": 0, f"%X%{sys}.CHW.FLOW": 0, f"%X%{sys}.ENA": 1}, "expect": {f"%X%{sys}1.ENA": 0, f"%X%{sys}.SAFE.ALM": 1}, "note": "no flow proven -- safety shutdown trips even though neither pressure trip is active"},
                {"force": {f"%X%{sys}.LOP.TRIP": 0, f"%X%{sys}.HCP.TRIP": 0, f"%X%{sys}.CHW.FLOW": 1, f"%X%{sys}.ENA": 1}, "expect": {f"%X%{sys}1.ENA": 1}, "note": "flow proven, no other trips -- normal run"}
            ]
        }
    },
    {
        "title": "Additional Device: Cooling Tower Vibration Switch Safety Shutdown Added",
        "difficulty": "expert",
        "systems": CT_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Vibration Switch Safety Shutdown Added",
            "original_prompt": f"Whenever Tower Fan Enable (\"%X%{sys}.FAN.ENA\") is ON, a LOOP statement modulates Tower Fan Speed Output (\"%X%{sys}.FAN.SPD\") to maintain Condenser Water Supply Temp (\"%X%{sys}.CWS.T\") at Setpoint (\"%X%{sys}.CWS.T.SP\"). When Tower Fan Enable is OFF, Tower Fan Speed Output is held at 0.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}.FAN.ENA\" .EQ. ON) THEN LOOP(0,\"%X%{sys}.CWS.T\",\"%X%{sys}.FAN.SPD\",\"%X%{sys}.CWS.T.SP\",4.0,0.2,0.0,1,50.0,20,100,0)\n"
                f"01010 IF (\"%X%{sys}.FAN.ENA\" .EQ. OFF) THEN SET(0,\"%X%{sys}.FAN.SPD\")"
            ),
            "changed_prompt": f"A vibration switch was field-installed on the fan deck to catch bearing failure before it gets worse. Add a safety shutdown: if the Vibration Switch (\"%X%{sys}.VIB.SW\") is ON (tripped), force Tower Fan Speed Output to 0 and turn ON a Vibration Safety Alarm (\"%X%{sys}.VIB.ALM\", a virtual point) -- skip the LOOP entirely while this condition is active, since a vibrating fan needs to stop regardless of what condenser water temp calls for. Return to the normal LOOP control above once the Vibration Switch clears and an operator pulses Manual Reset (\"%X%{sys}.VIB.RST\") ON.",
            "points": [
                {"name": f"%X%{sys}.FAN.ENA",
                    "desc": "Tower Fan Enable (already exists)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}.CWS.T",
                    "desc": "Condenser Water Supply Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.CWS.T.SP",
                    "desc": "Condenser Water Supply Temp Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.FAN.SPD",
                    "desc": "Tower Fan Speed Output (0-100%)", "type": "AO", "kind": "physical"},
                {"name": f"%X%{sys}.VIB.SW",
                    "desc": "Vibration Switch (new, DI -- ON = tripped)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.VIB.ALM",
                    "desc": "Vibration Safety Alarm (new, virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.VIB.RST",
                    "desc": "Manual Reset Pulse (new, operator-facing)", "type": "BV", "kind": "physical"}
            ],
            "help": "This needs a latch, similar to the VOC full-speed exhaust fan scenario -- use VIB.ALM itself (or a separate flag) to remember the trip even after VIB.SW clears, and only drop it when VIB.RST pulses while VIB.SW is also clear. Skip the LOOP call entirely whenever that latch is active, the same way the manual-override exercises skip it.",
            "checks": [
                {"force": {f"%X%{sys}.FAN.ENA": 1, f"%X%{sys}.CWS.T": 90, f"%X%{sys}.CWS.T.SP": 85, f"%X%{sys}.VIB.SW": 1, f"%X%{sys}.VIB.ALM": 0, f"%X%{sys}.VIB.RST": 0}, "expect": {f"%X%{sys}.FAN.SPD": 0, f"%X%{sys}.VIB.ALM": 1}, "note": "vibration switch trips -- fan speed forced to 0 and alarm latches on"},
                {"force": {f"%X%{sys}.FAN.ENA": 1, f"%X%{sys}.CWS.T": 90, f"%X%{sys}.CWS.T.SP": 85, f"%X%{sys}.VIB.SW": 0, f"%X%{sys}.VIB.ALM": 1, f"%X%{sys}.VIB.RST": 0}, "cycles": 3, "expect": {f"%X%{sys}.VIB.ALM": 1, f"%X%{sys}.FAN.SPD": 0}, "note": "switch cleared but not manually reset -- stays latched, fan still held at 0"},
                {"force": {f"%X%{sys}.FAN.ENA": 1, f"%X%{sys}.CWS.T": 90, f"%X%{sys}.CWS.T.SP": 85, f"%X%{sys}.VIB.SW": 0, f"%X%{sys}.VIB.ALM": 1, f"%X%{sys}.VIB.RST": 1}, "expect": {f"%X%{sys}.VIB.ALM": 0}, "note": "switch clear and manual reset pulsed -- alarm clears, normal LOOP control resumes"}
            ]
        }
    },
    {
        "title": "Compliance Fix: Add High Stack Temperature Safety Trip to Boiler Safety Dispatch",
        "difficulty": "expert",
        "systems": BOILER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} High Stack Temperature Safety Trip",
            "original_prompt": f"Boiler safeties override normal firing via a GOTO decision tree. If the Low Water Cutoff (\"%X%{sys}1.LWCO\") is ON, GOTO line 02000 (Safety Shutdown), skipping normal firing logic entirely. Otherwise, check Boiler 1 Firing Demand (\"%X%{sys}1.DMD\"): if ON, GOTO line 01100 (Firing); if OFF, GOTO line 01200 (Standby). At line 01100, turn ON Boiler 1 Enable (\"%X%{sys}1.ENA\"), then GOTO line 03000. At line 01200, turn OFF Boiler 1 Enable, then GOTO line 03000. At line 02000, turn OFF Boiler 1 Enable and turn ON a Low Water Safety Alarm (\"%X%{sys}1.LWCO.ALM\"), then GOTO line 03000. Line 03000 is a comment marking the end of the scan and is the last line in the program.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}1.LWCO\" .EQ. ON) THEN GOTO 02000\n"
                f"01010 IF (\"%X%{sys}1.DMD\" .EQ. ON) THEN GOTO 01100\n"
                f"01020 IF (\"%X%{sys}1.DMD\" .EQ. OFF) THEN GOTO 01200\n"
                f"01100 ON (\"%X%{sys}1.ENA\")\n"
                f"01110 GOTO 03000\n"
                f"01200 OFF (\"%X%{sys}1.ENA\")\n"
                f"01210 GOTO 03000\n"
                f"02000 OFF (\"%X%{sys}1.ENA\"), ON (\"%X%{sys}1.LWCO.ALM\")\n"
                f"02010 GOTO 03000\n"
                f"03000 C --- END ---"
            ),
            "changed_prompt": f"A stack temperature sensor was field-installed in the flue to catch an over-fired condition before it damages the heat exchanger. Extend the safety check at line 01000 to also GOTO 02000 whenever Stack Temperature (\"%X%{sys}1.STK.T\") rises above a High Stack Temp Limit (\"%X%{sys}1.STK.T.HI\"), using .OR. to add it alongside the existing low water cutoff condition. No renumbering is needed for this one -- you're extending the existing condition, not inserting a new block.",
            "points": [
                {"name": f"%X%{sys}1.LWCO",
                    "desc": "Low Water Cutoff Safety Switch", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}1.DMD",
                    "desc": "Firing Demand (virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}1.ENA",
                    "desc": "Boiler 1 Enable", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}1.LWCO.ALM",
                    "desc": "Low Water Safety Alarm (virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}1.STK.T",
                    "desc": "Stack Temperature Sensor (new)", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}1.STK.T.HI",
                    "desc": "High Stack Temp Limit (new, virtual point)", "type": "AV", "kind": "virtual"}
            ],
            "help": "Just add .OR. \"%X%{sys}1.STK.T\" .GT. \"%X%{sys}1.STK.T.HI\" to the existing condition at line 01000 -- the rest of the program doesn't need to change.",
            "checks": [
                {"force": {f"%X%{sys}1.LWCO": 0, f"%X%{sys}1.STK.T": 900, f"%X%{sys}1.STK.T.HI": 850, f"%X%{sys}1.DMD": 1}, "expect": {f"%X%{sys}1.ENA": 0, f"%X%{sys}1.LWCO.ALM": 1}, "note": "stack temp over the high limit -- safety shutdown trips even though low water cutoff isn't active"},
                {"force": {f"%X%{sys}1.LWCO": 0, f"%X%{sys}1.STK.T": 700, f"%X%{sys}1.STK.T.HI": 850, f"%X%{sys}1.DMD": 1}, "expect": {f"%X%{sys}1.ENA": 1}, "note": "stack temp under the limit, no other trips -- normal firing"}
            ]
        }
    },
    {
        "title": "Additional Device: CRAC High Head Pressure Safety Shutdown Added",
        "difficulty": "expert",
        "systems": CRAC_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys}201 High Head Pressure Safety Shutdown Added",
            "original_prompt": f"Whenever {sys}201 is enabled (\"%X%{sys}201.ENA\" is ON), a LOOP statement modulates the Modulating Cooling Output (\"%X%{sys}201.CLG.MOD\") to maintain Discharge Air Temp (\"%X%{sys}201.DAT\") at Setpoint (\"%X%{sys}201.DAT.SP\"). Separately, if the Modulating Cooling Output stays above 90% continuously, a Stage-Up Timer (\"%X%{sys}201.STGUP\") accumulates via SAMPLE(1); once it exceeds 180 seconds, a second compressor (\"%X%{sys}201.COMP2.ENA\") stages ON for extra capacity.",
            "starter_code": (
                f"01000 IF (\"%X%{sys}201.ENA\" .EQ. ON) THEN LOOP(0,\"%X%{sys}201.DAT\",\"%X%{sys}201.CLG.MOD\",\"%X%{sys}201.DAT.SP\",3.5,0.2,0.0,1,30.0,0,100,0)\n"
                f"01010 SAMPLE(1) IF (\"%X%{sys}201.CLG.MOD\" .GT. 90) THEN \"%X%{sys}201.STGUP\" = \"%X%{sys}201.STGUP\" + 1\n"
                f"01020 IF (\"%X%{sys}201.CLG.MOD\" .LE. 90) THEN \"%X%{sys}201.STGUP\" = 0\n"
                f"01030 IF (\"%X%{sys}201.STGUP\" .GT. 180) THEN ON (\"%X%{sys}201.COMP2.ENA\")\n"
                f"01040 IF (\"%X%{sys}201.CLG.MOD\" .LT. 60) THEN OFF (\"%X%{sys}201.COMP2.ENA\"), \"%X%{sys}201.STGUP\" = 0"
            ),
            "changed_prompt": f"A high head pressure switch was field-installed on the refrigeration circuit. Add a safety shutdown: if the High Head Pressure Switch (\"%X%{sys}201.HHP.SW\") is ON (tripped), force the Modulating Cooling Output to 0, force Compressor 2 OFF, and turn ON a High Head Pressure Alarm (\"%X%{sys}201.HHP.ALM\", a virtual point) -- skip the LOOP and staging logic entirely while this condition is active. Return to normal control once the switch clears and an operator pulses Manual Reset (\"%X%{sys}201.HHP.RST\") ON.",
            "points": [
                {"name": f"%X%{sys}201.ENA",
                    "desc": "CRAC201 Enable Command (already exists)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}201.DAT",
                    "desc": "Discharge Air Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}201.DAT.SP",
                    "desc": "Discharge Air Temp Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}201.CLG.MOD",
                    "desc": "Modulating Cooling Output (0-100%)", "type": "AO", "kind": "physical"},
                {"name": f"%X%{sys}201.STGUP",
                    "desc": "Stage-Up Timer Accumulator (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}201.COMP2.ENA",
                    "desc": "Compressor 2 Stage Enable", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}201.HHP.SW",
                    "desc": "High Head Pressure Switch (new, DI -- ON = tripped)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}201.HHP.ALM",
                    "desc": "High Head Pressure Alarm (new, virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}201.HHP.RST",
                    "desc": "Manual Reset Pulse (new, operator-facing)", "type": "BV", "kind": "physical"}
            ],
            "help": "Same latch pattern as the cooling tower vibration switch exercise -- use HHP.ALM to remember the trip past the moment HHP.SW clears, and only drop it when HHP.RST pulses while HHP.SW is also clear. While the latch is active, skip the LOOP call AND the staging logic (both the SAMPLE(1) timer and the two stage IFs) entirely.",
            "checks": [
                {"force": {f"%X%{sys}201.ENA": 1, f"%X%{sys}201.DAT": 80, f"%X%{sys}201.DAT.SP": 72, f"%X%{sys}201.HHP.SW": 1, f"%X%{sys}201.HHP.ALM": 0, f"%X%{sys}201.HHP.RST": 0, f"%X%{sys}201.COMP2.ENA": 1}, "expect": {f"%X%{sys}201.CLG.MOD": 0, f"%X%{sys}201.COMP2.ENA": 0, f"%X%{sys}201.HHP.ALM": 1}, "note": "high head pressure trips -- cooling output and compressor 2 forced off, alarm latches on"},
                {"force": {f"%X%{sys}201.ENA": 1, f"%X%{sys}201.DAT": 80, f"%X%{sys}201.DAT.SP": 72, f"%X%{sys}201.HHP.SW": 0, f"%X%{sys}201.HHP.ALM": 1, f"%X%{sys}201.HHP.RST": 0, f"%X%{sys}201.COMP2.ENA": 0}, "cycles": 3, "expect": {f"%X%{sys}201.HHP.ALM": 1, f"%X%{sys}201.CLG.MOD": 0}, "note": "switch cleared but not manually reset -- stays latched"},
                {"force": {f"%X%{sys}201.ENA": 1, f"%X%{sys}201.DAT": 80, f"%X%{sys}201.DAT.SP": 72, f"%X%{sys}201.HHP.SW": 0, f"%X%{sys}201.HHP.ALM": 1, f"%X%{sys}201.HHP.RST": 1}, "expect": {f"%X%{sys}201.HHP.ALM": 0}, "note": "switch clear and manual reset pulsed -- alarm clears, normal control resumes"}
            ]
        }
    }
]


VALID_DIFFICULTIES = {"easy", "medium", "expert"}


def _narrow_pool_by_equipment(pool, equipment):
    """Keep only templates that touch the requested equipment category."""
    if equipment not in EQUIPMENT_CATEGORIES:
        return pool
    prefixes = EQUIPMENT_CATEGORIES[equipment]["prefixes"]
    filtered = [
        t for t in pool
        if any(s.startswith(p) for s in t.get("systems", SYSTEMS) for p in prefixes)
    ]
    return filtered or pool


def _narrow_systems_by_equipment(systems_list, equipment):
    """Within one template's systems list, keep only the units matching the
    requested equipment category (e.g. drop MAU01 from an AHU01/MAU01 mixed
    pool when the student specifically asked to practice on AHUs)."""
    if equipment not in EQUIPMENT_CATEGORIES:
        return systems_list
    prefixes = EQUIPMENT_CATEGORIES[equipment]["prefixes"]
    narrowed = [s for s in systems_list if any(s.startswith(p) for p in prefixes)]
    return narrowed or systems_list


def generate_random_scenario(difficulty=None, equipment=None):
    pool = TEMPLATES
    if difficulty in VALID_DIFFICULTIES:
        filtered = [t for t in TEMPLATES if t.get("difficulty") == difficulty]
        if filtered:
            pool = filtered
    pool = _narrow_pool_by_equipment(pool, equipment)
    template = random.choice(pool)
    # Pick the equipment AFTER the template so the system chosen is always
    # one this sequence actually makes sense for (an economizer sequence
    # only ever lands on an AHU, never a pump, etc.), narrowed further to the
    # requested equipment category if one was given.
    candidate_systems = _narrow_systems_by_equipment(template.get("systems", SYSTEMS), equipment)
    sys = random.choice(candidate_systems)
    scenario = template["generate"](sys)
    scenario["difficulty"] = template.get("difficulty", "medium")
    scenario["multiLine"] = template.get("multiLine", False)
    scenario["openPoints"] = template.get("openPoints", False)
    return scenario


def generate_random_edit_scenario(difficulty=None, equipment=None):
    pool = EDIT_TEMPLATES
    if difficulty in VALID_DIFFICULTIES:
        filtered = [t for t in EDIT_TEMPLATES if t.get("difficulty") == difficulty]
        if filtered:
            pool = filtered
    pool = _narrow_pool_by_equipment(pool, equipment)
    template = random.choice(pool)
    candidate_systems = _narrow_systems_by_equipment(template.get("systems", SYSTEMS), equipment)
    sys = random.choice(candidate_systems)
    scenario = template["generate"](sys)
    scenario["difficulty"] = template.get("difficulty", "medium")
    scenario["mode"] = "edit"
    return scenario


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/get-scenario", methods=["POST"])
def get_scenario():
    data = request.get_json(silent=True) or {}
    difficulty = (data.get("difficulty") or "").strip().lower() or None
    equipment = (data.get("equipment") or "").strip().lower() or None
    scenario = generate_random_scenario(difficulty, equipment)
    return jsonify(scenario)


@app.route("/api/get-edit-scenario", methods=["POST"])
def get_edit_scenario():
    data = request.get_json(silent=True) or {}
    difficulty = (data.get("difficulty") or "").strip().lower() or None
    equipment = (data.get("equipment") or "").strip().lower() or None
    scenario = generate_random_edit_scenario(difficulty, equipment)
    return jsonify(scenario)


@app.route("/api/equipment-categories", methods=["GET"])
def equipment_categories():
    return jsonify([
        {"key": key, "label": info["label"]}
        for key, info in EQUIPMENT_CATEGORIES.items()
    ])


# NOTE: Program correctness is no longer graded by an AI call. Both the old
# /api/grade (AI code audit) and /api/hint (AI hint ladder) routes, along
# with the Diagnostics tab and Hint button that used them, have been
# removed -- see git history if you ever need them back. Program accuracy is
# now checked locally in the browser by actually running the student's code
# through the simulator against each scenario's declared "checks" (forced
# input values + the point states the sequence says should result) and
# comparing the outcome -- see runProgramChecks() / TEMPLATE_CHECKS-style
# "checks" fields in templates/index.html and the "checks"/"help" keys inside
# each TEMPLATES/EDIT_TEMPLATES entry's "generate" dict below.


# NOTE: Knowledge Check "code" questions in the Learn PPCL tab used to be
# graded by calling OpenAI here (see git history for the old
# /api/grade-knowledge-check route). They're now graded locally in the
# browser against real answer keys pulled from Gabe's completed PPCL
# workbook (see KNOWLEDGE_CHECKS / ppclAnswerCheck() in templates/index.html)
# -- no API call, no cost, and it grades against the actual answer key
# instead of an LLM's best guess.


if __name__ == "__main__":
    # Local dev only -- in production this app should run under a real WSGI
    # server (gunicorn), which is what the Procfile points to. debug mode
    # must stay off anywhere reachable from outside your own machine: it
    # exposes an interactive debugger/console to anyone who triggers an
    # unhandled error.
    debug_mode = os.getenv("FLASK_DEBUG", "0") == "1"
    port = int(os.getenv("PORT", "8080"))
    app.run(debug=debug_mode, host="0.0.0.0", port=port)
