// Buddy box v2: snug box with a hinged flap lid. Prints with NO supports.
// The hinge pin is a 4-5 cm piece of 1.75 mm filament pushed through the hinge.
// Pull it out and the lid comes off. All sizes in mm.

part = "both";          // "base", "lid", "both" (print layout) or "none"

// ---- The space inside (measure your parts and change these) ----
in_w = 110;             // inside, left to right
in_d = 70;              // inside, front to back (not counting the light slot)
in_h = 40;              // inside, floor to underside of the lid

wall = 2.0; floor_t = 2.0; R = 6;
lid_t = 2.4; lip_h = 3; lip_w = 1.2; fit = 0.25;

// Light slot behind the front slits
led_gap = 3.5; led_plate = 1.6;
slit_w = 1.4; slit_pitch = 3.5; slit_h = 8; slit_z = 6;   // slit_z = above the floor

// Hinge
kr = 3.2;               // knuckle radius
pin_d = 2.1;            // hole for 1.75 mm filament
k_gap = 0.4;            // gap between knuckles
hinge_len = 70;

// Cable notches (right side, near the back) and vents (left side)
notch_w = 8; notch_d = 7;

// Push button on the left side (12 mm panel-mount button; the hole is a touch bigger so the thread slides in)
btn_d = 12.4;
btn_boss_d = 19; btn_boss_t = 3;  // a thicker ring of plastic inside, so the nut has something solid to grip
btn_z = 20;                       // height above the floor
btn_y_from_back = 15;             // distance from the back wall

logo_size = 14; logo_depth = 0.6;   // the winking Buddy face on the lid, 14 mm square
logo_y = 21;                        // centre of the face, from the front edge
word_h = 4.5; word_y = 9;           // "BUDDY" under it (Nulshock Bold, saved as outlines in buddy_text.svg)
$fn = 56;

W = in_w + 2*wall;
D = in_d + led_gap + led_plate + 2*wall;
H = floor_t + in_h;
ay = D + kr + 0.4;      // hinge axis
az = H + lid_t - kr;
hl = min(hinge_len, W - 2*R - 4);
seg = (hl - 4*k_gap) / 5;
function seg_x(i) = W/2 - hl/2 + i*(seg + k_gap);
notch_y = [D - wall - 10, D - wall - 22];

module rrect(w, d, h, r) { translate([r, r, 0]) linear_extrude(h) offset(r) square([w - 2*r, d - 2*r]); }
module knuckle(i) { translate([seg_x(i), ay, az]) rotate([0, 90, 0]) cylinder(r = kr, h = seg); }
module pin_hole() { translate([-1, ay, az]) rotate([0, 90, 0]) cylinder(d = pin_d, h = W + 2, $fn = 24); }

btn_y = D - btn_y_from_back;
module base() {
  difference() {
    union() {
      base_body();
      translate([wall - 0.01, btn_y, floor_t + btn_z]) rotate([0, 90, 0]) cylinder(d = btn_boss_d, h = btn_boss_t);
    }
    translate([-1, btn_y, floor_t + btn_z]) rotate([0, 90, 0]) cylinder(d = btn_d, h = wall + btn_boss_t + 2);
  }
}

module base_body() {
  difference() {
    union() {
      hull() {
        translate([0.6, 0.6, 0]) rrect(W - 1.2, D - 1.2, 0.01, R - 0.6);
        translate([0, 0, 0.6]) rrect(W, D, H - 0.6, R);
      }
      for (i = [0, 2, 4]) hull() {   // knuckles with a 45-degree support underneath
        knuckle(i);
        translate([seg_x(i), D - 0.01, az - 2*kr - 0.4]) cube([seg, 0.01, H - (az - 2*kr - 0.4)]);
      }
    }
    translate([wall, wall, floor_t]) rrect(W - 2*wall, D - 2*wall, H, R - wall);
    pin_hole();
    // light slits (front)
    span = in_w - 14; n = floor(span / slit_pitch) + 1;
    for (i = [0 : n - 1])
      translate([W/2 - span/2 + i*slit_pitch - slit_w/2, -1, floor_t + slit_z]) cube([slit_w, wall + 2, slit_h]);
    // finger dip (front, top) to lift the lid
    translate([W/2, -1, H]) rotate([-90, 0, 0]) scale([1.6, 1, 1]) cylinder(r = 3, h = wall + 2);
    // cable notches (right side), open at the top
    for (y = notch_y) translate([W - wall - 1, y, 0]) {
      translate([0, -notch_w/2, H - notch_d + notch_w/2]) cube([wall + 2, notch_w, notch_d]);
      translate([0, 0, H - notch_d + notch_w/2]) rotate([0, 90, 0]) cylinder(d = notch_w, h = wall + 2);
    }
    // vents (left side)
    for (i = [0 : 5]) translate([-1, D/2 - 12.5 + i*5 - 1, floor_t + 6]) cube([wall + 2, 2, min(16, in_h - 12)]);
  }
  // the light sticks onto the front of this, shining out through the slits
  translate([wall + 3, wall + led_gap, floor_t - 0.01]) cube([W - 2*wall - 6, led_plate, slit_z + slit_h + 6]);
}


// The Buddy logo: a rounded square sunk into the lid, with the face (open eye, wink, smile) left standing.
// Drawn on a 100-unit grid (same as the logo picture), y pointing to the back of the box.
function qpts(p0, p1, p2, n = 12) = [for (i = [0 : n]) let (t = i / n)
  (1 - t) * (1 - t) * p0 + 2 * (1 - t) * t * p1 + t * t * p2];
module stroke(pts, w) { for (i = [0 : len(pts) - 2]) hull() { translate(pts[i]) circle(d = w, $fn = 20); translate(pts[i + 1]) circle(d = w, $fn = 20); } }
function L(x, y) = [x - 50, 50 - y];   // picture coordinates -> centred, y up
module logo2d(size) {
  scale(size / 88) difference() {
    offset(r = 30, $fn = 48) square(28, center = true);
    hull() { translate(L(36, 38)) circle(d = 10, $fn = 24); translate(L(36, 46)) circle(d = 10, $fn = 24); }  // open eye
    stroke(qpts(L(55, 46), L(63, 37), L(71, 46)), 7);   // wink
    stroke(qpts(L(38, 68), L(50, 76), L(62, 68)), 6);   // smile
  }
}

module lid() {   // modelled where it sits on the box
  iw = W - 2*wall - 2*fit; id = D - 2*wall - 2*fit;
  difference() {
    union() {
      hull() {
        translate([0, 0, H]) rrect(W, D - 0.5, lid_t - 0.6, R);
        translate([0.6, 0.6, H]) rrect(W - 1.2, D - 1.7, lid_t, R - 0.6);
      }
      for (i = [1, 3]) {
        knuckle(i);
        translate([seg_x(i), D - 2, H]) cube([seg, ay - (D - 2), lid_t]);
      }
      // lip: front and sides only (stops short of the back so the lid can swing open)
      translate([wall + fit, wall + fit, H - lip_h]) difference() {
        rrect(iw, id, lip_h + 0.01, R - wall - fit);
        translate([lip_w, lip_w, -1]) rrect(iw - 2*lip_w, id - 2*lip_w, lip_h + 2, R - wall - fit - lip_w);
        translate([-1, id - 10, -1]) cube([iw + 2, 11, lip_h + 2]);
        for (y = notch_y) translate([iw - lip_w - 1, y - wall - fit - notch_w/2 - 1, -1]) cube([lip_w + 2, notch_w + 2, lip_h + 2]);
      }
    }
    pin_hole();
    // microphone holes
    translate([W/2, D/2 + 4, H - 1]) {
      cylinder(d = 1.6, h = lid_t + 2);
      for (ring = [[6, 3.2], [12, 6.4]]) for (k = [0 : ring[0] - 1])
        rotate(k * 360 / ring[0]) translate([ring[1], 0, 0]) cylinder(d = 1.6, h = lid_t + 2);
    }
    translate([W/2, logo_y, H + lid_t - logo_depth]) linear_extrude(logo_depth + 1) logo2d(logo_size);
    translate([W/2, word_y, H + lid_t - logo_depth]) linear_extrude(logo_depth + 1)
      scale(word_h / 10) translate([-28.5, -25.3]) import("buddy_text.svg");   // centres the 57 x 10 outlines
  }
}

module lid_for_printing() { translate([W, 0, H + lid_t]) rotate([0, 180, 0]) lid(); }

if (part == "base") base();
else if (part == "lid") lid_for_printing();
else if (part == "both") { base(); translate([0, D + 12, 0]) lid_for_printing(); }
