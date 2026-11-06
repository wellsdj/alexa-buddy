# Buddy box (3D print)

Two parts, no supports needed. Print in black PLA or PETG.

| File | Print it | Size |
|---|---|---|
| `buddy_box_base.stl` | open side up | 130 × 100 × 60 mm |
| `buddy_box_lid.stl` | flat top down on the bed, lip pointing up | 130 × 100 × 9 mm |

Suggested settings: 0.2 mm layers, 15–20% infill, 3+ walls. Roughly 175 g of filament in total.

What's on it:
- **Front:** a row of slits. The light sits behind them and glows through. Inside there's a thin wall
  6 mm behind the front; stick the light onto it, facing the slits (centre of the light about 25 mm up from the floor).
- **Lid:** microphone holes in the middle (tape the mic under them, facing up) and "buddy" cut into the top.
- **Back:** two notches at the top edge for the USB power cable and the audio cable (just lay the cables in,
  then put the lid on), plus air vents.
- Inside space: about 125 × 95 mm and 50 mm tall, which fits a half-size breadboard, the board and the sound board.
- The lid is a push fit. Too tight: set `fit` bigger in `buddy_box.scad` (e.g. 0.35). Too loose: a bit of tape on the lip.

Change any size in `buddy_box.scad` (free OpenSCAD app), then File → Export → STL.
