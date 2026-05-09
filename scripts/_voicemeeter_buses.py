"""Print bus state, especially B3."""
import voicemeeterlib

with voicemeeterlib.api("potato") as vm:
    # Buses 5-7 are virtual outputs B1, B2, B3 in Potato
    for i in range(8):
        try:
            b = vm.bus[i]
            mute = getattr(b, "mute", "?")
            gain = getattr(b, "gain", "?")
            label = getattr(b, "label", "") or ""
            mono = getattr(b, "mono", "?")
            mute2 = getattr(b, "_mute", None)
            print(f"bus[{i}] label={label!r} mute={mute} gain={gain} mono={mono}")
        except Exception as e:
            print(f"bus[{i}] err: {e!r}")
    # Try inspecting strip 7 sub-fields
    s = vm.strip[7]
    print(f"\nstrip[7] all attrs:")
    for attr in dir(s):
        if attr.startswith("_"):
            continue
        try:
            v = getattr(s, attr)
            if not callable(v) and not isinstance(v, type):
                print(f"  {attr} = {v!r}")
        except Exception as e:
            print(f"  {attr} err: {e!r}")
