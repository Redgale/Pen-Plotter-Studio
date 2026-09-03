"""Tests for Clip Avoidance, the G-code settings header, and G-code import.

These cover the features added on top of the base conversion engine and are
independent of tests/test_gcode_core.py.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import gcode_core as core


class ClipAvoidanceTests(unittest.TestCase):
    def test_pulls_every_edge_in_by_the_requested_amount(self):
        # start with the full bed as the "usable" rect
        x0, y0, w, h = core.apply_clip_avoidance(
            0.0, 0.0, 220.0, 220.0,
            clip_left=5, clip_right=5, clip_front=10, clip_back=10)
        self.assertAlmostEqual(x0, 5.0)     # left edge moved in 5
        self.assertAlmostEqual(y0, 10.0)    # front edge moved in 10
        self.assertAlmostEqual(w, 210.0)    # 220 - 5 - 5
        self.assertAlmostEqual(h, 200.0)    # 220 - 10 - 10

    def test_stacks_on_top_of_existing_margins(self):
        # usable rect after a 20 mm right margin, then clip avoidance
        x0, y0, w, h = core.apply_clip_avoidance(
            0.0, 0.0, 200.0, 220.0,
            clip_left=5, clip_right=5, clip_front=10, clip_back=10)
        self.assertAlmostEqual(w, 190.0)
        self.assertAlmostEqual(h, 200.0)

    def test_width_and_height_never_invert(self):
        _, _, w, h = core.apply_clip_avoidance(
            0.0, 0.0, 8.0, 8.0, clip_left=50, clip_right=50,
            clip_front=50, clip_back=50)
        self.assertGreaterEqual(w, 1.0)
        self.assertGreaterEqual(h, 1.0)


class SettingsHeaderTests(unittest.TestCase):
    def test_header_is_written_and_is_valid_json(self):
        settings = {"width_mm": 180.0, "clip_top": 10, "fan_off": True}
        code = core.paths_to_gcode(
            [[(0, 0), (10, 10)]], 1.0, 1.0, 2.0, 0.0, 3000, 6000,
            settings_comment=settings)
        header = [l for l in code.splitlines()
                  if l.startswith("; PPS-SETTINGS:")]
        self.assertEqual(len(header), 1)
        parsed = json.loads(header[0].split(":", 1)[1].strip())
        self.assertEqual(parsed["width_mm"], 180.0)
        self.assertEqual(parsed["clip_top"], 10)
        self.assertIs(parsed["fan_off"], True)

    def test_no_header_when_not_requested(self):
        code = core.paths_to_gcode([[(0, 0), (10, 10)]], 1.0, 1.0, 2.0, 0.0,
                                   3000, 6000)
        self.assertNotIn("PPS-SETTINGS", code)


class GcodeImportTests(unittest.TestCase):
    def test_roundtrip_restores_settings_and_toolpath(self):
        settings = {
            "width_mm": 150.0, "draw_feed": 3000, "travel_feed": 6000,
            "pen_up_z": 2.0, "pen_down_z": 0.0, "clip_avoidance_enabled": True,
            "clip_top": 10, "clip_bottom": 10, "clip_left": 5, "clip_right": 5,
            "shading_style": "crosshatch", "fan_off": True,
        }
        # a small drawing: one L-shaped stroke, placed at a known origin
        code = core.paths_to_gcode(
            [[(0, 0), (10, 0), (10, 10)]], 1.0, 1.0,
            2.0, 0.0, 3000, 6000, origin_x=20.0, origin_y=30.0,
            content_w_px=11, content_h_px=11, flip_y=False,
            settings_comment=settings)

        paths, recovered = core.parse_gcode(code)

        # settings come straight back from the header
        self.assertEqual(recovered["width_mm"], 150.0)
        self.assertEqual(recovered["draw_feed"], 3000)
        self.assertTrue(recovered["clip_avoidance_enabled"])
        self.assertEqual(recovered["clip_top"], 10)

        # the toolpath is the real machine move: one 3-point stroke, offset
        self.assertEqual(len(paths), 1)
        self.assertEqual(len(paths[0]), 3)
        self.assertAlmostEqual(paths[0][0][0], 20.0, places=2)
        self.assertAlmostEqual(paths[0][0][1], 30.0, places=2)
        self.assertAlmostEqual(paths[0][2][0], 30.0, places=2)
        self.assertAlmostEqual(paths[0][2][1], 40.0, places=2)

    def test_infers_feeds_and_z_from_a_headerless_file(self):
        code = "\n".join([
            "G21", "G90", "M107",
            "G1 Z2.00 F1000",
            "G28 X Y",
            "G1 X5 Y5 F3200",
            "G1 Z0.00 F1000",
            "G1 X15 Y5 F1600",
            "G1 X15 Y15 F1600",
            "G1 Z2.00 F1000",
            "G1 X0 Y0 F3200",
        ])
        paths, recovered = core.parse_gcode(code)
        self.assertEqual(recovered["pen_down_z"], 0.0)
        self.assertEqual(recovered["pen_up_z"], 2.0)
        self.assertEqual(recovered["draw_feed"], 1600)
        self.assertEqual(recovered["travel_feed"], 3200)
        self.assertTrue(recovered["fan_off"])
        self.assertTrue(recovered["home_xy"])
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0][0], (5.0, 5.0))
        self.assertEqual(paths[0][-1], (15.0, 15.0))

    def test_dot_taps_come_back_as_single_point_paths(self):
        code = core.paths_to_gcode(
            [[(2, 2)], [(8, 8)]], 1.0, 1.0, 2.0, 0.0, 3000, 6000,
            flip_y=False, content_w_px=11, content_h_px=11,
            dot_dwell_ms=20)
        paths, _ = core.parse_gcode(code)
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(len(p) == 1 for p in paths))

    def test_render_bed_preview_returns_a_bed_sized_image(self):
        paths, _ = core.parse_gcode("\n".join([
            "G1 Z2 F1000", "G1 X10 Y10 F3000", "G1 Z0 F1000",
            "G1 X20 Y20 F1500", "G1 Z2 F1000",
        ]))
        img = core.render_gcode_bed_preview(paths, 220, 220, px_per_mm=2)
        self.assertEqual(img.shape, (440, 440, 3))


if __name__ == "__main__":
    unittest.main()
