"""Check generated texture payloads without a game installation or retoc.

Every payload the builder writes is verified by decoding it back, so a wrong
bit layout fails here instead of shipping an unreadable HUD."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build
from build import CLEAR, PLATES, bc7_mode6, parse_plates, uniform_pixels, verify_uniform

FORMATS = {'PF_B8G8R8A8': 4, 'PF_BC7': 16, 'PF_DXT1': 8}   # Bytes per pixel or per block.


def payload_size(fmt, w, h):
    if fmt == 'PF_B8G8R8A8':
        return w * h * FORMATS[fmt]
    return ((w + 3) // 4) * ((h + 3) // 4) * FORMATS[fmt]


class ClearedBackgrounds(unittest.TestCase):
    """0.1.0 removed every background; those payloads must not change."""

    def test_cleared_payloads_match_the_previous_release(self):
        w, h = 12, 8
        blocks = ((w + 3) // 4) * ((h + 3) // 4)
        for fmt, expected in (('PF_B8G8R8A8', b'\0' * (w * h * 4)),
                              ('PF_BC7', (b'\x40' + b'\0' * 15) * blocks),
                              ('PF_DXT1', (b'\0' * 4 + b'\xff' * 4) * blocks)):
            with self.subTest(fmt=fmt):
                self.assertEqual(uniform_pixels(fmt, w, h, CLEAR), expected)

    def test_cleared_payloads_decode_to_fully_transparent(self):
        for fmt in FORMATS:
            with self.subTest(fmt=fmt):
                pixels = uniform_pixels(fmt, 8, 8, CLEAR)
                verify_uniform(fmt, 8, 8, pixels, CLEAR)

    def test_payload_length_matches_the_replaced_export(self):
        for fmt in FORMATS:
            for w, h in ((4, 4), (8, 16), (13, 7)):
                with self.subTest(fmt=fmt, size=(w, h)):
                    self.assertEqual(len(uniform_pixels(fmt, w, h, CLEAR)),
                                     payload_size(fmt, w, h))


class TranslucentPlates(unittest.TestCase):
    """A plate keeps contrast behind the game's own dark HUD text."""

    colours = [(216, 214, 206, 120), (0, 0, 0, 90), (32, 32, 32, 128),
               (255, 255, 255, 255), (1, 1, 1, 1), (3, 5, 7, 9)]

    def test_plates_decode_to_exactly_the_requested_colour(self):
        for fmt in ('PF_B8G8R8A8', 'PF_BC7'):
            for colour in self.colours:
                with self.subTest(fmt=fmt, colour=colour):
                    pixels = uniform_pixels(fmt, 8, 8, colour)
                    self.assertEqual(len(pixels), payload_size(fmt, 8, 8))
                    verify_uniform(fmt, 8, 8, pixels, colour)

    def test_bgra_byte_order_is_not_swapped(self):
        # A pure red plate must not come back as blue.
        pixels = uniform_pixels('PF_B8G8R8A8', 4, 4, (254, 0, 0, 128))
        self.assertEqual(pixels[:4], bytes((0, 0, 254, 128)))
        verify_uniform('PF_B8G8R8A8', 4, 4, pixels, (254, 0, 0, 128))

    def test_the_shipped_plate_colour_is_buildable(self):
        self.assertTrue(PLATES, 'The ammo plate is the reason this exists.')
        for name, colour in PLATES.items():
            for fmt in ('PF_B8G8R8A8', 'PF_BC7'):
                with self.subTest(name=name, fmt=fmt):
                    verify_uniform(fmt, 8, 8, uniform_pixels(fmt, 8, 8, colour), colour)

    def test_dxt1_cannot_carry_a_plate(self):
        with self.assertRaisesRegex(ValueError, 'DXT1 cannot carry'):
            uniform_pixels('PF_DXT1', 8, 8, (216, 214, 206, 120))

    def test_mixed_low_bits_are_rejected_rather_than_rounded(self):
        with self.assertRaisesRegex(ValueError, 'shared low bit'):
            bc7_mode6((216, 214, 206, 121))

    def test_unknown_format_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unsupported texture format'):
            uniform_pixels('PF_R8G8B8A8', 8, 8, CLEAR)


class Verification(unittest.TestCase):
    def test_a_non_uniform_payload_fails(self):
        pixels = bytearray(uniform_pixels('PF_B8G8R8A8', 4, 4, (0, 0, 0, 120)))
        pixels[3] = 255
        with self.assertRaisesRegex(ValueError, 'not uniformly'):
            verify_uniform('PF_B8G8R8A8', 4, 4, bytes(pixels), (0, 0, 0, 120))

    def test_a_cleared_payload_is_not_accepted_as_a_plate(self):
        pixels = uniform_pixels('PF_BC7', 8, 8, CLEAR)
        with self.assertRaisesRegex(ValueError, 'not uniformly'):
            verify_uniform('PF_BC7', 8, 8, pixels, (216, 214, 206, 120))


class PlateOverrides(unittest.TestCase):
    def test_defaults_are_returned_unchanged(self):
        self.assertEqual(parse_plates([]), PLATES)

    def test_an_override_replaces_one_colour(self):
        plates = parse_plates(['T_Ammo_Back_Full=0,0,0,90'])
        self.assertEqual(plates['T_Ammo_Back_Full'], (0, 0, 0, 90))

    def test_zero_alpha_removes_the_plate_again(self):
        self.assertEqual(parse_plates(['T_Ammo_Back_Full=0,0,0,0'])['T_Ammo_Back_Full'], CLEAR)

    def test_malformed_overrides_are_rejected(self):
        for bad in ['T_Ammo_Back_Full', 'T_Ammo_Back_Full=1,2,3', '=1,2,3,4',
                    'T_Ammo_Back_Full=1,2,3,4,5', 'T_Ammo_Back_Full=1,2,3,300',
                    'T_Ammo_Back_Full=1,2,3,-4', 'T_Ammo_Back_Full=a,b,c,d']:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_plates([bad])

    def test_plate_names_are_target_texture_stems(self):
        targets = {Path(p).stem for p in
                   __import__('json').loads((Path(build.ROOT) / 'targets.json').read_text())}
        self.assertLessEqual(set(PLATES), targets)


if __name__ == '__main__':
    unittest.main()
