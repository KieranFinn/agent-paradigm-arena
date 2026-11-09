"""Agent Paradigm Arena: ReAct vs Plan-and-Execute vs Reflexion (+ CoT baseline).

Design rule: the three paradigms share everything except control flow.
Tools, tasks, evaluators, traces, pricing and cache all live outside
``agent_arena/frameworks/`` so that changing a paradigm cannot silently
change the exam room.
"""

__version__ = "1.0.0"
