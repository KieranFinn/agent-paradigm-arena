"""Task registry: three tasks chosen for orthogonal structural properties.

            information need knowable upfront?
        discover-at-runtime          fully-known-upfront
        +----------------------------+----------------------------+
 hard   |                            |                            |
 check  |   T1 explore               |   T3 verify                |
 exists |   (repo debugging)         |   (hidden unit tests)      |
        +----------------------------+----------------------------+
 none   |   (explorable but          |   T2 plan                  |
        |    unscoreable)            |   (warehouse analysis)     |
        +----------------------------+----------------------------+
"""

from .base import EvalResult, RunResult, TaskBase, TaskMeta
from .t1_explore import T1ExploreTask
from .t2_plan import T2PlanTask
from .t3_verify import T3VerifyTask

TASKS = {
    "T1": T1ExploreTask,
    "T2": T2PlanTask,
    "T3": T3VerifyTask,
}

DIFFICULTIES = ("easy", "hard")


def make_task(name: str, difficulty: str) -> TaskBase:
    if name not in TASKS:
        raise ValueError(f"unknown task {name!r}; known: {sorted(TASKS)}")
    if difficulty not in DIFFICULTIES:
        raise ValueError(f"unknown difficulty {difficulty!r}")
    return TASKS[name](difficulty)
