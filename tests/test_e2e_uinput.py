"""End to end through the kernel: a virtual touchpad (uinput) -> find_touchpad -> EdgeSwipe.

Needs write access to /dev/uinput and read access to /dev/input (root, or the
`uinput` and `input` groups); skipped otherwise. The test grabs its virtual
pad, so the desktop never sees the fake swipes.
"""
import os
import select
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import trackpad_edges as te                       # noqa: E402
from evdev import AbsInfo, UInput, ecodes as e    # noqa: E402

NAME = "trackpad-edges test"
X, Y = (1266, 5676), (1096, 4758)                 # a Synaptics-style range not starting at 0


def make_pad():
    return UInput({
        e.EV_KEY: [e.BTN_LEFT, e.BTN_TOUCH, e.BTN_TOOL_FINGER, e.BTN_TOOL_DOUBLETAP],
        e.EV_ABS: [
            (e.ABS_X, AbsInfo(0, *X, 0, 0, 40)), (e.ABS_Y, AbsInfo(0, *Y, 0, 0, 40)),
            (e.ABS_MT_SLOT, AbsInfo(0, 0, 4, 0, 0, 0)),
            (e.ABS_MT_TRACKING_ID, AbsInfo(0, 0, 65535, 0, 0, 0)),
            (e.ABS_MT_POSITION_X, AbsInfo(0, *X, 0, 0, 40)),
            (e.ABS_MT_POSITION_Y, AbsInfo(0, *Y, 0, 0, 40)),
        ]}, name=f"{NAME} Touchpad", input_props=[e.INPUT_PROP_POINTER, e.INPUT_PROP_BUTTONPAD])


def make_touchscreen():
    return UInput({
        e.EV_KEY: [e.BTN_TOUCH, e.BTN_TOOL_FINGER],
        e.EV_ABS: [
            (e.ABS_MT_POSITION_X, AbsInfo(0, 0, 4000, 0, 0, 0)),
            (e.ABS_MT_POSITION_Y, AbsInfo(0, 0, 3000, 0, 0, 0)),
        ]}, name=f"{NAME} Touchscreen", input_props=[e.INPUT_PROP_DIRECT])


def can_run():
    return os.access("/dev/uinput", os.W_OK) and (
        os.geteuid() == 0 or any(os.access(p, os.R_OK) for p in Path("/dev/input").glob("event*")))


@unittest.skipUnless(can_run(), "needs /dev/uinput and /dev/input access")
class KernelPathTest(unittest.TestCase):
    def setUp(self):
        self.saved = te.TOUCHPAD_NAME
        te.TOUCHPAD_NAME = NAME                   # matches both fakes; only one is a touchpad
        self.screen = make_touchscreen()
        self.pad = make_pad()
        time.sleep(0.5)                           # udev creates and permissions the nodes
        found = te.find_touchpad()
        self.assertIsNotNone(found, "no touchpad found")
        self.dev, self.axes = found
        self.dev.grab()                           # keep the fake swipes away from the desktop

    def tearDown(self):
        try:
            self.dev.ungrab()
        except OSError:
            pass
        self.dev.close()
        self.pad.close()
        self.screen.close()
        te.TOUCHPAD_NAME = self.saved

    def swipe(self, x, y0, y1, frames=200, tracking=1):
        p = self.pad
        p.write(e.EV_ABS, e.ABS_MT_SLOT, 0)
        p.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, tracking)
        for i in range(frames + 1):
            y = round(y0 + (y1 - y0) * i / frames)
            p.write(e.EV_ABS, e.ABS_MT_POSITION_X, x)
            p.write(e.EV_ABS, e.ABS_MT_POSITION_Y, y)
            if i == 0:
                p.write(e.EV_KEY, e.BTN_TOUCH, 1)
                p.write(e.EV_KEY, e.BTN_TOOL_FINGER, 1)
            p.syn()
        p.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, -1)
        p.write(e.EV_KEY, e.BTN_TOUCH, 0)
        p.write(e.EV_KEY, e.BTN_TOOL_FINGER, 0)
        p.syn()

    def read_steps(self):
        steps = []
        det = te.EdgeSwipe(te.axis_range(self.axes[e.ABS_MT_POSITION_X]),
                           te.axis_range(self.axes[e.ABS_MT_POSITION_Y]),
                           lambda zone, d: steps.append((zone, d)))
        while select.select([self.dev.fd], [], [], 0.5)[0]:
            for ev in self.dev.read():
                det.feed(ev)
        return steps

    def test_picks_touchpad_not_touchscreen_with_its_ranges(self):
        self.assertEqual(self.dev.name, f"{NAME} Touchpad")
        self.assertEqual(te.axis_range(self.axes[e.ABS_MT_POSITION_X]), X)
        self.assertEqual(te.axis_range(self.axes[e.ABS_MT_POSITION_Y]), Y)

    def test_swipes_through_the_kernel(self):
        h = Y[1] - Y[0]
        left = X[0] + 100                         # inside the 7% strip
        right = X[1] - 100
        self.swipe(left, Y[0] + int(h * 0.9), Y[0] + int(h * 0.3))
        up = self.read_steps()
        self.swipe(right, Y[0] + int(h * 0.3), Y[0] + int(h * 0.9), tracking=2)
        down = self.read_steps()
        self.swipe(left, Y[0] + int(h * 0.1), Y[1] - 50, tracking=3)    # NumberPad corner
        corner = self.read_steps()
        self.swipe((X[0] + X[1]) // 2, Y[0] + int(h * 0.9), Y[0] + int(h * 0.3), tracking=4)
        middle = self.read_steps()

        self.assertEqual(set(up), {("LEFT", +1)})
        self.assertEqual(set(down), {("RIGHT", -1)})
        # 0.6 of the pad: 0.03 to arm, then one step per 0.018 (frames are ~0.3% each)
        self.assertTrue(28 <= len(up) <= 32, len(up))
        self.assertEqual(len(up), len(down))
        self.assertEqual(corner, [])
        self.assertEqual(middle, [])


if __name__ == "__main__":
    unittest.main()
