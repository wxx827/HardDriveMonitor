import unittest

from core.treemap import squarify


class SquarifyTests(unittest.TestCase):
    def _check_area(self, values, w, h):
        rects = squarify(values, 0, 0, w, h)
        self.assertEqual(len(rects), len(values))
        total = sum(v for v in values if v > 0)
        for value, (x, y, rw, rh) in zip(values, rects):
            if value <= 0:
                self.assertEqual(rw * rh, 0)
                continue
            self.assertAlmostEqual(rw * rh, value / total * w * h, delta=1e-6 * w * h)
            self.assertGreaterEqual(x, -1e-9)
            self.assertGreaterEqual(y, -1e-9)
            self.assertLessEqual(x + rw, w + 1e-6)
            self.assertLessEqual(y + rh, h + 1e-6)
        return rects

    def test_area_conserved(self):
        self._check_area([6, 6, 4, 3, 2, 2, 1], 600, 400)

    def test_tall_container(self):
        self._check_area([50, 30, 10, 5, 5], 100, 900)

    def test_zero_and_negative_values(self):
        rects = self._check_area([10, 0, 5, -3], 200, 100)
        self.assertEqual(rects[1][2] * rects[1][3], 0)

    def test_no_overlap(self):
        rects = squarify([9, 7, 5, 4, 3, 2, 1, 1], 0, 0, 300, 200)
        for i, a in enumerate(rects):
            for b in rects[i + 1:]:
                overlap_w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
                overlap_h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
                self.assertFalse(overlap_w > 1e-6 and overlap_h > 1e-6)

    def test_aspect_ratio_reasonable(self):
        rects = squarify([1] * 16, 0, 0, 400, 400)
        for _x, _y, w, h in rects:
            self.assertLess(max(w / h, h / w), 2.5)

    def test_empty(self):
        self.assertEqual(squarify([], 0, 0, 10, 10), [])
        self.assertEqual(squarify([1, 2], 0, 0, 0, 10), [(0, 0, 0.0, 0.0)] * 2)


if __name__ == "__main__":
    unittest.main()
