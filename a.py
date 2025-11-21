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
        data = ser.read(32)   # read up to 32 bytes
        if data:
            print(f"RAW: {repr(data)}  HEX: {data.hex(' ')}")

except KeyboardInterrupt:
    pass
finally:
    ser.close()
    print("\nPort closed.")
