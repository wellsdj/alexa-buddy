# Buddy box (3D print)

A black box with a flap lid on a hinge. **Two files, no supports needed.**

| File | How it goes on the bed | Size |
|---|---|---|
| `buddy_box_base.stl` | open side up (as it comes) | 114 × 86 × 44 mm (with a 12 mm button hole in the left side) |
| `buddy_box_lid.stl` | flat top on the bed, rim pointing up (as it comes) | 114 × 86 × 6.4 mm |

Both fit a 15 × 15 cm bed, together or one at a time.

## Print settings
- Black PLA (PETG works too)
- 0.2 mm layers, 0.4 mm nozzle
- 3 walls, 15–20% infill
- **Supports: off.** Brim: optional (helps the corners stay flat).
- About 90 g of filament in total.

## Putting it together
1. **Hinge pin:** cut a 12 cm piece of 1.75 mm filament (the same filament it was printed with). Line up the lid's hinge
   with the box's hinge and push the filament through all five parts from one side. Snip off what sticks out,
   leaving 1 mm each end. Touch each end with a hot soldering iron or lighter for a second so it can't slide out.
   To take the lid off later, cut one end and pull the pin out.
2. **Light:** stick it onto the thin wall just behind the front slits, shining forwards.
3. **Microphone:** tape it under the lid, right below the little holes, facing up.
4. **Button:** push the 12 mm push button through the round hole in the left side from the inside and tighten its nut on the outside.
   Two of its wires go to pin 0 and GND on the board (it works like the board's BOOT button). Leave the light-ring wires unconnected and tape their ends.
5. **Cables:** the USB cable and the speaker cable go out through the two notches on the right side. Lay them in, then close the lid.
6. Open the lid with a finger in the dip at the front.

## Inside space
110 × 70 mm, 40 mm tall. To change any size, edit the numbers at the top of `buddy_box.scad`
(free OpenSCAD app), then export: `openscad -D 'part="base"' -o base.stl buddy_box.scad` (and `part="lid"`).
