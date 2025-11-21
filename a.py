import serial

PORT = "COM9"      # <-- change to your port name
BAUD = 4800

ser = serial.Serial(
    port=PORT,
    baudrate=BAUD,
    bytesize=serial.EIGHTBITS,
    parity=serial.PARITY_NONE,
    stopbits=serial.STOPBITS_ONE,
    timeout=1.0,          # 1 second read timeout
)

print(f"Opened {PORT} at {BAUD} baud. Printing raw data... (Ctrl+C to stop)")

try:
    while True:
        # Read up to one "line" – many indicators end frames with \r or \n
        data = ser.readline()

        if not data:
            continue

        # Print *exactly* what came in
        # repr() shows escape codes; .hex() shows bytes if you need it
        print(f"RAW: {repr(data)}  HEX: {data.hex(' ')}")

except KeyboardInterrupt:
    pass
finally:
    ser.close()
    print("\nPort closed.")
