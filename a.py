import serial
import time

# ---------------- CONFIG ----------------
PORT = "COM9"        # change as needed
BAUD = 4800
USE_7E1 = False       # set True if your scale needs 7E1 mode

# ----------------------------------------
if USE_7E1:
    BYTESIZE = serial.SEVENBITS
    PARITY   = serial.PARITY_EVEN
    STOPBITS = serial.STOPBITS_ONE
else:
    BYTESIZE = serial.EIGHTBITS
    PARITY   = serial.PARITY_NONE
    STOPBITS = serial.STOPBITS_ONE


def extract_number(line: str):
    """
    Extract a floating-point number from a line.
    Adjust this if your scale sends reversed digits.
    """
    line = line.strip()

    # Remove leading '=' or other characters
    if '=' in line:
        line = line.replace('=', '').strip()

    # If your device sends numbers reversed, flip them:
    #   e.g. "00045.6" → "6.54000"
    # Uncomment this ONLY if needed:
    # line = line[::-1]

    try:
        return float(line)
    except ValueError:
        return None


def main():
    print(f"Opening {PORT} @ {BAUD} baud...")
    ser = serial.Serial(
        port=PORT,
        baudrate=BAUD,
        bytesize=BYTESIZE,
        parity=PARITY,
        stopbits=STOPBITS,
        timeout=0.2
    )

    buffer = ""

    print("Reading... (Ctrl+C to stop)\n")

    while True:
        try:
            chunk = ser.read(32)   # blocking read
            if not chunk:
                continue

            # Convert to ASCII text
            text = chunk.decode("ascii", errors="ignore")
            buffer += text

            # If no newline yet, keep building buffer
            if "\n" not in buffer and "\r" not in buffer:
                continue

            # Split into full lines
            lines = buffer.splitlines(keepends=False)
            buffer = ""  # reset; the last partial line will be empty or handled

            for line in lines:
                if not line.strip():
                    continue

                value = extract_number(line)
                if value is not None:
                    print(f"Weight: {value:.3f} kg   (raw line: '{line}')")

        except KeyboardInterrupt:
            print("\nStopping...")
            break

        except Exception as e:
            print(f"Error: {e}")
            time.sleep(0.5)


if __name__ == "__main__":
    main()
