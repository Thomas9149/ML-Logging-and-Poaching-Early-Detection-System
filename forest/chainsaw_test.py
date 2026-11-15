#!/usr/bin/env python3
"""
Simple chainsaw-only test.

For every frame (about 2 per second) prints either:
  background   chainsaw 0.04
  CHAINSAW     chainsaw 0.72   <<< ALERT

Usage:
  python chainsaw_test.py --device "Web Camera" --channels 1
  python chainsaw_test.py --device "Web Camera" --channels 1 --threshold 0.5
"""
import argparse
import time

from audio_capture import run_live, run_file, find_device
from classifier import YamnetClassifier


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--device", help="part of the microphone name, e.g. 'Web Camera'")
    p.add_argument("--channels", type=int, default=1)
    p.add_argument("--input", help="WAV file instead of the microphone")
    p.add_argument("--models", default="models")
    p.add_argument("--threshold", type=float, default=0.3)
    a = p.parse_args()

    clf = YamnetClassifier(a.models)

    def handle(frame):
        if frame.sensor_id != 0:          # only use the first channel
            return
        _, cats = clf.classify(frame.samples)
        score = cats["chainsaw"]
        clock = time.strftime("%H:%M:%S", time.localtime(frame.start_time))
        if score >= a.threshold:
            print(f"{clock}  CHAINSAW    chainsaw {score:.2f}   <<< ALERT")
        else:
            print(f"{clock}  background  chainsaw {score:.2f}")

    if a.input:
        run_file(a.input, [handle], None, realtime=False)
    else:
        device = find_device(a.device) if a.device else None
        run_live(device, [handle], None, a.channels)


if __name__ == "__main__":
    main()
