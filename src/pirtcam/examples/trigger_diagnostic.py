"""
Diagnostic script to test trigger mode and basic triggering
Run this BEFORE attempting capture to verify trigger setup
"""

import os
import sys
import time

os.add_dll_directory(r"C:\BitFlow SDK 6.5\Bin64")
os.add_dll_directory(r"C:\Program Files\CameraLink\Serial")

import BFModule.CLComm as CLCom


def send_command(CL, command):
    """Send command and get response"""
    print(f"→ {command}")
    CL.SerialWrite(command + "\r", 100)
    time.sleep(0.1)

    output = ""
    t0 = time.time()
    while time.time() - t0 < 2.0:
        chunk = CL.SerialRead(1, 256)
        if chunk:
            output += chunk.decode("utf-8", errors="ignore")
            if "OK" in output and ">" in output:
                break
        else:
            time.sleep(0.05)

    # Parse response
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    values = [
        line
        for line in lines
        if not line.startswith((">", command.split(":")[0], "ERROR"))
        and line not in ("OK",)
    ]

    if values:
        print(f"← {values[-1]}")
        return values[-1]
    else:
        print(f"← (no response)")
        return None


def main():
    print("=" * 60)
    print("Camera Trigger Mode Diagnostic")
    print("=" * 60)

    # Initialize serial
    CL = CLCom.clsCLAllSerial()
    CL.SerialInit(0)
    CL.SetBaudRate(CLCom.BaudRates.CLBaudRate115200)
    time.sleep(0.2)

    try:
        # Check current trigger settings
        print("\n1. Current Trigger Configuration:")
        print("-" * 40)
        mode = send_command(CL, "SENS:TRIG:MODE?")
        src = send_command(CL, "SENS:TRIG:SRC?")
        count = send_command(CL, "SENS:TRIG:COUNT?")
        sent = send_command(CL, "SENS:TRIG:SENT?")
        status = send_command(CL, "SENS:TRIG?")

        print(f"\nCurrent Settings:")
        print(f"  Mode:   {mode}")
        print(f"  Source: {src}")
        print(f"  Count:  {count}")
        print(f"  Sent:   {sent}")
        print(f"  Status: {status}")

        # Test INT mode triggering
        print("\n2. Testing INT Mode Software Triggering:")
        print("-" * 40)

        # Set to INT mode if not already
        if mode != "INT":
            print("Setting mode to INT...")
            send_command(CL, "SENS:TRIG:MODE INT")
            time.sleep(0.2)
            new_mode = send_command(CL, "SENS:TRIG:MODE?")
            print(f"Mode is now: {new_mode}")

        # Reset triggers
        print("\nResetting trigger state...")
        send_command(CL, "SENS:TRIG OFF")
        time.sleep(0.2)

        # Set count and check
        print("\nSetting COUNT=5...")
        send_command(CL, "SENS:TRIG:COUNT 5")
        time.sleep(0.1)

        count_before = send_command(CL, "SENS:TRIG:COUNT?")
        sent_before = send_command(CL, "SENS:TRIG:SENT?")
        print(f"Before TRIG ON: COUNT={count_before}, SENT={sent_before}")

        # Fire triggers
        print("\nIssuing SENS:TRIG ON...")
        send_command(CL, "SENS:TRIG ON")
        time.sleep(0.3)  # Give camera time to process

        count_after = send_command(CL, "SENS:TRIG:COUNT?")
        sent_after = send_command(CL, "SENS:TRIG:SENT?")
        print(f"After TRIG ON:  COUNT={count_after}, SENT={sent_after}")

        # Analyze results
        print("\n3. Analysis:")
        print("-" * 40)
        try:
            if int(sent_after) == int(sent_before) + int(count_before):
                print("✓ SUCCESS! Triggers fired correctly")
                print(f"  SENT incremented by {count_before} as expected")
            else:
                print("✗ FAILED! Triggers did not fire")
                print(f"  Expected SENT={int(sent_before) + int(count_before)}")
                print(f"  Got SENT={sent_after}")
                print("\nPossible issues:")
                print("  - Camera may be in wrong trigger mode")
                print("  - Trigger source may be set to external")
                print("  - Camera may be in error state")
        except (ValueError, TypeError) as e:
            print(f"✗ Error parsing values: {e}")

        # Test second TRIG ON (should add more triggers)
        print("\n4. Testing Second TRIG ON (should add 5 more):")
        print("-" * 40)
        send_command(CL, "SENS:TRIG ON")
        time.sleep(0.3)

        sent_final = send_command(CL, "SENS:TRIG:SENT?")
        print(f"After 2nd TRIG ON: SENT={sent_final}")

        try:
            expected = int(sent_after) + int(count_after)
            if int(sent_final) == expected:
                print(f"✓ Second TRIG ON worked! SENT={sent_final}")
            else:
                print(f"✗ Second TRIG ON failed. Expected {expected}, got {sent_final}")
        except (ValueError, TypeError):
            pass

        # Cleanup
        print("\n5. Cleanup:")
        print("-" * 40)
        send_command(CL, "SENS:TRIG OFF")
        print("Triggers disabled")

    finally:
        CL.SerialClose()
        print("\nSerial connection closed")

    print("\n" + "=" * 60)
    print("Diagnostic complete")
    print("=" * 60)


if __name__ == "__main__":
    main()
