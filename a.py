#!/usr/bin/env python3
"""
Escape-room scale game using continuous serial stream (no newlines).

- Scale sends ASCII like: =0.21000=0.21000...
- We parse numbers, smooth them, and run game logic.

Screen rules:
- Live weight shows the real (smoothed) weight.
- When someone stands on and stays above MIN_TRIGGER_KG for ARM_HOLD_S:
    - System arms.
    - Baseline = 90% of that arming weight (rounded).
- While armed, players must make live weight >= baseline display.
    - If live < baseline for a bit -> DROP (doors lock, alarm).
    - If live >= baseline again for a bit -> RESTORE (doors unlock).

This is written to be lightweight for a weak PC.
"""

import sys
import time
import threading
from dataclasses import dataclass, asdict
from typing import Optional

import serial
import requests
from flask import Flask, jsonify, render_template_string

# ===================== CONFIG =====================

COM_PORT   = r"COM9"     # Change to your port
BAUD       = 4800        # Your scale's baudrate
USE_7E1    = False       # True => 7E1, False => 8N1

# If the raw number from the scale is not in kg, adjust this:
# actual_kg = raw_value * SCALE_FACTOR
SCALE_FACTOR = 1.0       # You can tweak this after testing

# Game thresholds (in kg, AFTER SCALE_FACTOR applied)
MIN_TRIGGER_KG   = 35.0   # weight required to arm the trap
ARM_HOLD_S       = 3.0    # must stay above MIN_TRIGGER_KG this long to arm

DROP_HOLD_S      = 0.40   # must stay below baseline this long to count as drop
RESTORE_HOLD_S   = 0.30   # must stay above baseline this long to restore

# Smoothing (Exponential Moving Average on actual kg)
EMA_ALPHA        = 0.3    # 0 < alpha <= 1; lower = more smoothing

# Display rounding (for both live and baseline)
DISPLAY_STEP_KG  = 0.5    # nearest 0.5 kg

# Baseline factor: 90% of arming weight
BASELINE_FACTOR  = 0.90

# Companion endpoints (doors, alarm, etc.)
COMPANION_HOST   = "192.168.2.202"
COMPANION_PORT   = 8000
EP_DROP          = "44/0/1"
EP_RESTORE       = "44/0/2"
EP_TRAPPED       = "44/0/3"
COMPANION_TIMEOUT = 1.0

# Flask server
LISTEN_HOST      = "0.0.0.0"
LISTEN_PORT      = 8420

# ================= SERIAL SETTINGS =================

if USE_7E1:
    BYTESIZE = serial.SEVENBITS
    PARITY   = serial.PARITY_EVEN
    STOPBITS = serial.STOPBITS_ONE
else:
    BYTESIZE = serial.EIGHTBITS
    PARITY   = serial.PARITY_NONE
    STOPBITS = serial.STOPBITS_ONE

# ==================== STATE ========================

def round_to_step(x: float, step: float) -> float:
    return round(x / step) * step

@dataclass
class GameState:
    # raw & processed weights (kg)
    last_raw_kg: Optional[float] = None
    smoothed_kg: Optional[float] = None

    # What the players see
    display_live_kg: Optional[float] = None
    baseline_display_kg: Optional[float] = None

    # state machine
    armed: bool = False
    fsm_state: str = "WAITING"  # WAITING, ARMING, TRAPPED, ESCAPE_ATTEMPT

    # timers
    arm_start: float = 0.0
    below_start: float = 0.0
    restore_start: float = 0.0

    # misc
    updated: float = 0.0

state = GameState()
lock = threading.Lock()
stop_flag = False

# ================= COMPANION HELPER =================

def press_companion(endpoint: str) -> None:
    url = f"http://{COMPANION_HOST}:{COMPANION_PORT}/api/location/{endpoint}/press"
    try:
        requests.post(url, timeout=COMPANION_TIMEOUT)
    except Exception:
        # Silently ignore errors; we don't want to crash the game.
        pass

def on_state_transition(prev_state: str, new_state: str) -> None:
    """Fire companion events on important transitions."""
    if prev_state != "TRAPPED" and new_state == "TRAPPED":
        # Newly trapped / armed
        press_companion(EP_TRAPPED)
        print("\n[TRAPPED] Trap armed/ready (or restored).")
    elif prev_state == "TRAPPED" and new_state == "ESCAPE_ATTEMPT":
        # Player has dropped below baseline long enough
        press_companion(EP_DROP)
        print("\n[DROP] Weight below baseline long enough -> doors lock, alarm on.")
    elif prev_state == "ESCAPE_ATTEMPT" and new_state == "TRAPPED":
        # Player has restored weight above baseline
        press_companion(EP_RESTORE)
        print("\n[RESTORE] Weight back above baseline -> doors unlock, alarm off.")

# ================= GAME LOGIC ======================

def step_game_logic_locked(actual_kg: float) -> None:
    """
    Called whenever a new actual_kg reading is available.
    Must be called with `lock` already held.
    """
    now = time.time()

    # Update smoothing
    if state.smoothed_kg is None:
        state.smoothed_kg = actual_kg
    else:
        state.smoothed_kg = EMA_ALPHA * actual_kg + (1.0 - EMA_ALPHA) * state.smoothed_kg

    # Live display is the REAL weight (smoothed) rounded to DISPLAY_STEP_KG
    state.display_live_kg = round_to_step(state.smoothed_kg, DISPLAY_STEP_KG)
    W = state.display_live_kg

    # Shortcuts
    baseline = state.baseline_display_kg

    prev_fsm = state.fsm_state

    # ------------- Not armed yet -------------
    if not state.armed:
        if state.smoothed_kg >= MIN_TRIGGER_KG:
            # Above trigger
            if state.fsm_state != "ARMING":
                state.fsm_state = "ARMING"
                state.arm_start = now
            else:
                # Already in ARMING, check hold time
                if (now - state.arm_start) >= ARM_HOLD_S:
                    # Arm the trap
                    state.armed = True
                    state.fsm_state = "TRAPPED"

                    # Baseline = 90% of arming weight, rounded
                    arming_kg = state.smoothed_kg
                    baseline_raw = BASELINE_FACTOR * arming_kg
                    state.baseline_display_kg = round_to_step(baseline_raw, DISPLAY_STEP_KG)

                    print(
                        f"\n[ARMED] arming_kg={arming_kg:.2f}  "
                        f"baseline_display={state.baseline_display_kg:.2f}"
                    )
                    on_state_transition(prev_fsm, state.fsm_state)
        else:
            # Below trigger -> back to waiting
            state.fsm_state = "WAITING"
            state.arm_start = 0.0
    else:
        # ------------- Armed states -------------
        if baseline is None:
            # Shouldn't happen, but guard anyway.
            baseline = 0.0
            state.baseline_display_kg = baseline

        # TRAPPED and ESCAPE_ATTEMPT share the same drop/restore rules.
        if state.fsm_state == "TRAPPED":
            # Check for drop below baseline
            if W < baseline:
                if state.below_start == 0.0:
                    state.below_start = now
                elif (now - state.below_start) >= DROP_HOLD_S:
                    state.fsm_state = "ESCAPE_ATTEMPT"
                    state.below_start = 0.0
                    state.restore_start = 0.0
                    on_state_transition(prev_fsm, state.fsm_state)
            else:
                state.below_start = 0.0

        elif state.fsm_state == "ESCAPE_ATTEMPT":
            # Check for restore (back above baseline)
            if W >= baseline:
                if state.restore_start == 0.0:
                    state.restore_start = now
                elif (now - state.restore_start) >= RESTORE_HOLD_S:
                    state.fsm_state = "TRAPPED"
                    state.restore_start = 0.0
                    state.below_start = 0.0
                    on_state_transition(prev_fsm, state.fsm_state)
            else:
                state.restore_start = 0.0

    state.updated = now

# ================ SERIAL READER ====================

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

    print(f"\nConnected to scale on {COM_PORT} @ {BAUD} {'7E1' if USE_7E1 else '8N1'}")
    print(f"Arms when smoothed weight >= {MIN_TRIGGER_KG:.1f} kg for {ARM_HOLD_S:.1f} s")
    print(f"Baseline = {BASELINE_FACTOR*100:.0f}% of arming weight, rounded to {DISPLAY_STEP_KG:.1f} kg")
    print(f"Players must make LIVE >= BASELINE to keep doors unlocked.\n")
    print(f"Overlay at: http://{LISTEN_HOST}:{LISTEN_PORT}/\n")

    buf = ""
    last_log = 0.0

    try:
        while not stop_flag:
            try:
                chunk = ser.read(64)
                if chunk:
                    try:
                        text = chunk.decode("ascii", errors="ignore")
                    except Exception:
                        text = "".join(chr(b) if 32 <= b <= 126 else "" for b in chunk)

                    buf += text

                    # We don't have newlines; stream looks like "=0.21000=0.21000..."
                    # Extract complete tokens starting with '=' followed by digits and '.'
                    # and leave a small tail in the buffer.
                    i = 0
                    while True:
                        start = buf.find("=", i)
                        if start == -1:
                            break
                        # Read digits and '.' until something else or end-of-buffer
                        j = start + 1
                        while j < len(buf) and (buf[j].isdigit() or buf[j] == "."):
                            j += 1
                        # If we reached end-of-buffer, number may be incomplete; keep it for later
                        if j == len(buf):
                            break
                        # We have a complete token from start to j
                        num_str = buf[start+1:j]
                        i = j  # continue searching after this token

                        if num_str:
                            try:
                                raw_val = float(num_str)
                            except ValueError:
                                continue

                            actual_kg = raw_val * SCALE_FACTOR

                            with lock:
                                state.last_raw_kg = actual_kg
                                step_game_logic_locked(actual_kg)

                    # Prevent buffer from growing forever; keep last 32 chars
                    if len(buf) > 64:
                        buf = buf[-32:]

                # simple console status occasionally
                now = time.time()
                if now - last_log >= 0.5:
                    with lock:
                        live = state.display_live_kg
                        base = state.baseline_display_kg
                        fsm  = state.fsm_state
                    if live is not None:
                        if base is not None:
                            print(
                                f"live {live:7.1f} kg | baseline {base:7.1f} kg | state={fsm:16s}",
                                end="\r",
                            )
                        else:
                            print(
                                f"live {live:7.1f} kg | waiting to arm (state={fsm})",
                                end="\r",
                            )
                    last_log = now

            except KeyboardInterrupt:
                stop_flag = True
            except Exception as e:
                print(f"\n[!] Serial read error: {e}")
                time.sleep(0.2)

    finally:
        try:
            ser.close()
        except Exception:
            pass
        print("\nSerial closed.")

# ================= RESET / DISARM ==================

def reset_state():
    with lock:
        state.last_raw_kg = None
        state.smoothed_kg = None
        state.display_live_kg = None
        state.baseline_display_kg = None
        state.armed = False
        state.fsm_state = "WAITING"
        state.arm_start = 0.0
        state.below_start = 0.0
        state.restore_start = 0.0
        state.updated = time.time()
    print("\n[DISARM] Game state reset.")

# =================== FLASK APP =====================

app = Flask(__name__)

HTML = """
<!doctype html>
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

  .banner{font-size:4.2vmin;font-weight:800;letter-spacing:.04em;margin:1.2rem 0 .6rem;
          padding:.4rem 1rem;border-radius:12px;border:2px solid #333;background:rgba(0,0,0,.45);display:none}
  .banner.on{display:inline-block}
  .banner.trapped{border-color:#244;color:#7bd3ff;}
  .banner.escape{border-color:#550;color:#ff5f58;}
  @keyframes pulse{0%{opacity:1}50%{opacity:.7;filter:drop-shadow(0 0 10px #d00)}100%{opacity:1;filter:none}}
  .escape.flash{animation:pulse .9s ease-in-out infinite;}

  .note{margin-top:1.0rem;font-size:2.8vmin;color:#ddd;max-width:70vw;text-align:center;line-height:1.2;display:none}
  .note.on{display:block}
  .twolines{white-space:pre-line}
</style>

<div class="wrap">
  <!-- LIVE DISPLAY WEIGHT -->
  <div class="big" id="kg">--.- kg</div>

  <!-- Banners -->
  <div id="bannerWaiting" class="banner trapped">STEP ON THE SCALE TO BEGIN</div>
  <div id="bannerTrapped" class="banner trapped">BABY TRAPPED!</div>
  <div id="bannerEscape"  class="banner escape">BABY TRYING TO ESCAPE</div>

  <!-- After armed: show BASELINE -->
  <div class="row" id="after" style="display:none">
    <div class="card">
      <div class="label">BASELINE</div>
      <div id="baseline" class="value">--.- kg</div>
    </div>
  </div>

  <div id="msg" class="note twolines">
If the live weight drops below the baseline,
all doors will lock until the weight is restored.
  </div>
</div>

<script>
let lastArmed = false;

function fmt1(x){
  return (x !== null && x !== undefined) ? Number(x).toFixed(1) : "--.-";
}

async function tick(){
  try{
    const r = await fetch('/api/state', {cache:'no-store'});
    const d = await r.json();

    // Live display
    document.getElementById('kg').textContent = fmt1(d.display_live_kg) + ' kg';

    const armed   = !!d.armed;
    const state   = d.fsm_state || "WAITING";

    const waiting = document.getElementById('bannerWaiting');
    const trapped = document.getElementById('bannerTrapped');
    const escape  = document.getElementById('bannerEscape');
    const after   = document.getElementById('after');
    const msg     = document.getElementById('msg');

    // Banners logic
    waiting.classList.remove('on');
    trapped.classList.remove('on');
    escape.classList.remove('on','flash');

    if (!armed){
      waiting.classList.add('on');
    } else {
      if (state === "TRAPPED"){
        trapped.classList.add('on');
      } else if (state === "ESCAPE_ATTEMPT"){
        escape.classList.add('on','flash');
      }
    }

    // Baseline section visibility
    if (armed){
      after.style.display = 'flex';
      msg.classList.add('on');
      document.getElementById('baseline').textContent = fmt1(d.baseline_display_kg) + ' kg';
    } else {
      after.style.display = 'none';
      msg.classList.remove('on');
      document.getElementById('baseline').textContent = "--.- kg";
    }
  }catch(e){
    // ignore transient errors
  }
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
    return jsonify(d)

@app.route("/api/reset", methods=["POST", "GET"])
def api_reset():
    reset_state()
    return jsonify(ok=True, msg="reset")

# ===================== MAIN ========================

def main():
    t = threading.Thread(target=reader_loop, daemon=True)
    t.start()
    print(f"HTTP ready at http://{LISTEN_HOST}:{LISTEN_PORT}/  (/, /api/state, /api/reset)")
    try:
        app.run(host=LISTEN_HOST, port=LISTEN_PORT, debug=False, threaded=True)
    finally:
        global stop_flag
        stop_flag = True

if __name__ == "__main__":
    main()
