import sys
import unittest

import cv2
import numpy as np

sys.path.insert(0, "src")
import gcode_core as core


class CoreTests(unittest.TestCase):
    def test_extreme_edge_sensitivity_stays_usable(self):
        img = np.full((100, 140), 255, np.uint8)
        cv2.circle(img, (70, 50), 30, 30, -1)
        bg = core.detect_background_mask(img)
        for low, high in ((0, 0), (50, 150), (500, 500), (500, 0)):
            paths = core.generate_outline_paths(img, low, high, background_mask=bg)
            self.assertTrue(paths)

    def test_dot_infill_contains_only_real_dot_paths(self):
        img = np.full((80, 80), 255, np.uint8)
        img[20:60, 20:60] = 20
        dots = core.generate_dot_infill_paths(img, 5)
        self.assertTrue(dots)
        self.assertTrue(all(len(p) == 1 for p in dots))

    def test_front_view_transform_is_180_degrees(self):
        paths = [[(0, 0), (9, 2)], [(4, 7)]]
        out = core.transform_paths_for_front_view(paths, 10, 10)
        self.assertEqual(out[0], [(9, 9), (0, 7)])
        self.assertEqual(out[1], [(5, 2)])

    def test_reserved_right_side_centering(self):
        x, y = core.compute_centered_origin(220, 220, 150, 150, 10)
        self.assertAlmostEqual(x, 30.0)
        self.assertAlmostEqual(y, 35.0)
        core.validate_drawing_placement(x, y, 150, 150, 220, 220, 10)
        with self.assertRaises(ValueError):
            core.validate_drawing_placement(0, 0, 211, 100, 220, 220, 10)

    def test_gcode_dots_fan_and_z_hop(self):
        code = core.paths_to_gcode([[(1, 2)]], 1, 1, 3, 0, 1500, 3000)
        self.assertIn("M107", code)
        self.assertIn("G1 Z3.00", code)
        self.assertNotIn("G1 X1.00 Y2.00 F1500", code)


if __name__ == "__main__":
    unittest.main()
