import serial

COM_PORT = "COM9"   # change if needed
BAUD     = 4800

def main():
    ser = serial.Serial(
        port=COM_PORT,
        baudrate=BAUD,
        bytesize=serial.EIGHTBITS,   # or SEVENBITS if your scale needs 7E1
        parity=serial.PARITY_NONE,   # or PARITY_EVEN for 7E1
        stopbits=serial.STOPBITS_ONE,
        timeout=1.0,
    )

    print(f"Opened {COM_PORT} @ {BAUD} baud")
    try:
        while True:
            chunk = ser.read(64)    # read up to 64 bytes (blocking until something arrives or timeout)
            if chunk:
                # raw bytes – exactly as received
                print(repr(chunk))
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()
        print("\nSerial closed.")

if __name__ == "__main__":
    main()