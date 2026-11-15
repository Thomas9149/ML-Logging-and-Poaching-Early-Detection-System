#!/usr/bin/env python3
"""
Full live pipeline:

  microphone -> audio_capture.py -> classifier.py -> incidents.py -> notifications
               (16 kHz frames)     (YAMNet scores)   (probabilities)  (screen/log/ntfy)

Every frame where chainsaw, gunshot or tree_fall scores at least
--obs-threshold becomes an Observation for the incident engine. The engine
decides when to send CHAINSAWING, LOGGING, GUNSHOT and POACHING.

Logs (in --log-dir, default logs/):
  observations.jsonl   every observation - replay with
                       python incidents.py --replay logs/observations.jsonl
  notifications.jsonl  every notification with its evidence

Usage:
  python run.py --device "Web Camera" --channels 1
  python run.py --device "Web Camera" --channels 1 \\
                --ntfy-prefix forestwatch-team42 --show-frames
  python run.py --input test.wav --show-frames     # test with a recording
"""
import argparse
from pathlib import Path

from audio_capture import run_live, run_file, find_device
from classifier import YamnetClassifier
from incidents import (IncidentEngine, Observation, console_printer,
                       JsonlLog, NtfyNotifier)

WATCHED = ("chainsaw", "gunshot", "tree_fall")


class FrameHandler:
    def __init__(self, clf, engine, obs_log, threshold, show_frames,
                 saw_threshold=None):
        self.clf = clf
        self.engine = engine
        self.obs_log = obs_log
        self.threshold = threshold
        self.saw_threshold = threshold if saw_threshold is None else saw_threshold
        self.show_frames = show_frames

    def __call__(self, frame):
        top, cats = self.clf.classify(frame.samples)
        t = frame.start_time
        for cat in WATCHED:
            score = cats[cat]
            limit = self.saw_threshold if cat == "chainsaw" else self.threshold
            if score < limit:
                continue
            # A running saw can light up "crack/breaking" classes; while the
            # saw is the louder sound, don't treat that as a tree fall.
            if cat == "tree_fall" and cats["chainsaw"] >= score:
                continue
            obs = Observation(t, frame.sensor_id, cat, round(score, 3))
            self.obs_log(obs)
            self.engine.observe(obs)
        self.engine.tick(t)

        if self.show_frames:
            scores = "  ".join(f"{c} {cats[c]:.2f}" for c in WATCHED)
            heard = ", ".join(f"{n} {p:.2f}" for n, p in top)
            print(f"t={t:.2f} s{frame.sensor_id} {frame.rms_dbfs:6.1f} dBFS | "
                  f"{scores} | P(chainsawing) "
                  f"{self.engine.p_chainsawing(t):.2f} | heard: {heard}",
                  flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", help="part of the microphone name, e.g. 'Web Camera'")
    p.add_argument("--channels", type=int, default=1)
    p.add_argument("--input", help="WAV file instead of the microphone")
    p.add_argument("--realtime", action="store_true",
                   help="with --input, play the file at real speed")
    p.add_argument("--models", default="models", help="folder with yamnet files")
    p.add_argument("--obs-threshold", type=float, default=0.3,
                   help="minimum gunshot / tree_fall score to count as an observation")
    p.add_argument("--chainsaw-threshold", type=float, default=0.1,
                   help="minimum chainsaw score to count (and, at max "
                        "sensitivity, to ping CHAINSAWING); default 0.1")
    p.add_argument("--chainsaw-repeat", type=float, default=10.0,
                   help="while a saw keeps running, send CHAINSAWING again "
                        "every N seconds (0 = once per episode); default 10")
    p.add_argument("--log-dir", default="logs")
    p.add_argument("--ntfy-prefix", help="push to ntfy topics <prefix>-<type>")
    p.add_argument("--ntfy-server", default="https://ntfy.sh")
    p.add_argument("--show-frames", action="store_true",
                   help="print scores for every frame")
    p.add_argument("--explain", action="store_true",
                   help="print why tree falls / shots did not trigger alerts")
    a = p.parse_args()

    models = Path(a.models)
    if not models.is_absolute() and not models.exists():
        # also allow running from inside forest/ with models/ one level up
        alt = Path(__file__).resolve().parent.parent / a.models
        if alt.exists():
            models = alt

    log_dir = Path(a.log_dir)
    engine = IncidentEngine({"chainsawing_repeat_s": a.chainsaw_repeat},
                            log=print if a.explain else None)
    engine.subscribe(console_printer)
    engine.subscribe(JsonlLog(log_dir / "notifications.jsonl"))
    if a.ntfy_prefix:
        engine.subscribe(NtfyNotifier(a.ntfy_prefix, a.ntfy_server))
        print(f"Pushing to ntfy topics {a.ntfy_prefix}-chainsawing, -logging, "
              f"-gunshot, -poaching and -all")

    handler = FrameHandler(YamnetClassifier(models), engine,
                           JsonlLog(log_dir / "observations.jsonl"),
                           a.obs_threshold, a.show_frames,
                           saw_threshold=a.chainsaw_threshold)
    if a.input:
        run_file(a.input, [handler], None, realtime=a.realtime)
    else:
        device = find_device(a.device) if a.device else None
        run_live(device, [handler], None, a.channels)


if __name__ == "__main__":
    main()
