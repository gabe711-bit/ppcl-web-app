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
        "title": "Zone Cooling Valve Staging with Condensate Overflow Safety",
        "difficulty": "easy",
        "multiLine": True,
        "systems": FCU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Cooling Valve Staging with Condensate Safety",
            "prompt": f"This FCU has two cooling valves for extra capacity, plus a condensate overflow safety switch. First check the safety: if Condensate Overflow Switch (\"%X%{sys}.COND.SW\") is ON, force both Cooling Valve 1 (\"%X%{sys}.CLG.VLV1\") and Cooling Valve 2 (\"%X%{sys}.CLG.VLV2\") OFF immediately, regardless of temperature -- a full condensate pan is a water-damage risk that overrides normal control. Otherwise, stage the valves off Room Temperature (\"%X%{sys}.RMT\") vs Cooling Setpoint (\"%X%{sys}.RMT.SP\"): if RMT is more than 2 degrees above RMT.SP, turn ON both valves for extra capacity. If RMT is above RMT.SP but by 2 degrees or less, turn ON only Valve 1 (Valve 2 OFF). If RMT is at or below RMT.SP, turn both valves OFF.",
            "points": [
                {"name": f"%X%{sys}.RMT",
                    "desc": "Room Temperature Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.RMT.SP",
                    "desc": "Cooling Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.CLG.VLV1",
                    "desc": "Cooling Valve 1 Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.CLG.VLV2",
                    "desc": "Cooling Valve 2 Command (second-stage capacity)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.COND.SW",
                    "desc": "Condensate Overflow Switch (closes when the drain pan is full)", "type": "DI", "kind": "physical"}
            ],
            "help": "Check the safety condition first: COND.SW ON should force both valves OFF and skip the staging logic below it entirely -- put the staging logic in the ELSE of the safety check, not after it unconditionally, or a full pan won't actually override anything. Inside that ELSE, you're comparing the same RMT-to-RMT.SP difference two different ways: RMT .GT. (RMT.SP + 2) decides whether Valve 2 joins in, while RMT .GT. RMT.SP alone decides whether Valve 1 is on at all. Write both comparisons explicitly rather than trying to combine them into one -- Valve 2 should never be ON while Valve 1 is OFF.",
            "checks": [
                {"force": {f"%X%{sys}.COND.SW": 1, f"%X%{sys}.RMT": 90, f"%X%{sys}.RMT.SP": 72}, "expect": {f"%X%{sys}.CLG.VLV1": 0, f"%X%{sys}.CLG.VLV2": 0}, "note": "condensate overflow -- both valves forced off even with RMT way above setpoint"},
                {"force": {f"%X%{sys}.COND.SW": 0, f"%X%{sys}.RMT": 90, f"%X%{sys}.RMT.SP": 72}, "expect": {f"%X%{sys}.CLG.VLV1": 1, f"%X%{sys}.CLG.VLV2": 1}, "note": "more than 2 degrees over setpoint -- both valves should stage ON"},
                {"force": {f"%X%{sys}.COND.SW": 0, f"%X%{sys}.RMT": 73.5, f"%X%{sys}.RMT.SP": 72}, "expect": {f"%X%{sys}.CLG.VLV1": 1, f"%X%{sys}.CLG.VLV2": 0}, "note": "only 1.5 degrees over setpoint -- Valve 1 only, Valve 2 stays off"},
                {"force": {f"%X%{sys}.COND.SW": 0, f"%X%{sys}.RMT": 65, f"%X%{sys}.RMT.SP": 72}, "expect": {f"%X%{sys}.CLG.VLV1": 0, f"%X%{sys}.CLG.VLV2": 0}, "note": "below setpoint -- both valves OFF"}
            ]
        }
    },
    {
        "title": "FCU Cycling Fan Control with Dirty Filter Alarm",
        "difficulty": "easy",
        "multiLine": True,
        "systems": FCU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Cycling Fan Control with Filter Alarm",
            "prompt": f"This FCU is a cycling unit -- instead of modulating a valve, the fan itself cycles on and off with zone temperature. If Room Temperature (\"%X%{sys}.RMT\") rises above the Cooling Setpoint (\"%X%{sys}.CLG.SP\") OR falls below the Heating Setpoint (\"%X%{sys}.HTG.SP\"), turn the Fan (\"%X%{sys}.FAN.ENA\") ON. When Room Temperature is between the two setpoints (in the deadband), turn the Fan OFF. Separately, this unit also monitors filter condition: if Filter Status Switch (\"%X%{sys}.FLT.S\") is ON (contacts close when the pressure drop across the filter gets too high), turn ON a Dirty Filter Alarm (\"%X%{sys}.FLT.ALM\" -- a virtual point). Otherwise turn it OFF. This alarm logic is completely independent of the fan cycling logic above -- it doesn't affect whether the fan runs.",
            "points": [
                {"name": f"%X%{sys}.RMT",
                    "desc": "Room Temperature Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.CLG.SP",
                    "desc": "Cooling Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.HTG.SP",
                    "desc": "Heating Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.FAN.ENA",
                    "desc": "Fan Enable Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.FLT.S",
                    "desc": "Filter Status Switch (pressure-drop contact)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.FLT.ALM",
                    "desc": "Dirty Filter Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "This is really two unrelated pieces of logic in the same program. The fan cycling half is two comparisons joined by .OR.: RMT .GT. CLG.SP .OR. RMT .LT. HTG.SP, both calling for the same action (fan ON), combined in one IF -- anything in between is the deadband and belongs in the ELSE (FAN.ENA OFF). The filter alarm half is a plain one-to-one mirror of FLT.S onto FLT.ALM with its own separate IF/THEN/ELSE -- don't try to fold it into the fan condition, since a dirty filter doesn't change whether the fan should be running.",
            "checks": [
                {"force": {f"%X%{sys}.RMT": 80, f"%X%{sys}.CLG.SP": 75, f"%X%{sys}.HTG.SP": 68}, "expect": {f"%X%{sys}.FAN.ENA": 1}, "note": "above cooling setpoint -- fan should be ON"},
                {"force": {f"%X%{sys}.RMT": 60, f"%X%{sys}.CLG.SP": 75, f"%X%{sys}.HTG.SP": 68}, "expect": {f"%X%{sys}.FAN.ENA": 1}, "note": "below heating setpoint -- fan should be ON"},
                {"force": {f"%X%{sys}.RMT": 70, f"%X%{sys}.CLG.SP": 75, f"%X%{sys}.HTG.SP": 68}, "expect": {f"%X%{sys}.FAN.ENA": 0}, "note": "in the deadband -- fan should be OFF"},
                {"force": {f"%X%{sys}.FLT.S": 1}, "expect": {f"%X%{sys}.FLT.ALM": 1}, "note": "filter switch tripped -- alarm should be ON"},
                {"force": {f"%X%{sys}.FLT.S": 0}, "expect": {f"%X%{sys}.FLT.ALM": 0}, "note": "filter switch clear -- alarm should be OFF"}
            ]
        }
    },
    {
        "title": "Outside Air Damper Fault Check with Latching Alarm",
        "difficulty": "easy",
        "multiLine": True,
        "systems": AHU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Damper Fault Alarm (Latching)",
            "prompt": f"If Outside Air Damper Command (\"%X%{sys}.OAD.CMD\") is ON (commanded open) and Outside Air Damper Status (\"%X%{sys}.OAD.S\") is OFF (not proven open), turn ON a Damper Fault Alarm (\"%X%{sys}.OAD.ALM\") -- a virtual point. Because a flickering end switch could otherwise chatter this alarm on and off, make it a LATCHING alarm: once OAD.ALM turns ON, it should stay ON even after the fault condition clears, until a technician manually clears it. Add a separate check: if Alarm Reset (\"%X%{sys}.OAD.ALM.RST\") is ON, force OAD.ALM OFF regardless of the current fault state, so a tech can acknowledge and clear the alarm from the operator workstation even while the underlying fault is still present.",
            "points": [
                {"name": f"%X%{sys}.OAD.CMD",
                    "desc": "Damper Open/Closed Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.OAD.S",
                    "desc": "Damper Proven-Open Status (proof for OAD.CMD)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.OAD.ALM",
                    "desc": "Damper Fault Alarm (virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.OAD.ALM.RST",
                    "desc": "Alarm Reset Command (pulsed from the operator graphic to acknowledge/clear a latched alarm)", "type": "BV", "kind": "virtual"}
            ],
            "help": "The fault check itself is the same .AND. as before -- CMD ON while S OFF sets OAD.ALM ON -- but this time DON'T write an ELSE for it. Without an ELSE, the alarm just holds whatever it was last set to once the fault clears, which is exactly the latching behavior the prompt wants. Write the reset as a completely separate IF/THEN placed AFTER the fault check, so a reset pulse can override an alarm the fault check just re-set on the same cycle -- that ordering is what lets a tech clear the alarm even while the fault is still physically present.",
            "checks": [
                {"force": {f"%X%{sys}.OAD.CMD": 1, f"%X%{sys}.OAD.S": 0, f"%X%{sys}.OAD.ALM.RST": 0}, "expect": {f"%X%{sys}.OAD.ALM": 1}, "note": "fault present, no reset -- latch sets"},
                {"force": {f"%X%{sys}.OAD.CMD": 1, f"%X%{sys}.OAD.S": 0, f"%X%{sys}.OAD.ALM.RST": 1}, "expect": {f"%X%{sys}.OAD.ALM": 0}, "note": "reset pulsed -- alarm forced off even while the fault is still present"},
                {"force": {f"%X%{sys}.OAD.CMD": 0, f"%X%{sys}.OAD.S": 0, f"%X%{sys}.OAD.ALM.RST": 0, f"%X%{sys}.OAD.ALM": 1}, "expect": {f"%X%{sys}.OAD.ALM": 1}, "note": "fault cleared but no reset pulsed -- alarm should stay latched ON, not self-clear"}
            ]
        }
    },
    {
        "title": "Supply Fan Status Verification Alarm with Silence Override",
        "difficulty": "easy",
        "multiLine": True,
        "systems": AHU_SYSTEMS + MAU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Supply Fan Fail Alarm (Silenceable)",
            "prompt": f"If Supply Fan Start/Stop Command (\"%X%{sys}.SF.SS\") is ON and Supply Fan Status (\"%X%{sys}.SF.S\") is OFF, turn ON a Supply Fan Fail Alarm (\"%X%{sys}.SF.ALM\") -- a virtual point. Otherwise, turn it OFF. Field techs also need to be able to silence this alarm during planned maintenance without editing the program: after evaluating the fault above, add a second check -- if Alarm Silence (\"%X%{sys}.SF.ALM.SIL\") is ON, force SF.ALM OFF regardless of what the fault check above just decided.",
            "points": [
                {"name": f"%X%{sys}.SF.SS",
                    "desc": "Supply Fan Start/Stop Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.SF.S",
                    "desc": "Supply Fan Running Status", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.SF.ALM",
                    "desc": "Supply Fan Fail Alarm (virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.SF.ALM.SIL",
                    "desc": "Alarm Silence Switch (maintenance override -- software point toggled from the graphic)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Write the fault check exactly as before -- .AND. together SS ON with S OFF, with an ELSE that clears the alarm otherwise. Then add the silence override as a SEPARATE IF/THEN placed after it: IF SF.ALM.SIL .EQ. 1 THEN SF.ALM = 0, with no ELSE needed (the fault check above already handles every other case). Putting the silence check second, not first, is what lets it override whatever the fault check just decided rather than being overridden by it.",
            "checks": [
                {"force": {f"%X%{sys}.SF.SS": 1, f"%X%{sys}.SF.S": 0, f"%X%{sys}.SF.ALM.SIL": 0}, "expect": {f"%X%{sys}.SF.ALM": 1}, "note": "commanded to run but not proven running, not silenced -- alarm should be ON"},
                {"force": {f"%X%{sys}.SF.SS": 1, f"%X%{sys}.SF.S": 0, f"%X%{sys}.SF.ALM.SIL": 1}, "expect": {f"%X%{sys}.SF.ALM": 0}, "note": "fault present but silenced -- alarm should be forced OFF"},
                {"force": {f"%X%{sys}.SF.SS": 1, f"%X%{sys}.SF.S": 1, f"%X%{sys}.SF.ALM.SIL": 0}, "expect": {f"%X%{sys}.SF.ALM": 0}, "note": "commanded to run and proven running -- no fault"},
                {"force": {f"%X%{sys}.SF.SS": 0, f"%X%{sys}.SF.S": 0, f"%X%{sys}.SF.ALM.SIL": 0}, "expect": {f"%X%{sys}.SF.ALM": 0}, "note": "not commanded to run -- no fault"}
            ]
        }
    },
    {
        "title": "Supply Fan VFD Trouble Alarm with Trip Bypass",
        "difficulty": "easy",
        "multiLine": True,
        "systems": AHU_SYSTEMS + MAU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} VFD Trouble Alarm with Trip Bypass",
            "prompt": f"Real field panels generate a VFD trouble alarm independent from the fan's start/stop status alarm -- a current switch confirms the fan is in the commanded state, but the VFD itself also has its own fault relay for drive-level problems (overcurrent, overtemp, etc.) that should trip the fan immediately regardless of what status says. If Supply Fan VFD Trouble (\"%X%{sys}.SF.VFD.FLT\") is ON, turn ON a VFD Trouble Alarm (\"%X%{sys}.SF.VFD.ALM\" -- a virtual point). Otherwise, turn the alarm OFF. Separately, add a troubleshooting bypass: if VFD.FLT is ON AND VFD Trip Bypass (\"%X%{sys}.SF.VFD.BYP\") is OFF, force Supply Fan Enable (\"%X%{sys}.SF.ENA\") OFF -- but if the bypass switch is ON, leave SF.ENA alone even while VFD.FLT is present, since techs sometimes need to bypass the automatic trip briefly to observe drive behavior under load. The alarm itself still comes on regardless of bypass -- bypassing the trip doesn't mean the fault isn't real.",
            "points": [
                {"name": f"%X%{sys}.SF.VFD.FLT",
                    "desc": "Supply Fan VFD Trouble Contact (fault relay from the VFD drive itself, separate from the fan's running-status feedback)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.SF.VFD.ALM",
                    "desc": "Supply Fan VFD Trouble Alarm (virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.SF.VFD.BYP",
                    "desc": "VFD Trip Bypass Switch (maintenance override -- keyed switch or software point)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.SF.ENA",
                    "desc": "Supply Fan Enable Command (already exists -- your logic must write to this point)", "type": "DO", "kind": "physical"}
            ],
            "help": "Two separate IF blocks. The alarm block is a plain mirror of VFD.FLT onto VFD.ALM with an ELSE. The trip block is its own IF: VFD.FLT .EQ. 1 .AND. VFD.BYP .EQ. 0 -- THEN force SF.ENA OFF, with NO ELSE. Leaving off the ELSE is what makes bypass work: when BYP is ON (or FLT is OFF), that IF simply never executes, so SF.ENA keeps whatever value it already had instead of being forced to anything.",
            "checks": [
                {"force": {f"%X%{sys}.SF.VFD.FLT": 1, f"%X%{sys}.SF.VFD.BYP": 0}, "expect": {f"%X%{sys}.SF.VFD.ALM": 1, f"%X%{sys}.SF.ENA": 0}, "note": "VFD trouble, not bypassed -- alarm ON and fan tripped off"},
                {"force": {f"%X%{sys}.SF.VFD.FLT": 1, f"%X%{sys}.SF.VFD.BYP": 1, f"%X%{sys}.SF.ENA": 1}, "expect": {f"%X%{sys}.SF.VFD.ALM": 1, f"%X%{sys}.SF.ENA": 1}, "note": "VFD trouble but bypassed -- alarm still ON, but fan enable left alone (should stay at its forced starting value)"},
                {"force": {f"%X%{sys}.SF.VFD.FLT": 0, f"%X%{sys}.SF.VFD.BYP": 0}, "expect": {f"%X%{sys}.SF.VFD.ALM": 0}, "note": "no VFD trouble -- alarm should be OFF"}
            ]
        }
    },
    {
        "title": "Exhaust Fan Louver Interlock with VFD Trouble Override",
        "difficulty": "easy",
        "multiLine": True,
        "systems": EF_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Louver Interlock with VFD Trouble Override",
            "prompt": f"If Exhaust Fan Start Command (\"%X%{sys}.EF.SS\") is ON and Exhaust Louver Position Feedback (\"%X%{sys}.LVR.POS\") is ON (proven open), turn ON the Exhaust Fan Run Output (\"%X%{sys}.EF.RUN\"). Otherwise, turn it OFF -- the fan should never run with the louver unproven. This fan also has a VFD trouble contact that takes priority over all of that: if Exhaust Fan VFD Trouble (\"%X%{sys}.EF.VFD.FLT\") is ON, turn ON a VFD Trouble Alarm (\"%X%{sys}.EF.VFD.ALM\" -- a virtual point) and force the Exhaust Fan Run Output OFF, overriding the louver interlock entirely -- a drive fault takes priority over normal sequencing. Otherwise, clear the VFD alarm and evaluate the louver interlock as described above.",
            "points": [
                {"name": f"%X%{sys}.EF.SS",
                    "desc": "Exhaust Fan Start Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.LVR.POS",
                    "desc": "Exhaust Louver Position Feedback (proven open)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.EF.RUN",
                    "desc": "Exhaust Fan Run Output", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.EF.VFD.FLT",
                    "desc": "Exhaust Fan VFD Trouble Contact", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.EF.VFD.ALM",
                    "desc": "Exhaust Fan VFD Trouble Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Structure this as one outer IF on VFD.FLT with the louver interlock nested in its ELSE: IF VFD.FLT .EQ. 1 THEN (VFD.ALM=1, EF.RUN=0) ELSE (VFD.ALM=0, then the same SS-AND-LVR.POS check as a plain louver interlock decides EF.RUN). Nesting it this way is what makes the VFD fault override the louver logic entirely rather than just being one more condition ANDed into it.",
            "checks": [
                {"force": {f"%X%{sys}.EF.VFD.FLT": 1, f"%X%{sys}.EF.SS": 1, f"%X%{sys}.LVR.POS": 1}, "expect": {f"%X%{sys}.EF.VFD.ALM": 1, f"%X%{sys}.EF.RUN": 0}, "note": "VFD trouble overrides even a good start command and proven-open louver"},
                {"force": {f"%X%{sys}.EF.VFD.FLT": 0, f"%X%{sys}.EF.SS": 1, f"%X%{sys}.LVR.POS": 1}, "expect": {f"%X%{sys}.EF.VFD.ALM": 0, f"%X%{sys}.EF.RUN": 1}, "note": "no VFD trouble, started and louver proven open -- fan should run"},
                {"force": {f"%X%{sys}.EF.VFD.FLT": 0, f"%X%{sys}.EF.SS": 1, f"%X%{sys}.LVR.POS": 0}, "expect": {f"%X%{sys}.EF.VFD.ALM": 0, f"%X%{sys}.EF.RUN": 0}, "note": "no VFD trouble, but louver not proven -- fan must not run"},
                {"force": {f"%X%{sys}.EF.VFD.FLT": 0, f"%X%{sys}.EF.SS": 0, f"%X%{sys}.LVR.POS": 1}, "expect": {f"%X%{sys}.EF.VFD.ALM": 0, f"%X%{sys}.EF.RUN": 0}, "note": "no VFD trouble, not started -- fan should be off regardless of louver"}
            ]
        }
    },
    {
        "title": "Exhaust Fan Interlocked with AHU Mode, with Manual Override",
        "difficulty": "easy",
        "multiLine": True,
        "systems": EF_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} AHU-Interlocked Exhaust Fan with Manual Override",
            "prompt": f"Exhaust Fan {sys} has no start/stop command of its own -- it's interlocked with AHU01 and simply follows whatever AHU01's supply fan is doing. If AHU01 Supply Fan Enable (\"%X%AHU01.SF.ENA\") is ON and Louver Position Feedback (\"%X%{sys}.LVR.POS\") is ON (proven open), turn ON the Exhaust Fan Run Output (\"%X%{sys}.EF.RUN\"). Otherwise, turn it OFF. Add a manual override for maintenance testing: if Manual Override (\"%X%{sys}.EF.MAN\") is ON, ignore the AHU interlock entirely and run the fan any time the louver is proven open (regardless of AHU01's supply fan state) -- this lets a tech bench-test the fan without needing to start the whole air handler. When Manual Override is OFF, use the normal AHU-interlocked logic described above.",
            "points": [
                {"name": "%X%AHU01.SF.ENA",
                    "desc": "AHU01 Supply Fan Enable Command (already exists on the interlocked AHU -- read-only here)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.LVR.POS",
                    "desc": "Exhaust Louver Position Feedback (proven open)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.EF.RUN",
                    "desc": "Exhaust Fan Run Output", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.EF.MAN",
                    "desc": "Manual Override Switch (bypasses the AHU interlock for maintenance testing)", "type": "DI", "kind": "physical"}
            ],
            "help": "Structure this as one outer IF on EF.MAN. In the THEN branch (manual mode), EF.RUN just follows LVR.POS alone. In the ELSE branch (normal mode), use the same AND pattern as a normal fan/louver interlock -- except the fan-enable half of that condition comes from a different unit entirely, AHU01.SF.ENA, not anything on this EF's own point tree. Reference \"%X%AHU01.SF.ENA\" literally; don't substitute this EF's own system prefix onto it.",
            "checks": [
                {"force": {f"%X%{sys}.EF.MAN": 1, "%X%AHU01.SF.ENA": 0, f"%X%{sys}.LVR.POS": 1}, "expect": {f"%X%{sys}.EF.RUN": 1}, "note": "manual override ON, louver proven open -- fan should run even though AHU01 isn't running"},
                {"force": {f"%X%{sys}.EF.MAN": 1, f"%X%{sys}.LVR.POS": 0}, "expect": {f"%X%{sys}.EF.RUN": 0}, "note": "manual override ON but louver not proven -- fan must not run"},
                {"force": {f"%X%{sys}.EF.MAN": 0, "%X%AHU01.SF.ENA": 1, f"%X%{sys}.LVR.POS": 1}, "expect": {f"%X%{sys}.EF.RUN": 1}, "note": "normal mode, AHU01 running and louver proven open -- EF should run"},
                {"force": {f"%X%{sys}.EF.MAN": 0, "%X%AHU01.SF.ENA": 0, f"%X%{sys}.LVR.POS": 1}, "expect": {f"%X%{sys}.EF.RUN": 0}, "note": "normal mode, AHU01 not running -- EF should be off despite the proven louver"}
            ]
        }
    },
    {
        "title": "Make-Up Air Unit Heating Valve Control with Filter Interlock",
        "difficulty": "easy",
        "multiLine": True,
        "systems": MAU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Heating Valve Control with Filter Interlock",
            "prompt": f"If Discharge Air Temp (\"%X%{sys}.DAT\") is less than Discharge Air Setpoint (\"%X%{sys}.DAT.SP\"), turn ON the Heating Valve (\"%X%{sys}.HTG.VLV\"). Otherwise, turn it OFF. This unit also has a filter status switch protecting the heating coil: if Filter OK (\"%X%{sys}.FLT.OK\") is OFF (filter badly clogged, airflow suspect), force the Heating Valve OFF regardless of temperature -- don't fire a heating coil into stagnant or blocked airflow. Only run the temperature-based valve logic above when Filter OK is ON.",
            "points": [
                {"name": f"%X%{sys}.DAT",
                    "desc": "Discharge Air Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.DAT.SP",
                    "desc": "Discharge Air Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.HTG.VLV",
                    "desc": "Heating Valve Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.FLT.OK",
                    "desc": "Filter OK Status (airflow/DP proof switch -- ON when filter condition is acceptable)", "type": "DI", "kind": "physical"}
            ],
            "help": "Make FLT.OK the outer condition: IF FLT.OK .EQ. 0 THEN HTG.VLV = 0, ELSE run the DAT .LT. DAT.SP comparison from before (with its own ELSE) to decide the valve. Don't just AND the two conditions together into a single IF -- nesting it this way keeps the temperature comparison completely separate from the filter interlock, matching how the prompt describes it as two distinct checks.",
            "checks": [
                {"force": {f"%X%{sys}.FLT.OK": 0, f"%X%{sys}.DAT": 60, f"%X%{sys}.DAT.SP": 68}, "expect": {f"%X%{sys}.HTG.VLV": 0}, "note": "filter not OK -- valve forced off even though DAT is well below setpoint"},
                {"force": {f"%X%{sys}.FLT.OK": 1, f"%X%{sys}.DAT": 60, f"%X%{sys}.DAT.SP": 68}, "expect": {f"%X%{sys}.HTG.VLV": 1}, "note": "filter OK and DAT below setpoint -- valve should be ON"},
                {"force": {f"%X%{sys}.FLT.OK": 1, f"%X%{sys}.DAT": 75, f"%X%{sys}.DAT.SP": 68}, "expect": {f"%X%{sys}.HTG.VLV": 0}, "note": "filter OK but DAT above setpoint -- valve should be OFF"}
            ]
        }
    },
    {
        "title": "Chiller Enable by Cooling Demand with Low Refrigerant Pressure Safety",
        "difficulty": "easy",
        "multiLine": True,
        "systems": CHILLER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Enable by Demand with Low Pressure Safety",
            "prompt": f"If Building Cooling Demand (\"%X%{sys}.DMD\") is ON, turn ON Chiller Plant Master Enable (\"%X%{sys}.ENA\"). Otherwise, turn it OFF. This chiller also has a low refrigerant pressure safety switch. After evaluating demand, add a safety check: if Low Refrigerant Pressure Fault (\"%X%{sys}.LOP.SW\") is ON, turn ON a Low Refrigerant Pressure Alarm (\"%X%{sys}.LOP.ALM\" -- virtual) and force Chiller Plant Master Enable OFF, overriding demand -- a refrigerant safety trip should never be overridden by cooling demand. Otherwise, clear the alarm and leave ENA at whatever the demand logic set it to.",
            "points": [
                {"name": f"%X%{sys}.DMD",
                    "desc": "Building Cooling Demand (virtual point, driven by a separate plant-load program)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.ENA",
                    "desc": "Chiller Plant Master Enable Command", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}.LOP.SW",
                    "desc": "Low Refrigerant Pressure Fault Contact", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.LOP.ALM",
                    "desc": "Low Refrigerant Pressure Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Write the demand mirror first (DMD onto ENA, with an ELSE), then write the safety check as a SEPARATE IF/THEN/ELSE placed after it: IF LOP.SW .EQ. 1 THEN (LOP.ALM=1, ENA=0) ELSE LOP.ALM=0. Placing the safety check second is what lets it override the ENA value the demand logic just wrote on the same cycle.",
            "checks": [
                {"force": {f"%X%{sys}.DMD": 1, f"%X%{sys}.LOP.SW": 0}, "expect": {f"%X%{sys}.ENA": 1, f"%X%{sys}.LOP.ALM": 0}, "note": "demand present, no fault -- plant enables normally"},
                {"force": {f"%X%{sys}.DMD": 1, f"%X%{sys}.LOP.SW": 1}, "expect": {f"%X%{sys}.ENA": 0, f"%X%{sys}.LOP.ALM": 1}, "note": "low refrigerant pressure fault -- alarm ON and plant forced off despite demand"},
                {"force": {f"%X%{sys}.DMD": 0, f"%X%{sys}.LOP.SW": 0}, "expect": {f"%X%{sys}.ENA": 0, f"%X%{sys}.LOP.ALM": 0}, "note": "no demand, no fault -- plant off, no alarm"}
            ]
        }
    },
    {
        "title": "Pump Enable by System Demand with Fail Alarm",
        "difficulty": "easy",
        "multiLine": True,
        "systems": PUMP_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Enable by Demand with Fail Alarm",
            "prompt": f"If System Demand (\"%X%{sys}.DMD\") is ON, turn ON Pump Enable (\"%X%{sys}.ENA\"). Otherwise, turn it OFF. This pump also has a running status feedback for fail detection. Separately from the demand logic, add: if Pump Enable is ON and Pump Running Status (\"%X%{sys}.PMP.S\") is OFF, turn ON a Pump Fail Alarm (\"%X%{sys}.PMP.ALM\" -- virtual). Otherwise turn it OFF.",
            "points": [
                {"name": f"%X%{sys}.DMD",
                    "desc": "System Demand (virtual point, driven by a separate plant-load program)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.ENA",
                    "desc": "Pump Enable Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.PMP.S",
                    "desc": "Pump Running Status (current switch or flow-proving feedback)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.PMP.ALM",
                    "desc": "Pump Fail Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Write the demand mirror first (DMD onto ENA, with an ELSE). Then write the fail check as its own separate IF/THEN/ELSE: ENA .EQ. 1 .AND. PMP.S .EQ. 0 sets the alarm, everything else clears it. Since ENA was just written a moment earlier in the same program, referencing it in the fail check picks up the value your own demand logic just set -- you don't need to re-derive it from DMD.",
            "checks": [
                {"force": {f"%X%{sys}.DMD": 1}, "expect": {f"%X%{sys}.ENA": 1}, "note": "demand present -- pump should enable"},
                {"force": {f"%X%{sys}.DMD": 0}, "expect": {f"%X%{sys}.ENA": 0}, "note": "no demand -- pump should be off"},
                {"force": {f"%X%{sys}.DMD": 1, f"%X%{sys}.PMP.S": 0}, "expect": {f"%X%{sys}.PMP.ALM": 1}, "note": "enabled but not proven running -- fail alarm should be ON"},
                {"force": {f"%X%{sys}.DMD": 1, f"%X%{sys}.PMP.S": 1}, "expect": {f"%X%{sys}.PMP.ALM": 0}, "note": "enabled and proven running -- no fail"},
                {"force": {f"%X%{sys}.DMD": 0, f"%X%{sys}.PMP.S": 0}, "expect": {f"%X%{sys}.PMP.ALM": 0}, "note": "not enabled -- no fail alarm regardless of status"}
            ]
        }
    },
    {
        "title": "Cooling Tower Basin Heater Freeze Protection with Fail Alarm",
        "difficulty": "easy",
        "multiLine": True,
        "systems": CT_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Basin Heater Control with Fail Alarm",
            "prompt": f"If Basin Water Temp (\"%X%{sys}.BASIN.T\") is less than Freeze Protection Setpoint (\"%X%{sys}.BASIN.T.SP\"), turn ON the Basin Heater (\"%X%{sys}.BASIN.HTR\"). Otherwise, turn it OFF. This basin heater also has a current-sensing switch for fail detection. Separately from the freeze-protection logic, add: if the Basin Heater is commanded ON and Heater Current Switch (\"%X%{sys}.HTR.CS\") is OFF (no current draw sensed), turn ON a Basin Heater Fail Alarm (\"%X%{sys}.HTR.ALM\" -- virtual). Otherwise turn it OFF.",
            "points": [
                {"name": f"%X%{sys}.BASIN.T",
                    "desc": "Basin Water Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.BASIN.T.SP",
                    "desc": "Freeze Protection Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.BASIN.HTR",
                    "desc": "Basin Heater Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.HTR.CS",
                    "desc": "Heater Current-Sensing Switch (proves current draw when the heater is commanded on)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.HTR.ALM",
                    "desc": "Basin Heater Fail Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Write the freeze-protection comparison first (BASIN.T .LT. BASIN.T.SP onto BASIN.HTR, with an ELSE). Then write the fail check as a separate IF: BASIN.HTR .EQ. 1 .AND. HTR.CS .EQ. 0 sets HTR.ALM, everything else clears it. Because BASIN.HTR was just written by the line above, checking it in the fail-alarm IF automatically reflects this cycle's freeze-protection decision.",
            "checks": [
                {"force": {f"%X%{sys}.BASIN.T": 35, f"%X%{sys}.BASIN.T.SP": 40}, "expect": {f"%X%{sys}.BASIN.HTR": 1}, "note": "basin water below freeze setpoint -- heater should be ON"},
                {"force": {f"%X%{sys}.BASIN.T": 55, f"%X%{sys}.BASIN.T.SP": 40}, "expect": {f"%X%{sys}.BASIN.HTR": 0}, "note": "basin water above freeze setpoint -- heater should be OFF"},
                {"force": {f"%X%{sys}.BASIN.T": 35, f"%X%{sys}.BASIN.T.SP": 40, f"%X%{sys}.HTR.CS": 0}, "expect": {f"%X%{sys}.HTR.ALM": 1}, "note": "heater commanded on but no current sensed -- fail alarm should be ON"},
                {"force": {f"%X%{sys}.BASIN.T": 35, f"%X%{sys}.BASIN.T.SP": 40, f"%X%{sys}.HTR.CS": 1}, "expect": {f"%X%{sys}.HTR.ALM": 0}, "note": "heater commanded on and current sensed -- no fail"},
                {"force": {f"%X%{sys}.BASIN.T": 55, f"%X%{sys}.BASIN.T.SP": 40, f"%X%{sys}.HTR.CS": 0}, "expect": {f"%X%{sys}.HTR.ALM": 0}, "note": "heater not commanded -- no fail alarm regardless of current switch"}
            ]
        }
    },
    {
        "title": "Boiler Enable by Firing Demand with Low Flow Interlock",
        "difficulty": "easy",
        "multiLine": True,
        "systems": BOILER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys}1 Enable by Demand with Flow Interlock",
            "prompt": f"This boiler requires proven water flow before it's allowed to fire. If Firing Demand (\"%X%{sys}1.DMD\") is ON AND Flow Switch (\"%X%{sys}1.FLOW.SW\") is ON (flow proven), turn ON Boiler 1 Enable (\"%X%{sys}1.ENA\"). Otherwise, turn it OFF. Separately, if Firing Demand is ON but Flow Switch is OFF (no flow proven, so the boiler can't be enabled), turn ON a Low Flow Alarm (\"%X%{sys}1.FLOW.ALM\" -- virtual). Otherwise, keep the alarm OFF.",
            "points": [
                {"name": f"%X%{sys}1.DMD",
                    "desc": "Firing Demand (virtual point, driven by hot water temperature reset logic elsewhere in the program)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}1.ENA",
                    "desc": "Boiler 1 Enable Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}1.FLOW.SW",
                    "desc": "Boiler Flow Switch (proves water flow through the boiler)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}1.FLOW.ALM",
                    "desc": "Low Flow Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "ENA needs BOTH DMD and FLOW.SW ON -- .AND. them together in one IF, with an ELSE that turns ENA OFF. FLOW.ALM is the opposite combination: DMD ON but FLOW.SW OFF, written as its own separate IF/THEN/ELSE. Don't try to derive the alarm from ENA's value -- write out the DMD/FLOW.SW comparison directly for the alarm too, since \"demand present but blocked\" and \"no demand at all\" are different situations that both leave ENA off.",
            "checks": [
                {"force": {f"%X%{sys}1.DMD": 1, f"%X%{sys}1.FLOW.SW": 1}, "expect": {f"%X%{sys}1.ENA": 1, f"%X%{sys}1.FLOW.ALM": 0}, "note": "demand present, flow proven -- boiler enables, no alarm"},
                {"force": {f"%X%{sys}1.DMD": 1, f"%X%{sys}1.FLOW.SW": 0}, "expect": {f"%X%{sys}1.ENA": 0, f"%X%{sys}1.FLOW.ALM": 1}, "note": "demand present but no flow -- boiler blocked, low flow alarm ON"},
                {"force": {f"%X%{sys}1.DMD": 0, f"%X%{sys}1.FLOW.SW": 0}, "expect": {f"%X%{sys}1.ENA": 0, f"%X%{sys}1.FLOW.ALM": 0}, "note": "no demand, no flow -- boiler off, no alarm (nothing is being blocked)"},
                {"force": {f"%X%{sys}1.DMD": 0, f"%X%{sys}1.FLOW.SW": 1}, "expect": {f"%X%{sys}1.ENA": 0, f"%X%{sys}1.FLOW.ALM": 0}, "note": "flow proven but no demand -- boiler stays off, no alarm"}
            ]
        }
    },
    {
        "title": "CRAC Compressor Enable with Independent High Humidity Alarm",
        "difficulty": "easy",
        "multiLine": True,
        "systems": CRAC_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys}201 Compressor Enable with Humidity Alarm",
            "prompt": f"If Room Temperature (\"%X%{sys}201.RMT\") is greater than Cooling Setpoint (\"%X%{sys}201.CLG.SP\"), turn ON Compressor Enable (\"%X%{sys}201.COMP.ENA\"). Otherwise, turn it OFF. This CRAC unit also monitors room humidity independently. Separately from the compressor logic, add: if Room Humidity (\"%X%{sys}201.RMH\") is greater than High Humidity Setpoint (\"%X%{sys}201.RMH.SP\"), turn ON a High Humidity Alarm (\"%X%{sys}201.RMH.ALM\" -- virtual). Otherwise turn it OFF. This is a standalone check -- it doesn't affect compressor operation.",
            "points": [
                {"name": f"%X%{sys}201.RMT",
                    "desc": "Room Temperature Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}201.CLG.SP",
                    "desc": "Cooling Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}201.COMP.ENA",
                    "desc": "Compressor Enable Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}201.RMH",
                    "desc": "Room Humidity Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}201.RMH.SP",
                    "desc": "High Humidity Alarm Setpoint (virtual point, typically around 65% RH)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}201.RMH.ALM",
                    "desc": "High Humidity Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Two completely unrelated IF/THEN/ELSE blocks in the same program. The compressor block is a single .GT. comparison of RMT to CLG.SP, same as the FCU cooling-valve scenario -- don't forget its ELSE, or the compressor never shuts back off. The humidity block is its own independent .GT. comparison of RMH to RMH.SP -- it doesn't read or write anything the compressor block touches.",
            "checks": [
                {"force": {f"%X%{sys}201.RMT": 78, f"%X%{sys}201.CLG.SP": 74}, "expect": {f"%X%{sys}201.COMP.ENA": 1}, "note": "RMT above setpoint -- compressor should enable"},
                {"force": {f"%X%{sys}201.RMT": 70, f"%X%{sys}201.CLG.SP": 74}, "expect": {f"%X%{sys}201.COMP.ENA": 0}, "note": "RMT below setpoint -- compressor should be off"},
                {"force": {f"%X%{sys}201.RMH": 70, f"%X%{sys}201.RMH.SP": 65}, "expect": {f"%X%{sys}201.RMH.ALM": 1}, "note": "humidity above setpoint -- alarm should be ON"},
                {"force": {f"%X%{sys}201.RMH": 55, f"%X%{sys}201.RMH.SP": 65}, "expect": {f"%X%{sys}201.RMH.ALM": 0}, "note": "humidity below setpoint -- alarm should be OFF"}
            ]
        }
    },
    {
        "title": "Air Compressor Cooling Water and Discharge Temp High Alarms",
        "difficulty": "easy",
        "multiLine": True,
        "systems": AC_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Cooling Water and Discharge Temp Alarms",
            "prompt": f"If Compressor Run Status (\"%X%{sys}.STAT\") is ON and Cooling Water Entering Temp (\"%X%{sys}.CHW.ET\") is greater than High Limit (\"%X%{sys}.CHW.ET.HI\"), turn ON a Cooling Water High Temp Alarm (\"%X%{sys}.CHW.ET.ALM\"). Otherwise, turn it OFF. This compressor also monitors discharge air temperature independently. Add a second safety check: if Compressor Run Status is ON and Discharge Air Temp (\"%X%{sys}.DAT\") is greater than Discharge High Limit (\"%X%{sys}.DAT.HI\"), turn ON a Discharge High Temp Alarm (\"%X%{sys}.DAT.ALM\" -- virtual). Otherwise turn it OFF. This is independent of the cooling water alarm above -- both can be true at the same time.",
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
                    "desc": "Compressor Discharge Air Temp Sensor (hot compressed air leaving the compressor head, not comfort-conditioned air)", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.DAT.HI",
                    "desc": "Compressor Discharge High Limit (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.DAT.ALM",
                    "desc": "Discharge High Temp Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Two separate IF blocks, both gated the same way -- AND the run-status check with the relevant high-temp comparison, since neither alarm means anything while the compressor is stopped. Write them as independent IF/THEN/ELSE blocks rather than trying to combine them; a real unit can trip the cooling-water alarm, the discharge-temp alarm, both, or neither, all at once.",
            "checks": [
                {"force": {f"%X%{sys}.STAT": 1, f"%X%{sys}.CHW.ET": 95, f"%X%{sys}.CHW.ET.HI": 85}, "expect": {f"%X%{sys}.CHW.ET.ALM": 1}, "note": "running and cooling water over limit -- alarm should be ON"},
                {"force": {f"%X%{sys}.STAT": 1, f"%X%{sys}.CHW.ET": 75, f"%X%{sys}.CHW.ET.HI": 85}, "expect": {f"%X%{sys}.CHW.ET.ALM": 0}, "note": "running and cooling water under limit -- no alarm"},
                {"force": {f"%X%{sys}.STAT": 0, f"%X%{sys}.CHW.ET": 95, f"%X%{sys}.CHW.ET.HI": 85}, "expect": {f"%X%{sys}.CHW.ET.ALM": 0}, "note": "compressor not running -- no alarm regardless of temp"},
                {"force": {f"%X%{sys}.STAT": 1, f"%X%{sys}.DAT": 260, f"%X%{sys}.DAT.HI": 250}, "expect": {f"%X%{sys}.DAT.ALM": 1}, "note": "running and discharge temp over limit -- alarm should be ON"},
                {"force": {f"%X%{sys}.STAT": 1, f"%X%{sys}.DAT": 200, f"%X%{sys}.DAT.HI": 250}, "expect": {f"%X%{sys}.DAT.ALM": 0}, "note": "running and discharge temp under limit -- no alarm"},
                {"force": {f"%X%{sys}.STAT": 0, f"%X%{sys}.DAT": 260, f"%X%{sys}.DAT.HI": 250}, "expect": {f"%X%{sys}.DAT.ALM": 0}, "note": "compressor not running -- no discharge alarm regardless of temp"}
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
            "title": f"{sys} Discharge Air Temp Alarm (Delayed + Critical)",
            "prompt": f"Real Siemens panel dumps often write a delayed alarm as a GOTO branch rather than one big IF/THEN/ELSE. Build it that way here: if Discharge Air Temp (\"%X%{sys}.DAT\") is greater than High Limit (\"%X%{sys}.DAT.HI\"), GOTO a higher-numbered alarm-timer block. Otherwise (still on the lines right after that check), reset the Alarm Delay Timer (\"%X%{sys}.DAT.ALM.TMR\") to 0, turn the High Temp Alarm (\"%X%{sys}.DAT.ALM\") OFF, then GOTO past the alarm-timer block entirely so it can't run while the temperature is back in range. In the alarm-timer block itself, use SAMPLE(1) IF (\"%X%{sys}.DAT\" .GT. \"%X%{sys}.DAT.HI\") THEN ... to accumulate the Alarm Delay Timer by 1 each second, then turn ON the High Temp Alarm once the timer exceeds 60 seconds -- leave the alarm alone otherwise in that block, since dropping back below the limit (handled by the earlier GOTO branch) is what turns it back off. This unit also needs an immediate trip for genuinely dangerous temperatures rather than waiting through the 60-second delay: BEFORE any of the logic above, if DAT is greater than a separate Critical High Limit (\"%X%{sys}.DAT.HI.CRIT\"), skip straight to a block that turns the High Temp Alarm ON immediately, bypassing the delay timer entirely. Otherwise, fall through into the normal delayed-alarm logic described above. DAT.HI, DAT.HI.CRIT, DAT.ALM.TMR, and DAT.ALM are all virtual points. Remember: GOTO only ever jumps to a HIGHER line number than the line it's on.",
            "points": [
                {"name": f"%X%{sys}.DAT",
                    "desc": "Discharge Air Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.DAT.HI",
                    "desc": "High Temp Alarm Limit (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.DAT.HI.CRIT",
                    "desc": "Critical High Temp Limit (virtual point -- bypasses the delay timer)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.DAT.ALM.TMR",
                    "desc": "Alarm Delay Timer Accumulator (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.DAT.ALM",
                    "desc": "High Discharge Temp Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "GOTO can only ever jump forward to a higher line number, never backward -- so lay your lines out top to bottom as: the critical-limit check first, then the normal DAT.HI check, then the reset-and-skip branch, then the timer block, and finally the immediate-alarm block LAST (so the critical check's GOTO can reach it by jumping clean over everything else). If your timer never seems to move, check that you're using SAMPLE(1) IF (...) THEN inside the timer block rather than a bare IF. Both the reset branch and the timer block need their own GOTO past the immediate-alarm block at the very end, otherwise execution would fall through into it and force the alarm ON even when nothing critical tripped.",
            "checks": [
                {"force": {f"%X%{sys}.DAT": 60, f"%X%{sys}.DAT.HI": 55, f"%X%{sys}.DAT.HI.CRIT": 110, f"%X%{sys}.DAT.ALM.TMR": 0}, "cycles": 65, "expect": {f"%X%{sys}.DAT.ALM": 1}, "note": "DAT stays over the high limit for more than 60 scans -- alarm should latch ON"},
                {"force": {f"%X%{sys}.DAT": 60, f"%X%{sys}.DAT.HI": 55, f"%X%{sys}.DAT.HI.CRIT": 110, f"%X%{sys}.DAT.ALM.TMR": 0, f"%X%{sys}.DAT.ALM": 0}, "cycles": 5, "expect": {f"%X%{sys}.DAT.ALM": 0}, "note": "DAT over the limit but not yet 60 seconds, and below the critical limit -- alarm should NOT have tripped yet"},
                {"force": {f"%X%{sys}.DAT": 50, f"%X%{sys}.DAT.HI": 55, f"%X%{sys}.DAT.HI.CRIT": 110}, "cycles": 10, "expect": {f"%X%{sys}.DAT.ALM": 0, f"%X%{sys}.DAT.ALM.TMR": 0}, "note": "DAT back under the limit -- timer resets to 0 and alarm stays OFF"},
                {"force": {f"%X%{sys}.DAT": 115, f"%X%{sys}.DAT.HI": 55, f"%X%{sys}.DAT.HI.CRIT": 110, f"%X%{sys}.DAT.ALM.TMR": 0, f"%X%{sys}.DAT.ALM": 0}, "cycles": 1, "expect": {f"%X%{sys}.DAT.ALM": 1}, "note": "DAT exceeds the critical high limit -- alarm should trip immediately, without waiting for the 60-second delay"}
            ]
        }
    },
    {
        "title": "FCU Occupied/Unoccupied Setpoint Strategy with Standby Override",
        "difficulty": "medium",
        "multiLine": True,
        "systems": FCU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Occ/Unocc Setpoint Strategy with Standby Override",
            "prompt": f"The {sys} zone needs different cooling setpoints depending on building occupancy. Building occupancy is tracked by a single, building-wide Building State point (\"%X%BLDG.OCC.STATE\") where 0 = Occupied and 1 = Unoccupied -- there is no per-zone occupancy point. When Occupied, the Active Cooling Setpoint (\"%X%{sys}.CLG.SP.ACT\") should be 72 degrees. When Unoccupied, it should be 78 degrees. This zone also supports an after-hours tenant override: if Standby Status (\"%X%{sys}.STBY.STAT\") is ON (a tenant has pressed the after-hours override button), use a Standby Cooling Setpoint of 75 degrees instead, regardless of the building occupancy state -- standby takes priority over both occupied and unoccupied. Only fall back to the occupied/unoccupied logic when Standby Status is OFF. Use the fixed setpoint values (72, 75, 78) directly in your logic.",
            "points": [
                {"name": "%X%BLDG.OCC.STATE",
                    "desc": "Building-wide Occupancy Mode (0 = Occupied, 1 = Unoccupied) -- a virtual point driven by a scheduling program, shared across all equipment", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.STBY.STAT",
                    "desc": "After-Hours Override Status (tenant-pressed override button)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.CLG.SP.ACT",
                    "desc": "Active Cooling Setpoint (virtual point -- calculated and written by your program logic)", "type": "AV", "kind": "virtual"}
            ],
            "help": "Nest the occupancy comparison inside the ELSE of an outer IF on STBY.STAT: IF STBY.STAT .EQ. 1 THEN CLG.SP.ACT = 75, ELSE (the BLDG.OCC.STATE .EQ. 0 / .EQ. 1 comparison from before, deciding between 72 and 78). BLDG.OCC.STATE is an analog virtual point holding 0 or 1, not a digital ON/OFF point -- compare it with .EQ. 0 / .EQ. 1 rather than treating it like a DI.",
            "checks": [
                {"force": {f"%X%{sys}.STBY.STAT": 1, "%X%BLDG.OCC.STATE": 1}, "expect": {f"%X%{sys}.CLG.SP.ACT": 75}, "note": "standby override active -- setpoint should be 75 even though the building is unoccupied"},
                {"force": {f"%X%{sys}.STBY.STAT": 0, "%X%BLDG.OCC.STATE": 0}, "expect": {f"%X%{sys}.CLG.SP.ACT": 72}, "note": "no standby, occupied (state 0) -- active cooling setpoint should be 72"},
                {"force": {f"%X%{sys}.STBY.STAT": 0, "%X%BLDG.OCC.STATE": 1}, "expect": {f"%X%{sys}.CLG.SP.ACT": 78}, "note": "no standby, unoccupied (state 1) -- active cooling setpoint should be 78"}
            ]
        }
    },
    {
        "title": "AHU Occupied/Unoccupied Mode Strategy",
        "difficulty": "medium",
        "multiLine": True,
        "systems": AHU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Occupied/Unoccupied Mode Strategy with Fire Alarm Override",
            "prompt": f"{sys} needs to change more than just its setpoint between occupied and unoccupied mode -- a full air handler typically shuts down overnight rather than just relaxing its setpoint like a single zone would. Building occupancy is tracked by a single, building-wide Building State point (\"%X%BLDG.OCC.STATE\") where 0 = Occupied and 1 = Unoccupied. When Occupied: turn ON Supply Fan Enable (\"%X%{sys}.SF.ENA\"), drive Active Cooling Setpoint (\"%X%{sys}.CLG.SP.ACT\") to 72 degrees, and open the Outside Air Damper (\"%X%{sys}.OAD.CMD\") for ventilation. When Unoccupied: turn OFF Supply Fan Enable, drive Active Cooling Setpoint to 78 degrees, and close the Outside Air Damper -- no ventilation air needed with nobody in the building. Use the fixed setpoint values directly in your logic. Separately, this AHU also has to respond to the building fire alarm system: after evaluating occupancy mode above, if Fire Alarm (\"%X%{sys}.FIRE.ALM\") is ON, force Supply Fan Enable OFF and close the Outside Air Damper immediately, overriding whatever the occupancy logic above just decided -- life safety always wins over normal scheduling. Leave Active Cooling Setpoint alone during a fire alarm; it doesn't matter with the fan off.",
            "points": [
                {"name": "%X%BLDG.OCC.STATE",
                    "desc": "Building-wide Occupancy Mode (0 = Occupied, 1 = Unoccupied) -- a virtual point driven by a scheduling program, shared across all equipment", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.SF.ENA",
                    "desc": "Supply Fan Enable Command (already exists -- your logic must write to this point)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.CLG.SP.ACT",
                    "desc": "Active Cooling Setpoint (virtual point -- calculated and written by your program logic)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.OAD.CMD",
                    "desc": "Outside Air Damper Open/Closed Command (already exists -- your logic must write to this point)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.FIRE.ALM",
                    "desc": "Building Fire Alarm Contact (from the fire alarm panel)", "type": "DI", "kind": "physical"}
            ],
            "help": "Write the occupancy IF/THEN/ELSE first exactly as before -- three writes grouped in each branch, keyed on BLDG.OCC.STATE. Then write the fire alarm check as a SEPARATE IF placed AFTER it, with no ELSE: IF FIRE.ALM .EQ. 1 THEN (SF.ENA=0, OAD.CMD=0). Placing it after and without an ELSE is what lets it override the occupancy block's SF.ENA/OAD.CMD values when there's a fire, while leaving them untouched (whatever occupancy already set) the rest of the time.",
            "checks": [
                {"force": {"%X%BLDG.OCC.STATE": 0, f"%X%{sys}.FIRE.ALM": 0}, "expect": {f"%X%{sys}.SF.ENA": 1, f"%X%{sys}.CLG.SP.ACT": 72, f"%X%{sys}.OAD.CMD": 1}, "note": "occupied, no fire alarm -- fan on, 72 setpoint, OA damper open"},
                {"force": {"%X%BLDG.OCC.STATE": 1, f"%X%{sys}.FIRE.ALM": 0}, "expect": {f"%X%{sys}.SF.ENA": 0, f"%X%{sys}.CLG.SP.ACT": 78, f"%X%{sys}.OAD.CMD": 0}, "note": "unoccupied, no fire alarm -- fan off, 78 setpoint, OA damper closed"},
                {"force": {"%X%BLDG.OCC.STATE": 0, f"%X%{sys}.FIRE.ALM": 1}, "expect": {f"%X%{sys}.SF.ENA": 0, f"%X%{sys}.OAD.CMD": 0}, "note": "fire alarm overrides even occupied mode -- fan off and OA damper closed"}
            ]
        }
    },
    {
        "title": "Warmup Mode Once-Per-Day Lockout",
        "difficulty": "medium",
        "multiLine": True,
        "systems": AHU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Warmup Mode Lockout with Freezestat Interlock",
            "prompt": f"{sys} is allowed to enter Warmup mode (preheating the space ahead of occupancy) at most once per day -- real sequences explicitly prevent re-triggering it repeatedly. If Room Temperature (\"%X%{sys}.RMT\") is below the Warmup Enable Setpoint (\"%X%{sys}.WU.SP\") and the Warmup Lockout flag (\"%X%{sys}.WU.LOCKOUT\") is OFF, turn ON Warmup Enable (\"%X%{sys}.WU.ENA\") and latch the Warmup Lockout flag ON so the mode can't retrigger again until reset -- once latched, Room Temperature dropping below setpoint again should NOT turn Warmup back on. Separately, a scheduling program pulses a Daily Reset point (\"%X%{sys}.WU.RST\") ON once every day; whenever that happens, turn Warmup Enable OFF and clear the Warmup Lockout flag back OFF, ready for the next cycle. This unit also has a freeze protection interlock: after both checks above, if Freezestat (\"%X%{sys}.FRZ.STAT\") is ON (mixed air near freezing), immediately force Warmup Enable OFF as a safety measure -- don't leave a heating call energized while the freezestat is tripped. This check should NOT touch the Warmup Lockout flag either way, since it's a temporary safety trip, not a normal daily cycle event.",
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
                    "desc": "Daily Reset Pulse (virtual point, driven by a separate scheduling program once per day)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.FRZ.STAT",
                    "desc": "Freezestat Contact (mixed air near freezing)", "type": "DI", "kind": "physical"}
            ],
            "help": "Three independent checks, not one IF/THEN/ELSE: the warmup-trigger condition (RMT below setpoint AND lockout still clear), the daily-reset condition (WU.RST pulsed), and the freezestat safety check, each in their own IF block. The lockout flag only ever gets set inside the trigger block, never cleared there -- once WU.LOCKOUT is ON, the AND condition is permanently false until the reset block clears it back OFF. Put the freezestat check LAST, with no ELSE, so it can override WU.ENA after the other two blocks without needing to touch WU.LOCKOUT.",
            "checks": [
                {"force": {f"%X%{sys}.RMT": 60, f"%X%{sys}.WU.SP": 65, f"%X%{sys}.WU.LOCKOUT": 0, f"%X%{sys}.WU.RST": 0, f"%X%{sys}.FRZ.STAT": 0}, "cycles": 3, "expect": {f"%X%{sys}.WU.ENA": 1, f"%X%{sys}.WU.LOCKOUT": 1}, "note": "RMT below setpoint, not yet locked out, no freeze trip -- warmup fires and stays latched across scans"},
                {"force": {f"%X%{sys}.RMT": 70, f"%X%{sys}.WU.SP": 65, f"%X%{sys}.WU.LOCKOUT": 0, f"%X%{sys}.WU.RST": 0, f"%X%{sys}.FRZ.STAT": 0}, "cycles": 1, "expect": {f"%X%{sys}.WU.LOCKOUT": 0}, "note": "RMT above setpoint -- warmup should not trigger or latch"},
                {"force": {f"%X%{sys}.RMT": 60, f"%X%{sys}.WU.SP": 65, f"%X%{sys}.WU.LOCKOUT": 1, f"%X%{sys}.WU.ENA": 1, f"%X%{sys}.WU.RST": 1, f"%X%{sys}.FRZ.STAT": 0}, "cycles": 1, "expect": {f"%X%{sys}.WU.ENA": 0, f"%X%{sys}.WU.LOCKOUT": 0}, "note": "daily reset pulse clears both the enable and the lockout flag"},
                {"force": {f"%X%{sys}.RMT": 60, f"%X%{sys}.WU.SP": 65, f"%X%{sys}.WU.LOCKOUT": 0, f"%X%{sys}.WU.RST": 0, f"%X%{sys}.FRZ.STAT": 1}, "cycles": 1, "expect": {f"%X%{sys}.WU.ENA": 0}, "note": "freezestat tripped -- warmup forced off even though the trigger condition is otherwise met"}
            ]
        }
    },
    {
        "title": "Continuously Operated Exhaust Fan with VOC Speed-Up",
        "difficulty": "medium",
        "multiLine": True,
        "systems": EF_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Continuous Operation VOC Speed-Up with Manual Override",
            "prompt": f"Exhaust Fan {sys} runs continuously at half speed once its louver is proven open -- per code, this fan is never allowed to shut off. If a VOC Detected alarm (\"%X%{sys}.VOC.DET\") trips ON, latch Full Speed Command (\"%X%{sys}.EF.FULL\") ON, driving the fan to full airflow -- it must stay latched even after the VOC alarm clears, since real code requires an operator to manually confirm the space is clear. It should only return to half speed when an operator pulses Manual Reset (\"%X%{sys}.EF.RST\") ON, which clears Full Speed Command back OFF. Also add a maintenance override: if Manual Full Speed (\"%X%{sys}.EF.MAN.FULL\") is ON, also drive Full Speed Command ON regardless of the VOC alarm or lockout state above -- lets a tech force full speed for testing without tripping the VOC sensor. This should combine with (not replace) the VOC-latch logic: Full Speed Command should be ON whenever EITHER the VOC latch is active OR the manual switch is on.",
            "points": [
                {"name": f"%X%{sys}.VOC.DET",
                    "desc": "VOC Detected Alarm (from a duct-mounted VOC sensor)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.EF.FULL",
                    "desc": "Full Speed Command (virtual point -- latches ON until manually reset)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.EF.RST",
                    "desc": "Manual Reset Pulse (operator-facing)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}.EF.MAN.FULL",
                    "desc": "Manual Full Speed Override Switch (maintenance testing)", "type": "DI", "kind": "physical"}
            ],
            "help": "Three IF blocks in order: the VOC-latch block sets EF.FULL ON when VOC.DET trips with NO ELSE (that's what makes it a latch); the reset block clears EF.FULL back OFF when EF.RST pulses; the manual-override block, placed LAST and also with NO ELSE, sets EF.FULL ON whenever EF.MAN.FULL is ON. Because none of the three blocks have an ELSE that forces EF.FULL OFF, any one of them can turn it ON without the others fighting it -- that's what makes the manual switch \"add\" full speed rather than replace the VOC logic.",
            "checks": [
                {"force": {f"%X%{sys}.VOC.DET": 1, f"%X%{sys}.EF.FULL": 0, f"%X%{sys}.EF.RST": 0, f"%X%{sys}.EF.MAN.FULL": 0}, "expect": {f"%X%{sys}.EF.FULL": 1}, "note": "VOC detected -- full speed latches ON"},
                {"force": {f"%X%{sys}.VOC.DET": 0, f"%X%{sys}.EF.FULL": 1, f"%X%{sys}.EF.RST": 0, f"%X%{sys}.EF.MAN.FULL": 0}, "cycles": 3, "expect": {f"%X%{sys}.EF.FULL": 1}, "note": "VOC alarm cleared but not manually reset -- full speed stays latched across scans"},
                {"force": {f"%X%{sys}.VOC.DET": 0, f"%X%{sys}.EF.FULL": 1, f"%X%{sys}.EF.RST": 1, f"%X%{sys}.EF.MAN.FULL": 0}, "expect": {f"%X%{sys}.EF.FULL": 0}, "note": "manual reset pulsed -- clears full speed back OFF"},
                {"force": {f"%X%{sys}.VOC.DET": 0, f"%X%{sys}.EF.FULL": 0, f"%X%{sys}.EF.RST": 0, f"%X%{sys}.EF.MAN.FULL": 1}, "expect": {f"%X%{sys}.EF.FULL": 1}, "note": "maintenance override switch ON -- full speed forced even with no VOC alarm"}
            ]
        }
    },
    {
        "title": "Mixed Air Damper Economizer Sequence",
        "difficulty": "medium",
        "multiLine": True,
        "systems": AHU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Economizer Damper Sequence with Freeze Interlock",
            "prompt": f"If Outside Air Temp (\"%X%{sys}.OAT\") is less than Return Air Temp (\"%X%{sys}.RAT\"), open the Outside Air Damper (\"%X%{sys}.OAD.CMD\") -- outside air is cooler than return air, so free cooling is available. If Outside Air Temp is greater than or equal to Return Air Temp, close the Outside Air Damper. This unit also has a freeze protection interlock that takes priority over the economizer logic: if Freezestat (\"%X%{sys}.FRZ.STAT\") is ON (mixed air temperature near freezing), force the Outside Air Damper CLOSED regardless of the OAT/RAT comparison above -- protecting the coil from freeze damage always wins over free cooling. Only run the OAT-vs-RAT economizer logic when Freezestat is OFF.",
            "points": [
                {"name": f"%X%{sys}.OAT",
                    "desc": "Outside Air Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.RAT",
                    "desc": "Return Air Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.OAD.CMD",
                    "desc": "Outside Air Damper Open/Closed Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.FRZ.STAT",
                    "desc": "Freezestat Contact (mixed air near freezing)", "type": "DI", "kind": "physical"}
            ],
            "help": "Make FRZ.STAT the outer condition: IF FRZ.STAT .EQ. 1 THEN OAD.CMD = 0, ELSE run the OAT .LT. RAT comparison (with its own ELSE) from before to decide the damper. Nesting it this way -- rather than ANDing freeze status into the economizer condition -- is what makes the freeze interlock override the economizer decision entirely instead of just being one more input to it.",
            "checks": [
                {"force": {f"%X%{sys}.FRZ.STAT": 1, f"%X%{sys}.OAT": 50, f"%X%{sys}.RAT": 75}, "expect": {f"%X%{sys}.OAD.CMD": 0}, "note": "freezestat tripped -- damper forced closed even though OAT is great for free cooling"},
                {"force": {f"%X%{sys}.FRZ.STAT": 0, f"%X%{sys}.OAT": 50, f"%X%{sys}.RAT": 75}, "expect": {f"%X%{sys}.OAD.CMD": 1}, "note": "no freeze trip, OAT cooler than RAT -- damper should open for free cooling"},
                {"force": {f"%X%{sys}.FRZ.STAT": 0, f"%X%{sys}.OAT": 85, f"%X%{sys}.RAT": 75}, "expect": {f"%X%{sys}.OAD.CMD": 0}, "note": "no freeze trip, OAT warmer than RAT -- damper should close"}
            ]
        }
    },
    {
        "title": "Duct Static Pressure High and Low Limit Alarms",
        "difficulty": "medium",
        "multiLine": True,
        "systems": AHU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Duct Static Pressure High/Low Alarms",
            "prompt": f"If Duct Static Pressure (\"%X%{sys}.DSP\") is greater than High Limit (\"%X%{sys}.DSP.HI\"), turn ON a High Static Pressure Alarm (\"%X%{sys}.DSP.ALM\") and command Supply Fan Speed (\"%X%{sys}.SF.SPD\") down to a minimum speed of 30. Otherwise, turn the alarm OFF. This unit also alarms on abnormally LOW static pressure while running, which usually means a fan belt or coupling failure. Separately, if Supply Fan Enable (\"%X%{sys}.SF.ENA\") is ON and Duct Static Pressure drops below a separate Low Limit (\"%X%{sys}.DSP.LO\"), turn ON a Low Static Pressure Alarm (\"%X%{sys}.DSP.LO.ALM\" -- virtual). Otherwise turn it OFF. DSP.HI, DSP.ALM, DSP.LO, and DSP.LO.ALM are all virtual points.",
            "points": [
                {"name": f"%X%{sys}.DSP",
                    "desc": "Duct Static Pressure Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.DSP.HI",
                    "desc": "High Static Pressure Limit (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.DSP.ALM",
                    "desc": "High Static Pressure Alarm (virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.SF.SPD",
                    "desc": "Supply Fan Speed Command (0-100%)", "type": "AO", "kind": "physical"},
                {"name": f"%X%{sys}.SF.ENA",
                    "desc": "Supply Fan Enable Command (already exists -- read-only here)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.DSP.LO",
                    "desc": "Low Static Pressure Limit (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.DSP.LO.ALM",
                    "desc": "Low Static Pressure Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Two independent IF/THEN/ELSE blocks. The high-limit block has two writes in its THEN (set the alarm AND clamp fan speed down to 30), but its ELSE only needs to clear the alarm -- leave SF.SPD under normal control there rather than writing a fixed value to it. The low-limit block is a separate AND condition (SF.ENA ON and DSP below DSP.LO) driving its own alarm, with no effect on fan speed at all.",
            "checks": [
                {"force": {f"%X%{sys}.DSP": 3.5, f"%X%{sys}.DSP.HI": 3.0}, "expect": {f"%X%{sys}.DSP.ALM": 1, f"%X%{sys}.SF.SPD": 30}, "note": "static pressure over the high limit -- alarm ON and fan speed clamped to 30"},
                {"force": {f"%X%{sys}.DSP": 2.0, f"%X%{sys}.DSP.HI": 3.0}, "expect": {f"%X%{sys}.DSP.ALM": 0}, "note": "static pressure under the high limit -- alarm should be OFF"},
                {"force": {f"%X%{sys}.SF.ENA": 1, f"%X%{sys}.DSP": 0.5, f"%X%{sys}.DSP.LO": 1.0}, "expect": {f"%X%{sys}.DSP.LO.ALM": 1}, "note": "fan running, static pressure below the low limit -- possible belt failure, low alarm should be ON"},
                {"force": {f"%X%{sys}.SF.ENA": 1, f"%X%{sys}.DSP": 1.5, f"%X%{sys}.DSP.LO": 1.0}, "expect": {f"%X%{sys}.DSP.LO.ALM": 0}, "note": "fan running, static pressure above the low limit -- no low alarm"},
                {"force": {f"%X%{sys}.SF.ENA": 0, f"%X%{sys}.DSP": 0.5, f"%X%{sys}.DSP.LO": 1.0}, "expect": {f"%X%{sys}.DSP.LO.ALM": 0}, "note": "fan not running -- no low alarm regardless of pressure"}
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
            "prompt": f"If Chiller Enable (\"%X%{sys}.ENA\") is ON and System Chilled Water Differential Pressure (\"%X%{sys}.CHW.DP\") is greater than Setpoint (\"%X%{sys}.CHW.DP.SP\"), increment a Stage Up Timer (\"%X%{sys}.STGUP\") by 1 second using SAMPLE(1). Once the Stage Up Timer exceeds 300 seconds, enable Next Stage (\"%X%{sys}.STG2.ENA\"). Otherwise, reset the Stage Up Timer to 0. If Chiller Enable is ON and Differential Pressure remains below Setpoint, turn ON a Differential Pressure Alarm (\"%X%{sys}.ALM\"). This plant also needs to stage back down when it has excess capacity: separately, if Stage 2 is currently enabled and Differential Pressure exceeds a higher Stage-Down Setpoint (\"%X%{sys}.CHW.DP.SP.HI\", intentionally set well above the stage-up setpoint to prevent short-cycling), increment a second timer, Stage Down Timer (\"%X%{sys}.STGDN\"), by 1 second using SAMPLE(1); otherwise reset it to 0. Once the Stage Down Timer exceeds 180 seconds, disable Next Stage again. CHW.DP.SP, CHW.DP.SP.HI, STGUP, STGDN, and ALM are all virtual points.",
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
                 "desc": "Differential Pressure Alarm (virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.CHW.DP.SP.HI",
                 "desc": "Stage-Down Setpoint (virtual point, set above the stage-up setpoint)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.STGDN",
                 "desc": "Stage Down Timer Accumulator (virtual point)", "type": "AV", "kind": "virtual"}
            ],
            "help": "Three separate pieces sharing one general shape: the stage-up timer (SAMPLE(1), accumulates while ENA is ON and DP is above CHW.DP.SP, resets to 0 otherwise, enables STG2.ENA past 300 seconds), the DP alarm (ENA ON and DP below CHW.DP.SP), and now a mirrored stage-down timer (SAMPLE(1), accumulates while STG2.ENA is ON and DP is above CHW.DP.SP.HI, resets to 0 otherwise, disables STG2.ENA back to 0 past 180 seconds). Keep each timer's reset condition broad -- it needs to fire whenever its own accumulate-condition isn't true, not just when ENA or STG2.ENA drops out.",
            "checks": [
                {"force": {f"%X%{sys}.ENA": 1, f"%X%{sys}.CHW.DP": 10, f"%X%{sys}.CHW.DP.SP": 5, f"%X%{sys}.CHW.DP.SP.HI": 999, f"%X%{sys}.STGUP": 0, f"%X%{sys}.STGDN": 0}, "cycles": 301, "expect": {f"%X%{sys}.STG2.ENA": 1}, "note": "pressure stays above setpoint with chiller enabled past 300 seconds -- next stage should enable"},
                {"force": {f"%X%{sys}.ENA": 1, f"%X%{sys}.CHW.DP": 10, f"%X%{sys}.CHW.DP.SP": 5, f"%X%{sys}.CHW.DP.SP.HI": 999, f"%X%{sys}.STGUP": 0, f"%X%{sys}.STGDN": 0, f"%X%{sys}.STG2.ENA": 0}, "cycles": 50, "expect": {f"%X%{sys}.STG2.ENA": 0}, "note": "timer hasn't reached 300 seconds yet -- next stage should not have enabled"},
                {"force": {f"%X%{sys}.ENA": 1, f"%X%{sys}.CHW.DP": 3, f"%X%{sys}.CHW.DP.SP": 5, f"%X%{sys}.CHW.DP.SP.HI": 999}, "expect": {f"%X%{sys}.ALM": 1, f"%X%{sys}.STGUP": 0}, "note": "DP stays below setpoint while enabled -- alarm should trip and the stage timer resets"},
                {"force": {f"%X%{sys}.ENA": 0, f"%X%{sys}.CHW.DP": 10, f"%X%{sys}.CHW.DP.SP": 5, f"%X%{sys}.CHW.DP.SP.HI": 999}, "cycles": 5, "expect": {f"%X%{sys}.STGUP": 0}, "note": "chiller not enabled -- stage timer stays reset regardless of pressure"},
                {"force": {f"%X%{sys}.STG2.ENA": 1, f"%X%{sys}.CHW.DP": 15, f"%X%{sys}.CHW.DP.SP": 5, f"%X%{sys}.CHW.DP.SP.HI": 12, f"%X%{sys}.STGDN": 0}, "cycles": 181, "expect": {f"%X%{sys}.STG2.ENA": 0}, "note": "excess pressure sustained past 180 seconds with stage 2 already enabled -- it should stage back down"},
                {"force": {f"%X%{sys}.STG2.ENA": 1, f"%X%{sys}.CHW.DP": 15, f"%X%{sys}.CHW.DP.SP": 5, f"%X%{sys}.CHW.DP.SP.HI": 12, f"%X%{sys}.STGDN": 0}, "cycles": 50, "expect": {f"%X%{sys}.STG2.ENA": 1}, "note": "stage-down timer hasn't reached 180 seconds yet -- stage 2 should still be enabled"}
            ]
        }
    },
    {
        "title": "Chilled Water Sequence Change & Flag Management",
        "difficulty": "medium",
        "multiLine": True,
        "systems": CHILLER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Sequence Change & Flag Reset with Fault Latch",
            "prompt": f"If a Sequence Alarm (\"%X%{sys}.SEQALM\") is equal to ON, latch the current Active Sequence Command (\"%X%{sys}.SEQCMD\") into local variable $LOC1 so its value at the moment of the fault is captured before anything changes it on the next scan. Also, whenever Sequence Alarm trips, latch a Sequence Fault Latch (\"%X%{sys}.SEQ.FLT.LATCH\" -- virtual) ON, and keep it latched even after the alarm itself clears -- it should only clear when an operator pulses Sequence Fault Reset (\"%X%{sys}.SEQ.FLT.RST\") ON. Separately, whenever Master Enable (\"%X%{sys}.ENA\") is OFF, reset the Stage Timer (\"%X%{sys}.STGUP\") to 0 so staging always starts from zero the next time the plant is enabled. SEQALM, SEQCMD, SEQ.FLT.LATCH, and STGUP are all virtual points.",
            "points": [
                {"name": f"%X%{sys}.SEQALM",
                    "desc": "Sequence Fault Alarm (virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.SEQCMD",
                    "desc": "Active Sequence Command (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.SEQ.FLT.LATCH",
                    "desc": "Sequence Fault Latch (virtual point -- latches ON when a sequence alarm has fired)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.SEQ.FLT.RST",
                    "desc": "Sequence Fault Reset Pulse (operator-facing)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.STGUP",
                    "desc": "Stage Timer Accumulator (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.ENA", "desc": "Chiller Master Enable", "type": "BV", "kind": "physical"},
                {"name": "$LOC1", "desc": "Local Variable", "type": "AV", "kind": "local"}
            ],
            "help": "Three unrelated IF blocks, not one combined condition. The SEQALM trigger block writes BOTH $LOC1 and SEQ.FLT.LATCH (no ELSE on either -- that's what makes the latch a latch). A separate block clears SEQ.FLT.LATCH back OFF only when SEQ.FLT.RST pulses. A third, still-unrelated block resets STGUP whenever ENA is OFF. None of the three needs an ELSE beyond what's described -- the prompt never says what should happen to $LOC1 or SEQ.FLT.LATCH when their trigger is false, or to STGUP when ENA is ON.",
            "checks": [
                {"force": {f"%X%{sys}.SEQALM": 1, f"%X%{sys}.SEQCMD": 42, "$LOC1": 0}, "expect": {"$LOC1": 42}, "note": "sequence alarm trips -- current active sequence command should be captured into $LOC1"},
                {"force": {f"%X%{sys}.ENA": 0, f"%X%{sys}.STGUP": 150}, "expect": {f"%X%{sys}.STGUP": 0}, "note": "master enable OFF -- stage timer resets to 0"},
                {"force": {f"%X%{sys}.ENA": 1, f"%X%{sys}.STGUP": 150}, "expect": {f"%X%{sys}.STGUP": 150}, "note": "master enable ON -- stage timer left alone (only resets while ENA is OFF)"},
                {"force": {f"%X%{sys}.SEQALM": 1, f"%X%{sys}.SEQ.FLT.LATCH": 0, f"%X%{sys}.SEQ.FLT.RST": 0}, "expect": {f"%X%{sys}.SEQ.FLT.LATCH": 1}, "note": "sequence alarm trips -- fault latch should set"},
                {"force": {f"%X%{sys}.SEQALM": 0, f"%X%{sys}.SEQ.FLT.LATCH": 1, f"%X%{sys}.SEQ.FLT.RST": 0}, "cycles": 3, "expect": {f"%X%{sys}.SEQ.FLT.LATCH": 1}, "note": "alarm cleared but no reset pulsed -- fault latch should stay ON across scans"},
                {"force": {f"%X%{sys}.SEQALM": 0, f"%X%{sys}.SEQ.FLT.LATCH": 1, f"%X%{sys}.SEQ.FLT.RST": 1}, "expect": {f"%X%{sys}.SEQ.FLT.LATCH": 0}, "note": "reset pulsed -- fault latch clears back OFF"}
            ]
        }
    },
    {
        "title": "FCU Multi-Speed Fan Control",
        "difficulty": "medium",
        "multiLine": True,
        "systems": FCU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Multi-Speed Fan Control (Heating and Cooling)",
            "prompt": f"This is a heating-and-cooling FCU, so multi-speed logic runs on both sides of the deadband, not just cooling. If Room Temperature (\"%X%{sys}.RMT\") exceeds Cooling Setpoint (\"%X%{sys}.RMT.SP\") by more than 3 degrees, run the fan on High Speed (\"%X%{sys}.FAN.HI\") and keep Low Speed (\"%X%{sys}.FAN.LO\") OFF. If it exceeds Cooling Setpoint by any smaller amount (up to 3 degrees), run on Low Speed and keep High Speed OFF. Symmetrically on the heating side: if Room Temperature falls below Heating Setpoint (\"%X%{sys}.RMT.HTG.SP\") by more than 3 degrees, run High Speed; if it falls below by any smaller amount (up to 3 degrees), run Low Speed. Anywhere else -- between the two setpoints, not in either 3-degree band -- turn both speed outputs OFF.",
            "points": [
                {"name": f"%X%{sys}.RMT",
                    "desc": "Room Temperature Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.RMT.SP",
                    "desc": "Cooling Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.RMT.HTG.SP",
                    "desc": "Heating Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.FAN.HI",
                    "desc": "Fan High Speed Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.FAN.LO",
                    "desc": "Fan Low Speed Command", "type": "DO", "kind": "physical"}
            ],
            "help": "A five-way split written as a chain of ELSE IFs, checked in this order: (RMT - RMT.SP) .GT. 3 -> high; (RMT - RMT.SP) .GT. 0 -> low; (RMT.HTG.SP - RMT) .GT. 3 -> high; (RMT.HTG.SP - RMT) .GT. 0 -> low; else both off. Check each side's bigger threshold before its smaller one (cooling-high before cooling-low, heating-high before heating-low) -- otherwise temperatures far outside either band would incorrectly fall into a low-speed branch instead of high.",
            "checks": [
                {"force": {f"%X%{sys}.RMT": 80, f"%X%{sys}.RMT.SP": 75, f"%X%{sys}.RMT.HTG.SP": 68}, "expect": {f"%X%{sys}.FAN.HI": 1, f"%X%{sys}.FAN.LO": 0}, "note": "5 degrees over cooling setpoint (more than 3) -- high speed"},
                {"force": {f"%X%{sys}.RMT": 77, f"%X%{sys}.RMT.SP": 75, f"%X%{sys}.RMT.HTG.SP": 68}, "expect": {f"%X%{sys}.FAN.LO": 1, f"%X%{sys}.FAN.HI": 0}, "note": "2 degrees over cooling setpoint (within 3) -- low speed"},
                {"force": {f"%X%{sys}.RMT": 73, f"%X%{sys}.RMT.SP": 75, f"%X%{sys}.RMT.HTG.SP": 68}, "expect": {f"%X%{sys}.FAN.HI": 0, f"%X%{sys}.FAN.LO": 0}, "note": "between both setpoints, outside both 3-degree bands -- both speeds off"},
                {"force": {f"%X%{sys}.RMT": 60, f"%X%{sys}.RMT.SP": 75, f"%X%{sys}.RMT.HTG.SP": 68}, "expect": {f"%X%{sys}.FAN.HI": 1, f"%X%{sys}.FAN.LO": 0}, "note": "8 degrees under heating setpoint (more than 3) -- high speed"},
                {"force": {f"%X%{sys}.RMT": 66, f"%X%{sys}.RMT.SP": 75, f"%X%{sys}.RMT.HTG.SP": 68}, "expect": {f"%X%{sys}.FAN.LO": 1, f"%X%{sys}.FAN.HI": 0}, "note": "2 degrees under heating setpoint (within 3) -- low speed"}
            ]
        }
    },
    {
        "title": "Cooling Tower Fan Enable by Condenser Water Temp",
        "difficulty": "medium",
        "multiLine": True,
        "systems": CT_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Fan Enable with Freeze Lockout",
            "prompt": f"If Condenser Water Supply Temp (\"%X%{sys}.CWS.T\") is greater than High Limit (\"%X%{sys}.CWS.T.HI\"), turn ON Tower Fan Enable (\"%X%{sys}.FAN.ENA\") and set Tower Fan Speed Output (\"%X%{sys}.FAN.SPD\") to 100. Otherwise, turn OFF Tower Fan Enable and set Tower Fan Speed Output to 0. This tower fan also has a freeze lockout: if Outside Air Temp (\"%X%{sys}.OAT\") is below 35 degrees, force Tower Fan Enable OFF and Tower Fan Speed Output to 0 regardless of condenser water temperature -- running the fan in freezing conditions risks icing the tower fill. Only run the condenser-water-temp logic above when OAT is 35 or above. CWS.T.HI is a virtual point.",
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
                    "desc": "Outside Air Temp Sensor", "type": "AI", "kind": "physical"}
            ],
            "help": "Make OAT the outer condition: IF OAT .LT. 35 THEN (FAN.ENA=0, FAN.SPD=0), ELSE run the CWS.T .GT. CWS.T.HI comparison from before (with its own ELSE, both writes moving together each way) to decide the fan. Nesting it this way is what makes the freeze lockout override the condenser-water logic entirely rather than just being one more input to it.",
            "checks": [
                {"force": {f"%X%{sys}.OAT": 20, f"%X%{sys}.CWS.T": 90, f"%X%{sys}.CWS.T.HI": 85}, "expect": {f"%X%{sys}.FAN.ENA": 0, f"%X%{sys}.FAN.SPD": 0}, "note": "freeze lockout -- fan forced off even though condenser water is well over the limit"},
                {"force": {f"%X%{sys}.OAT": 50, f"%X%{sys}.CWS.T": 90, f"%X%{sys}.CWS.T.HI": 85}, "expect": {f"%X%{sys}.FAN.ENA": 1, f"%X%{sys}.FAN.SPD": 100}, "note": "no freeze lockout, condenser water over the limit -- fan on at full speed"},
                {"force": {f"%X%{sys}.OAT": 50, f"%X%{sys}.CWS.T": 78, f"%X%{sys}.CWS.T.HI": 85}, "expect": {f"%X%{sys}.FAN.ENA": 0, f"%X%{sys}.FAN.SPD": 0}, "note": "no freeze lockout, condenser water under the limit -- fan off"}
            ]
        }
    },
    {
        "title": "Condenser Water High Temp Alarm with Time Delay (GOTO)",
        "difficulty": "medium",
        "multiLine": True,
        "systems": CT_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Condenser Water Alarm (Delayed, Silenceable)",
            "prompt": f"Write this the way a real Siemens panel dump does it: a GOTO branch instead of one big IF/THEN/ELSE. If Condenser Water Supply Temp (\"%X%{sys}.CWS.T\") is greater than High Limit (\"%X%{sys}.CWS.T.HI\"), GOTO a higher-numbered alarm-timer block. Otherwise (on the lines right after that check), reset an Alarm Delay Timer (\"%X%{sys}.CWS.T.ALM.TMR\") to 0, turn a High Condenser Water Temp Alarm (\"%X%{sys}.CWS.T.ALM\") OFF, then GOTO past the alarm-timer block entirely. In the alarm-timer block, use SAMPLE(1) IF (\"%X%{sys}.CWS.T\" .GT. \"%X%{sys}.CWS.T.HI\") THEN ... to accumulate the Alarm Delay Timer by 1 each second, then turn ON the alarm once the timer exceeds 45 seconds -- leave the alarm alone otherwise in that block, since dropping back below the limit (handled by the earlier GOTO branch) is what clears it. Finally, after both branches above rejoin (this line runs no matter which path was taken), add a maintenance silence: if Alarm Silence (\"%X%{sys}.CWS.T.ALM.SIL\") is ON, force the alarm OFF and reset the delay timer to 0, overriding whatever the GOTO logic above just decided. CWS.T.HI, CWS.T.ALM.TMR, CWS.T.ALM, and CWS.T.ALM.SIL are all virtual points. Remember: GOTO only ever jumps to a HIGHER line number than the line it's on.",
            "points": [
                {"name": f"%X%{sys}.CWS.T",
                    "desc": "Condenser Water Supply Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.CWS.T.HI",
                    "desc": "Condenser Water High Limit (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.CWS.T.ALM.TMR",
                    "desc": "Alarm Delay Timer Accumulator (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.CWS.T.ALM",
                    "desc": "High Condenser Water Temp Alarm (virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.CWS.T.ALM.SIL",
                    "desc": "Alarm Silence Switch (maintenance override)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Same GOTO/timer shape as the discharge-air-temp delayed alarm -- lay lines out top to bottom (main check, reset-and-skip branch, then the timer block). Use SAMPLE(1) IF (...) THEN inside the timer block so it accumulates once per scan, and give the reset branch its own GOTO landing right where the silence check begins, immediately after the timer block, so it doesn't fall through and immediately re-accumulate. The silence check itself is a plain IF placed after the timer block, with no ELSE needed -- both the reset path and the timer path naturally arrive at that line, so it always gets evaluated last regardless of which branch ran.",
            "checks": [
                {"force": {f"%X%{sys}.CWS.T": 95, f"%X%{sys}.CWS.T.HI": 85, f"%X%{sys}.CWS.T.ALM.TMR": 0, f"%X%{sys}.CWS.T.ALM.SIL": 0}, "cycles": 46, "expect": {f"%X%{sys}.CWS.T.ALM": 1}, "note": "over the limit for more than 45 scans, not silenced -- alarm latches ON"},
                {"force": {f"%X%{sys}.CWS.T": 95, f"%X%{sys}.CWS.T.HI": 85, f"%X%{sys}.CWS.T.ALM.TMR": 0, f"%X%{sys}.CWS.T.ALM": 0, f"%X%{sys}.CWS.T.ALM.SIL": 0}, "cycles": 5, "expect": {f"%X%{sys}.CWS.T.ALM": 0}, "note": "over the limit but under 45 seconds -- alarm hasn't tripped yet"},
                {"force": {f"%X%{sys}.CWS.T": 70, f"%X%{sys}.CWS.T.HI": 85, f"%X%{sys}.CWS.T.ALM.SIL": 0}, "cycles": 10, "expect": {f"%X%{sys}.CWS.T.ALM": 0, f"%X%{sys}.CWS.T.ALM.TMR": 0}, "note": "back under the limit -- timer resets and alarm stays OFF"},
                {"force": {f"%X%{sys}.CWS.T": 95, f"%X%{sys}.CWS.T.HI": 85, f"%X%{sys}.CWS.T.ALM.TMR": 0, f"%X%{sys}.CWS.T.ALM.SIL": 1}, "cycles": 46, "expect": {f"%X%{sys}.CWS.T.ALM": 0, f"%X%{sys}.CWS.T.ALM.TMR": 0}, "note": "alarm silenced -- forced off and timer held at 0 even though CWS.T has been over the limit for more than 45 scans"}
            ]
        }
    },
    {
        "title": "Air Compressor Header Pressure Load/Unload Control",
        "difficulty": "medium",
        "multiLine": True,
        "systems": AC_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Header Pressure Load/Unload with Safety Relief",
            "prompt": f"{sys} uses classic load/unload staging to hold discharge header pressure in a band rather than cycling the whole compressor on and off. If Header Pressure (\"%X%{sys}.HDR.PSI\") drops below the Load Setpoint (\"%X%{sys}.HDR.PSI.LO\"), turn ON the Load Solenoid (\"%X%{sys}.LOAD.SOL\") so the compressor starts pumping air into the header again. If Header Pressure rises above the Unload Setpoint (\"%X%{sys}.HDR.PSI.HI\"), turn OFF the Load Solenoid so the compressor unloads (runs unloaded/idles rather than continuing to add air). Leave the Load Solenoid in whatever state it's already in while Header Pressure is between the two setpoints -- don't write to it at all in that band. This compressor also has a hard safety limit well above the normal unload setpoint: after the deadband logic above, if Header Pressure exceeds Safety Limit (\"%X%{sys}.HDR.PSI.SAFE\"), force the Load Solenoid OFF (overriding the deadband logic) and turn ON a High Pressure Safety Alarm (\"%X%{sys}.HDR.PSI.ALM\" -- virtual). This alarm should self-clear (turn back OFF) as soon as pressure drops back at or below the safety limit -- no manual reset needed. HDR.PSI.LO, HDR.PSI.HI, and HDR.PSI.SAFE are all virtual points.",
            "points": [
                {"name": f"%X%{sys}.HDR.PSI",
                    "desc": "Discharge Header Pressure Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.HDR.PSI.LO",
                    "desc": "Load Setpoint -- Header Pressure below this loads the compressor (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.HDR.PSI.HI",
                    "desc": "Unload Setpoint -- Header Pressure above this unloads the compressor (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.LOAD.SOL",
                    "desc": "Load Solenoid Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.HDR.PSI.SAFE",
                    "desc": "High Pressure Safety Limit (virtual point, set well above HDR.PSI.HI)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.HDR.PSI.ALM",
                    "desc": "High Pressure Safety Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "The deadband is two separate IF blocks with no ELSE at all (one for the LO threshold turning the solenoid ON, one for the HI threshold turning it OFF) -- adding an ELSE to either would write to LOAD.SOL every scan even mid-band, defeating the deadband. The safety check is a THIRD, separate IF placed AFTER those two, WITH an ELSE this time: HDR.PSI .GT. HDR.PSI.SAFE forces LOAD.SOL off and sets HDR.PSI.ALM, the ELSE just clears the alarm (self-clearing, unlike the deadband writes). Placing it last is what lets it override whatever the deadband logic just decided.",
            "checks": [
                {"force": {f"%X%{sys}.HDR.PSI": 60, f"%X%{sys}.HDR.PSI.LO": 70, f"%X%{sys}.HDR.PSI.HI": 90, f"%X%{sys}.HDR.PSI.SAFE": 140, f"%X%{sys}.LOAD.SOL": 0}, "expect": {f"%X%{sys}.LOAD.SOL": 1}, "note": "pressure below load setpoint -- solenoid should turn ON"},
                {"force": {f"%X%{sys}.HDR.PSI": 100, f"%X%{sys}.HDR.PSI.LO": 70, f"%X%{sys}.HDR.PSI.HI": 90, f"%X%{sys}.HDR.PSI.SAFE": 140, f"%X%{sys}.LOAD.SOL": 1}, "expect": {f"%X%{sys}.LOAD.SOL": 0}, "note": "pressure above unload setpoint -- solenoid should turn OFF"},
                {"force": {f"%X%{sys}.HDR.PSI": 80, f"%X%{sys}.HDR.PSI.LO": 70, f"%X%{sys}.HDR.PSI.HI": 90, f"%X%{sys}.HDR.PSI.SAFE": 140, f"%X%{sys}.LOAD.SOL": 1}, "expect": {f"%X%{sys}.LOAD.SOL": 1}, "note": "pressure inside the deadband -- solenoid should be left alone (stays ON)"},
                {"force": {f"%X%{sys}.HDR.PSI": 80, f"%X%{sys}.HDR.PSI.LO": 70, f"%X%{sys}.HDR.PSI.HI": 90, f"%X%{sys}.HDR.PSI.SAFE": 140, f"%X%{sys}.LOAD.SOL": 0}, "expect": {f"%X%{sys}.LOAD.SOL": 0}, "note": "pressure inside the deadband -- solenoid should be left alone (stays OFF)"},
                {"force": {f"%X%{sys}.HDR.PSI": 150, f"%X%{sys}.HDR.PSI.LO": 70, f"%X%{sys}.HDR.PSI.HI": 90, f"%X%{sys}.HDR.PSI.SAFE": 140, f"%X%{sys}.LOAD.SOL": 1}, "expect": {f"%X%{sys}.LOAD.SOL": 0, f"%X%{sys}.HDR.PSI.ALM": 1}, "note": "pressure exceeds the safety limit -- solenoid forced off and safety alarm ON, overriding the deadband hold"},
                {"force": {f"%X%{sys}.HDR.PSI": 100, f"%X%{sys}.HDR.PSI.LO": 70, f"%X%{sys}.HDR.PSI.HI": 90, f"%X%{sys}.HDR.PSI.SAFE": 140}, "expect": {f"%X%{sys}.HDR.PSI.ALM": 0}, "note": "pressure below the safety limit -- safety alarm should be OFF (self-clearing)"}
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
            "title": f"{sys} Filter DP Monitoring Routine with Manual Reset",
            "prompt": f"Every 60 seconds (using SAMPLE), jump to Subroutine 5000 (GOSUB). In the subroutine, check if Filter Differential Pressure (\"%X%{sys}.FLT.DP\") is greater than High Threshold (\"%X%{sys}.FLT.DP.SP\"). If true, turn ON a Filter Alarm (\"%X%{sys}.FLT.ALM\") -- a virtual point -- store the current DP in local variable $LOC1, and return with RETURN. Add a manual reset OUTSIDE the subroutine, in the normal main-line program flow (this doesn't need the 60-second gate -- check it every scan): if Filter Alarm Reset (\"%X%{sys}.FLT.ALM.RST\") is ON, clear Filter Alarm back OFF, letting a tech acknowledge and clear the latch right after changing the filter, without waiting for the next subroutine call.",
            "points": [
                {"name": f"%X%{sys}.FLT.DP",
                    "desc": "Filter Differential Pressure", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.FLT.DP.SP",
                    "desc": "Filter DP High Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.FLT.ALM",
                    "desc": "Dirty Filter Alarm (virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.FLT.ALM.RST",
                    "desc": "Filter Alarm Reset Pulse (operator-facing)", "type": "BV", "kind": "virtual"},
                {"name": "$LOC1", "desc": "Local Scratchpad Variable", "type": "AV", "kind": "local"}
            ],
            "help": "SAMPLE(60) IF (...) THEN GOSUB 5000 fires the subroutine call once every 60 scans, not every scan -- don't confuse this with the SAMPLE(1) timer-accumulator pattern used in delayed-alarm scenarios elsewhere. Inside the subroutine, end with RETURN so execution resumes right after the GOSUB line. The reset check is a plain, ungated IF written in the normal main-line flow (not inside the subroutine, and not behind a SAMPLE) -- FLT.ALM.RST .EQ. 1 THEN FLT.ALM = 0, no ELSE. The prompt never says to clear FLT.ALM on a good reading inside the subroutine, so within the subroutine it's still a latch -- only the reset check ever turns it back off.",
            "checks": [
                {"force": {f"%X%{sys}.FLT.DP": 6, f"%X%{sys}.FLT.DP.SP": 5, f"%X%{sys}.FLT.ALM": 0, f"%X%{sys}.FLT.ALM.RST": 0}, "cycles": 61, "expect": {f"%X%{sys}.FLT.ALM": 1, "$LOC1": 6}, "note": "DP over setpoint when the 60-second subroutine call fires -- alarm sets and DP is captured into $LOC1"},
                {"force": {f"%X%{sys}.FLT.DP": 3, f"%X%{sys}.FLT.DP.SP": 5, f"%X%{sys}.FLT.ALM": 0, f"%X%{sys}.FLT.ALM.RST": 0}, "cycles": 61, "expect": {f"%X%{sys}.FLT.ALM": 0}, "note": "DP under setpoint when the subroutine runs -- alarm should stay OFF"},
                {"force": {f"%X%{sys}.FLT.ALM": 1, f"%X%{sys}.FLT.ALM.RST": 1, f"%X%{sys}.FLT.DP": 3, f"%X%{sys}.FLT.DP.SP": 5}, "cycles": 1, "expect": {f"%X%{sys}.FLT.ALM": 0}, "note": "reset pulsed -- alarm should clear immediately, without waiting for the next subroutine call"},
                {"force": {f"%X%{sys}.FLT.ALM": 1, f"%X%{sys}.FLT.ALM.RST": 0, f"%X%{sys}.FLT.DP": 3, f"%X%{sys}.FLT.DP.SP": 5}, "cycles": 1, "expect": {f"%X%{sys}.FLT.ALM": 1}, "note": "no reset pulsed -- alarm stays latched ON"}
            ]
        }
    },
    {
        "title": "Variable Speed Pump Differential Pressure PID Loop",
        "difficulty": "expert",
        "multiLine": False,
        "systems": PUMP_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Pump Speed DP Control with Manual Override",
            "prompt": f"When Pump Enable (\"%X%{sys}.ENA\") is ON, execute a LOOP statement to modulate Pump Speed Output (\"%X%{sys}.SPD\") (0-100%) to maintain System Differential Pressure (\"%X%{sys}.DP\") at Setpoint (\"%X%{sys}.DP.SP\"). If System Differential Pressure falls below half of Setpoint, turn ON a Low Pressure Fault Alarm (\"%X%{sys}.ALM\") -- a virtual point. This loop also needs a manual override for troubleshooting: if Manual Speed Override (\"%X%{sys}.SPD.MAN\") is ON, skip the LOOP entirely and drive Pump Speed Output directly to Manual Speed Value (\"%X%{sys}.SPD.MAN.VAL\") instead -- and skip the low-pressure fault check too, since the operator is intentionally taking manual control and doesn't want alarms firing while testing. Only run the LOOP-and-fault-check logic above when Manual Speed Override is OFF.",
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
                    "desc": "Manual Speed Override Switch (maintenance)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.SPD.MAN.VAL",
                    "desc": "Manual Speed Value (virtual point -- operator-entered target speed)", "type": "AV", "kind": "virtual"}
            ],
            "help": "A real LOOP call takes 12 positional arguments in this exact order: LOOP(type, pv, cv, sp, pg, ig, dg, st, bias, lo, hi, 0). Use: type=128 (reverse acting -- speed must increase as pressure falls below setpoint), pv=\"%X%{sys}.DP\", cv=\"%X%{sys}.SPD\", sp=\"%X%{sys}.DP.SP\", P Gain (pg)=5.0, I Gain (ig)=0.2, D Gain (dg)=0.0, sample time (st)=1, bias=50.0, low limit (lo)=0, high limit (hi)=100, final parameter 0. Wrap the whole ENA-gated LOOP block AND the fault-check IF inside the ELSE of an outer IF on SPD.MAN -- the THEN branch of that outer IF just sets SPD = SPD.MAN.VAL and touches nothing else (leaving ALM at whatever it already was, since the fault check is skipped entirely in manual mode).",
            "checks": [
                {"force": {f"%X%{sys}.SPD.MAN": 0, f"%X%{sys}.ENA": 1, f"%X%{sys}.DP": 20, f"%X%{sys}.DP.SP": 100}, "expect": {f"%X%{sys}.ALM": 1}, "note": "not in manual mode, DP has fallen below half of setpoint (20 < 50) -- low pressure fault should trip"},
                {"force": {f"%X%{sys}.SPD.MAN": 0, f"%X%{sys}.ENA": 1, f"%X%{sys}.DP": 80, f"%X%{sys}.DP.SP": 100}, "expect": {f"%X%{sys}.ALM": 0}, "note": "not in manual mode, DP is above half of setpoint -- no fault"},
                {"force": {f"%X%{sys}.SPD.MAN": 1, f"%X%{sys}.SPD.MAN.VAL": 45, f"%X%{sys}.DP": 20, f"%X%{sys}.DP.SP": 100, f"%X%{sys}.ALM": 0}, "expect": {f"%X%{sys}.SPD": 45, f"%X%{sys}.ALM": 0}, "note": "manual override active -- speed driven directly to the manual value, and the fault check is skipped even though DP is below half of setpoint"}
            ]
        }
    },
    {
        "title": "Air Compressor PID Bypass Valve Control",
        "difficulty": "expert",
        "multiLine": False,
        "systems": AC_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Water-Cooled Bypass Valve Control with Discharge Safety",
            "prompt": f"Air Compressor {sys} is water-cooled: cooling water flows through the compressor's heat exchanger, and a three-way bypass valve mixes cooled return water with warm bypassed water to hold a stable Compressor Water Entering Temp (\"%X%{sys}.CHW.ET\") at Setpoint (\"%X%{sys}.CHW.ET.SP\") -- too cold risks condensation damage inside the compressor, too warm hurts cooling performance. Whenever the compressor is running (Compressor Run Status \"%X%{sys}.STAT\" is ON), execute a LOOP statement to modulate the Bypass Valve Output (\"%X%{sys}.BYP.VLV\") (0-100%) to hold Entering Temp at Setpoint. When the compressor is not running, hold the Bypass Valve Output at 0. This compressor also has a high discharge temperature safety shutdown that takes priority over all of that: BEFORE the STAT-gated logic above, if Discharge Air Temp (\"%X%{sys}.DAT\") exceeds 260 degrees, skip the LOOP entirely, force Bypass Valve Output to 100 (full cooling water flow) and turn ON a Discharge Temp Safety Alarm (\"%X%{sys}.DAT.SAFE.ALM\" -- virtual). Only run the STAT-gated LOOP/hold logic when DAT is 260 or below; in that case also make sure the safety alarm is clear.",
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
                    "desc": "Compressor Discharge Air Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.DAT.SAFE.ALM",
                    "desc": "Discharge Temp Safety Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "A real LOOP call takes 12 positional arguments in this exact order: LOOP(type, pv, cv, sp, pg, ig, dg, st, bias, lo, hi, 0). Use: type=0 (direct acting), pv=\"%X%{sys}.CHW.ET\", cv=\"%X%{sys}.BYP.VLV\", sp=\"%X%{sys}.CHW.ET.SP\", P Gain (pg)=4.0, I Gain (ig)=0.3, D Gain (dg)=0.0, sample time (st)=1, bias=50.0, low limit (lo)=0, high limit (hi)=100, final parameter 0. Make DAT the OUTER condition: IF DAT .GT. 260 THEN (BYP.VLV=100, DAT.SAFE.ALM=1), ELSE (DAT.SAFE.ALM=0, then the STAT-gated LOOP-or-hold-at-0 logic from before). Nesting it this way is what lets the safety shutdown skip the LOOP call entirely rather than fighting it.",
            "checks": [
                {"force": {f"%X%{sys}.DAT": 200, f"%X%{sys}.STAT": 0, f"%X%{sys}.CHW.ET": 75, f"%X%{sys}.CHW.ET.SP": 65}, "expect": {f"%X%{sys}.BYP.VLV": 0, f"%X%{sys}.DAT.SAFE.ALM": 0}, "note": "compressor off, no safety trip -- bypass valve forced to 0 regardless of temperature"},
                {"force": {f"%X%{sys}.DAT": 200, f"%X%{sys}.STAT": 0, f"%X%{sys}.CHW.ET": 55, f"%X%{sys}.CHW.ET.SP": 65}, "expect": {f"%X%{sys}.BYP.VLV": 0, f"%X%{sys}.DAT.SAFE.ALM": 0}, "note": "compressor off -- bypass valve forced to 0 even if temp is below setpoint"},
                {"force": {f"%X%{sys}.DAT": 270, f"%X%{sys}.STAT": 1, f"%X%{sys}.CHW.ET": 75, f"%X%{sys}.CHW.ET.SP": 65, f"%X%{sys}.BYP.VLV": 0}, "expect": {f"%X%{sys}.BYP.VLV": 100, f"%X%{sys}.DAT.SAFE.ALM": 1}, "note": "discharge temp safety trip -- bypass valve forced wide open and safety alarm ON, overriding the PID loop entirely"}
            ]
        }
    },
    {
        "title": "Occupied/Unoccupied Fan Scheduling with Optimal Start",
        "difficulty": "medium",
        "multiLine": True,
        "systems": AHU_SYSTEMS + MAU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Occupied Fan Scheduling with Demand-Controlled Ventilation",
            "prompt": f"Supply Fan operation at {sys} should follow the building's occupancy mode, tracked by a single Building State point (\"%X%BLDG.OCC.STATE\") where 0 = Occupied and 1 = Unoccupied, with an allowance for optimal start: a separate scheduling program sets Optimal Start Command (\"%X%{sys}.OPT.START\") ON up to 2 hours before scheduled occupancy, telling this sequence to start the fan early to pre-condition the space. This unit also supports demand-controlled ventilation for after-hours events: if Space CO2 (\"%X%{sys}.CO2\") exceeds a High CO2 Setpoint (\"%X%{sys}.CO2.SP\"), that should ALSO call for the fan to run -- a crowded after-hours event needs ventilation even outside the normal schedule. Drive Supply Fan Enable (\"%X%{sys}.SF.ENA\") ON whenever ANY of the three conditions is true -- Building State is Occupied (0), Optimal Start Command is ON, or Space CO2 exceeds its setpoint -- and OFF only when none of them are true.",
            "points": [
                {"name": "%X%BLDG.OCC.STATE",
                    "desc": "Building-wide Occupancy Mode (0 = Occupied, 1 = Unoccupied) -- a virtual point driven by a scheduling program", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.OPT.START",
                    "desc": "Optimal Start Command (driven by a separate scheduling program)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}.SF.ENA",
                    "desc": "Supply Fan Enable Command (already exists -- your logic must write to this point)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.CO2",
                    "desc": "Space CO2 Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.CO2.SP",
                    "desc": "High CO2 Setpoint (virtual point)", "type": "AV", "kind": "virtual"}
            ],
            "help": "OR together all three conditions -- occupied OR optimal-start commanded OR CO2 above setpoint -- in one IF, since all three call for the same action (fan ON). BLDG.OCC.STATE is an analog point holding 0 or 1, so compare it with .EQ. 0 rather than treating it like a digital point.",
            "checks": [
                {"force": {"%X%BLDG.OCC.STATE": 0, f"%X%{sys}.OPT.START": 0, f"%X%{sys}.CO2": 400, f"%X%{sys}.CO2.SP": 1000}, "expect": {f"%X%{sys}.SF.ENA": 1}, "note": "occupied -- fan should be on"},
                {"force": {"%X%BLDG.OCC.STATE": 1, f"%X%{sys}.OPT.START": 1, f"%X%{sys}.CO2": 400, f"%X%{sys}.CO2.SP": 1000}, "expect": {f"%X%{sys}.SF.ENA": 1}, "note": "unoccupied but optimal start commanded -- fan should start early"},
                {"force": {"%X%BLDG.OCC.STATE": 1, f"%X%{sys}.OPT.START": 0, f"%X%{sys}.CO2": 400, f"%X%{sys}.CO2.SP": 1000}, "expect": {f"%X%{sys}.SF.ENA": 0}, "note": "unoccupied, no optimal start, CO2 fine -- fan should be off"},
                {"force": {"%X%BLDG.OCC.STATE": 1, f"%X%{sys}.OPT.START": 0, f"%X%{sys}.CO2": 1200, f"%X%{sys}.CO2.SP": 1000}, "expect": {f"%X%{sys}.SF.ENA": 1}, "note": "unoccupied and no optimal start, but CO2 above setpoint -- fan should run for ventilation"}
            ]
        }
    },
    {
        "title": "Hot Water Supply Temperature Reset",
        "difficulty": "expert",
        "multiLine": True,
        "systems": HWP_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Hot Water Supply Temperature Reset with Emergency Boost",
            "prompt": f"The Hot Water Supply Setpoint at {sys} should reset based on Outside Air Temp (\"%X%{sys}.OAT\") to save energy. Use these fixed boundaries directly in your logic: at 20 degrees OAT or colder, Hot Water Supply Setpoint (\"%X%{sys}.HW.SP\") should be 180 degrees. At 60 degrees OAT or warmer, it should be 140 degrees. Between 20 and 60 degrees, calculate a proportional (linear) reset between those two values based on where Outside Air Temp falls in that range. This plant also supports an emergency heat boost for extreme demand events: after computing the reset curve above, if Emergency Heat Demand (\"%X%{sys}.EHT.DMD\") is ON, override whatever the curve calculated and force Hot Water Supply Setpoint to 190 degrees instead.",
            "points": [
                {"name": f"%X%{sys}.OAT",
                    "desc": "Outside Air Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.HW.SP",
                    "desc": "Hot Water Supply Setpoint (already exists -- your logic must write to this point)", "type": "AV", "kind": "physical"},
                {"name": f"%X%{sys}.EHT.DMD",
                    "desc": "Emergency Heat Demand Contact (from a separate high-demand detection sequence)", "type": "DI", "kind": "physical"}
            ],
            "help": "This is a linear reset curve between two fixed points (20 deg OAT to 180 deg HW.SP, and 60 deg OAT to 140 deg HW.SP) -- the real PPCL tool for this is the TABLE() function (see the Learn PPCL tab's TABLE() section for the exact syntax), which interpolates between breakpoints for you. If you'd rather hand-roll the math: clamp OAT to the 20-60 range first, then the slope between the two anchor points is (140-180)/(60-20) = -1 degree of setpoint per degree of OAT above 20. Write the emergency boost as a SEPARATE IF placed AFTER the reset curve, with no ELSE: IF EHT.DMD .EQ. 1 THEN HW.SP = 190. Placing it after and without an ELSE is what lets it override the curve's result when active, while leaving the curve's calculation alone the rest of the time.",
            "checks": [
                {"force": {f"%X%{sys}.OAT": 10, f"%X%{sys}.EHT.DMD": 0}, "expect": {f"%X%{sys}.HW.SP": 180}, "note": "OAT colder than 20, no emergency demand -- HW setpoint pinned at 180"},
                {"force": {f"%X%{sys}.OAT": 70, f"%X%{sys}.EHT.DMD": 0}, "expect": {f"%X%{sys}.HW.SP": 140}, "note": "OAT warmer than 60, no emergency demand -- HW setpoint pinned at 140"},
                {"force": {f"%X%{sys}.OAT": 40, f"%X%{sys}.EHT.DMD": 0}, "expect": {f"%X%{sys}.HW.SP": 160}, "tolerance": 1, "note": "OAT halfway between 20 and 60, no emergency demand -- HW setpoint should be linearly interpolated to 160"},
                {"force": {f"%X%{sys}.OAT": 40, f"%X%{sys}.EHT.DMD": 1}, "expect": {f"%X%{sys}.HW.SP": 190}, "note": "emergency heat demand ON -- setpoint forced to 190 regardless of what the reset curve computed"}
            ]
        }
    },
    {
        "title": "Lead-Lag Pump Rotation on Failure",
        "difficulty": "medium",
        "multiLine": True,
        "systems": PUMP_PLANT_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{PLANT_LABELS.get(sys, sys)} Lead-Lag Rotation with Minimum Run-Time Protection",
            "prompt": f"Two pumps in the {sys} plant ({sys}.P1 and {sys}.P2) share standby duty: one is normally lead, the other lag (backup). If the lead pump develops a fault -- Pump 1 Fault (\"%X%{sys}.P1.FLT\") if {sys}.P1 is currently lead, or Pump 2 Fault (\"%X%{sys}.P2.FLT\") if {sys}.P2 is currently lead -- the other pump should automatically take over as lead. That state should persist (it should not automatically swap back once the fault clears, to avoid short-cycling equipment through a bouncing fault). Drive Pump 1 Enable (\"%X%{sys}.P1.ENA\") and Pump 2 Enable (\"%X%{sys}.P2.ENA\") accordingly, using a Lead-Lag Status point (\"%X%{sys}.LL.STAT\", 1 = Pump 1 lead, 2 = Pump 2 lead) to track which pump is currently lead. Add minimum run-time protection so a flaky fault can't bounce the rotation back and forth: track a Swap Lockout Timer (\"%X%{sys}.SWAP.TMR\"), accumulated by 1 second using SAMPLE(1) every scan, and reset it to 0 whenever a swap actually occurs. A swap should only be allowed to trigger when the Swap Lockout Timer is greater than 300 seconds -- so the currently-lead pump always gets at least 5 minutes of run time before another swap can happen, even if its fault contact is active the whole time.",
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
                    "desc": "Lead-Lag Status Code (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.SWAP.TMR",
                    "desc": "Swap Lockout Timer Accumulator (virtual point)", "type": "AV", "kind": "virtual"}
            ],
            "help": "Track lead/lag purely off LL.STAT, not off which fault is active -- when LL.STAT is 1 and P1.FLT trips (AND SWAP.TMR is over 300), flip LL.STAT to 2, swap the enables, and reset SWAP.TMR to 0; when LL.STAT is 2 and P2.FLT trips (AND SWAP.TMR is over 300), flip back to 1 and reset SWAP.TMR. The SAMPLE(1) accumulation itself should run unconditionally every scan, separate from the two swap IFs -- it's the swap IFs' own THEN blocks that reset it back to 0 when they fire. Neither swap condition should also check the other pump's fault, and there's still no condition anywhere that swaps LL.STAT back just because a fault clears on its own.",
            "checks": [
                {"force": {f"%X%{sys}.LL.STAT": 1, f"%X%{sys}.P1.FLT": 0, f"%X%{sys}.P2.FLT": 0, f"%X%{sys}.SWAP.TMR": 999}, "expect": {f"%X%{sys}.P1.ENA": 1, f"%X%{sys}.P2.ENA": 0, f"%X%{sys}.LL.STAT": 1}, "note": "P1 lead and healthy -- P1 runs, P2 stands by, status unchanged"},
                {"force": {f"%X%{sys}.LL.STAT": 1, f"%X%{sys}.P1.FLT": 1, f"%X%{sys}.P2.FLT": 0, f"%X%{sys}.SWAP.TMR": 0}, "cycles": 301, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}.P1.ENA": 0, f"%X%{sys}.P2.ENA": 1}, "note": "lead pump (P1) faults and stays faulted past the 300-second lockout -- P2 takes over as lead"},
                {"force": {f"%X%{sys}.LL.STAT": 1, f"%X%{sys}.P1.FLT": 1, f"%X%{sys}.P2.FLT": 0, f"%X%{sys}.SWAP.TMR": 0}, "cycles": 50, "expect": {f"%X%{sys}.LL.STAT": 1}, "note": "P1 faulted but the swap lockout timer hasn't reached 300 seconds yet -- swap should be blocked for now"},
                {"force": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}.P1.FLT": 0, f"%X%{sys}.P2.FLT": 0, f"%X%{sys}.P1.ENA": 0, f"%X%{sys}.P2.ENA": 1, f"%X%{sys}.SWAP.TMR": 999}, "cycles": 3, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}.P2.ENA": 1, f"%X%{sys}.P1.ENA": 0}, "note": "P2 already lead with no active faults -- stays lead, no automatic swap back to P1"}
            ]
        }
    },
    {
        "title": "Lead-Lag Chiller Failure Rotation",
        "difficulty": "medium",
        "multiLine": True,
        "systems": CHILLER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Lead-Lag Chiller Failure Rotation with Manual Override",
            "prompt": f"Two chillers, {sys}1 and {sys}2, share standby duty: one is normally lead, the other lag (backup). If the Lead Chiller Fault Alarm (\"%X%{sys}.LEAD.FLT\") is ON, the lag chiller should immediately take over as lead: turn ON Chiller 2 Enable (\"%X%{sys}2.ENA\"), turn OFF Chiller 1 Enable (\"%X%{sys}1.ENA\"), and set Lead-Lag Status (\"%X%{sys}.LL.STAT\") to 2, resetting a Fault-Clear Timer (\"%X%{sys}.FLT.CLR.TMR\") to 0. While Lead-Lag Status is 2 and the Fault Alarm is OFF, accumulate the Fault-Clear Timer by 1 second using SAMPLE(1); once it exceeds 300 seconds (5 minutes proven clear), restore normal operation -- turn ON Chiller 1 Enable, turn OFF Chiller 2 Enable, and set Lead-Lag Status back to 1. This way the rotation doesn't bounce back the instant the fault first clears. This plant also supports a manual lead-select override for planned maintenance: if Manual Lead Select (\"%X%{sys}.MAN.LEAD\") is set to 1, force Chiller 1 Enable ON, Chiller 2 Enable OFF, and Lead-Lag Status to 1, regardless of fault state or timers -- skip all the automatic failover/restore logic above entirely while in manual mode. If Manual Lead Select is set to 2, force the mirror image (Chiller 2 lead, Chiller 1 off). Only run the automatic failover-and-restore logic described above when Manual Lead Select is 0 (Auto).",
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
                    "desc": "Manual Lead Select (virtual point: 0 = Auto, 1 = force Chiller 1 lead, 2 = force Chiller 2 lead)", "type": "AV", "kind": "virtual"}
            ],
            "help": "Wrap everything in an outer dispatch on MAN.LEAD: MAN.LEAD .EQ. 1 forces chiller 1 lead outright, MAN.LEAD .EQ. 2 forces chiller 2 lead outright, and only the ELSE (MAN.LEAD .EQ. 0) runs the automatic logic -- the immediate takeover IF (LEAD.FLT trips -> swap to chiller 2, reset the timer), the SAMPLE(1) timer that only accumulates while LL.STAT is 2 AND LEAD.FLT is OFF, and the restore-to-normal IF once that timer exceeds 300.",
            "checks": [
                {"force": {f"%X%{sys}.MAN.LEAD": 0, f"%X%{sys}.LEAD.FLT": 1, f"%X%{sys}.LL.STAT": 1}, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}1.ENA": 0, f"%X%{sys}2.ENA": 1, f"%X%{sys}.FLT.CLR.TMR": 0}, "note": "auto mode, lead chiller faults -- chiller 2 takes over immediately and the fault-clear timer resets"},
                {"force": {f"%X%{sys}.MAN.LEAD": 0, f"%X%{sys}.LEAD.FLT": 0, f"%X%{sys}.LL.STAT": 2, f"%X%{sys}.FLT.CLR.TMR": 0}, "cycles": 100, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}2.ENA": 1}, "note": "auto mode, fault-clear timer running but hasn't reached 300 seconds yet -- stays on chiller 2"},
                {"force": {f"%X%{sys}.MAN.LEAD": 0, f"%X%{sys}.LEAD.FLT": 0, f"%X%{sys}.LL.STAT": 2, f"%X%{sys}.FLT.CLR.TMR": 0}, "cycles": 301, "expect": {f"%X%{sys}.LL.STAT": 1, f"%X%{sys}1.ENA": 1, f"%X%{sys}2.ENA": 0}, "note": "auto mode, fault stayed clear for over 300 seconds -- rotation restores back to chiller 1"},
                {"force": {f"%X%{sys}.MAN.LEAD": 1, f"%X%{sys}.LEAD.FLT": 1, f"%X%{sys}.LL.STAT": 2}, "expect": {f"%X%{sys}.LL.STAT": 1, f"%X%{sys}1.ENA": 1, f"%X%{sys}2.ENA": 0}, "note": "manual override forces chiller 1 lead even with an active fault and chiller 2 previously lead"},
                {"force": {f"%X%{sys}.MAN.LEAD": 2, f"%X%{sys}.LEAD.FLT": 0, f"%X%{sys}.LL.STAT": 1}, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}2.ENA": 1, f"%X%{sys}1.ENA": 0}, "note": "manual override forces chiller 2 lead"}
            ]
        }
    },
    {
        "title": "Boiler N+1 Standby Rotation on Failure",
        "difficulty": "medium",
        "multiLine": True,
        "systems": BOILER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Boiler Standby Rotation with Minimum Run-Time Protection",
            "prompt": f"Two boilers, {sys}1 and {sys}2, provide N+1 redundancy -- normally only one runs, the other sits as a spare. If the Lead Boiler Fault Alarm (\"%X%{sys}.LEAD.FLT\") is ON, the standby boiler should immediately take over: turn ON Boiler 2 Enable (\"%X%{sys}2.ENA\"), turn OFF Boiler 1 Enable (\"%X%{sys}1.ENA\"), and set Lead-Lag Status (\"%X%{sys}.LL.STAT\") to 2. That state should persist -- it should not automatically swap back once the fault clears, to avoid short-cycling equipment through a bouncing fault. Add minimum run-time protection on top of that: track a Run Timer (\"%X%{sys}.RUN.TMR\"), accumulated by 1 second using SAMPLE(1) every scan, and reset it to 0 whenever a swap actually occurs. A swap should only be allowed to trigger when the Run Timer is greater than 300 seconds, so the currently-running boiler always gets at least 5 minutes before another failover can happen, even if its fault contact stays active the whole time.",
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
                    "desc": "Run Timer Accumulator (virtual point)", "type": "AV", "kind": "virtual"}
            ],
            "help": "One IF, no ELSE: only write to LL.STAT/BLR1.ENA/BLR2.ENA when LEAD.FLT trips AND RUN.TMR is over 300. Leaving out the ELSE (rather than writing one that would swap back to boiler 1) is exactly what makes the rotation persist instead of bouncing back the instant the fault clears. The SAMPLE(1) accumulation of RUN.TMR runs unconditionally every scan, as its own separate line -- it's the swap IF's own THEN block that resets it to 0 when a swap fires.",
            "checks": [
                {"force": {f"%X%{sys}.LEAD.FLT": 1, f"%X%{sys}.LL.STAT": 1, f"%X%{sys}1.ENA": 1, f"%X%{sys}2.ENA": 0, f"%X%{sys}.RUN.TMR": 0}, "cycles": 301, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}1.ENA": 0, f"%X%{sys}2.ENA": 1}, "note": "lead boiler faults and stays faulted past the 300-second lockout -- boiler 2 takes over"},
                {"force": {f"%X%{sys}.LEAD.FLT": 1, f"%X%{sys}.LL.STAT": 1, f"%X%{sys}1.ENA": 1, f"%X%{sys}2.ENA": 0, f"%X%{sys}.RUN.TMR": 0}, "cycles": 50, "expect": {f"%X%{sys}.LL.STAT": 1}, "note": "lead boiler faulted but the run timer hasn't reached 300 seconds yet -- swap should be blocked for now"},
                {"force": {f"%X%{sys}.LEAD.FLT": 0, f"%X%{sys}.LL.STAT": 2, f"%X%{sys}1.ENA": 0, f"%X%{sys}2.ENA": 1, f"%X%{sys}.RUN.TMR": 999}, "cycles": 5, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}2.ENA": 1}, "note": "fault clears -- boiler 2 stays lead, no automatic swap back"}
            ]
        }
    },
    {
        "title": "CRAC Standby Unit Failover",
        "difficulty": "medium",
        "multiLine": True,
        "systems": CRAC_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys}201/202 CRAC Standby Failover with Manual Lead Select",
            "prompt": f"{sys}201 and {sys}202 form an N+1 redundant pair serving the same room: normally one runs as lead and the other sits in standby. If {sys}201's Alarm (\"%X%{sys}201.ALM\") is ON while {sys}201 is currently lead, or {sys}202's Alarm (\"%X%{sys}202.ALM\") is ON while {sys}202 is currently lead, the other unit should take over: enable the healthy unit, disable the alarmed one, and update Lead-Lag Status (\"%X%{sys}.LL.STAT\", 1 = {sys}201 lead, 2 = {sys}202 lead) accordingly. That state should persist -- don't automatically swap back once the alarm clears. This pair also supports a manual lead-select override for planned maintenance: if Manual Lead Select (\"%X%{sys}.MAN.LEAD\") is set to 1, force {sys}201 Enable ON, {sys}202 Enable OFF, and Lead-Lag Status to 1, regardless of alarm state -- skip the automatic failover logic entirely while in manual mode. If set to 2, force the mirror image ({sys}202 lead, {sys}201 off). Only run the automatic failover logic described above when Manual Lead Select is 0 (Auto).",
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
                    "desc": "Manual Lead Select (virtual point: 0 = Auto, 1 = force 201 lead, 2 = force 202 lead)", "type": "AV", "kind": "virtual"}
            ],
            "help": "Wrap everything in an outer dispatch on MAN.LEAD: MAN.LEAD .EQ. 1 forces 201 lead outright, MAN.LEAD .EQ. 2 forces 202 lead outright, and only the ELSE (MAN.LEAD .EQ. 0) runs the automatic logic. Inside that ELSE, each failover condition needs to check BOTH which unit is alarming AND which unit is currently lead -- 201's alarm should only trigger a swap if LL.STAT says 201 is lead; if 202 is already lead, 201's alarm doesn't matter since it's already the standby doing nothing.",
            "checks": [
                {"force": {f"%X%{sys}.MAN.LEAD": 0, f"%X%{sys}201.ALM": 1, f"%X%{sys}202.ALM": 0, f"%X%{sys}.LL.STAT": 1, f"%X%{sys}201.ENA": 1, f"%X%{sys}202.ENA": 0}, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}201.ENA": 0, f"%X%{sys}202.ENA": 1}, "note": "auto mode, 201 (currently lead) alarms -- 202 takes over"},
                {"force": {f"%X%{sys}.MAN.LEAD": 0, f"%X%{sys}201.ALM": 0, f"%X%{sys}202.ALM": 1, f"%X%{sys}.LL.STAT": 2, f"%X%{sys}201.ENA": 0, f"%X%{sys}202.ENA": 1}, "expect": {f"%X%{sys}.LL.STAT": 1, f"%X%{sys}201.ENA": 1, f"%X%{sys}202.ENA": 0}, "note": "auto mode, 202 (currently lead) alarms -- 201 takes over"},
                {"force": {f"%X%{sys}.MAN.LEAD": 0, f"%X%{sys}201.ALM": 1, f"%X%{sys}202.ALM": 0, f"%X%{sys}.LL.STAT": 2, f"%X%{sys}201.ENA": 0, f"%X%{sys}202.ENA": 1}, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}202.ENA": 1}, "note": "auto mode, 201 alarms but it isn't currently lead (202 is) -- no failover needed, nothing changes"},
                {"force": {f"%X%{sys}.MAN.LEAD": 1, f"%X%{sys}201.ALM": 1, f"%X%{sys}.LL.STAT": 2}, "expect": {f"%X%{sys}.LL.STAT": 1, f"%X%{sys}201.ENA": 1, f"%X%{sys}202.ENA": 0}, "note": "manual override forces 201 lead even though 201 itself is alarming and 202 was previously lead"},
                {"force": {f"%X%{sys}.MAN.LEAD": 2, f"%X%{sys}202.ALM": 0, f"%X%{sys}.LL.STAT": 1}, "expect": {f"%X%{sys}.LL.STAT": 2, f"%X%{sys}202.ENA": 1, f"%X%{sys}201.ENA": 0}, "note": "manual override forces 202 lead"}
            ]
        }
    },
    {
        "title": "Chiller Compressor Staging",
        "difficulty": "medium",
        "multiLine": True,
        "systems": CHILLER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Compressor Staging with Minimum On-Time",
            "prompt": f"If Chilled Water Supply Temp (\"%X%{sys}.CHW.ST\") stays above Setpoint (\"%X%{sys}.CHW.ST.SP\") continuously, accumulate a Load Timer (\"%X%{sys}.LOAD.TMR\") by 1 second using SAMPLE(1). Once the Load Timer exceeds 300 seconds, stage ON Compressor 2 (\"%X%{sys}.COMP2.ENA\"). If Chilled Water Supply Temp drops more than 2 degrees below Setpoint, stage OFF Compressor 2 and reset the Load Timer to 0. To prevent short-cycling the second compressor, it also needs a minimum on-time: track a Comp2 Run Timer (\"%X%{sys}.COMP2.RUN.TMR\"), accumulated by 1 second using SAMPLE(1) whenever Compressor 2 Enable is ON, and reset to 0 whenever it's OFF. The stage-off condition (more than 2 degrees below setpoint) should only be allowed to actually stage the compressor off once the Comp2 Run Timer exceeds 120 seconds -- if the temperature drops below the stage-off threshold before that, leave Compressor 2 running (and don't reset the Load Timer) until the minimum on-time is satisfied. CHW.ST.SP, LOAD.TMR, and COMP2.RUN.TMR are virtual points.",
            "points": [
                {"name": f"%X%{sys}.CHW.ST",
                    "desc": "Chilled Water Supply Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.CHW.ST.SP",
                    "desc": "Chilled Water Supply Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.LOAD.TMR",
                    "desc": "Compressor Load Timer Accumulator (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.COMP2.ENA",
                    "desc": "Compressor 2 Stage Enable", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.COMP2.RUN.TMR",
                    "desc": "Compressor 2 Run Timer Accumulator (virtual point)", "type": "AV", "kind": "virtual"}
            ],
            "help": "The staging timer only accumulates while CHW.ST stays above setpoint (SAMPLE(1) inside that IF); the stage-off condition is a separate IF checking for more than 2 degrees below setpoint AND COMP2.RUN.TMR .GT. 120. The COMP2.RUN.TMR accumulation is its own separate IF/ELSE, gated purely on COMP2.ENA (SAMPLE(1) accumulate when ON, reset to 0 when OFF) -- independent of both the stage-up and stage-off conditions. Between the stage-up and stage-off thresholds -- at or slightly below setpoint but not yet 2 degrees under -- neither staging condition should be true, so just leave the load timer and COMP2.ENA alone in that narrow band.",
            "checks": [
                {"force": {f"%X%{sys}.CHW.ST": 60, f"%X%{sys}.CHW.ST.SP": 52, f"%X%{sys}.LOAD.TMR": 0, f"%X%{sys}.COMP2.ENA": 0, f"%X%{sys}.COMP2.RUN.TMR": 0}, "cycles": 301, "expect": {f"%X%{sys}.COMP2.ENA": 1}, "note": "supply temp stays above setpoint for over 300 seconds -- compressor 2 stages on"},
                {"force": {f"%X%{sys}.CHW.ST": 60, f"%X%{sys}.CHW.ST.SP": 52, f"%X%{sys}.LOAD.TMR": 0, f"%X%{sys}.COMP2.ENA": 0, f"%X%{sys}.COMP2.RUN.TMR": 0}, "cycles": 50, "expect": {f"%X%{sys}.COMP2.ENA": 0}, "note": "load timer hasn't reached 300 seconds yet -- compressor 2 should not have staged on"},
                {"force": {f"%X%{sys}.CHW.ST": 48, f"%X%{sys}.CHW.ST.SP": 52, f"%X%{sys}.LOAD.TMR": 200, f"%X%{sys}.COMP2.ENA": 1, f"%X%{sys}.COMP2.RUN.TMR": 150}, "expect": {f"%X%{sys}.COMP2.ENA": 0, f"%X%{sys}.LOAD.TMR": 0}, "note": "supply temp drops more than 2 degrees below setpoint and compressor 2 has already run past its 120-second minimum -- it stages off and the load timer resets"},
                {"force": {f"%X%{sys}.CHW.ST": 48, f"%X%{sys}.CHW.ST.SP": 52, f"%X%{sys}.LOAD.TMR": 200, f"%X%{sys}.COMP2.ENA": 1, f"%X%{sys}.COMP2.RUN.TMR": 30}, "expect": {f"%X%{sys}.COMP2.ENA": 1}, "note": "supply temp is 4 degrees under setpoint, but compressor 2 hasn't reached its 120-second minimum on-time yet -- it should keep running"}
            ]
        }
    },
    {
        "title": "AHU Supply Fan VFD Speed Control by Duct Static Pressure",
        "difficulty": "expert",
        "multiLine": True,
        "systems": AHU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Supply Fan VFD Speed Control with Fault Clamp",
            "prompt": f"Using a LOOP statement, modulate Supply Fan Speed Output (\"%X%{sys}.SF.SPD\") (0-100%) to maintain Duct Static Pressure (\"%X%{sys}.DSP\") at Setpoint (\"%X%{sys}.DSP.SP\"). If Duct Static Pressure exceeds 150% of Setpoint, turn ON a High Static Fault (\"%X%{sys}.DSP.FLT\") -- a virtual point. When the High Static Fault trips, don't just alarm -- also clamp Supply Fan Speed Output down to a safe minimum of 30% (overriding whatever the LOOP is currently commanding), since continuing to trust the LOOP's output during a static pressure fault risks a duct blowout. Once DSP drops back to 150% of setpoint or below, let the LOOP resume normal control -- don't keep forcing the clamp.",
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
            "help": "A real LOOP call takes 12 positional arguments in this exact order: LOOP(type, pv, cv, sp, pg, ig, dg, st, bias, lo, hi, 0). Use: type=128 (reverse acting), pv=\"%X%{sys}.DSP\", cv=\"%X%{sys}.SF.SPD\", sp=\"%X%{sys}.DSP.SP\", P Gain (pg)=3.0, I Gain (ig)=0.15, D Gain (dg)=0.0, sample time (st)=1, bias=50.0, low limit (lo)=0, high limit (hi)=100, final parameter 0. After the LOOP call, write the fault check as an ordinary IF/THEN/ELSE comparing DSP against 1.5 times DSP.SP. Then add a THIRD, separate line placed after both: IF DSP.FLT .EQ. 1 THEN SF.SPD = 30, with no ELSE -- since SF.SPD was already set correctly by either the LOOP or nothing needs changing when there's no fault, this line only needs to act when the fault is active.",
            "checks": [
                {"force": {f"%X%{sys}.DSP": 8, f"%X%{sys}.DSP.SP": 5, f"%X%{sys}.SF.SPD": 90}, "expect": {f"%X%{sys}.DSP.FLT": 1, f"%X%{sys}.SF.SPD": 30}, "note": "DSP is 160% of setpoint (over 150%) -- high static fault trips and fan speed clamps down to 30, overriding whatever the LOOP was commanding"},
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
            "title": f"{sys} Fan VFD Sequence with Belt Loss Alarm",
            "prompt": f"If Condenser Water Supply Temp (\"%X%{sys}.CWS.T\") is greater than Setpoint (\"%X%{sys}.CWS.T.SP\"), increase Tower Fan Speed Output (\"%X%{sys}.FAN.SPD\") by 5 every 10 seconds using SAMPLE(10), up to a maximum of 100. If Condenser Water Supply Temp is more than 2 degrees below Setpoint, decrease Tower Fan Speed Output by 5 every 10 seconds, down to a minimum of 20. This fan also has a belt-loss alarm: if the fan is commanded to run (Fan Speed Output above 0) but Fan Current Switch (\"%X%{sys}.FAN.CUR\") stays OFF (no current draw sensed), accumulate a Belt Loss Timer (\"%X%{sys}.BELT.TMR\") by 1 second using SAMPLE(1); reset it to 0 whenever that condition isn't true. Once the Belt Loss Timer exceeds 30 seconds, turn ON a Belt Loss Alarm (\"%X%{sys}.FAN.BELT.ALM\" -- virtual); otherwise keep it OFF. CWS.T.SP, BELT.TMR, and FAN.BELT.ALM are all virtual points.",
            "points": [
                {"name": f"%X%{sys}.CWS.T",
                    "desc": "Condenser Water Supply Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.CWS.T.SP",
                    "desc": "Condenser Water Supply Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.FAN.SPD",
                    "desc": "Tower Fan Speed Output (0-100%)", "type": "AO", "kind": "physical"},
                {"name": f"%X%{sys}.FAN.CUR",
                    "desc": "Fan Current Switch (proves current draw when the fan is commanded to run)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.BELT.TMR",
                    "desc": "Belt Loss Timer Accumulator (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.FAN.BELT.ALM",
                    "desc": "Belt Loss Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "SAMPLE(10) IF (...) THEN gates the +/-5 step to fire once every 10 scans, not every scan -- that's what turns it into a gradual ramp instead of one big jump. Clamp the result (MIN()/MAX(), or an extra IF) so FAN.SPD never exceeds 100 on the way up or drops below 20 on the way down. Between the two trigger conditions -- within 2 degrees of setpoint -- neither ramp should fire. The belt-loss logic is a completely separate pair of IF blocks: a SAMPLE(1) timer gated on FAN.SPD .GT. 0 .AND. FAN.CUR .EQ. 0 (reset to 0 otherwise), and an ordinary IF/THEN/ELSE comparing that timer against 30 to drive the alarm.",
            "checks": [
                {"force": {f"%X%{sys}.CWS.T": 90, f"%X%{sys}.CWS.T.SP": 85, f"%X%{sys}.FAN.SPD": 50}, "cycles": 15, "expect": {f"%X%{sys}.FAN.SPD": 55}, "note": "CWS.T above setpoint -- after the 10-second SAMPLE fires once, fan speed steps up by 5"},
                {"force": {f"%X%{sys}.CWS.T": 90, f"%X%{sys}.CWS.T.SP": 85, f"%X%{sys}.FAN.SPD": 98}, "cycles": 15, "expect": {f"%X%{sys}.FAN.SPD": 100}, "note": "fan speed increase clamps at 100 even though the +5 step would overshoot"},
                {"force": {f"%X%{sys}.CWS.T": 80, f"%X%{sys}.CWS.T.SP": 85, f"%X%{sys}.FAN.SPD": 50}, "cycles": 15, "expect": {f"%X%{sys}.FAN.SPD": 45}, "note": "CWS.T more than 2 degrees below setpoint -- fan speed steps down by 5"},
                {"force": {f"%X%{sys}.CWS.T": 80, f"%X%{sys}.CWS.T.SP": 85, f"%X%{sys}.FAN.SPD": 22}, "cycles": 15, "expect": {f"%X%{sys}.FAN.SPD": 20}, "note": "fan speed decrease clamps at a minimum of 20"},
                {"force": {f"%X%{sys}.CWS.T": 84, f"%X%{sys}.CWS.T.SP": 85, f"%X%{sys}.FAN.SPD": 50}, "cycles": 15, "expect": {f"%X%{sys}.FAN.SPD": 50}, "note": "CWS.T within the 2-degree deadband -- fan speed stays unchanged"},
                {"force": {f"%X%{sys}.CWS.T": 84, f"%X%{sys}.CWS.T.SP": 85, f"%X%{sys}.FAN.SPD": 50, f"%X%{sys}.FAN.CUR": 0, f"%X%{sys}.BELT.TMR": 0}, "cycles": 31, "expect": {f"%X%{sys}.FAN.BELT.ALM": 1}, "note": "fan commanded to run for over 30 seconds with no current sensed -- belt loss alarm should trip"},
                {"force": {f"%X%{sys}.CWS.T": 84, f"%X%{sys}.CWS.T.SP": 85, f"%X%{sys}.FAN.SPD": 50, f"%X%{sys}.FAN.CUR": 1, f"%X%{sys}.BELT.TMR": 0}, "cycles": 31, "expect": {f"%X%{sys}.FAN.BELT.ALM": 0}, "note": "fan running with current proven -- no belt loss alarm"}
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
            "prompt": f"Real Siemens AHU programs open with a decision tree: check safety trips first, then dispatch to the block for the current operating mode via GOTO -- exactly the architecture used in production APOGEE sequences. Build it here. Remember GOTO can only jump forward to a higher line number, never backward, so lay the blocks out in this order. First, check safety: if Supply Duct Smoke Detector (\"%X%{sys}.SSD\") is ON OR Freeze Stat (\"%X%{sys}.FRZ.STAT\") is ON, GOTO line 02000 immediately, skipping mode logic entirely -- either safety condition alone overrides schedule. Otherwise, check Building Occupancy State (\"%X%BLDG.OCC.STATE\", 0 = Occupied, 1 = Unoccupied): if Occupied, GOTO line 01100; if Unoccupied, GOTO line 01200. At line 01100 (the Occupied block), turn ON Supply Fan Enable (\"%X%{sys}.SF.ENA\"), then GOTO line 03000 to end the scan. At line 01200 (the Unoccupied block), turn OFF Supply Fan Enable, then GOTO line 03000. At line 02000 (the Safety block), turn OFF Supply Fan Enable, then GOTO line 03000. Line 03000 should be a comment marking the end of the scan, and it needs to be the LAST line in your program -- every block above it ends with its own GOTO there so none of them can fall straight through into whatever comes next in the file.",
            "points": [
                {"name": f"%X%{sys}.SSD",
                    "desc": "Supply Duct Smoke Detector Trip", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.FRZ.STAT",
                    "desc": "Freeze Stat Trip (low-limit safety on the coil)", "type": "DI", "kind": "physical"},
                {"name": "%X%BLDG.OCC.STATE",
                    "desc": "Building-wide Occupancy Mode (0 = Occupied, 1 = Unoccupied) -- a virtual point driven by a scheduling program, shared across all equipment", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.SF.ENA",
                    "desc": "Supply Fan Enable Command (already exists -- your logic must write to this point)", "type": "DO", "kind": "physical"}
            ],
            "help": "This is a GOTO dispatch tree, not nested IF/THEN/ELSE -- the safety check comes first and jumps straight to the safety block (line 02000) with no further evaluation of occupancy at all when it's true. OR the two safety conditions together in that single check -- either SSD or FRZ.STAT alone is enough to dispatch there. Each of the three blocks (Occupied, Unoccupied, Safety) needs to end with its own GOTO 03000, otherwise a block would fall straight through into the next one's code and re-execute it.",
            "checks": [
                {"force": {f"%X%{sys}.SSD": 1, f"%X%{sys}.FRZ.STAT": 0, "%X%BLDG.OCC.STATE": 0}, "expect": {f"%X%{sys}.SF.ENA": 0}, "note": "smoke detected -- safety overrides occupancy, fan forced off"},
                {"force": {f"%X%{sys}.SSD": 0, f"%X%{sys}.FRZ.STAT": 1, "%X%BLDG.OCC.STATE": 0}, "expect": {f"%X%{sys}.SF.ENA": 0}, "note": "freeze stat tripped -- also routes to the safety block, fan forced off"},
                {"force": {f"%X%{sys}.SSD": 0, f"%X%{sys}.FRZ.STAT": 0, "%X%BLDG.OCC.STATE": 0}, "expect": {f"%X%{sys}.SF.ENA": 1}, "note": "no safety trips, occupied -- fan on"},
                {"force": {f"%X%{sys}.SSD": 0, f"%X%{sys}.FRZ.STAT": 0, "%X%BLDG.OCC.STATE": 1}, "expect": {f"%X%{sys}.SF.ENA": 0}, "note": "no safety trips, unoccupied -- fan off"}
            ]
        }
    },
    {
        "title": "AHU Multi-Mode Dispatch: Warmup / Cooldown / Occupied / Unoccupied (GOTO Decision Tree)",
        "difficulty": "expert",
        "multiLine": True,
        "systems": AHU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Warmup/Cooldown/Occupied/Unoccupied Dispatch with Smoke Purge",
            "prompt": f"Real VAV AHU sequences run a four-way mode dispatch every scan: Unoccupied, Warmup, Cooldown, and normal Occupied, each its own block reached by GOTO. Remember GOTO can only jump forward to a higher line number, never backward, so lay the blocks out in that order. This unit also supports a smoke purge mode used during a fire event, which takes priority over everything else including occupancy: BEFORE any of the checks below, if Smoke Purge Command (\"%X%{sys}.SMOKE.PURGE\") is ON, GOTO line 01500 (a new Purge block, placed AFTER the Unoccupied block, i.e. a higher line number than every other block), skipping every other check entirely. Otherwise, check occupancy: if Building Occupancy State (\"%X%BLDG.OCC.STATE\") equals 1 (Unoccupied), GOTO line 01300 immediately, skipping the mode checks below -- an unoccupied building never enters Warmup or Cooldown. Otherwise (still Occupied), check Room Temperature (\"%X%{sys}.RMT\") against the two mode setpoints: if RMT is less than the Warmup Setpoint (\"%X%{sys}.WU.SP\"), GOTO line 01100 (Warmup); if RMT is greater than the Cooldown Setpoint (\"%X%{sys}.CD.SP\"), GOTO line 01200 (Cooldown). If neither is true, fall through into the Normal Occupied block that follows immediately. At line 01030 (Normal Occupied), turn ON Supply Fan Enable (\"%X%{sys}.SF.ENA\"), turn OFF Warmup Enable (\"%X%{sys}.WU.ENA\"), turn OFF Cooldown Enable (\"%X%{sys}.CD.ENA\"), then GOTO line 01400 to end the scan. At line 01100 (Warmup), turn ON Supply Fan Enable, turn ON Warmup Enable, turn OFF Cooldown Enable, then GOTO line 01400. At line 01200 (Cooldown), turn ON Supply Fan Enable, turn OFF Warmup Enable, turn ON Cooldown Enable, then GOTO line 01400. At line 01300 (Unoccupied), turn OFF Supply Fan Enable, turn OFF Warmup Enable, turn OFF Cooldown Enable, then GOTO line 01400. At line 01500 (Purge), turn ON Supply Fan Enable, turn OFF Warmup Enable, turn OFF Cooldown Enable, then GOTO line 01400 -- the same end-of-scan line every other block uses. Line 01400 should be a comment marking the end of the scan, and it needs to be the LAST line in your program -- every block above it (including Purge, even though it's numbered higher than 01400's neighbors) ends with its own GOTO there so none of them can fall straight through into whatever comes next in the file.",
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
                    "desc": "Cooldown Enable Command (already exists -- your logic must write to this point)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.SMOKE.PURGE",
                    "desc": "Smoke Purge Command (from the fire alarm panel)", "type": "DI", "kind": "physical"}
            ],
            "help": "Check smoke purge FIRST, before even occupancy, and dispatch it straight to its own block (line 01500) with no further evaluation of anything else. Then check occupancy and dispatch Unoccupied straight to its own block, skipping the warmup/cooldown checks entirely. Only once you know the building is occupied do you compare RMT to the two mode setpoints -- and if neither Warmup nor Cooldown applies, just fall through into the Normal Occupied block right below. Every block, including Normal Occupied and Purge, still needs its own GOTO 01400 at the end -- note that Purge is numbered HIGHER than 01400 even though it behaves like any other block, which is fine, since GOTO only cares that a jump target is forward of the line doing the jumping, not that blocks appear in some particular relative order to each other.",
            "checks": [
                {"force": {f"%X%{sys}.SMOKE.PURGE": 0, "%X%BLDG.OCC.STATE": 1, f"%X%{sys}.RMT": 70, f"%X%{sys}.WU.SP": 65, f"%X%{sys}.CD.SP": 78}, "expect": {f"%X%{sys}.SF.ENA": 0, f"%X%{sys}.WU.ENA": 0, f"%X%{sys}.CD.ENA": 0}, "note": "unoccupied, no purge -- fan and both modes off regardless of RMT"},
                {"force": {f"%X%{sys}.SMOKE.PURGE": 0, "%X%BLDG.OCC.STATE": 0, f"%X%{sys}.RMT": 60, f"%X%{sys}.WU.SP": 65, f"%X%{sys}.CD.SP": 78}, "expect": {f"%X%{sys}.SF.ENA": 1, f"%X%{sys}.WU.ENA": 1, f"%X%{sys}.CD.ENA": 0}, "note": "occupied, no purge, RMT below warmup setpoint -- warmup mode"},
                {"force": {f"%X%{sys}.SMOKE.PURGE": 0, "%X%BLDG.OCC.STATE": 0, f"%X%{sys}.RMT": 82, f"%X%{sys}.WU.SP": 65, f"%X%{sys}.CD.SP": 78}, "expect": {f"%X%{sys}.SF.ENA": 1, f"%X%{sys}.WU.ENA": 0, f"%X%{sys}.CD.ENA": 1}, "note": "occupied, no purge, RMT above cooldown setpoint -- cooldown mode"},
                {"force": {f"%X%{sys}.SMOKE.PURGE": 0, "%X%BLDG.OCC.STATE": 0, f"%X%{sys}.RMT": 72, f"%X%{sys}.WU.SP": 65, f"%X%{sys}.CD.SP": 78}, "expect": {f"%X%{sys}.SF.ENA": 1, f"%X%{sys}.WU.ENA": 0, f"%X%{sys}.CD.ENA": 0}, "note": "occupied, no purge, RMT between the two setpoints -- normal occupied mode"},
                {"force": {f"%X%{sys}.SMOKE.PURGE": 1, "%X%BLDG.OCC.STATE": 1, f"%X%{sys}.RMT": 60, f"%X%{sys}.WU.SP": 65, f"%X%{sys}.CD.SP": 78}, "expect": {f"%X%{sys}.SF.ENA": 1, f"%X%{sys}.WU.ENA": 0, f"%X%{sys}.CD.ENA": 0}, "note": "smoke purge commanded -- overrides even unoccupied mode, fan forced on to purge smoke"}
            ]
        }
    },
    {
        "title": "FCU Freeze Protection & Occupancy Dispatch (GOTO Decision Tree)",
        "difficulty": "expert",
        "multiLine": True,
        "systems": FCU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Freeze Protection & Occupancy Dispatch with Condensate Priority",
            "prompt": f"Real FCU programs open with a decision tree just like AHUs do: check safety first, then dispatch to the current mode via GOTO. Build it here. Remember GOTO can only jump forward to a higher line number, never backward, so lay the blocks out in this order. This unit also has a condensate overflow switch that takes priority over even freeze protection: BEFORE the freeze-stat check, if Condensate Overflow Switch (\"%X%{sys}.COND.SW\") is ON, GOTO line 02500 (a new Condensate block, placed AFTER the Freeze Protection block, i.e. a higher line number than 02000), skipping every other check entirely -- a full drain pan is an immediate water-damage risk that outranks even freeze protection. Otherwise, check safety: if the Coil Freeze Stat (\"%X%{sys}.FRZ.STAT\") is ON (tripped -- a low-limit safety on the water coil), GOTO line 02000 immediately, skipping mode logic entirely -- freeze protection overrides occupancy. Otherwise, check Building Occupancy State (\"%X%BLDG.OCC.STATE\", 0 = Occupied, 1 = Unoccupied): if Occupied, GOTO line 01100; if Unoccupied, GOTO line 01200. At line 01100 (the Occupied block), turn ON the Fan (\"%X%{sys}.FAN.ENA\"), then GOTO line 03000 to end the scan. At line 01200 (the Unoccupied block), turn OFF the Fan, then GOTO line 03000. At line 02000 (the Freeze Protection block), turn OFF the Fan and turn ON the Heating Valve (\"%X%{sys}.HTG.VLV\") to its fail-safe fully-open position to keep hot water moving through the coil, then GOTO line 03000. At line 02500 (the Condensate block), turn OFF the Fan and turn OFF the Cooling Valve (\"%X%{sys}.CLG.VLV\"), then GOTO line 03000. Line 03000 should be a comment marking the end of the scan, and it needs to be the LAST line in your program -- every block above it (including Condensate, even though it's numbered higher than 02000) ends with its own GOTO there so none of them can fall straight through into whatever comes next in the file.",
            "points": [
                {"name": f"%X%{sys}.FRZ.STAT",
                    "desc": "Coil Freeze Stat Trip (low-limit safety, opens below roughly 38 degrees)", "type": "DI", "kind": "physical"},
                {"name": "%X%BLDG.OCC.STATE",
                    "desc": "Building-wide Occupancy Mode (0 = Occupied, 1 = Unoccupied) -- a virtual point driven by a scheduling program, shared across all equipment", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.FAN.ENA",
                    "desc": "Fan Enable Command (already exists -- your logic must write to this point)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.HTG.VLV",
                    "desc": "Heating Valve Command (already exists -- your logic must write to this point)", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.COND.SW",
                    "desc": "Condensate Overflow Switch (closes when the drain pan is full)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.CLG.VLV",
                    "desc": "Cooling Valve Command (already exists -- your logic must write to this point)", "type": "DO", "kind": "physical"}
            ],
            "help": "Same dispatch shape as the AHU safety interlock, but now with condensate checked FIRST, before even the freeze stat. Both the Freeze Protection and Condensate blocks turn the fan off, but they touch different valves -- only Freeze Protection writes to HTG.VLV, and only Condensate writes to CLG.VLV. The Occupied/Unoccupied blocks touch neither valve, so leave both alone there. Note that the Condensate block is numbered higher than the Freeze Protection block (02500 vs 02000) purely because that's where there was room to add it -- GOTO only requires that a jump target be forward of the line doing the jumping, not that blocks be laid out in the same order they're checked in.",
            "checks": [
                {"force": {f"%X%{sys}.COND.SW": 0, f"%X%{sys}.FRZ.STAT": 1, "%X%BLDG.OCC.STATE": 0}, "expect": {f"%X%{sys}.FAN.ENA": 0, f"%X%{sys}.HTG.VLV": 1}, "note": "freeze stat tripped, no condensate issue -- overrides occupancy, fan off and heating valve driven open"},
                {"force": {f"%X%{sys}.COND.SW": 0, f"%X%{sys}.FRZ.STAT": 0, "%X%BLDG.OCC.STATE": 0}, "expect": {f"%X%{sys}.FAN.ENA": 1}, "note": "no freeze trip, no condensate issue, occupied -- fan on"},
                {"force": {f"%X%{sys}.COND.SW": 0, f"%X%{sys}.FRZ.STAT": 0, "%X%BLDG.OCC.STATE": 1}, "expect": {f"%X%{sys}.FAN.ENA": 0}, "note": "no freeze trip, no condensate issue, unoccupied -- fan off"},
                {"force": {f"%X%{sys}.COND.SW": 1, f"%X%{sys}.FRZ.STAT": 1, "%X%BLDG.OCC.STATE": 0}, "expect": {f"%X%{sys}.FAN.ENA": 0, f"%X%{sys}.CLG.VLV": 0}, "note": "condensate overflow present even alongside an active freeze trip -- condensate takes priority, fan off and cooling valve closed"}
            ]
        }
    },
    {
        "title": "Exhaust Fan VFD Speed Control by Building Static Pressure",
        "difficulty": "expert",
        "multiLine": True,
        "systems": EF_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Building Static Pressure Control with Manual Override",
            "prompt": f"{sys}'s speed needs to modulate continuously to control Building Static Pressure rather than simply cycling on and off. Whenever the Exhaust Fan Start Command (\"%X%{sys}.EF.SS\") is ON, execute a LOOP statement to modulate Exhaust Fan Speed Output (\"%X%{sys}.EF.SPD\") (0-100%) to maintain Building Static Pressure (\"%X%{sys}.BSP\") at Setpoint (\"%X%{sys}.BSP.SP\"). When Exhaust Fan Start Command is OFF, hold Exhaust Fan Speed Output at 0. Add a manual speed override for troubleshooting: if Manual Speed Override (\"%X%{sys}.EF.SPD.MAN\") is ON, skip the LOOP entirely and drive Exhaust Fan Speed Output directly to Manual Speed Value (\"%X%{sys}.EF.SPD.MAN.VAL\") instead, regardless of the start command. Only run the EF.SS-gated LOOP/hold logic above when Manual Speed Override is OFF.",
            "points": [
                {"name": f"%X%{sys}.EF.SS",
                    "desc": "Exhaust Fan Start Command", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.BSP",
                    "desc": "Building Static Pressure Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.BSP.SP",
                    "desc": "Building Static Pressure Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.EF.SPD",
                    "desc": "Exhaust Fan Speed Output (0-100%)", "type": "AO", "kind": "physical"},
                {"name": f"%X%{sys}.EF.SPD.MAN",
                    "desc": "Manual Speed Override Switch (maintenance)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.EF.SPD.MAN.VAL",
                    "desc": "Manual Speed Value (virtual point -- operator-entered target speed)", "type": "AV", "kind": "virtual"}
            ],
            "help": "A real LOOP call takes 12 positional arguments in this exact order: LOOP(type, pv, cv, sp, pg, ig, dg, st, bias, lo, hi, 0). Use: type=0 (direct acting), pv=\"%X%{sys}.BSP\", cv=\"%X%{sys}.EF.SPD\", sp=\"%X%{sys}.BSP.SP\", P Gain (pg)=4.0, I Gain (ig)=0.25, D Gain (dg)=0.0, sample time (st)=1, bias=50.0, low limit (lo)=20, high limit (hi)=100, final parameter 0. Wrap the whole EF.SS-gated LOOP/hold block inside the ELSE of an outer IF on EF.SPD.MAN -- the THEN branch of that outer IF just sets EF.SPD = EF.SPD.MAN.VAL and skips the LOOP entirely.",
            "checks": [
                {"force": {f"%X%{sys}.EF.SPD.MAN": 0, f"%X%{sys}.EF.SS": 0, f"%X%{sys}.BSP": 0.1, f"%X%{sys}.BSP.SP": 0.05}, "expect": {f"%X%{sys}.EF.SPD": 0}, "note": "not in manual mode, fan not started -- speed output forced to 0"},
                {"force": {f"%X%{sys}.EF.SPD.MAN": 0, f"%X%{sys}.EF.SS": 0, f"%X%{sys}.BSP": 0.02, f"%X%{sys}.BSP.SP": 0.05}, "expect": {f"%X%{sys}.EF.SPD": 0}, "note": "not in manual mode, fan not started -- speed output forced to 0 regardless of pressure"},
                {"force": {f"%X%{sys}.EF.SPD.MAN": 1, f"%X%{sys}.EF.SPD.MAN.VAL": 65, f"%X%{sys}.EF.SS": 0}, "expect": {f"%X%{sys}.EF.SPD": 65}, "note": "manual override active -- speed driven directly to the manual value even though the fan start command is off"}
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
            "prompt": f"Chiller safeties always override normal staging -- real chiller plant programs check safety first via a GOTO decision tree, exactly like an AHU program does. Build it here. Remember GOTO can only jump forward to a higher line number, never backward, so lay the blocks out in this order. First, check safety: if Low Refrigerant Pressure Trip (\"%X%{sys}.LOP.TRIP\") is ON OR High Condenser Pressure Trip (\"%X%{sys}.HCP.TRIP\") is ON OR Chilled Water Flow Switch (\"%X%{sys}.CHW.FLOW\") is OFF (no flow proven), GOTO line 02000 immediately, skipping normal staging entirely -- any one of the three alone shuts the whole plant down. Otherwise, check Chiller Plant Master Enable (\"%X%{sys}.ENA\"): if ON, GOTO line 01100 (Normal Run); if OFF, GOTO line 01200 (Standby). At line 01100 (Normal Run), turn ON Chiller 1 Enable (\"%X%{sys}1.ENA\"), then GOTO line 03000 to end the scan. At line 01200 (Standby), turn OFF Chiller 1 Enable, then GOTO line 03000. At line 02000 (the Safety Shutdown block), turn OFF Chiller 1 Enable and turn ON a Safety Shutdown Alarm (\"%X%{sys}.SAFE.ALM\") -- a virtual point -- then GOTO line 03000. Line 03000 should be a comment marking the end of the scan, and it needs to be the LAST line in your program -- every block above it ends with its own GOTO there so none of them can fall straight through into whatever comes next in the file.",
            "points": [
                {"name": f"%X%{sys}.LOP.TRIP",
                    "desc": "Low Refrigerant Pressure Safety Trip", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.HCP.TRIP",
                    "desc": "High Condenser Pressure Safety Trip", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.CHW.FLOW",
                    "desc": "Chilled Water Flow Switch (proves flow through the evaporator)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.ENA",
                    "desc": "Chiller Plant Master Enable (already exists)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}1.ENA",
                    "desc": "Chiller 1 Enable", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}.SAFE.ALM",
                    "desc": "Safety Shutdown Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "OR all three safety conditions together in the first check -- either pressure trip being ON, or CHW.FLOW being OFF, should jump to the shutdown block. Only that block writes to SAFE.ALM; the Normal Run and Standby blocks don't touch it, so don't add logic there that clears it.",
            "checks": [
                {"force": {f"%X%{sys}.LOP.TRIP": 1, f"%X%{sys}.HCP.TRIP": 0, f"%X%{sys}.CHW.FLOW": 1, f"%X%{sys}.ENA": 1}, "expect": {f"%X%{sys}1.ENA": 0, f"%X%{sys}.SAFE.ALM": 1}, "note": "low refrigerant pressure trip -- safety shutdown overrides master enable"},
                {"force": {f"%X%{sys}.LOP.TRIP": 0, f"%X%{sys}.HCP.TRIP": 1, f"%X%{sys}.CHW.FLOW": 1, f"%X%{sys}.ENA": 1}, "expect": {f"%X%{sys}1.ENA": 0, f"%X%{sys}.SAFE.ALM": 1}, "note": "high condenser pressure trip -- safety shutdown overrides master enable"},
                {"force": {f"%X%{sys}.LOP.TRIP": 0, f"%X%{sys}.HCP.TRIP": 0, f"%X%{sys}.CHW.FLOW": 0, f"%X%{sys}.ENA": 1}, "expect": {f"%X%{sys}1.ENA": 0, f"%X%{sys}.SAFE.ALM": 1}, "note": "no flow proven -- safety shutdown overrides master enable even with no pressure trips"},
                {"force": {f"%X%{sys}.LOP.TRIP": 0, f"%X%{sys}.HCP.TRIP": 0, f"%X%{sys}.CHW.FLOW": 1, f"%X%{sys}.ENA": 1}, "expect": {f"%X%{sys}1.ENA": 1}, "note": "no trips, flow proven, master enable on -- normal run"},
                {"force": {f"%X%{sys}.LOP.TRIP": 0, f"%X%{sys}.HCP.TRIP": 0, f"%X%{sys}.CHW.FLOW": 1, f"%X%{sys}.ENA": 0}, "expect": {f"%X%{sys}1.ENA": 0}, "note": "no trips, flow proven, master enable off -- standby"}
            ]
        }
    },
    {
        "title": "Cooling Tower Fan VFD Speed Control PID Loop",
        "difficulty": "expert",
        "multiLine": True,
        "systems": CT_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Fan Speed PID Control with Vibration Safety Shutdown",
            "prompt": f"Rather than staged on/off fan speed, {sys}'s fan should modulate continuously via LOOP to hold Condenser Water Supply Temp at setpoint. Whenever Tower Fan Enable (\"%X%{sys}.FAN.ENA\") is ON, execute a LOOP statement to modulate Tower Fan Speed Output (\"%X%{sys}.FAN.SPD\") (0-100%) to maintain Condenser Water Supply Temp (\"%X%{sys}.CWS.T\") at Setpoint (\"%X%{sys}.CWS.T.SP\"). When Tower Fan Enable is OFF, hold Tower Fan Speed Output at 0. This fan also has a vibration switch for mechanical safety: if Vibration Switch (\"%X%{sys}.VIB.SW\") trips ON, latch a Vibration Shutdown Alarm (\"%X%{sys}.VIB.ALM\" -- virtual) ON and force Tower Fan Speed Output to 0, skipping the LOOP entirely -- keep it latched even after the vibration switch itself clears, since a bearing or belt problem needs a technician's eyes before running again. It should only clear when an operator pulses Vibration Reset (\"%X%{sys}.VIB.RST\") ON.",
            "points": [
                {"name": f"%X%{sys}.FAN.ENA",
                    "desc": "Tower Fan Enable (already exists -- your logic must write to this point)", "type": "BV", "kind": "physical"},
                {"name": f"%X%{sys}.CWS.T",
                    "desc": "Condenser Water Supply Temp Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.CWS.T.SP",
                    "desc": "Condenser Water Supply Temp Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.FAN.SPD",
                    "desc": "Tower Fan Speed Output (0-100%)", "type": "AO", "kind": "physical"},
                {"name": f"%X%{sys}.VIB.SW",
                    "desc": "Vibration Switch (mechanical safety, fan/gearbox bearing protection)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.VIB.ALM",
                    "desc": "Vibration Shutdown Alarm (virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.VIB.RST",
                    "desc": "Vibration Reset Pulse (operator-facing)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Write the latch (VIB.SW sets VIB.ALM, no ELSE) and the reset (VIB.RST clears VIB.ALM) as two separate IF blocks first. Then wrap the FAN.ENA-gated LOOP/hold logic inside the ELSE of an outer IF on VIB.ALM: IF VIB.ALM .EQ. 1 THEN FAN.SPD = 0, ELSE (the LOOP-or-hold-at-0 logic from before, keyed on FAN.ENA). A real LOOP call takes 12 positional arguments: LOOP(type, pv, cv, sp, pg, ig, dg, st, bias, lo, hi, 0) -- type=0, pv=\"%X%{sys}.CWS.T\", cv=\"%X%{sys}.FAN.SPD\", sp=\"%X%{sys}.CWS.T.SP\", pg=4.0, ig=0.2, dg=0.0, st=1, bias=50.0, lo=20, hi=100, final 0.",
            "checks": [
                {"force": {f"%X%{sys}.VIB.SW": 0, f"%X%{sys}.VIB.ALM": 0, f"%X%{sys}.VIB.RST": 0, f"%X%{sys}.FAN.ENA": 0, f"%X%{sys}.CWS.T": 90, f"%X%{sys}.CWS.T.SP": 85}, "expect": {f"%X%{sys}.FAN.SPD": 0}, "note": "no vibration trip, fan not enabled -- speed forced to 0"},
                {"force": {f"%X%{sys}.VIB.SW": 0, f"%X%{sys}.VIB.ALM": 0, f"%X%{sys}.VIB.RST": 0, f"%X%{sys}.FAN.ENA": 0, f"%X%{sys}.CWS.T": 80, f"%X%{sys}.CWS.T.SP": 85}, "expect": {f"%X%{sys}.FAN.SPD": 0}, "note": "no vibration trip, fan not enabled -- speed forced to 0 regardless of temp"},
                {"force": {f"%X%{sys}.VIB.SW": 1, f"%X%{sys}.VIB.ALM": 0, f"%X%{sys}.VIB.RST": 0, f"%X%{sys}.FAN.ENA": 1}, "expect": {f"%X%{sys}.VIB.ALM": 1, f"%X%{sys}.FAN.SPD": 0}, "note": "vibration switch trips -- alarm latches and fan speed forced to 0 even with the fan enabled"},
                {"force": {f"%X%{sys}.VIB.SW": 0, f"%X%{sys}.VIB.ALM": 1, f"%X%{sys}.VIB.RST": 0}, "cycles": 3, "expect": {f"%X%{sys}.VIB.ALM": 1}, "note": "vibration switch cleared but not manually reset -- alarm stays latched across scans"},
                {"force": {f"%X%{sys}.VIB.SW": 0, f"%X%{sys}.VIB.ALM": 1, f"%X%{sys}.VIB.RST": 1}, "expect": {f"%X%{sys}.VIB.ALM": 0}, "note": "vibration reset pulsed -- alarm clears back OFF"}
            ]
        }
    },
    {
        "title": "Boiler Low Water Cutoff Safety Shutdown Dispatch (GOTO Decision Tree)",
        "difficulty": "expert",
        "multiLine": True,
        "systems": BOILER_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys}1 Low Water Cutoff Safety Dispatch with Stack Temp Trip",
            "prompt": f"Boiler safeties always override normal firing -- real boiler programs check safety first via a GOTO decision tree. Build it here. Remember GOTO can only jump forward to a higher line number, never backward, so lay the blocks out in this order. First, check safety: if the Low Water Cutoff (\"%X%{sys}1.LWCO\") is ON (tripped -- water level below the safe minimum) OR Stack Temperature (\"%X%{sys}1.STK.T\") exceeds a High Stack Temp Limit (\"%X%{sys}1.STK.T.HI\"), GOTO line 02000 immediately, skipping normal firing logic entirely -- either condition alone shuts the boiler down regardless of demand. Otherwise, check Boiler 1 Firing Demand (\"%X%{sys}1.DMD\", a virtual point driven by hot water temperature reset logic elsewhere in the program): if ON, GOTO line 01100 (Firing); if OFF, GOTO line 01200 (Standby). At line 01100 (Firing), turn ON Boiler 1 Enable (\"%X%{sys}1.ENA\"), then GOTO line 03000 to end the scan. At line 01200 (Standby), turn OFF Boiler 1 Enable, then GOTO line 03000. At line 02000 (the Safety Shutdown block), turn OFF Boiler 1 Enable and turn ON a Low Water Safety Alarm (\"%X%{sys}1.LWCO.ALM\") -- a virtual point -- then GOTO line 03000. Line 03000 should be a comment marking the end of the scan, and it needs to be the LAST line in your program -- every block above it ends with its own GOTO there so none of them can fall straight through into whatever comes next in the file.",
            "points": [
                {"name": f"%X%{sys}1.LWCO",
                    "desc": "Low Water Cutoff Safety Switch", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}1.STK.T",
                    "desc": "Stack (Flue) Temperature Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}1.STK.T.HI",
                    "desc": "High Stack Temp Limit (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}1.DMD",
                    "desc": "Firing Demand (virtual point, driven by hot water temperature reset logic elsewhere in the program)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}1.ENA",
                    "desc": "Boiler 1 Enable", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}1.LWCO.ALM",
                    "desc": "Low Water Safety Alarm (virtual point)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Same dispatch shape as the chiller and AHU safety trees -- OR the LWCO check together with the STK.T comparison in the first line, and jump straight to the shutdown block, bypassing the demand check entirely when either is true. Only the shutdown block writes to LWCO.ALM.",
            "checks": [
                {"force": {f"%X%{sys}1.LWCO": 1, f"%X%{sys}1.STK.T": 400, f"%X%{sys}1.STK.T.HI": 600, f"%X%{sys}1.DMD": 1}, "expect": {f"%X%{sys}1.ENA": 0, f"%X%{sys}1.LWCO.ALM": 1}, "note": "low water cutoff tripped -- shuts boiler down and alarms regardless of demand"},
                {"force": {f"%X%{sys}1.LWCO": 0, f"%X%{sys}1.STK.T": 650, f"%X%{sys}1.STK.T.HI": 600, f"%X%{sys}1.DMD": 1}, "expect": {f"%X%{sys}1.ENA": 0, f"%X%{sys}1.LWCO.ALM": 1}, "note": "stack temp over the high limit -- also shuts the boiler down and alarms, even with no low-water trip"},
                {"force": {f"%X%{sys}1.LWCO": 0, f"%X%{sys}1.STK.T": 400, f"%X%{sys}1.STK.T.HI": 600, f"%X%{sys}1.DMD": 1}, "expect": {f"%X%{sys}1.ENA": 1}, "note": "no trips, firing demand present -- boiler enabled"},
                {"force": {f"%X%{sys}1.LWCO": 0, f"%X%{sys}1.STK.T": 400, f"%X%{sys}1.STK.T.HI": 600, f"%X%{sys}1.DMD": 0}, "expect": {f"%X%{sys}1.ENA": 0}, "note": "no trips, no demand -- standby"}
            ]
        }
    },
    {
        "title": "CRAC Discharge Air Temp PID Loop with Compressor Staging",
        "difficulty": "expert",
        "multiLine": True,
        "systems": CRAC_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys}201 Discharge Air Temp PID Control & Staging with High Head Pressure Safety",
            "prompt": f"{sys}201's cooling needs finer control than a simple on/off compressor -- real CRAC units modulate a hot-gas bypass or EEV to hold discharge air temp precisely, staging a second compressor only under heavy load. Whenever {sys}201 is enabled (\"%X%{sys}201.ENA\" is ON), execute a LOOP statement to modulate the Modulating Cooling Output (\"%X%{sys}201.CLG.MOD\") (0-100%) to maintain Discharge Air Temp (\"%X%{sys}201.DAT\") at Setpoint (\"%X%{sys}201.DAT.SP\"). Separately, if the Modulating Cooling Output stays above 90% continuously, accumulate a Stage-Up Timer (\"%X%{sys}201.STGUP\") by 1 second using SAMPLE(1); once it exceeds 180 seconds, stage ON a second compressor (\"%X%{sys}201.COMP2.ENA\") for extra capacity. If the Modulating Cooling Output drops below 60%, stage OFF Compressor 2 and reset the Stage-Up Timer to 0. This unit also has a high head pressure safety switch: if High Head Pressure Switch (\"%X%{sys}201.HHP.SW\") trips ON, latch a High Head Pressure Alarm (\"%X%{sys}201.HHP.ALM\" -- virtual) ON and force both the Modulating Cooling Output to 0 and Compressor 2 Enable OFF, skipping the LOOP and staging logic entirely -- keep it latched even after the switch itself clears, since a head pressure trip needs a technician's attention before running again. It should only clear when an operator pulses High Head Pressure Reset (\"%X%{sys}201.HHP.RST\") ON.",
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
                    "desc": "Compressor 2 Stage Enable", "type": "DO", "kind": "physical"},
                {"name": f"%X%{sys}201.HHP.SW",
                    "desc": "High Head Pressure Switch (mechanical/refrigerant safety)", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}201.HHP.ALM",
                    "desc": "High Head Pressure Alarm (virtual point)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}201.HHP.RST",
                    "desc": "High Head Pressure Reset Pulse (operator-facing)", "type": "BV", "kind": "virtual"}
            ],
            "help": "Write the latch (HHP.SW sets HHP.ALM, no ELSE) and the reset (HHP.RST clears HHP.ALM) as two separate IF blocks first. Then wrap BOTH the ENA-gated LOOP call and the entire staging block inside the ELSE of an outer IF on HHP.ALM: IF HHP.ALM .EQ. 1 THEN (CLG.MOD=0, COMP2.ENA=0), ELSE (the LOOP call and the staging logic from before, unchanged). A real LOOP call takes 12 positional arguments: LOOP(type, pv, cv, sp, pg, ig, dg, st, bias, lo, hi, 0) -- type=0, pv=\"%X%{sys}201.DAT\", cv=\"%X%{sys}201.CLG.MOD\", sp=\"%X%{sys}201.DAT.SP\", pg=3.5, ig=0.2, dg=0.0, st=1, bias=30.0, lo=0, hi=100, final 0.",
            "checks": [
                {"force": {f"%X%{sys}201.HHP.SW": 0, f"%X%{sys}201.HHP.ALM": 0, f"%X%{sys}201.HHP.RST": 0, f"%X%{sys}201.CLG.MOD": 95, f"%X%{sys}201.STGUP": 0, f"%X%{sys}201.COMP2.ENA": 0}, "cycles": 181, "expect": {f"%X%{sys}201.COMP2.ENA": 1}, "note": "no head pressure trip, cooling output stays above 90% for over 180 seconds -- compressor 2 stages on"},
                {"force": {f"%X%{sys}201.HHP.SW": 0, f"%X%{sys}201.HHP.ALM": 0, f"%X%{sys}201.HHP.RST": 0, f"%X%{sys}201.CLG.MOD": 95, f"%X%{sys}201.STGUP": 0, f"%X%{sys}201.COMP2.ENA": 0}, "cycles": 30, "expect": {f"%X%{sys}201.COMP2.ENA": 0}, "note": "timer hasn't reached 180 seconds yet -- compressor 2 should not have staged on"},
                {"force": {f"%X%{sys}201.HHP.SW": 0, f"%X%{sys}201.HHP.ALM": 0, f"%X%{sys}201.HHP.RST": 0, f"%X%{sys}201.CLG.MOD": 40, f"%X%{sys}201.STGUP": 150, f"%X%{sys}201.COMP2.ENA": 1}, "expect": {f"%X%{sys}201.COMP2.ENA": 0, f"%X%{sys}201.STGUP": 0}, "note": "cooling output drops below 60% -- compressor 2 stages off and stage-up timer resets"},
                {"force": {f"%X%{sys}201.HHP.SW": 1, f"%X%{sys}201.HHP.ALM": 0, f"%X%{sys}201.HHP.RST": 0, f"%X%{sys}201.ENA": 1, f"%X%{sys}201.COMP2.ENA": 1}, "expect": {f"%X%{sys}201.HHP.ALM": 1, f"%X%{sys}201.CLG.MOD": 0, f"%X%{sys}201.COMP2.ENA": 0}, "note": "head pressure trip -- alarm latches, cooling output forced to 0 and compressor 2 forced off, overriding everything else"},
                {"force": {f"%X%{sys}201.HHP.SW": 0, f"%X%{sys}201.HHP.ALM": 1, f"%X%{sys}201.HHP.RST": 0}, "cycles": 3, "expect": {f"%X%{sys}201.HHP.ALM": 1}, "note": "switch cleared but not manually reset -- alarm stays latched across scans"},
                {"force": {f"%X%{sys}201.HHP.SW": 0, f"%X%{sys}201.HHP.ALM": 1, f"%X%{sys}201.HHP.RST": 1}, "expect": {f"%X%{sys}201.HHP.ALM": 0}, "note": "reset pulsed -- alarm clears back OFF"}
            ]
        }
    },
    {
        "title": "MAU Freezestat Safety Dispatch (GOTO Decision Tree)",
        "difficulty": "expert",
        "multiLine": True,
        "systems": MAU_SYSTEMS,
        "generate": lambda sys: {
            "title": f"{sys} Freezestat Safety Dispatch with Latching, Resettable Alarm",
            "prompt": f"Real Siemens MAU programs open with a decision tree just like AHUs do: check safety first, then dispatch to the current mode via GOTO. Build it here. Remember GOTO can only jump forward to a higher line number, never backward, so lay the blocks out in this order. As the very first line in the program, before any dispatch logic at all, check for a manual reset: if Freeze Alarm Reset (\"%X%{sys}.FRZ.ALM.RST\") is ON, clear Freeze Alarm (\"%X%{sys}.FRZ.ALM\") back OFF. This way the reset gets evaluated every single scan, regardless of which mode block ends up running afterward -- though it doesn't stop the freezestat check right below it from re-setting the alarm again on the same scan if the freezestat is still tripped. Next, check safety: if the Low-Limit Freezestat (\"%X%{sys}.FRZ.STAT\") is ON (tripped -- discharge air temp dropped below the coil freeze threshold), GOTO line 02000 immediately, skipping mode logic entirely -- since this unit is 100% outside air with no return air to temper it, freeze protection has to override everything else. Otherwise, check Building Occupancy State (\"%X%BLDG.OCC.STATE\", 0 = Occupied, 1 = Unoccupied): if Occupied, GOTO line 01100; if Unoccupied, GOTO line 01200. At line 01100 (the Occupied block), turn ON Supply Fan Enable (\"%X%{sys}.SF.ENA\") and turn ON the Heating Valve (\"%X%{sys}.HTG.VLV\") to satisfy Discharge Air Setpoint (keep that as a simple ON for this exercise), then GOTO line 03000 to end the scan. At line 01200 (the Unoccupied block), turn OFF Supply Fan Enable and turn OFF the Heating Valve, then GOTO line 03000. At line 02000 (the Freeze Protection block), turn OFF Supply Fan Enable, turn ON the Heating Valve fully open to keep hot water moving through the coil, and turn ON Freeze Alarm (making it a LATCHING alarm -- since nothing but the reset check at the very top ever clears it, it will stay on even once the freezestat itself resets), then GOTO line 03000. Line 03000 should be a comment marking the end of the scan, and it needs to be the LAST line in your program -- every block above it ends with its own GOTO there so none of them can fall straight through into whatever comes next in the file.",
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
                    "desc": "Freeze Alarm (virtual point -- latches until manually reset)", "type": "BV", "kind": "virtual"},
                {"name": f"%X%{sys}.FRZ.ALM.RST",
                    "desc": "Freeze Alarm Reset Pulse (operator-facing)", "type": "BV", "kind": "virtual"}
            ],
            "help": "The reset check has to be a plain, ungated IF sitting BEFORE the freezestat safety check -- not inside any of the three dispatch blocks -- since a GOTO tree only ever runs ONE of those blocks per scan, and the reset needs to be checked no matter which one that is. Same dispatch shape as the other safety-first GOTO trees otherwise -- the freezestat check comes right after the reset check and jumps straight to its own block, skipping the occupancy check entirely when tripped. Only the Freeze Protection block ever WRITES a 1 to FRZ.ALM (with no ELSE anywhere clearing it) -- that, combined with the reset check at the top, is what makes it a proper latch.",
            "checks": [
                {"force": {f"%X%{sys}.FRZ.ALM.RST": 0, f"%X%{sys}.FRZ.STAT": 1, "%X%BLDG.OCC.STATE": 0}, "expect": {f"%X%{sys}.SF.ENA": 0, f"%X%{sys}.HTG.VLV": 1, f"%X%{sys}.FRZ.ALM": 1}, "note": "freezestat tripped -- overrides occupancy: fan off, heating valve driven open, alarm on"},
                {"force": {f"%X%{sys}.FRZ.ALM.RST": 0, f"%X%{sys}.FRZ.STAT": 0, "%X%BLDG.OCC.STATE": 0}, "expect": {f"%X%{sys}.SF.ENA": 1, f"%X%{sys}.HTG.VLV": 1}, "note": "no freeze trip, occupied -- fan on, heating valve on"},
                {"force": {f"%X%{sys}.FRZ.ALM.RST": 0, f"%X%{sys}.FRZ.STAT": 0, "%X%BLDG.OCC.STATE": 1}, "expect": {f"%X%{sys}.SF.ENA": 0, f"%X%{sys}.HTG.VLV": 0}, "note": "no freeze trip, unoccupied -- fan and heating valve off"},
                {"force": {f"%X%{sys}.FRZ.ALM.RST": 0, f"%X%{sys}.FRZ.STAT": 0, "%X%BLDG.OCC.STATE": 0, f"%X%{sys}.FRZ.ALM": 1}, "cycles": 3, "expect": {f"%X%{sys}.FRZ.ALM": 1}, "note": "freezestat cleared, back in normal occupied mode, but no reset pulsed -- alarm should stay latched ON across scans"},
                {"force": {f"%X%{sys}.FRZ.ALM.RST": 1, f"%X%{sys}.FRZ.STAT": 0, "%X%BLDG.OCC.STATE": 0, f"%X%{sys}.FRZ.ALM": 1}, "expect": {f"%X%{sys}.FRZ.ALM": 0}, "note": "reset pulsed -- alarm clears back OFF even though the dispatch falls into the Occupied block, which never otherwise touches FRZ.ALM"}
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
# each TEMPLATES entry's "generate" dict below.


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
