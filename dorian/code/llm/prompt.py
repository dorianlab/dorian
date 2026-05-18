"""
dorian/code/llm/prompt.py
--------------------------
Prompt construction and history management for LLM-based rule generation.
"""
import os
from pathlib import Path

from dorian.code.utils import find_graph_difference, dag_to_graph
from dorian.code.parsing.rule import RewriteRule
from dorian.dag import DAG

_BASE_PROMPT_PATH = Path(__file__).parent / "base_prompt.txt"
_HISTORY_PATH = Path("data/llm_chat_history.txt")

_base_prompt: str | None = None


def _get_base_prompt() -> str:
    """Lazily load the base system prompt from disk."""
    global _base_prompt
    if _base_prompt is None:
        _base_prompt = _BASE_PROMPT_PATH.read_text(encoding="utf-8")
    return _base_prompt


def prepare_history() -> str:
    """Load the LLM chat history from the history file."""
    history = ""
    if _HISTORY_PATH.exists():
        history = _HISTORY_PATH.read_text(encoding="utf-8")

    return f"""--Your Responses History and Outcomes--
{history}
--End of History--"""


def add_to_history(new_entry: str) -> None:
    """Append a new entry to the LLM chat history file."""
    _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(new_entry + "\n")


def format_rules_for_llm(rules: list[RewriteRule]) -> str:
    """Format the current rule list as a string for the LLM prompt."""
    return str(rules)


def prepare_full_prompt(
    initial_graph: DAG,
    final_graph: DAG,
    gt_graph: DAG,
    rules: list[RewriteRule],
) -> str:
    """Assemble the complete prompt for LLM rule generation.

    Includes the base prompt, execution history, initial/final graphs,
    current rules, and the difference with the ground truth.
    """
    rules_formatted = format_rules_for_llm(rules)
    execution_history = prepare_history()
    g_difference = find_graph_difference(
        dag_to_graph(gt_graph), dag_to_graph(final_graph)
    )

    return f"""{_get_base_prompt()}
{execution_history}

INITIAL GRAPH: {initial_graph}
RULES: {rules_formatted}
FINAL GRAPH: {final_graph}
DIFFERENCE WITH GROUND TRUTH GRAPH: {g_difference}
    """
