#!/usr/bin/env python3
"""
Stage 3 - incident engine: sound events over time -> notifications.

YAMNet (stage 2) says what a single 1-second frame sounds like. This stage
looks at WHAT was heard and WHEN, and works out how likely it is that
something is really happening:

  many chainsaw frames close together      -> CHAINSAWING  (HIGH)
  chainsawing, then a tree falling         -> LOGGING      (URGENT)
  one gunshot                              -> GUNSHOT      (HIGH)  "possible gunshot"
  several gunshots within 10 minutes       -> POACHING     (URGENT)

How the probabilities work
--------------------------
Each observation is a piece of evidence. Its strength is
    weight x YAMNet score x fade,   fade = e^(-age / decay time)
so old evidence counts less and less. Pieces of evidence are combined with a
"noisy-OR": the chance that ALL of them are false alarms is the product of
(1 - strength), so
    P = 1 - (1 - s1)(1 - s2)(1 - s3)...
One weak frame gives a low P; many frames in a row push P towards 1.

Subscribing
-----------
    engine = IncidentEngine()
    engine.subscribe(console_printer)                       # everything
    engine.subscribe(send_sms, types={"POACHING"})          # one type only
    engine.subscribe(NtfyNotifier("forestwatch-team42"))    # phone push

Rangers subscribe in the ntfy phone app to the topics they care about:
    <prefix>-chainsawing  <prefix>-logging  <prefix>-gunshot
    <prefix>-poaching     <prefix>-all

Usage
-----
    python incidents.py --demo                         # scripted story, instant
    python incidents.py --demo --speed 10              # 10x real time
    python incidents.py --demo --ntfy-prefix forestwatch-team42
    python incidents.py --replay logs/observations.jsonl
"""
import argparse
import json
import math
import queue
import ssl
import sys
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, asdict, field
from pathlib import Path

# --------------------------------------------------------------------------
# All tuning numbers in one place. Hand-tuned, not trained.
# --------------------------------------------------------------------------
DEFAULTS = {
    # CHAINSAWING: noisy-OR over recent chainsaw frames.
    # MAX SENSITIVITY (demo setting): ONE chainsaw-like frame triggers.
    # Weight 1.0 -> a frame's strength = its score; any observation that
    # reaches the engine (run.py --chainsaw-threshold, default 0.1) passes
    # chainsawing_on. Earlier settings: weight 0.35 / on 0.8 / off 0.3
    # (~7 frames), then weight 0.6 (~4 frames).
    "chainsaw_weight": 1.0,
    "chainsaw_decay_s": 20.0,
    "chainsaw_window_s": 60.0,
    "chainsawing_on": 0.1,       # send CHAINSAWING at or above this
    "chainsawing_off": 0.05,     # episode over (re-armed) below this;
                                 # must stay below chainsawing_on
    "chainsawing_repeat_s": 10.0,  # while the saw keeps running, send
                                   # CHAINSAWING again every N s (0 = once)

    # LOGGING: tree fall after a confirmed chainsawing episode.
    "logging_window_s": 600.0,   # tree fall must come within 10 min of sawing
    "cutting_memory_s": 300.0,   # memory of sawing fades over ~5 min
    "tree_fall_full_score": 0.6, # fall score that counts as fully certain
    "logging_on": 0.5,
    "logging_cooldown_s": 60.0,  # one LOGGING per felled tree

    # GUNSHOT / POACHING
    "shot_merge_s": 1.5,         # observations closer than this = one shot
    "gunshot_min_score": 0.3,
    "poaching_window_s": 600.0,
    "poaching_weight": 0.6,
    "poaching_on": 0.6,
    "poaching_min_shots": 2,
    "poaching_cooldown_s": 600.0,
}

LEVELS = {"CHAINSAWING": "HIGH", "LOGGING": "URGENT",
          "GUNSHOT": "HIGH", "POACHING": "URGENT"}
NOTIFICATION_TYPES = tuple(LEVELS)


# --------------------------------------------------------------------------
# Data passed in and out
# --------------------------------------------------------------------------
@dataclass
class Observation:
    """One frame where a category scored above the observation threshold."""
    time: float          # Unix time of the frame
    sensor_id: int
    category: str        # "chainsaw", "tree_fall", "gunshot", ...
    score: float         # YAMNet category score, 0..1

    def as_dict(self):
        return asdict(self)


@dataclass
class Notification:
    type: str            # CHAINSAWING / LOGGING / GUNSHOT / POACHING
    level: str           # HIGH / URGENT
    probability: float
    time: float          # Unix time of the evidence that triggered it
    sensors: list
    message: str
    evidence: list = field(default_factory=list)   # Observation dicts

    def as_dict(self):
        return asdict(self)


def noisy_or(strengths):
    p_none = 1.0
    for s in strengths:
        p_none *= 1.0 - min(max(s, 0.0), 1.0)
    return 1.0 - p_none


def clock(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------
class IncidentEngine:
    def __init__(self, config=None, log=None):
        self.cfg = DEFAULTS | (config or {})
        self.log = log or (lambda msg: None)   # explanations, e.g. print
        self.subscribers = []                  # (callback, set of types | None)

        # chainsawing state
        self.saw_obs = deque()                 # recent chainsaw Observations
        self.saw_armed = True                  # ready to send CHAINSAWING
        self.saw_confirmed_last = None         # last saw frame of a confirmed episode
        self.saw_peak = 0.0
        self.saw_last_sent = None              # time of last CHAINSAWING sent

        # logging state
        self.last_logging = None

        # gunshot state: list of shots, each {"time","last","score","sensors","obs"}
        self.shots = []
        self.last_poaching = None

    # ---- subscribing -----------------------------------------------------
    def subscribe(self, callback, types=None):
        """callback(notification). types: e.g. {"POACHING"}; None = all."""
        if types is not None:
            types = {t.upper() for t in types}
            unknown = types - set(NOTIFICATION_TYPES)
            if unknown:
                raise ValueError(f"Unknown notification types: {unknown}")
        self.subscribers.append((callback, types))

    def _emit(self, n):
        for callback, types in self.subscribers:
            if types is None or n.type in types:
                try:
                    callback(n)
                except Exception as e:          # one bad subscriber must
                    print(f"Subscriber error: {e!r}", file=sys.stderr)  # not stop the rest

    # ---- input -----------------------------------------------------------
    def observe(self, obs):
        if obs.category == "chainsaw":
            self._on_chainsaw(obs)
        elif obs.category == "tree_fall":
            self._on_tree_fall(obs)
        elif obs.category == "gunshot":
            self._on_gunshot(obs)
        # other categories (vehicle, chopping, ...) are ignored for now

    def tick(self, t):
        """Call once per frame, even when nothing was heard, so P fades."""
        self._update_chainsawing(t)

    # ---- CHAINSAWING -----------------------------------------------------
    def p_chainsawing(self, t):
        c = self.cfg
        while self.saw_obs and t - self.saw_obs[0].time > c["chainsaw_window_s"]:
            self.saw_obs.popleft()
        return noisy_or(
            c["chainsaw_weight"] * o.score
            * math.exp(-max(0.0, t - o.time) / c["chainsaw_decay_s"])
            for o in self.saw_obs)

    def _on_chainsaw(self, obs):
        self.saw_obs.append(obs)
        if not self.saw_armed:                 # episode already confirmed
            self.saw_confirmed_last = obs.time
        self._update_chainsawing(obs.time)

    def _update_chainsawing(self, t):
        c = self.cfg
        p = self.p_chainsawing(t)
        if self.saw_armed and p >= c["chainsawing_on"]:
            self.saw_armed = False
            self.saw_confirmed_last = t
            self.saw_peak = p
            self._send_chainsawing(t, p, still=False)
        elif not self.saw_armed:
            self.saw_peak = max(self.saw_peak, p)
            # Saw still running (heard within the last 2 s): remind again.
            repeat = c["chainsawing_repeat_s"]
            if (repeat > 0 and p >= c["chainsawing_on"] and self.saw_obs
                    and t - self.saw_obs[-1].time <= 2.0
                    and t - self.saw_last_sent >= repeat):
                self._send_chainsawing(t, p, still=True)
            if p < c["chainsawing_off"]:
                self.saw_armed = True
                self.log(f"[{clock(t)}] chainsawing stopped (P={p:.2f}) - re-armed")

    def _send_chainsawing(self, t, p, still):
        c = self.cfg
        self.saw_last_sent = t
        ev = list(self.saw_obs)
        what = "Chainsaw still running" if still else "Chainsaw running"
        self._emit(Notification(
            type="CHAINSAWING", level=LEVELS["CHAINSAWING"],
            probability=round(p, 3), time=t,
            sensors=sorted({o.sensor_id for o in ev}),
            message=(f"{what} ({len(ev)} chainsaw frames in the "
                     f"last {c['chainsaw_window_s']:.0f} s) - possible "
                     f"illegal logging"),
            evidence=[o.as_dict() for o in ev[-10:]]))

    # ---- LOGGING ---------------------------------------------------------
    def _on_tree_fall(self, obs):
        c = self.cfg
        t = obs.time
        last_cut = self.saw_confirmed_last
        if last_cut is None or t - last_cut > c["logging_window_s"]:
            self.log(f"[{clock(t)}] tree fall {obs.score:.2f} ignored - "
                     f"no confirmed chainsawing in the last "
                     f"{c['logging_window_s'] / 60:.0f} min")
            return
        if self.last_logging is not None and \
                t - self.last_logging < c["logging_cooldown_s"]:
            return                             # same tree, already reported
        memory = math.exp(-max(0.0, t - last_cut) / c["cutting_memory_s"])
        fall = min(1.0, obs.score / c["tree_fall_full_score"])
        p = memory * fall
        if p < c["logging_on"]:
            self.log(f"[{clock(t)}] tree fall {obs.score:.2f}, "
                     f"P(logging)={p:.2f} below {c['logging_on']} - not sent")
            return
        self.last_logging = t
        ago = max(0.0, t - last_cut)
        self._emit(Notification(
            type="LOGGING", level=LEVELS["LOGGING"],
            probability=round(p, 3), time=t, sensors=[obs.sensor_id],
            message=(f"Tree fall heard {ago:.0f} s after chainsawing - "
                     f"tree probably felled, illegal logging likely"),
            evidence=[obs.as_dict()]))

    # ---- GUNSHOT / POACHING ---------------------------------------------
    def _on_gunshot(self, obs):
        c = self.cfg
        t = obs.time
        if obs.score < c["gunshot_min_score"]:
            return
        # forget shots that are too old to matter
        self.shots = [s for s in self.shots
                      if t - s["last"] <= c["poaching_window_s"]]

        last = self.shots[-1] if self.shots else None
        if last is not None and t - last["last"] < c["shot_merge_s"]:
            # same bang seen in an overlapping frame (or a second sensor)
            last["last"] = t
            last["score"] = max(last["score"], obs.score)
            last["sensors"].add(obs.sensor_id)
            last["obs"].append(obs)
        else:
            shot = {"time": t, "last": t, "score": obs.score,
                    "sensors": {obs.sensor_id}, "obs": [obs]}
            self.shots.append(shot)
            self._emit(Notification(
                type="GUNSHOT", level=LEVELS["GUNSHOT"],
                probability=round(obs.score, 3), time=t,
                sensors=[obs.sensor_id],
                message="Possible gunshot detected",
                evidence=[obs.as_dict()]))
        self._update_poaching(t)

    def _update_poaching(self, t):
        c = self.cfg
        recent = [s for s in self.shots
                  if t - s["time"] <= c["poaching_window_s"]]
        if len(recent) < c["poaching_min_shots"]:
            return
        if self.last_poaching is not None and \
                t - self.last_poaching < c["poaching_cooldown_s"]:
            return
        p = noisy_or(c["poaching_weight"] * s["score"] for s in recent)
        if p < c["poaching_on"]:
            self.log(f"[{clock(t)}] {len(recent)} shots, P(poaching)={p:.2f} "
                     f"below {c['poaching_on']} - not sent")
            return
        self.last_poaching = t
        span = recent[-1]["time"] - recent[0]["time"]
        self._emit(Notification(
            type="POACHING", level=LEVELS["POACHING"],
            probability=round(p, 3), time=t,
            sensors=sorted(set().union(*(s["sensors"] for s in recent))),
            message=(f"{len(recent)} gunshots within {span:.0f} s - "
                     f"possible poaching"),
            evidence=[o.as_dict() for s in recent for o in s["obs"]]))


# --------------------------------------------------------------------------
# Subscribers (outputs)
# --------------------------------------------------------------------------
def console_printer(n):
    sensors = ", ".join(str(s) for s in n.sensors)
    print(f"\n{'=' * 64}\n"
          f"  [{n.level}] {n.type}   P={n.probability:.2f}   "
          f"{clock(n.time)}   sensor(s) {sensors}\n"
          f"  {n.message}\n"
          f"{'=' * 64}\n", flush=True)


class JsonlLog:
    """Appends every notification (with its evidence) to a JSON-lines file."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a")

    def __call__(self, item):
        d = item.as_dict() if hasattr(item, "as_dict") else item
        self.f.write(json.dumps(d) + "\n")
        self.f.flush()


class NtfyNotifier:
    """Push notifications through ntfy (free, no account needed).

    Posts each notification to <prefix>-<type> and <prefix>-all, in a
    background thread so a slow network never holds up the audio loop.
    """
    PRIORITY = {"URGENT": "5", "HIGH": "4"}
    TAGS = {"CHAINSAWING": "warning,evergreen_tree",
            "LOGGING": "rotating_light,evergreen_tree",
            "GUNSHOT": "warning,boom",
            "POACHING": "rotating_light,boom"}

    def __init__(self, prefix, server="https://ntfy.sh"):
        self.prefix = prefix
        self.server = server.rstrip("/")
        # python.org's macOS Python has no certificate store of its own;
        # certifi's bundle makes HTTPS work there and everywhere else.
        try:
            import certifi
            self.ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            self.ssl_ctx = ssl.create_default_context()
        self.q = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def topics(self, n):
        return [f"{self.prefix}-{n.type.lower()}", f"{self.prefix}-all"]

    def __call__(self, n):
        self.q.put(n)

    def _worker(self):
        while True:
            n = self.q.get()
            title = f"{n.type} ({n.level}) - P={n.probability:.0%}"
            body = (f"{n.message}\nTime {clock(n.time)}, "
                    f"sensor(s) {', '.join(map(str, n.sensors))}")
            for topic in self.topics(n):
                req = urllib.request.Request(
                    f"{self.server}/{topic}", data=body.encode("utf-8"),
                    method="POST",
                    headers={"Title": title,
                             "Priority": self.PRIORITY.get(n.level, "3"),
                             "Tags": self.TAGS.get(n.type, "")})
                try:
                    urllib.request.urlopen(req, timeout=10,
                                           context=self.ssl_ctx).close()
                except Exception as e:
                    print(f"ntfy: could not post to {topic}: {e}",
                          file=sys.stderr)
            self.q.task_done()

    def flush(self, timeout=15):
        """Wait (up to timeout seconds) for queued posts to go out."""
        end = time.time() + timeout
        while self.q.unfinished_tasks and time.time() < end:
            time.sleep(0.1)


# --------------------------------------------------------------------------
# Demo and replay
# --------------------------------------------------------------------------
FRAME_STEP = 0.4875          # one YAMNet frame every 7,800 samples


def demo_story(t0):
    """(time, text or Observation) events telling a forest story."""
    ev = []

    def say(t, text):
        ev.append((t0 + t, text))

    def frames(start, seconds, category, score):
        n = int(seconds / FRAME_STEP)
        for i in range(n):
            ev.append((t0 + start + i * FRAME_STEP,
                       Observation(t0 + start + i * FRAME_STEP, 0,
                                   category, score)))

    say(15, "Tree creaks with no chainsaw before it - should be ignored")
    frames(16, 0.5, "tree_fall", 0.7)
    say(40, "Chainsaw starts and runs for 25 s - expect CHAINSAWING")
    frames(41, 25, "chainsaw", 0.7)
    say(70, "Saw stops; tree falls 5 s later - expect LOGGING")
    frames(71, 1.0, "tree_fall", 0.65)
    say(180, "One gunshot (heard in 2 overlapping frames) - expect one GUNSHOT")
    frames(181, 1.0, "gunshot", 0.55)
    say(240, "Second gunshot a minute later - expect GUNSHOT + POACHING")
    frames(241, 1.0, "gunshot", 0.75)
    say(260, "End of story")
    ev.sort(key=lambda e: e[0])
    return ev


def play(events, engine, speed=0.0, tick_step=FRAME_STEP):
    """Feed (time, Observation|str) events into the engine with ticks between.
    speed 0 = as fast as possible, 1 = real time, 10 = ten times faster."""
    if not events:
        return
    t = events[0][0]
    i = 0
    end = events[-1][0] + tick_step
    while t <= end:
        while i < len(events) and events[i][0] <= t:
            item = events[i][1]
            if isinstance(item, Observation):
                engine.observe(item)
            else:
                print(f"\n--- {item} ---", flush=True)
            i += 1
        engine.tick(t)
        if speed > 0:
            time.sleep(tick_step / speed)
        t += tick_step


def load_observations(path):
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                d = json.loads(line)
                o = Observation(float(d["time"]), int(d["sensor_id"]),
                                d["category"], float(d["score"]))
                out.append((o.time, o))
    out.sort(key=lambda e: e[0])
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--demo", action="store_true", help="run a scripted story")
    src.add_argument("--replay", metavar="FILE",
                     help="replay an observations.jsonl file from run.py")
    p.add_argument("--speed", type=float, default=0.0,
                   help="playback speed: 0 = instant (default), 1 = real time")
    p.add_argument("--ntfy-prefix", help="also push to ntfy topics <prefix>-...")
    p.add_argument("--ntfy-server", default="https://ntfy.sh")
    p.add_argument("--log", help="write notifications to this .jsonl file")
    p.add_argument("--quiet", action="store_true",
                   help="hide explanations of ignored events")
    a = p.parse_args()

    engine = IncidentEngine(log=None if a.quiet else print)
    engine.subscribe(console_printer)
    if a.log:
        engine.subscribe(JsonlLog(a.log))
    ntfy = None
    if a.ntfy_prefix:
        ntfy = NtfyNotifier(a.ntfy_prefix, a.ntfy_server)
        engine.subscribe(ntfy)
        print(f"Pushing to ntfy topics {a.ntfy_prefix}-<type> and "
              f"{a.ntfy_prefix}-all on {a.ntfy_server}")

    events = demo_story(time.time()) if a.demo else load_observations(a.replay)
    play(events, engine, speed=a.speed)
    if ntfy:
        ntfy.flush()


if __name__ == "__main__":
    main()
