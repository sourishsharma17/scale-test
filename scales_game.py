# serial_scale_game_overlay.py
# Overlay + game logic for “Baby Trapped” using direct serial from scales.
# Display shown = round_nearest(actual * 0.90, 0.5 kg).
# Arming: actual ≥ 35.0 kg for 3.0 s (smoothed).
# Hidden thresholds use capped arming actual = min(arming_actual, 112.0):
#   DROP if actual < 0.90 * capped_arming_actual for 0.40 s
#   RESTORE if actual ≥ 0.90 * capped_arming_actual for 0.30 s
# Baseline shown on overlay at arming:
#   if arming_actual > 112.0 -> 100.0 kg
#   else -> round_nearest(arming_actual * 0.90, 0.5 kg)
# No rapid-fall logic; just the hold-downs.

import math
import re
import sys
import time
import threading
import statistics
from dataclasses import dataclass, asdict
from typing import Optional, List

import requests
import serial
from flask import Flask, jsonify, render_template_string

# ===================== HARD-CODED CONFIG =====================

COM_PORT = r"COM9"          # your USB adapter port
BAUD = 9600
USE_7E1 = False             # True => 7E1, False => 8N1

# Arming thresholds (ACTUAL kg)
MIN_TRIGGER_KG = 35.0
STABLE_SECONDS = 3.0

# Display factor & rounding
DISPLAY_FACTOR = 0.90
DISPLAY_STEP_KG = 0.5

# Escape/Restore thresholds (ACTUAL)
DROP_FACTOR = 0.90
RESTORE_FACTOR = 0.90

# Debounce / hold-times
DROP_HOLDDOWN_S = 0.40
RESTORE_HOLDDOWN_S = 0.30

# Smoothing on ACTUAL
SMOOTH_WINDOW = 4

# Companion (press endpoints)
COMPANION_HOST = "192.168.2.202"
COMPANION_PORT = 8000
EP_DROP = "44/0/1"
EP_RESTORE = "44/0/2"
EP_TRAPPED = "44/0/3"
COMPANION_TIMEOUT = 1.0

# Flask server
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8420

# ============================================================

# ----- Serial config -----
if USE_7E1:
    BYTESIZE = serial.SEVENBITS
    PARITY = serial.PARITY_EVEN
    STOPBITS = serial.STOPBITS_ONE
else:
    BYTESIZE = serial.EIGHTBITS
    PARITY = serial.PARITY_NONE
    STOPBITS = serial.STOPBITS_ONE

# Pattern for scale lines like '=6.54000'
PAT = re.compile(r'=\s*([0-9.]+)')


def round_to_step_nearest(x: float, step: float) -> float:
    return round(x / step) * step


def display_round_nearest(actual_kg: float) -> float:
    return round_to_step_nearest(actual_kg * DISPLAY_FACTOR, DISPLAY_STEP_KG)


def reverse_weight_string(raw: str) -> Optional[float]:
    s = raw[::-1]  # incoming reversed digits
    try:
        return float(s)
    except ValueError:
        return None


def press_companion(endpoint: str) -> bool:
    url = f"http://{COMPANION_HOST}:{COMPANION_PORT}/api/location/{endpoint}/press"
    try:
        requests.post(url, timeout=COMPANION_TIMEOUT)
        return True
    except Exception:
        return False


@dataclass
class GameState:
    armed: bool = False

    # ACTUAL readings
    last_seen_kg: Optional[float] = None
    smoothed_kg: Optional[float] = None

    # DISPLAY numbers
    display_kg: Optional[float] = None
    baseline_display_kg: Optional[float] = None

    # Hidden detection baselines (ACTUAL-based)
    arming_actual_kg: Optional[float] = None
    capped_arm_actual_kg: Optional[float] = None
    drop_limit_actual_kg: Optional[float] = None
    restore_limit_actual_kg: Optional[float] = None

    # timers & flags
    is_below: bool = False
    above_start: float = 0.0
    below_start: float = 0.0
    above_limit_start: float = 0.0
    updated: float = 0.0
    last_ascii: str = ""


state = GameState()
history: List[float] = []
lock = threading.Lock()
stop_flag = False


def reader_loop():
    global stop_flag

    try:
        ser = serial.Serial(
            port=COM_PORT,
            baudrate=BAUD,
            bytesize=BYTESIZE,
            parity=PARITY,
            stopbits=STOPBITS,
            timeout=0.2,
            rtscts=False,
            dsrdtr=False,
            xonxoff=False,
        )
    except Exception as e:
        print(f"\n[!] ERROR opening {COM_PORT}: {e}")
        sys.exit(1)

    print(f"\nConnected: {COM_PORT} @ {BAUD} {'7E1' if USE_7E1 else '8N1'}")
    print(f"Arming when ACTUAL ≥ {MIN_TRIGGER_KG:.1f} kg for {STABLE_SECONDS:.1f}s")
    print(f"Display = round_nearest(actual × {DISPLAY_FACTOR:.2f}, {DISPLAY_STEP_KG:.1f}kg)")
    print(f"DROP if ACTUAL < {int(DROP_FACTOR*100)}% of capped arming actual (hold {DROP_HOLDDOWN_S:.2f}s)")
    print(f"RESTORE if ACTUAL ≥ {int(RESTORE_FACTOR*100)}% of capped arming actual (hold {RESTORE_HOLDDOWN_S:.2f}s)")
    print(f"Overlay: http://{LISTEN_HOST}:{LISTEN_PORT}/ API endpoints: /api/state /api/disarm /api/dev/arm/<kg>\n")

    buf = ""
    last_log = 0.0

    while not stop_flag:
        try:
            chunk = ser.read(1024)
            if chunk:
                try:
                    text = chunk.decode("utf-8", errors="ignore")
                except Exception:
                    text = "".join(chr(b) if 32 <= b <= 126 else "" for b in chunk)

                buf += text
                matches = PAT.findall(buf)

                if len(buf) > 256:
                    buf = buf[-64:]

                if matches:
                    for raw in matches[-3:]:
                        actual_kg = reverse_weight_string(raw)
                        if actual_kg is None:
                            continue

                        with lock:
                            state.last_seen_kg = actual_kg
                            state.last_ascii = raw

                            history.append(actual_kg)
                            if len(history) > max(1, SMOOTH_WINDOW):
                                del history[:-SMOOTH_WINDOW]

                            try:
                                state.smoothed_kg = statistics.median(history)
                            except statistics.StatisticsError:
                                state.smoothed_kg = actual_kg

                            state.display_kg = display_round_nearest(state.smoothed_kg)
                            state.updated = time.time()

                            step_state_machine_locked()

            # periodic console status
            now = time.time()
            if now - last_log >= 0.5:
                with lock:
                    disp = state.display_kg
                    if disp is not None:
                        if not state.armed:
                            print(f"display {disp:7.1f} kg | waiting ACTUAL ≥ {MIN_TRIGGER_KG:.1f} for {STABLE_SECONDS:.1f}s",
                                  end="\r")
                        else:
                            side = "below" if state.is_below else "above"
                            drop_l = state.drop_limit_actual_kg or float("nan")
                            rest_l = state.restore_limit_actual_kg or float("nan")
                            base_d = state.baseline_display_kg if state.baseline_display_kg is not None else float("nan")

                            print(
                                f"display {disp:7.1f} | baseline {base_d:7.1f} | "
                                f"ACTUAL drop<{drop_l:6.1f} / restore≥{rest_l:6.1f} ({side})",
                                end="\r"
                            )
                last_log = now

        except KeyboardInterrupt:
            stop_flag = True
        except Exception as e:
            print(f"\n[!] Serial read error: {e}")
            time.sleep(0.2)

    try:
        ser.close()
    except Exception:
        pass
    print("\nSerial closed.")


def step_state_machine_locked():
    now = time.time()
    actual = state.smoothed_kg
    disp = state.display_kg

    if actual is None or disp is None:
        return

    # ----- Not armed yet -----
    if not state.armed:
        if actual >= MIN_TRIGGER_KG:
            if state.above_start == 0.0:
                state.above_start = now

            if (now - state.above_start) >= STABLE_SECONDS:
                state.armed = True
                state.arming_actual_kg = actual
                state.capped_arm_actual_kg = min(actual, 112.0)
                state.drop_limit_actual_kg = state.capped_arm_actual_kg * DROP_FACTOR
                state.restore_limit_actual_kg = state.capped_arm_actual_kg * RESTORE_FACTOR

                if actual > 112.0:
                    state.baseline_display_kg = 100.0
                else:
                    state.baseline_display_kg = display_round_nearest(actual)

                state.is_below = False
                state.below_start = 0.0
                state.above_limit_start = 0.0

                press_companion(EP_TRAPPED)

                print(
                    f"\n[ARMED] ACTUAL={actual:.2f} shown_at_arm={display_round_nearest(actual):.2f} "
                    f"baseline_display={state.baseline_display_kg:.2f} "
                    f"capForThresh={state.capped_arm_actual_kg:.2f} "
                    f"drop<{state.drop_limit_actual_kg:.2f} restore≥{state.restore_limit_actual_kg:.2f}"
                )
        else:
            state.above_start = 0.0

        return

    # ----- Armed: enforce thresholds -----
    drop_limit = state.drop_limit_actual_kg
    restore_limit = state.restore_limit_actual_kg

    if drop_limit is None or restore_limit is None:
        return

    if state.is_below:
        # Look for RESTORE
        if actual >= restore_limit:
            if state.above_limit_start == 0.0:
                state.above_limit_start = now

            if (now - state.above_limit_start) >= RESTORE_HOLDDOWN_S:
                press_companion(EP_RESTORE)
                state.is_below = False
                state.below_start = 0.0
                state.above_limit_start = 0.0
                print(f"\n[RESTORE] actual {actual:.2f} ≥ {restore_limit:.2f}")
        else:
            state.above_limit_start = 0.0

    else:
        # Look for DROP
        if actual < drop_limit:
            if state.below_start == 0.0:
                state.below_start = now

            if (now - state.below_start) >= DROP_HOLDDOWN_S:
                press_companion(EP_DROP)
                state.is_below = True
                state.below_start = 0.0
                state.above_limit_start = 0.0
                print(f"\n[DROP] actual {actual:.2f} < {drop_limit:.2f}")
        else:
            state.below_start = 0.0


def _reset_state():
    with lock:
        state.armed = False
        state.last_seen_kg = None
        state.smoothed_kg = None
        state.display_kg = None
        state.baseline_display_kg = None
        state.arming_actual_kg = None
        state.capped_arm_actual_kg = None
        state.drop_limit_actual_kg = None
        state.restore_limit_actual_kg = None

        state.is_below = False
        state.above_start = 0.0
        state.below_start = 0.0
        state.above_limit_start = 0.0

        state.updated = time.time()
        state.last_ascii = ""
        history.clear()

    print("\n[DISARM] state reset.")


# =================== Flask (HTML + API) ======================

app = Flask(__name__)

HTML = """<!doctype html>
<meta charset="utf-8">
<title>Weight Game</title>
<style>
:root{color-scheme:dark}
html,body{margin:0;height:100%;background:transparent;color:#eee;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif}
.wrap{display:flex;flex-direction:column;justify-content:center;align-items:center;height:100%;text-align:center}
.big{font-size:12vmin;font-weight:800;letter-spacing:.02em;text-shadow:0 0 10px rgba(0,0,0,.35)}
.row{display:flex;gap:1.2rem;margin-top:1.0rem;flex-wrap:wrap;justify-content:center}
.card{min-width:20ch;padding:.6rem 1rem;border:1px solid #333;border-radius:12px;background:rgba(0,0,0,.35)}
.label{font-size:2.2vmin;opacity:.85}
.value{font-size:5.6vmin;font-weight:700;margin-top:.2rem}
.banner{font-size:4.2vmin;font-weight:800;letter-spacing:.04em;margin:1.2rem 0 .6rem;padding:.4rem 1rem;border-radius:12px;border:2px solid #333;background:rgba(0,0,0,.45);display:none}
.banner.on{display:inline-block}
.banner.trapped{border-color:#244;color:#7bd3ff;}
.banner.escape{border-color:#550;color:#ff5f58;}
@keyframes pulse {0%{opacity:1}50%{opacity:.7;filter:drop-shadow(0 0 10px #d00)}100%{opacity:1;filter:none}}
.escape.flash {animation: pulse .9s ease-in-out infinite;}
.note{margin-top:1.0rem;font-size:2.8vmin;color:#ddd;max-width:70vw;text-align:center;line-height:1.2;display:none}
.note.on{display:block}
.twolines{white-space:pre-line}
</style>

<div class="wrap">
  <div class="big" id="kg">--.- kg</div>

  <div id="bannerTrapped" class="banner trapped">BABY TRAPPED!</div>
  <div id="bannerEscape" class="banner escape">BABY TRYING TO ESCAPE</div>

  <div class="row" id="after" style="display:none">
    <div class="card">
      <div class="label">BASELINE</div>
      <div id="baseline" class="value">--.- kg</div>
    </div>
  </div>

  <div id="msg" class="note twolines">
    If the display weight drops below the baseline, all doors will lock until the weight is restored.
  </div>
</div>

<script>
let lastArmed = false;

function fmt1(x){
  return (x!==null&&x!==undefined) ? Number(x).toFixed(1) : "--.-";
}

async function tick(){
  try{
    const r = await fetch('/api/state',{cache:'no-store'});
    const d = await r.json();

    document.getElementById('kg').textContent = fmt1(d.display_kg) + ' kg';

    const armed = !!d.armed;
    const trapped = document.getElementById('bannerTrapped');
    const escape = document.getElementById('bannerEscape');
    const after = document.getElementById('after');
    const msg = document.getElementById('msg');

    if (armed && !lastArmed) {
        after.style.display = 'flex';
        msg.classList.add('on');
    }
    if (!armed && lastArmed) {
        after.style.display = 'none';
        msg.classList.remove('on');
        trapped.classList.remove('on');
        escape.classList.remove('on','flash');
        document.getElementById('baseline').textContent = "--.- kg";
    }
    lastArmed = armed;

    if (armed){
      document.getElementById('baseline').textContent = fmt1(d.baseline_display_kg) + ' kg';

      const currentlyBelow = (d.is_below === true) || (d.below_start > 0);

      trapped.classList.toggle('on', !currentlyBelow);
      escape.classList.toggle('on', currentlyBelow);
      escape.classList.toggle('flash', currentlyBelow);
    }

  }catch(e){ }
}

setInterval(tick, 250);
tick();
</script>
"""


@app.route("/")
def index():
    return render_template_string(HTML)


@app.get("/api/state")
def api_state():
    with lock:
        d = asdict(state)
        d["now"] = time.time()
        d["config"] = dict(
            MIN_TRIGGER_KG=MIN_TRIGGER_KG,
            STABLE_SECONDS=STABLE_SECONDS,
            DISPLAY_FACTOR=DISPLAY_FACTOR,
            DISPLAY_STEP_KG=DISPLAY_STEP_KG,
            DROP_FACTOR=DROP_FACTOR,
            RESTORE_FACTOR=RESTORE_FACTOR,
            DROP_HOLDDOWN_S=DROP_HOLDDOWN_S,
            RESTORE_HOLDDOWN_S=RESTORE_HOLDDOWN_S,
            SMOOTH_WINDOW=SMOOTH_WINDOW,
            EP_DROP=EP_DROP,
            EP_RESTORE=EP_RESTORE,
            EP_TRAPPED=EP_TRAPPED,
        )
        return jsonify(d)


@app.route("/api/disarm", methods=["POST", "GET"])
def api_disarm():
    _reset_state()
    return jsonify(ok=True, msg="disarmed/reset")


@app.route("/api/reset", methods=["POST", "GET"])
def api_reset():
    _reset_state()
    return jsonify(ok=True, msg="reset")


# ====== DEV HELPERS ======

@app.route("/api/dev/arm/<float:actual>", methods=["POST","GET"])
def dev_arm(actual):
    with lock:
        capped = min(actual, 112.0)
        drop = capped * DROP_FACTOR
        rest = capped * RESTORE_FACTOR

        if actual > 112.0:
            baseline_display = 100.0
        else:
            baseline_display = round(
                round((actual * DISPLAY_FACTOR) / DISPLAY_STEP_KG) * DISPLAY_STEP_KG, 2
            )

        state.armed = True
        state.arming_actual_kg = actual
        state.capped_arm_actual_kg = capped
        state.drop_limit_actual_kg = drop
        state.restore_limit_actual_kg= rest

        state.baseline_display_kg = baseline_display
        state.display_kg = baseline_display

        state.is_below = False
        state.above_start = state.below_start = state.above_limit_start = 0.0
        state.updated = time.time()

        return jsonify(
            ok=True,
            armed=True,
            arming_actual=actual,
            baseline_display_kg=baseline_display,
            drop_limit_actual_kg=drop,
            restore_limit_actual_kg=rest
        )


@app.route("/api/dev/disarm", methods=["POST","GET"])
def dev_disarm():
    _reset_state()
    return jsonify(ok=True, armed=False)


def main():
    t = threading.Thread(target=reader_loop, daemon=True)
    t.start()

    print(f"HTTP ready at http://{LISTEN_HOST}:{LISTEN_PORT} "
          f"(/, /api/state, /api/disarm, /api/dev/arm/<kg>)")

    app.run(host=LISTEN_HOST, port=LISTEN_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
