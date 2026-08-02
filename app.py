from flask import Flask, render_template, request, jsonify, Response
import random
import os
import time
import functools
from collections import defaultdict, deque
from openai import OpenAI

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


# --- BASIC RATE LIMITING (protects the shared OpenAI bill) ---
# In-memory, per-process fixed-window limiter. Good enough for a small
# internal tool on a single web instance; not meant to survive a restart or
# scale across multiple server processes.
RATE_LIMIT_MAX_REQUESTS = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "30"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "3600"))
_rate_limit_hits = defaultdict(deque)


def rate_limited(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        key = request.remote_addr or "unknown"
        now = time.time()
        hits = _rate_limit_hits[key]
        while hits and now - hits[0] > RATE_LIMIT_WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= RATE_LIMIT_MAX_REQUESTS:
            retry_in = int(RATE_LIMIT_WINDOW_SECONDS - (now - hits[0]))
            return jsonify({
                "error": f"Rate limit reached ({RATE_LIMIT_MAX_REQUESTS} requests/hour). "
                         f"Try again in about {max(retry_in, 1)} seconds."
            }), 429
        hits.append(now)
        return f(*args, **kwargs)
    return wrapper

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
            "prompt": f"When Pump Enable (\"%X%{sys}.ENA\") is ON, execute a LOOP statement to modulate Pump Speed Output (\"%X%{sys}.SPD\") (0-100%) to maintain System Differential Pressure (\"%X%{sys}.DP\") at Setpoint (\"%X%{sys}.DP.SP\"). A real LOOP call takes 12 positional arguments in this exact order: LOOP(type, pv, cv, sp, pg, ig, dg, st, bias, lo, hi, 0). Use: type=128 (reverse acting -- speed must increase as pressure falls below setpoint, the opposite direction of the process variable), pv=\"%X%{sys}.DP\", cv=\"%X%{sys}.SPD\", sp=\"%X%{sys}.DP.SP\", P Gain (pg)=5.0, I Gain (ig)=0.2, D Gain (dg)=0.0, sample time (st)=1, bias=50.0 (mid-range output when DP is exactly at setpoint), low limit (lo)=0, high limit (hi)=100, and the final parameter is always 0 (unused). If System Differential Pressure falls below half of Setpoint, turn ON a Low Pressure Fault Alarm (\"%X%{sys}.ALM\") -- a virtual point.",
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
            "prompt": f"Air Compressor {sys} is water-cooled: cooling water flows through the compressor's heat exchanger, and a three-way bypass valve mixes cooled return water with warm bypassed water to hold a stable Compressor Water Entering Temp (\"%X%{sys}.CHW.ET\") at Setpoint (\"%X%{sys}.CHW.ET.SP\") -- too cold risks condensation damage inside the compressor, too warm hurts cooling performance. Whenever the compressor is running (Compressor Run Status \"%X%{sys}.STAT\" is ON), execute a LOOP statement to modulate the Bypass Valve Output (\"%X%{sys}.BYP.VLV\") (0-100%) to hold Entering Temp at Setpoint. A real LOOP call takes 12 positional arguments in this exact order: LOOP(type, pv, cv, sp, pg, ig, dg, st, bias, lo, hi, 0). Use: type=0 (direct acting -- valve output must increase as entering temp rises above setpoint, the same direction as the process variable), pv=\"%X%{sys}.CHW.ET\", cv=\"%X%{sys}.BYP.VLV\", sp=\"%X%{sys}.CHW.ET.SP\", P Gain (pg)=4.0, I Gain (ig)=0.3, D Gain (dg)=0.0, sample time (st)=1, bias=50.0 (mid-range output when Entering Temp is exactly at setpoint), low limit (lo)=0, high limit (hi)=100, and the final parameter is always 0 (unused). When the compressor is not running, hold the Bypass Valve Output at 0.",
            "points": [
                {"name": f"%X%{sys}.STAT",
                    "desc": "Compressor Run Status", "type": "DI", "kind": "physical"},
                {"name": f"%X%{sys}.CHW.ET",
                    "desc": "Compressor Cooling Water Entering Temp", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.CHW.ET.SP",
                    "desc": "Cooling Water Entering Temp Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.BYP.VLV",
                    "desc": "Three-Way Bypass Valve Control Output (0-100%)", "type": "AO", "kind": "physical"}
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
            "prompt": f"Using a LOOP statement, modulate Supply Fan Speed Output (\"%X%{sys}.SF.SPD\") (0-100%) to maintain Duct Static Pressure (\"%X%{sys}.DSP\") at Setpoint (\"%X%{sys}.DSP.SP\"). A real LOOP call takes 12 positional arguments in this exact order: LOOP(type, pv, cv, sp, pg, ig, dg, st, bias, lo, hi, 0). Use: type=128 (reverse acting -- fan speed must increase as static pressure falls below setpoint, the opposite direction of the process variable), pv=\"%X%{sys}.DSP\", cv=\"%X%{sys}.SF.SPD\", sp=\"%X%{sys}.DSP.SP\", P Gain (pg)=3.0, I Gain (ig)=0.15, D Gain (dg)=0.0, sample time (st)=1, bias=50.0 (mid-range output when DSP is exactly at setpoint), low limit (lo)=0, high limit (hi)=100, and the final parameter is always 0 (unused). If Duct Static Pressure exceeds 150% of Setpoint, turn ON a High Static Fault (\"%X%{sys}.DSP.FLT\") -- a virtual point.",
            "points": [
                {"name": f"%X%{sys}.SF.SPD",
                    "desc": "Supply Fan Speed Output (0-100%)", "type": "AO", "kind": "physical"},
                {"name": f"%X%{sys}.DSP",
                    "desc": "Duct Static Pressure Sensor", "type": "AI", "kind": "physical"},
                {"name": f"%X%{sys}.DSP.SP",
                    "desc": "Duct Static Pressure Setpoint (virtual point)", "type": "AV", "kind": "virtual"},
                {"name": f"%X%{sys}.DSP.FLT",
                    "desc": "High Static Fault (virtual point)", "type": "BV", "kind": "virtual"}
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


@app.route("/api/grade", methods=["POST"])
@rate_limited
def grade_code():
    data = request.get_json(silent=True) or {}
    user_code = (data.get("code") or "").strip()
    prompt = (data.get("prompt") or "").strip()
    points = data.get("points") or []
    api_key = os.getenv("OPENAI_API_KEY", "")

    if not user_code:
        return jsonify({"error": "No PPCL code submitted."}), 400
    if not prompt:
        return jsonify({"error": "No active scenario. Generate a scenario before auditing."}), 400
    if not api_key:
        return jsonify({"error": "The server isn't configured with an OpenAI API key yet. Set OPENAI_API_KEY on the host."}), 400

    try:
        client = OpenAI(api_key=api_key)

        point_str = "\n".join(
            [f"- {p['name']} ({p['type']}, {p.get('kind', 'physical')}): {p['desc']}" for p in points])

        system_msg = (
            "You are a Senior Siemens APOGEE / Desigo CC System Automation Engineer reviewing real field PPCL code.\n"
            "Audit the student's PPCL code against Siemens field standards shown in actual panel dumps:\n"
            "1. POINT NAMES: Enclosed in double quotes (e.g., \"%X%CHLR.ENA\", \"%X%CHLR.CHW.DP\", \"%X%CHLR.ALM\").\n"
            "2. SUFFIX CONVENTIONS: Use .ENA for enables/commands, .DP for differential pressure, .ALM for alarms, .SP for setpoints, .S for status.\n"
            "3. COMMENTS & BLOCK HEADERS: Structured comment blocks starting with C ==================== and C ----- SECTION HEADER -----.\n"
            "4. OPERATORS & STATEMENTS: Explicit relational operators (.GT., .LT., .EQ., .AND., .OR.) and line numbers (e.g. 10000, 10010, 10020).\n"
            "5. MATH & ACCUMULATORS: Direct assignments like \"%X%CHLR.STGUP\" = \"%X%CHLR.STGUP\" + 1.\n"
            "6. LOCAL VARIABLES: Use $LOC1, $LOC2 for scratchpad calculations.\n"
            "7. ACTION ARGUMENTS ARE MANDATORY: every ON, OFF, and SET action -- whether after THEN or after ELSE -- MUST be followed by its point name in parentheses, e.g. ELSE OFF (\"%X%CHLR.CLG.VLV\"). A bare ON, OFF, or SET with no parenthesized point argument (e.g. \"...THEN ON (\\\"%X%A\\\") ELSE OFF\" with nothing after OFF) is INVALID PPCL and must be marked as an error, never scored as correct, even if the omission seems minor.\n"
            "8. PHYSICAL vs VIRTUAL POINTS: the point list marks each point 'physical' or 'virtual'. Physical points (real AI/AO/DI/DO hardware, or genuinely tech-adjustable setpoints) already exist -- the student must reference them exactly as given, never create logic that 'sets' a sensor or feedback point. Virtual points (alarms, timers, delay accumulators, internal flags) do NOT pre-exist -- there is no physical wiring for them, so the student's own program logic must derive and drive their value (e.g. comparing a sensor to a limit and setting the alarm ON/OFF). Do not penalize a student for 'creating' a virtual point's value with logic; DO flag it if they treat a virtual point as if it already has a value without ever writing logic that sets it, or if they mistakenly try to command/write to a physical sensor/feedback point.\n"
            "9. LOOP (PID) SYNTAX: a real LOOP call always takes exactly 12 positional arguments in this order: LOOP(type,pv,cv,sp,pg,ig,dg,st,bias,lo,hi,0) -- control type (0=direct, 128=reverse acting), process variable, control variable, setpoint, proportional gain, integral gain, derivative gain, sample time in seconds, bias, low output limit, high output limit, and a final unused 0. If the exercise prompt specifies these values, flag a LOOP call that is missing arguments, has them out of order, or omits the trailing 0 as a syntax error -- not just a style nitpick.\n"
            "10. GOTO DECISION TREES: real AHU programs check safety trips (smoke detectors, freeze stats, high static) BEFORE checking occupancy/mode -- safety must always be evaluated first and override the mode logic, never the other way around. Every GOTO target must be a HIGHER line number than the line it jumps from -- GOTO only ever jumps forward, never backward, so a decision tree's blocks must be laid out in ascending line-number order matching their dispatch order. A GOTO to a lower or equal line number is a real bug (it can create an infinite loop or re-run earlier logic) and must be flagged as an error, not a style note. When an exercise asks for a GOTO-dispatched decision tree, every block the tree jumps to must end with its own GOTO forward to a shared closing line that sits last in the program (the highest line number); a block missing that trailing GOTO will silently fall through into whatever code comes next in the file, which is also a real bug and should be flagged as one even if the block's own logic is otherwise correct.\n"
            "11. SAMPLE PLACEMENT: SAMPLE(n) is valid in TWO different real positions and both are correct PPCL -- do not flag either as wrong. It can gate a single action inside a THEN/ELSE clause, e.g. IF (cond) THEN SAMPLE(1) \"%X%PT\" = \"%X%PT\" + 1, OR it can gate an entire IF statement placed directly before it, e.g. SAMPLE(1) IF (cond) THEN \"%X%PT\" = \"%X%PT\" + 1 -- in that second form the whole condition check (not just the action) only runs once every N seconds. Real field panel dumps use both forms interchangeably; judge correctness by whether the timing behavior matches the sequence prompt, not by which of the two forms the student picked.\n\n"
            "Provide: Score (0-100%), Syntax Checks, Point Name Validation, and Field Engineering Feedback."
        )

        user_msg = f"SEQUENCE PROMPT:\n{prompt}\n\nREQUIRED POINT LIST:\n{point_str}\n\nSTUDENT'S PPCL CODE:\n{user_code}"

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg}
            ]
        )
        return jsonify({"feedback": response.choices[0].message.content})
    except Exception as e:
        msg = str(e)
        if "401" in msg or "invalid_api_key" in msg.lower() or "incorrect api key" in msg.lower():
            friendly = "Invalid OpenAI API key. Double-check the key and try again."
        elif "429" in msg or "rate limit" in msg.lower() or "quota" in msg.lower():
            friendly = "OpenAI rate limit or quota exceeded. Wait a moment and try again."
        else:
            friendly = f"Audit failed: {msg}"
        return jsonify({"error": friendly}), 502


@app.route("/api/hint", methods=["POST"])
@rate_limited
def get_hint():
    data = request.get_json(silent=True) or {}
    user_code = (data.get("code") or "").strip()
    prompt = (data.get("prompt") or "").strip()
    points = data.get("points") or []
    hint_level = data.get("hintLevel", 1)
    api_key = os.getenv("OPENAI_API_KEY", "")

    try:
        hint_level = int(hint_level)
    except (TypeError, ValueError):
        hint_level = 1
    hint_level = max(1, min(hint_level, 3))

    if not prompt:
        return jsonify({"error": "No active scenario. Generate a scenario before asking for a hint."}), 400
    if not api_key:
        return jsonify({"error": "The server isn't configured with an OpenAI API key yet. Set OPENAI_API_KEY on the host."}), 400

    try:
        client = OpenAI(api_key=api_key)

        point_str = "\n".join(
            [f"- {p['name']} ({p['type']}, {p.get('kind', 'physical')}): {p['desc']}" for p in points]) or "(none given -- the student must design appropriate point names themselves)"

        level_instructions = {
            1: "Give a LEVEL 1 (gentle nudge) hint. Point the student toward which part of the sequence they should focus on next (e.g. which condition or point they haven't handled yet). Do NOT mention specific PPCL syntax or write any code. 2-3 sentences max.",
            2: "Give a LEVEL 2 (more specific) hint. Name the specific PPCL construct or pattern they likely need (e.g. \"you'll need an IF/THEN/ELSE\" or \"this needs a SAMPLE(1) timer\"), and describe the logic in plain English, but do NOT write actual PPCL code. 3-4 sentences max.",
            3: "Give a LEVEL 3 (worked example) hint. Write ONE short illustrative line of PPCL code showing the pattern needed (using a DIFFERENT point name than the actual scenario, or a simplified stand-in) so the student sees the syntax shape without getting the literal answer to copy-paste. Briefly explain it. Keep it under 6 lines total."
        }

        system_msg = (
            "You are a patient Siemens APOGEE PPCL instructor helping a student who is stuck on a training exercise.\n"
            "You will be given the exercise prompt, the point list, and the student's current (possibly incomplete or empty) code.\n"
            "Your job is to give ONLY the hint level requested -- never the full solution, never the complete corrected code for their exact scenario.\n"
            f"{level_instructions[hint_level]}\n"
            "Be encouraging and concise. Do not restate the entire prompt back to them."
        )

        user_msg = f"SEQUENCE PROMPT:\n{prompt}\n\nPOINT LIST:\n{point_str}\n\nSTUDENT'S CURRENT CODE (may be empty or partial):\n{user_code or '(nothing written yet)'}"

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg}
            ]
        )
        return jsonify({"hint": response.choices[0].message.content, "hintLevel": hint_level})
    except Exception as e:
        msg = str(e)
        if "401" in msg or "invalid_api_key" in msg.lower() or "incorrect api key" in msg.lower():
            friendly = "Invalid OpenAI API key. Double-check the key and try again."
        elif "429" in msg or "rate limit" in msg.lower() or "quota" in msg.lower():
            friendly = "OpenAI rate limit or quota exceeded. Wait a moment and try again."
        else:
            friendly = f"Hint request failed: {msg}"
        return jsonify({"error": friendly}), 502


if __name__ == "__main__":
    # Local dev only -- in production this app should run under a real WSGI
    # server (gunicorn), which is what the Procfile points to. debug mode
    # must stay off anywhere reachable from outside your own machine: it
    # exposes an interactive debugger/console to anyone who triggers an
    # unhandled error.
    debug_mode = os.getenv("FLASK_DEBUG", "0") == "1"
    port = int(os.getenv("PORT", "8080"))
    app.run(debug=debug_mode, host="0.0.0.0", port=port)
