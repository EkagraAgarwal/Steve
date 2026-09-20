"""Scripted scenarios: a list of ticks, each a State the brain reacts to."""

from __future__ import annotations

from dogbrain.state import Heard, Scene, Seen, SelfState, State


def _s(tick, seen=(), heard=None, quiet=0.0, owner=False, battery=90.0, posture="standing"):
    return State(
        self_=SelfState(posture=posture, battery=battery),
        seen=tuple(seen),
        heard=Heard(heard) if heard else None,
        scene=Scene(quiet_for_s=quiet, owner_visible=owner),
        tick=tick,
    )


BALL = lambda d, b=0.0: Seen("sports_ball_3", "sports ball", d, b, motion="still")
BALL2 = lambda d, b: Seen("sports_ball_7", "sports ball", d, b)
OWNER = lambda d, b=0.0: Seen("owner", "person", d, b)


FOLLOW_THE_BALL = [
    _s(0, seen=[BALL(3.0), OWNER(2.0, 30)], owner=True),
    _s(1, seen=[BALL(3.0), OWNER(2.0, 30)], heard="follow this ball", owner=True),
    _s(2, seen=[BALL(2.4)], owner=True),
    _s(3, seen=[BALL(0.9)]),                       # close -> face
    _s(4, seen=[]),                                # lost -> search
    _s(5, seen=[]),
    _s(6, seen=[BALL(1.8)]),                       # reacquired
    _s(7, seen=[BALL(1.8)], heard="stop"),         # owner stops it
]

AMBIGUOUS = [
    _s(0, seen=[BALL(2.0), BALL2(2.1, -40)]),
    _s(1, seen=[BALL(2.0), BALL2(2.1, -40)], heard="go to the kitchen and find the ball then follow it"),
]

SAFETY = [
    _s(0, seen=[BALL(3.0)], heard="follow this ball"),
    _s(1, seen=[BALL(2.0), Seen("wall_1", "wall", 0.3, 5)]),   # reflex must win
]

IDLE = [
    _s(0, quiet=45.0),
    _s(1, quiet=50.0),
]

ALL = {
    "follow": FOLLOW_THE_BALL,
    "ambiguous": AMBIGUOUS,
    "safety": SAFETY,
    "idle": IDLE,
}


# A longer run that leans on memory: the target is occluded twice, reacquired
# under a NEW track id, and the plan carries a variable between steps.
BALL_NEW_ID = lambda d, b=0.0: Seen("sports_ball_11", "sports ball", d, b)
CHAIR = lambda d, b: Seen("chair_2", "chair", d, b)

COMPLEX = [
    _s(0,  seen=[OWNER(2.0, 20), CHAIR(3.0, -50)], owner=True),
    _s(1,  seen=[OWNER(2.0, 20), CHAIR(3.0, -50)], owner=True,
           heard="go to the kitchen, find the ball, and follow it"),
    _s(2,  seen=[CHAIR(2.6, -40)]),
    _s(3,  seen=[]),
    _s(4,  seen=[BALL(3.4, 10)]),          # candidate appears
    _s(5,  seen=[BALL(2.6, 5)]),
    _s(6,  seen=[BALL(1.2, 0)]),
    _s(7,  seen=[]),                        # occluded
    _s(8,  seen=[]),
    _s(9,  seen=[BALL_NEW_ID(1.6, -8)]),    # same ball, new track id
    _s(10, seen=[BALL_NEW_ID(0.8, 0)]),
    _s(11, seen=[BALL_NEW_ID(0.8, 0)], heard="stop"),
]

ALL["complex"] = COMPLEX
