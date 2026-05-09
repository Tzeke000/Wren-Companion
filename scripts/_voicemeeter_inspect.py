"""Print full Voicemeeter Potato state so we know A1..A5 routing."""
import voicemeeterlib

with voicemeeterlib.api("potato") as vm:
    print(f"type={vm.type} version={vm.version}")
    # Hardware out devices
    for i in range(5):
        try:
            bus = vm.bus[i]
            print(f"bus[{i}] (Hardware Out A{i+1}): device={bus.device.name!r} mute={bus.mute} gain={bus.gain}")
        except Exception as e:
            print(f"bus[{i}] err: {e!r}")
    # Strip overview
    for i in range(8):
        try:
            s = vm.strip[i]
            routes = " ".join(b for b in ("A1","A2","A3","A4","A5","B1","B2","B3") if getattr(s, b, False))
            label = getattr(s, "label", "") or ""
            mute = getattr(s, "mute", False)
            gain = getattr(s, "gain", 0.0)
            print(f"strip[{i}] label={label!r} mute={mute} gain={gain:.1f} routes=[{routes}]")
        except Exception as e:
            print(f"strip[{i}] err: {e!r}")
