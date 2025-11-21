#!/usr/bin/env python3
"""
Overlay + game logic for “Baby Trapped” using direct serial from scales.

Key points for the scale protocol (your specific device):

- The device sends a continuous byte stream, NO newline terminators.
- Somewhere in the stream appears an '=' character.
- The next 7 characters after '=' form a reversed numeric string:

    Example: "=0.21000"  (7 chars: "0.21000")
    Reversed: "00012.0"  -> float(...) == 12.0 kg actual

So decoding rule is:

    1. Find '=' in the byte stream.
    2. Take the next 7 chars as ASCII.
    3. Reverse that string.
    4. Convert to float => actual kg.

This script:
- Prints raw bytes and parsed frames for debugging.
- Smooths the weight with a simple EMA.
- Uses actual kg as the live display (rounded).
- Uses 90% of the arming actual as the baseline display.
- Compares live display vs baseline display for the trap logic.
"""

import sys
import time
import threading
from dataclasses import dataclass, asdict
from typing import Optional

import requests
import serial
from flask import Flask, jsonify, render_template_string

# ===================== HARD-CODED CONFIG =====================
COM_PORT   = r"COM9"      # your USB adapter port (e.g. "COM9" or "/dev/ttyUSB0")
BAUD       = 4800         # your scale baudrate
USE_7E1    = False        # True => 7E1, False => 8N1

# Arming thresholds (ACTUAL kg)
MIN_TRIGGER_KG   = 35.0   # need at least this to arm
ARM_HOLD_S       = 3.0    # must stay above MIN_TRIGGER_KG this long to arm

# Display rounding
DISPLAY_STEP_KG  = 0.5    # round live & baseline to nearest 0.5 kg

# EMA smoothing factor for actual kg
SMOOTH_ALPHA     = 0.3    # 0..1, higher = more responsive, less smooth

# Drop / restore hold times (in *display space*)
DROP_HOLD_S      = 0.40
RESTORE_HOLD_S   = 0.30

# Companion endpoints
COMPANION_HOST   = "192.168.2.202"
COMPANION_PORT   = 8000
EP_DROP          = "44/0/1"
EP_RESTORE       = "44/0/2"
EP_TRAPPED       = "44/0/3"
COMPANION_TIMEOUT = 1.0

# Flask server
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8420
# ============================================================

# ----- Serial mode (pyserial) -----
if USE_7E1:
    BYTESIZE = serial.SEVENBITS
    PARITY   = serial.PARITY_EVEN
    STOPBITS = serial.STOPBITS_ONE
else:
    BYTESIZE = serial.EIGHTBITS
    PARITY   = serial.PARITY_NONE
    STOPBITS = serial.STOPBITS_ONE


def round_to_step_nearest(x: float, step: float) -> float:
    return round(x / step) * step


def press_companion(endpoint: str) -> bool:
    """
    Fire a Companion endpoint. Same semantics as your original file.
    """
    url = f"http://{COMPANION_HOST}:{COMPANION_PORT}/api/location/{endpoint}/press"
    try:
        requests.post(url, timeout=COMPANION_TIMEOUT)
        return True
    except Exception as e:
        print(f"[WARN] Companion press to {endpoint} failed: {e}")
        return False


@dataclass
class GameState:
    # high-level state
    armed: bool = False
    phase: str = "WAITING"   # "WAITING", "ARMING", "TRAPPED", "ESCAPE_ATTEMPT"

    # raw / actual
    last_raw_kg: Optional[float] = None      # decoded from the scale protocol
    smoothed_kg: Optional[float] = None      # EMA of actual

    # display
    display_kg: Optional[float] = None       # what players see (rounded actual)
    baseline_display_kg: Optional[float] = None  # 90% of arming actual, rounded

    # baselines
    arming_actual_kg: Optional[float] = None

    # timers
    arm_start: float = 0.0          # when we first went above trigger (for arming)
    drop_start: float = 0.0         # when display first fell below baseline
    restore_start: float = 0.0      # when display first went back above baseline

    # meta
    updated: float = 0.0            # last update timestamp

    # for debugging
    last_segment: str = ""          # last 7-char segment after '='
    last_segment_reversed: str = "" # reversed string used for parsing


state = GameState()
lock = threading.Lock()
stop_flag = False


def decode_weight_from_stream(buffer: bytearray):
    """
    Consume bytes from 'buffer', looking for '=' and the next 7 characters.
    Whenever a full frame is found, yield (actual_kg, raw_segment, reversed_segment).

    This function mutates 'buffer': it removes bytes that have been processed.
    """

    weights = []

    while True:
        try:
            idx = buffer.index(ord('='))
        except ValueError:
            # '=' not found; keep only the tail to avoid unbounded growth
            if len(buffer) > 32:
                del buffer[:-32]
            break

        # Check if we have '=' plus 7 following bytes
        if idx + 8 <= len(buffer):
            # segment is the 7 chars after '='
            seg_bytes = buffer[idx+1:idx+8]
            # drop everything up through the segment
            del buffer[:idx+8]

            try:
                seg = seg_bytes.decode("ascii", errors="ignore")
            except Exception:
                continue

            if len(seg) != 7:
                # malformed frame; skip
                continue

            rev = seg[::-1]
            try:
                actual = float(rev)
                
                # --- NEW RULE: weight is 10% less ---
                actual = actual * 0.9

            except ValueError:
                # couldn’t parse as float; skip
                continue

            weights.append((actual, seg, rev))
        else:
            # not enough bytes yet; keep from '=' onwards
            if idx > 0:
                del buffer[:idx]
            break

    return weights


def reader_loop():
    """
    Serial reader: reads bytes at 4800 baud, decodes weight frames from the
    continuous stream, updates GameState, and steps the state machine.

    Also prints raw bytes and parsed frames so you can see exactly what’s happening.
    """
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
    print("Decoding rule: '=' + 7 chars, reverse them, parse float -> actual kg.")
    print(f"Arming when actual ≥ {MIN_TRIGGER_KG:.1f} kg for {ARM_HOLD_S:.1f}s\n")
    print(f"HTTP overlay at http://{LISTEN_HOST}:{LISTEN_PORT}/\n")

    buf = bytearray()
    last_log = 0.0

    while not stop_flag:
        try:
            chunk = ser.read(64)
            if chunk:
                # print raw bytes so you can verify the stream
                print(f"RAW BYTES: {chunk.hex(' ')} | {repr(chunk)}")

                buf.extend(chunk)
                frames = decode_weight_from_stream(buf)

                for actual_kg, seg, rev in frames:
                    now = time.time()
                    with lock:
                        state.last_raw_kg = actual_kg
                        state.last_segment = seg
                        state.last_segment_reversed = rev
                        state.updated = now

                        # EMA smoothing
                        if state.smoothed_kg is None:
                            state.smoothed_kg = actual_kg
                        else:
                            state.smoothed_kg = (
                                SMOOTH_ALPHA * actual_kg +
                                (1.0 - SMOOTH_ALPHA) * state.smoothed_kg
                            )

                        # Display weight: real weight, rounded to nearest 0.5 kg
                        state.display_kg = round_to_step_nearest(
                            state.smoothed_kg, DISPLAY_STEP_KG
                        )

                        # Debug print of the decoded frame
                        print(
                            f"PARSED FRAME: seg='{seg}' -> rev='{rev}' -> "
                            f"actual={actual_kg:.3f} kg, display={state.display_kg:.1f} kg"
                        )

                        # Step game state machine
                        step_state_machine_locked(now)

            # periodic console status (every 0.5s)
            now = time.time()
            if now - last_log >= 0.5:
                with lock:
                    disp = state.display_kg
                    base = state.baseline_display_kg
                    phase = state.phase
                    if disp is None:
                        msg = "display: --.- kg"
                    else:
                        msg = f"display: {disp:6.1f} kg"

                    if base is None:
                        base_msg = "baseline: --.- kg"
                    else:
                        base_msg = f"baseline: {base:6.1f} kg"

                    print(
                        f"{msg} | {base_msg} | phase={phase:<16}",
                        end="\r",
                        flush=True,
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


def step_state_machine_locked(now: float):
    """
    Game state machine.

    - Arming uses actual kg vs MIN_TRIGGER_KG.
    - Baseline is 90% of arming actual (rounded).
    - Drop / restore use display_kg vs baseline_display_kg, in *display space*
      so what players see is exactly what the logic uses.
    """
    actual = state.smoothed_kg
    disp   = state.display_kg

    if actual is None or disp is None:
        return

    # ---- PHASE: WAITING / ARMING (arming based on actual kg) ----
    if state.phase in ("WAITING", "ARMING") and not state.armed:
        if actual >= MIN_TRIGGER_KG:
            if state.phase == "WAITING":
                state.phase = "ARMING"
                state.arm_start = now
                print(f"\n[ARMING] actual={actual:.2f} kg ≥ {MIN_TRIGGER_KG:.1f} kg")

            if (now - state.arm_start) >= ARM_HOLD_S:
                # ARM NOW
                state.armed = True
                state.phase = "TRAPPED"
                state.arming_actual_kg = actual

                # --- NEW RULE: baseline capped at 100 kg ---
                baseline_actual = 1.0 * actual
                baseline_actual = min(baseline_actual, 100.0)
                state.baseline_display_kg = round_to_step_nearest(
                    baseline_actual, DISPLAY_STEP_KG
                )
 
                state.drop_start = 0.0
                state.restore_start = 0.0

                press_companion(EP_TRAPPED)
                print(
                    f"\n[ARMED] actual={actual:.2f} kg | baseline_display="
                    f"{state.baseline_display_kg:.1f} kg (90% of arming actual)"
                )
        else:
            if state.phase == "ARMING":
                print("\n[ARMING CANCELLED] actual dropped below trigger.")
            state.phase = "WAITING"
            state.arm_start = 0.0
        return

    # If we're here and not armed, nothing to do
    if not state.armed or state.baseline_display_kg is None:
        return

    baseline = state.baseline_display_kg
    W = disp
    B = baseline

    # ---- PHASE: TRAPPED -> ESCAPE_ATTEMPT (drop) ----
    if state.phase == "TRAPPED":
        if W < B:
            if state.drop_start == 0.0:
                state.drop_start = now
            if (now - state.drop_start) >= DROP_HOLD_S:
                state.phase = "ESCAPE_ATTEMPT"
                state.drop_start = 0.0
                state.restore_start = 0.0
                press_companion(EP_DROP)
                print(
                    f"\n[DROP] display {W:.2f} < baseline {B:.2f} "
                    f"(held {DROP_HOLD_S:.2f}s) -> ESCAPE_ATTEMPT"
                )
        else:
            state.drop_start = 0.0
        return

    # ---- PHASE: ESCAPE_ATTEMPT -> TRAPPED (restore) ----
    if state.phase == "ESCAPE_ATTEMPT":
        if W >= B:
            if state.restore_start == 0.0:
                state.restore_start = now
            if (now - state.restore_start) >= RESTORE_HOLD_S:
                prev_phase = state.phase
                state.phase = "TRAPPED"
                state.restore_start = 0.0
                state.drop_start = 0.0
                press_companion(EP_RESTORE)
                print(
                    f"\n[RESTORE] display {W:.2f} ≥ baseline {B:.2f} "
                    f"(held {RESTORE_HOLD_S:.2f}s) -> TRAPPED"
                )
        else:
            state.restore_start = 0.0


def _reset_state():
    with lock:
        state.armed = False
        state.phase = "WAITING"
        state.last_raw_kg = None
        state.smoothed_kg = None
        state.display_kg = None
        state.baseline_display_kg = None
        state.arming_actual_kg = None
        state.arm_start = 0.0
        state.drop_start = 0.0
        state.restore_start = 0.0
        state.updated = time.time()
        state.last_segment = ""
        state.last_segment_reversed = ""
    print("\n[DISARM] state reset.")


# =================== Flask (HTML + API) ======================
app = Flask(__name__)

HTML = """
<!doctype html><meta charset="utf-8"><title>Weight Game</title>
<style>
  :root{color-scheme:dark}
  html,body{margin:0;height:100%;background:transparent;color:#eee;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif}
  .wrap{display:flex;flex-direction:column;justify-content:center;align-items:center;height:100%;text-align:center}

  .big{font-size:12vmin;font-weight:800;letter-spacing:.02em; text-shadow:0 0 10px rgba(0,0,0,.35)}
  .row{display:flex;gap:1.2rem;margin-top:1.0rem;flex-wrap:wrap;justify-content:center}
  .card{min-width:20ch;padding:.6rem 1rem;border:1px solid #333;border-radius:12px;background:rgba(0,0,0,.35)}
  .label{font-size:2.2vmin;opacity:.85}
  .value{font-size:5.6vmin;font-weight:700;margin-top:.2rem}

  .banner{font-size:4.2vmin;font-weight:800;letter-spacing:.04em;margin:1.2rem 0 .6rem;padding:.4rem 1rem;border-radius:12px;border:2px solid #333;background:rgba(0,0,0,.45);display:none}
  .banner.on{display:inline-block}
  .banner.trapped{border-color:#244; color:#7bd3ff;}
  .banner.escape{border-color:#550; color:#ff5f58;}
  @keyframes pulse { 0%{opacity:1} 50%{opacity:.7; filter:drop-shadow(0 0 10px #d00)} 100%{opacity:1; filter:none} }
  .escape.flash { animation: pulse .9s ease-in-out infinite; }

  .note{margin-top:1.0rem;font-size:2.8vmin;color:#ddd;max-width:70vw;text-align:center;line-height:1.2;display:none}
  .note.on{display:block}
  .twolines{white-space:pre-line}
</style>

<div class="wrap">
  <!-- LIVE DISPLAY WEIGHT (real weight, rounded to nearest step) -->
  <div class="big" id="kg">--.- kg</div>

  <!-- Either/Or banners when ARMED -->
  <div id="bannerTrapped" class="banner trapped">BABY TRAPPED!</div>
  <div id="bannerEscape"  class="banner escape">BABY TRYING TO ESCAPE</div>

  <!-- After armed: show BASELINE (display baseline only) -->
  <div class="row" id="after" style="display:none">
    <div class="card">
      <div class="label">BASELINE</div>
      <div id="baseline" class="value">--.- kg</div>
    </div>
  </div>

  <!-- Centered, forced two lines -->
  <div id="msg" class="note twolines">
    If the display weight drops below the baseline,
    all doors will lock until the weight is restored.
  </div>
</div>

<script>
let lastArmed = false;

function fmt1(x){
  if (x === null || x === undefined){ return "--.-"; }
  return Number(x).toFixed(1);
}

async function tick(){
  try{
    const r = await fetch('/api/state',{cache:'no-store'});
    const d = await r.json();

    // Live display
    document.getElementById('kg').textContent = fmt1(d.display_kg) + ' kg';

    const armed   = !!d.armed;
    const phase   = d.phase || "WAITING";
    const trapped = document.getElementById('bannerTrapped');
    const escape  = document.getElementById('bannerEscape');
    const after   = document.getElementById('after');
    const msg     = document.getElementById('msg');

    // Flip UI on arming
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

      const currentlyBelow = (phase === "ESCAPE_ATTEMPT");
      trapped.classList.toggle('on', !currentlyBelow);
      escape.classList.toggle('on',  currentlyBelow);
      escape.classList.toggle('flash', currentlyBelow);
    }
  }catch(e){ /* ignore transient fetch errors */ }
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
        # Keep a config block so external tools can still introspect if needed.
        d["config"] = dict(
            MIN_TRIGGER_KG=MIN_TRIGGER_KG,
            ARM_HOLD_S=ARM_HOLD_S,
            DISPLAY_STEP_KG=DISPLAY_STEP_KG,
            DROP_HOLD_S=DROP_HOLD_S,
            RESTORE_HOLD_S=RESTORE_HOLD_S,
        )
    return jsonify(d)

@app.route("/api/disarm", methods=["POST","GET"])
def api_disarm():
    _reset_state()
    return jsonify(ok=True, msg="disarmed/reset")

@app.route("/api/reset", methods=["POST","GET"])
def api_reset():
    _reset_state()
    return jsonify(ok=True, msg="reset")

# ====== DEV HELPERS (for remote testing) ======
@app.route("/api/dev/arm/<float:actual>", methods=["POST","GET"])
def dev_arm(actual):
    with lock:
        baseline_actual = 0.90 * actual
        baseline_display = round_to_step_nearest(baseline_actual, DISPLAY_STEP_KG)

        state.armed = True
        state.phase = "TRAPPED"
        state.arming_actual_kg     = actual
        state.baseline_display_kg  = baseline_display
        state.display_kg           = baseline_display
        state.smoothed_kg          = actual
        state.last_raw_kg          = actual
        state.arm_start = state.drop_start = state.restore_start = 0.0
        state.updated = time.time()
    return jsonify(
        ok=True, armed=True,
        arming_actual=actual,
        baseline_display_kg=baseline_display,
    )

@app.route("/api/dev/disarm", methods=["POST","GET"])
def dev_disarm():
    _reset_state()
    return jsonify(ok=True, armed=False)

def main():
    t = threading.Thread(target=reader_loop, daemon=True)
    t.start()
    print(
        f"HTTP ready at http://{LISTEN_HOST}:{LISTEN_PORT}  "
        f"(/, /api/state, /api/disarm, /api/dev/arm/<kg>)"
    )
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, debug=False, threaded=True)

if __name__ == "__main__":
    main()
