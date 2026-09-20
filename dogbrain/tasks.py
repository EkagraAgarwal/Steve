"""The fixed task library. Plans may only reference what lives here.

Each task declares the parameters it needs, what it does per tick, when it is
finished, when it has failed, and what may interrupt it. Stepping is plain
code -- no judgment call is made to decide 'approach vs face'.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class TaskSpec:
    name: str
    params: tuple[str, ...] = ()               # required parameter names
    optional: tuple[str, ...] = ()
    done_when: tuple[str, ...] = ()
    fail_when: dict[str, float] = field(default_factory=dict)
    interrupt_by: tuple[str, ...] = ("safety", "owner_command")
    needs_target: bool = False
    description: str = ""


LIBRARY: dict[str, TaskSpec] = {
    "follow": TaskSpec(
        name="follow", params=("target",), needs_target=True,
        done_when=("owner_says_stop", "new_task"), fail_when={"lost_for_ticks": 20},
        description="stay with a specific thing, approaching when far and facing when close",
    ),
    "find": TaskSpec(
        name="find", params=("what",), optional=("save_as",),
        done_when=("candidate_matches",), fail_when={"timeout_ticks": 90},
        description="search the area for something matching a description",
    ),
    "go_to": TaskSpec(
        name="go_to", params=("place",),
        done_when=("arrived",), fail_when={"timeout_ticks": 60},
        description="navigate to a named place",
    ),
    "watch": TaskSpec(
        name="watch", params=("target",), needs_target=True,
        done_when=("owner_says_stop",), fail_when={"timeout_ticks": 60},
        description="hold position and keep facing something",
    ),
    "stay": TaskSpec(
        name="stay", params=("duration_ticks",),
        done_when=("duration", "owner_command"),
        description="sit and remain in place",
    ),
    "greet": TaskSpec(
        name="greet", params=("target",), needs_target=True,
        done_when=("sequence_complete",),
        description="approach someone and perform a friendly greeting",
    ),
    "perform": TaskSpec(
        name="perform", params=("gesture",),
        done_when=("sequence_complete",),
        description="perform a named gesture in place",
    ),
    "return_to_owner": TaskSpec(
        name="return_to_owner", done_when=("owner_close",), fail_when={"timeout_ticks": 90},
        description="come back to the owner",
    ),
    "patrol": TaskSpec(
        name="patrol", params=("duration_ticks",), done_when=("duration",),
        description="patrol the known area",
    ),
}

TASK_NAMES: tuple[str, ...] = tuple(LIBRARY)


def spec(name: str) -> Optional[TaskSpec]:
    return LIBRARY.get(name)
