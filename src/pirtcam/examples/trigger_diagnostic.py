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
        result = values[-1]
        print(f"← {result}")
        return result
    else:
        print(f"← (OK)")
        return None


def query_trigger_state(CL):
    """Query all trigger-related states and return as dict"""
    status = send_command(CL, "SENS:TRIG?")
    count = send_command(CL, "SENS:TRIG:COUNT?")
    sent = send_command(CL, "SENS:TRIG:SENT?")
    return {"status": status, "count": count, "sent": sent}


def main():
    print("=" * 70)
    print("Camera Trigger Mode Diagnostic with Timing Analysis")
    print("=" * 70)

    # Initialize serial
    CL = CLCom.clsCLAllSerial()
    CL.SerialInit(0)
    CL.SetBaudRate(CLCom.BaudRates.CLBaudRate115200)
    time.sleep(0.2)

    try:
        # Check current trigger settings
        print("\n1. Current Trigger Configuration:")
        print("-" * 70)
        mode = send_command(CL, "SENS:TRIG:MODE?")
        src = send_command(CL, "SENS:TRIG:SOURCE?")

        state = query_trigger_state(CL)

        print(f"\nCurrent Settings:")
        print(f"  Mode:   {mode}")
        print(f"  Source: {src} (only matters if Mode=FSYNC)")
        print(f"  Status: {state['status']}")
        print(f"  Count:  {state['count']}")
        print(f"  Sent:   {state['sent']}")

        # Get exposure and frame times
        exp = send_command(CL, "SENS:EXPPER?")
        frame = send_command(CL, "SENS:FRAMEPER?")
        exp_s = None
        frame_s = None
        if exp:
            try:
                CLOCK_FREQ = 15.0  # MHz
                current_cycles = int(exp)
                exp_s = current_cycles / (CLOCK_FREQ * 1e6)
                print(f"  Exposure: {exp_s:.6f}s ({exp} cycles)")
            except:
                pass
        if frame:
            try:
                CLOCK_FREQ = 15.0  # MHz
                frame_cycles = int(frame)
                frame_s = frame_cycles / (CLOCK_FREQ * 1e6)
                print(f"  Frame Period: {frame_s:.6f}s ({frame} cycles)")
            except:
                pass

        # Test INT mode triggering
        print("\n2. Setting Up Test:")
        print("-" * 70)

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

        print("\nState before TRIG ON:")
        state_before = query_trigger_state(CL)
        print(f"  STATUS: {state_before['status']}")
        print(f"  COUNT:  {state_before['count']}")
        print(f"  SENT:   {state_before['sent']}")

        # Fire triggers and MONITOR
        print("\n3. Issuing TRIG ON and Monitoring:")
        print("-" * 70)

        if exp_s:
            expected_time = exp_s + 1.0  # exposure + ~1s overhead
            timeout = max(15.0, exp_s * 2 + 5.0)  # Dynamic timeout based on exposure
            print(
                f"\nExpected trigger time: ~{expected_time:.2f}s (exposure + overhead)"
            )
            print(f"Monitoring timeout: {timeout:.1f}s")
        else:
            timeout = 15.0
            print(f"\nMonitoring timeout: {timeout:.1f}s (exposure time unknown)")

        print("\nIssuing SENS:TRIG ON...")
        t_start = time.time()
        send_command(CL, "SENS:TRIG ON")

        print(f"\n{'Time(s)':<8} {'STATUS':<8} {'COUNT':<8} {'SENT':<8} {'Notes'}")
        print("-" * 70)

        # Initial immediate check
        t_now = time.time() - t_start
        state_now = query_trigger_state(CL)
        print(
            f"{t_now:7.2f}  {state_now['status']:<8} {state_now['count']:<8} {state_now['sent']:<8} Immediate"
        )

        previous_sent = state_now["sent"]
        trigger_fired_at = None

        # Monitor until timeout or SENT changes
        check_interval = 0.5

        while (time.time() - t_start) < timeout:
            time.sleep(check_interval)
            t_now = time.time() - t_start

            state_now = query_trigger_state(CL)

            # Check if SENT changed
            notes = ""
            if state_now["sent"] != previous_sent and trigger_fired_at is None:
                trigger_fired_at = t_now
                notes = "✓ SENT CHANGED!"
                if exp_s:
                    timing_note = f" (expected ~{exp_s + 1.0:.2f}s)"
                    if abs(t_now - (exp_s + 1.0)) < 2.0:
                        notes += timing_note + " ✓ timing matches"
                    else:
                        notes += timing_note + " ⚠ timing differs"
            elif trigger_fired_at is not None:
                notes = f"(stable, {t_now - trigger_fired_at:.1f}s since change)"

            print(
                f"{t_now:7.2f}  {state_now['status']:<8} {state_now['count']:<8} {state_now['sent']:<8} {notes}"
            )

            # If we detected the change, wait a bit more then break
            if trigger_fired_at is not None and (t_now - trigger_fired_at) > 1.0:
                print(f"{'':<8}  ... SENT stabilized, ending monitoring")
                break

            previous_sent = state_now["sent"]

        # Final state
        print("\n4. Final State After TRIG ON:")
        print("-" * 70)
        state_after = query_trigger_state(CL)
        print(f"  STATUS: {state_before['status']} → {state_after['status']}")
        print(f"  COUNT:  {state_before['count']} → {state_after['count']}")
        print(f"  SENT:   {state_before['sent']} → {state_after['sent']}")

        # Analysis
        print("\n5. Analysis:")
        print("-" * 70)
        try:
            sent_before = int(state_before["sent"] or 0)
            sent_after = int(state_after["sent"] or 0)
            count_before = int(state_before["count"] or 0)
            expected = sent_before + count_before

            if trigger_fired_at is not None:
                print(f"✓ TRIGGER DETECTED!")
                print(f"  Time to SENT change: {trigger_fired_at:.2f} seconds")
                print(
                    f"  SENT increment: {sent_after - sent_before} (expected {count_before})"
                )

                if exp_s:
                    expected_time = exp_s + 1.0
                    timing_diff = abs(trigger_fired_at - expected_time)
                    print(
                        f"  Expected time: ~{expected_time:.2f}s (exposure + overhead)"
                    )
                    print(f"  Timing difference: {timing_diff:.2f}s")
                    if timing_diff < 2.0:
                        print(f"  ✓ Timing matches expected pattern")
                    else:
                        print(f"  ⚠ Timing differs from expected - may need adjustment")

                if sent_after == expected:
                    print(f"  ✓ SENT value is correct!")
                else:
                    print(
                        f"  ⚠ SENT value unexpected (got {sent_after}, expected {expected})"
                    )
            else:
                print(f"✗ NO TRIGGER DETECTED!")
                print(f"  SENT did not change after {timeout}s")
                print(f"  Expected SENT to increment from {sent_before} to {expected}")
                print("\nPossible issues:")
                print(
                    "  - Camera may not be responding to software triggers in INT mode"
                )
                print("  - SOC configuration may have non-standard behavior")
                print("  - Trigger might require frame grabber to be armed first")
                print("  - Camera might be in an error state")

                # Check for errors
                error = send_command(CL, "SYS:ERR?")
                if error and error != "0":
                    print(f"  ! Camera error detected: {error}")
        except (ValueError, TypeError) as e:
            print(f"✗ Error parsing values: {e}")

        # Test second TRIG ON if first worked
        if trigger_fired_at is not None:
            print("\n6. Testing Second TRIG ON (should add 5 more):")
            print("-" * 70)
            print("Issuing second SENS:TRIG ON...")

            sent_before_2nd = state_after["sent"]
            t_start_2nd = time.time()
            send_command(CL, "SENS:TRIG ON")

            # Quick check
            time.sleep(0.5)
            state_2nd = query_trigger_state(CL)

            time_2nd = time.time() - t_start_2nd
            print(f"After {time_2nd:.2f}s: SENT={state_2nd['sent']}")

            try:
                if int(state_2nd["sent"]) > int(sent_before_2nd):
                    print(f"✓ Second TRIG ON worked! SENT incremented again")
                else:
                    print(f"✗ Second TRIG ON had no effect")
            except (ValueError, TypeError):
                pass

        # Cleanup
        print("\n7. Cleanup:")
        print("-" * 70)
        send_command(CL, "SENS:TRIG OFF")
        final_state = query_trigger_state(CL)
        print(f"Triggers disabled")
        print(f"Final SENT: {final_state['sent']}")

    finally:
        CL.SerialClose()
        print("\nSerial connection closed")

    print("\n" + "=" * 70)
    print("Diagnostic complete")
    print("=" * 70)

    if trigger_fired_at:
        print(f"\n┌{'─' * 68}┐")
        print(f"│ RESULT: Triggers work! ✓{' ' * 44}│")
        print(f"├{'─' * 68}┤")
        print(
            f"│ Time to SENT update: {trigger_fired_at:.2f}s{' ' * (45 - len(f'{trigger_fired_at:.2f}'))}│"
        )
        if exp_s:
            print(f"│ Exposure time: {exp_s:.3f}s{' ' * (51 - len(f'{exp_s:.3f}'))}│")
            recommended_wait = max(trigger_fired_at + 1.0, exp_s + 2.0)
            print(
                f"│ Recommended wait: {recommended_wait:.2f}s{' ' * (46 - len(f'{recommended_wait:.2f}'))}│"
            )
        print(f"└{'─' * 68}┘")
        print("\nIn your capture code, wait at least this long after TRIG ON")
        print("before expecting SENT to update.")
    else:
        print(f"\n┌{'─' * 68}┐")
        print(f"│ RESULT: Triggers NOT working ✗{' ' * 40}│")
        print(f"└{'─' * 68}┘")
        print("\nCamera not responding to TRIG ON in INT mode.")
        print("Further investigation needed - try manufacturer software.")


if __name__ == "__main__":
    main()
