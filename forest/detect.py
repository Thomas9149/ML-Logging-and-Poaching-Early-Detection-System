#!/usr/bin/env python3
"""
Stage 2 - live detection: microphone -> frames -> YAMNet -> events.

Reuses audio_capture.py for the microphone and framing, and classifier.py
for YAMNet. Turns per-frame scores into EVENTS with an alert level:

  chainsaw / chopping          -> WARNING   "possible illegal logging"
  tree_fall after cutting      -> CONFIRMED "tree felled" (cutting heard in
                                  the last few minutes on any sensor)
  tree_fall with no cutting    -> INFO      "possible natural tree fall"
  gunshot                      -> URGENT    "possible poaching"
  vehicle                      -> INFO      logged only

Each category has its own rule for how many recent frames must agree, so
short sounds (gunshot, tree fall) aren't missed and long sounds (chainsaw)
don't trigger on a single odd frame.

Usage:
  python detect.py --device "Web Camera" --channels 1
  python detect.py --input test.wav            # test with a recording
  python detect.py ... --quiet                 # only print events
"""
import argparse
import time
from collections import defaultdict, deque
from dataclasses import dataclass, asdict

from audio_capture import run_live, run_file, find_device
from classifier import YamnetClassifier

# category: (hits needed, out of this many recent frames)
# Frames overlap by 50%, so a short bang usually lands in 1-2 frames.
RULES = {
    "chainsaw":  (2, 3),
    "chopping":  (2, 4),
    "tree_fall": (1, 1),
    "gunshot":   (1, 1),
    "vehicle":   (2, 3),
}

CUTTING = {"chainsaw", "chopping"}
CONFIRM_WINDOW_S = 300     # a tree fall within 5 min of cutting = confirmed


@dataclass
class Event:
    time: float           # Unix time of the frame that triggered it
    sensor_id: int
    category: str
    level: str            # URGENT / CONFIRMED / WARNING / INFO
    score: float
    message: str

    def as_dict(self):
        return asdict(self)


class Detector:
    def __init__(self, clf, threshold=0.3, quiet=False, on_event=None):
        self.clf = clf
        self.threshold = threshold
        self.quiet = quiet
        self.on_event = on_event or []          # stage 3 hooks (alerts etc.)
        self.history = defaultdict(lambda: defaultdict(deque))
        self.active = set()                     # (sensor, category) ongoing
        self.last_cutting_time = None           # last frame with saw/axe sound

    # ---- per-frame entry point -------------------------------------------
    def __call__(self, frame):
        top, cats = self.clf.classify(frame.samples)
        hits = {c for c, p in cats.items() if p >= self.threshold}
        t = frame.start_time

        if hits & CUTTING:
            self.last_cutting_time = t

        if not self.quiet:
            shown = "  ".join(f"{c} {p:.2f}" for c, p in cats.items())
            heard = ", ".join(f"{n} {p:.2f}" for n, p in top)
            print(f"t={t:.2f} s{frame.sensor_id} {frame.rms_dbfs:6.1f} dBFS"
                  f" | {shown} | heard: {heard}")

        for cat, (needed, window) in RULES.items():
            hist = self.history[frame.sensor_id][cat]
            hist.append(cat in hits)
            while len(hist) > window:
                hist.popleft()
            key = (frame.sensor_id, cat)
            count = sum(hist)

            if count >= needed and key not in self.active:
                self.active.add(key)
                self._emit(self._make_event(t, frame.sensor_id, cat, cats[cat]))
            elif count == 0 and key in self.active:
                self.active.discard(key)
                if not self.quiet:
                    print(f"--- {cat} on sensor {frame.sensor_id} has stopped ---")

    # ---- turning a detection into an event with a level ------------------
    def _make_event(self, t, sensor, cat, score):
        if cat == "gunshot":
            return Event(t, sensor, cat, "URGENT", score,
                         "Gunshot detected - possible poaching")
        if cat in CUTTING:
            what = "Chainsaw" if cat == "chainsaw" else "Axe chopping"
            return Event(t, sensor, cat, "WARNING", score,
                         f"{what} detected - possible illegal logging")
        if cat == "tree_fall":
            recent = (self.last_cutting_time is not None
                      and t - self.last_cutting_time <= CONFIRM_WINDOW_S)
            if recent:
                ago = t - self.last_cutting_time
                return Event(t, sensor, cat, "CONFIRMED", score,
                             f"Tree fall {ago:.0f} s after cutting - "
                             f"tree felled, logging confirmed")
            return Event(t, sensor, cat, "INFO", score,
                         "Tree fall with no cutting heard - "
                         "probably natural")
        return Event(t, sensor, cat, "INFO", score, "Vehicle detected")

    def _emit(self, event):
        stamp = time.strftime("%H:%M:%S", time.localtime(event.time))
        print(f"\n*** [{event.level}] {stamp} sensor {event.sensor_id}: "
              f"{event.message} (score {event.score:.2f}) ***\n")
        for hook in self.on_event:
            hook(event)          # stage 3: send alert / trigger the Arduino


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--device", help="part of the microphone name, e.g. 'Web Camera'")
    p.add_argument("--channels", type=int, default=1)
    p.add_argument("--input", help="WAV file instead of the microphone")
    p.add_argument("--models", default="models", help="folder with yamnet files")
    p.add_argument("--threshold", type=float, default=0.3)
    p.add_argument("--quiet", action="store_true", help="only print events")
    a = p.parse_args()

    det = Detector(YamnetClassifier(a.models), threshold=a.threshold,
                   quiet=a.quiet)
    if a.input:
        run_file(a.input, [det], None, realtime=False)
    else:
        device = find_device(a.device) if a.device else None
        run_live(device, [det], None, a.channels)


if __name__ == "__main__":
    main()
