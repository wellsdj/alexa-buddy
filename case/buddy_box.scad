// Buddy box: a base and a lid, for 3D printing in black.
// Prints with NO supports. Base: open side up. Lid: flat top down on the bed (lip pointing up).
// All sizes in mm. Change the numbers below and re-export if something doesn't fit.

part = "both";        // "base", "lid" or "both"

W = 130;              // outside width  (left-right)
D = 100;              // outside depth  (front-back)
H = 60;               // base height
R = 10;               // corner roundness
wall = 2.4;           // wall thickness (6 lines with a 0.4 nozzle)
floor_t = 2.4;        // floor thickness

lid_t = 3;            // lid thickness
lip_h = 6;            // how far the lid's lip goes into the box
lip_w = 1.6;          // lip thickness
fit = 0.25;           // gap between lip and wall (bigger = looser)

// Light: slits on the front, with a slot behind them to hold the light
slit_w = 1.6; slit_h = 10; slit_gap = 4; slit_span = 84;
slit_z = 20;          // height of the bottom of the slits
led_gap = 5;          // space between front wall and the holder (the light sits here)
led_plate = 1.6;
led_plate_h = 38;

// Back: two cable notches (USB power, audio) and air vents
notch_w = 10; notch_d = 9;
notch_x = [24, W - 24];
vent_w = 2; vent_h = 22; vent_n = 9; vent_pitch = 5; vent_z = 14;

// Lid: microphone holes and the name
mic_hole = 1.6;
label = "buddy"; label_size = 10; label_depth = 0.6;
font = "Liberation Sans:style=Bold";

$fn = 64;

module rrect(w, d, h, r) {
  translate([r, r, 0]) linear_extrude(h) offset(r) square([w - 2*r, d - 2*r]);
}

module base() {
  difference() {
    union() {
      // outer shell with a small chamfer at the bottom edge (stops "elephant foot")
      hull() {
        translate([0.8, 0.8, 0]) rrect(W - 1.6, D - 1.6, 0.01, R - 0.8);
        translate([0, 0, 0.8]) rrect(W, D, H - 0.8, R);
      }
    }
    // inside
    translate([wall, wall, floor_t]) rrect(W - 2*wall, D - 2*wall, H, R - wall);
    // light slits (front)
    n = floor(slit_span / slit_gap) + 1;
    for (i = [0 : n - 1])
      translate([W/2 - slit_span/2 + i*slit_gap - slit_w/2, -1, slit_z])
        cube([slit_w, wall + 2, slit_h]);
    // cable notches (back), open at the top so cables just drop in
    for (x = notch_x)
      translate([x, D - wall - 1, 0]) {
        translate([-notch_w/2, 0, H - notch_d + notch_w/2]) cube([notch_w, wall + 2, notch_d]);
        translate([0, 0, H - notch_d + notch_w/2]) rotate([-90, 0, 0]) cylinder(d = notch_w, h = wall + 2);
      }
    // vents (back)
    for (i = [0 : vent_n - 1])
      translate([W/2 - (vent_n - 1)*vent_pitch/2 + i*vent_pitch - vent_w/2, D - wall - 1, vent_z])
        cube([vent_w, wall + 2, vent_h]);
  }
  // holder behind the slits: the light sticks to this, shining forward
  translate([W/2 - slit_span/2 - 6, wall + led_gap, floor_t - 0.01])
    cube([slit_span + 12, led_plate, led_plate_h]);
}

module lid() {
  in_w = W - 2*wall - 2*fit; in_d = D - 2*wall - 2*fit;
  difference() {
    union() {
      // top plate with a soft chamfer on the top edge (it's on the bed when printing)
      hull() {
        translate([0.8, 0.8, 0]) rrect(W - 1.6, D - 1.6, 0.01, R - 0.8);
        translate([0, 0, 0.8]) rrect(W, D, lid_t - 0.8, R);
      }
      // lip that slides into the box
      translate([wall + fit, wall + fit, lid_t - 0.01])
        difference() {
          rrect(in_w, in_d, lip_h, R - wall - fit);
          translate([lip_w, lip_w, -1]) rrect(in_w - 2*lip_w, in_d - 2*lip_w, lip_h + 2, R - wall - fit - lip_w);
        }
    }
    // gaps in the lip over the cable notches
    for (x = notch_x)
      translate([W - x - notch_w/2 - 1, D - wall - lip_w - 3, lid_t - 0.1]) cube([notch_w + 2, lip_w + 6, lip_h + 1]);
    // microphone holes (middle of the lid)
    translate([W/2, D/2 + 8, -1]) {
      cylinder(d = mic_hole, h = lid_t + 2);
      for (ring = [[6, 3.4], [12, 6.8]]) for (k = [0 : ring[0] - 1])
        rotate(k * 360 / ring[0]) translate([ring[1], 0, 0]) cylinder(d = mic_hole, h = lid_t + 2);
    }
    // name, cut into the top (mirrored because the lid prints upside down)
    translate([W/2, 22, -0.01]) mirror([1, 0, 0]) linear_extrude(label_depth)
      text(label, size = label_size, font = font, halign = "center", valign = "center");
  }
}

if (part == "none") {}
else if (part == "base") base();
else if (part == "lid") lid();
else { base(); translate([0, D + 10, 0]) lid(); }
