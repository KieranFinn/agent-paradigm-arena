"""Framework registry: the ONLY variable of the experiment lives here."""

from .cot import run_cot
from .plan_execute import run_plan_execute
from .react import run_react
from .reflexion import run_reflexion

FRAMEWORKS = {
    "react": run_react,
    "plan_execute": run_plan_execute,
    "reflexion": run_reflexion,
    "cot": run_cot,
}

AGENTS = ("react", "plan_execute", "reflexion", "cot")
