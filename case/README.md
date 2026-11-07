# Buddy box (3D print)

A snug box with a hinged flap lid. Two parts, no supports needed. Black PLA or PETG.

- **Hinge:** the pin is a 4–5 cm piece of 1.75 mm filament pushed through the hinge. Pull it out and the lid comes off.
- **Front:** slits for the light, which sticks onto a thin wall just behind them, plus a finger dip to lift the lid.
- **Lid:** microphone holes and "buddy" cut into the top.
- **Right side:** two notches for the USB cable and the speaker cable. **Left side:** air vents.

The inside size is set at the top of `buddy_box.scad` (`in_w`, `in_d`, `in_h`). Measure the parts first,
set those numbers, then export: `openscad -D 'part="base"' -o base.stl buddy_box.scad` (and `part="lid"`).
Print the base open side up, and the lid flat top down.
