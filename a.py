import serial

COM_PORT = "COM9"       # change if needed
BAUD     = 4800         # as per your requirement

def main():
    try:
        ser = serial.Serial(
            port=COM_PORT,
            baudrate=BAUD,
            bytesize=serial.EIGHTBITS,   # or SEVENBITS depending on device
            parity=serial.PARITY_NONE,   # or PARITY_EVEN (7E1)
            stopbits=serial.STOPBITS_ONE,
            timeout=1.0
        )
    except Exception as e:
        print("Error opening serial port:", e)
        return

    print(f"Connected to {COM_PORT} @ {BAUD} baud")
    print("Printing raw incoming data... (Ctrl+C to stop)\n")

    try:
        while True:
            data = ser.read(64)  # read up to 64 bytes at a time
            if data:
                text = data.decode("ascii", errors="replace")
                print(text, end="", flush=True)

    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        ser.close()

if __name__ == "__main__":
    main()
