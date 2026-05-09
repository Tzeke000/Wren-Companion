"""Configure Voicemeeter Potato so VAIO3 input routes to B3 output bus.

Uses the voicemeeterlib Python wrapper around VoicemeeterRemote64.dll.

Strip layout in Potato:
  0..4 = Hardware Input 1..5
  5    = Voicemeeter Virtual Input (VAIO)
  6    = Voicemeeter VAIO2 Input
  7    = Voicemeeter VAIO3 Input  ← target
"""
import sys
import time

import voicemeeterlib

KIND = "potato"  # Voicemeeter Potato
VAIO3_STRIP_INDEX = 7  # documented Potato strip index for VAIO3

with voicemeeterlib.api(KIND) as vm:
    # Don't reload Voicemeeter — connect to existing instance.
    print(f"voicemeeter type={vm.type} version={vm.version}")
    strip = vm.strip[VAIO3_STRIP_INDEX]
    print(f"strip[{VAIO3_STRIP_INDEX}] label={strip.label!r}")
    # Print all bus routing flags currently set
    routes = {}
    for b in ("A1", "A2", "A3", "A4", "A5", "B1", "B2", "B3"):
        try:
            routes[b] = bool(getattr(strip, b))
        except AttributeError:
            routes[b] = "n/a"
    print(f"strip[{VAIO3_STRIP_INDEX}] before: {routes}")

    # Enable B3
    strip.B3 = True
    # Voicemeeter API needs a small settle wait before reading back
    time.sleep(0.3)
    routes2 = {b: bool(getattr(strip, b, False)) for b in routes if routes[b] != "n/a"}
    print(f"strip[{VAIO3_STRIP_INDEX}] after : {routes2}")

    # Save current state so the routing persists across Voicemeeter restarts
    try:
        vm.command.save("D:\\AvaAgentv2\\state\\voicemeeter_ava_loop.xml")
        print("saved Voicemeeter state to state/voicemeeter_ava_loop.xml")
    except Exception as e:
        print(f"save warn: {e!r}")

print("done")
