"""Shared plumbing for the pipeline's CrewAI agents.

Three of the four agents (content preparation, browser execution, verification) must be
deterministic, so their CrewAI Agent is backed by StageLLM: when CrewAI asks the "model" for
the task result, StageLLM runs the stage's Python handler instead of calling a language model.
CrewAI still owns sequencing and context passing; Python does the work.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from crewai import Agent, BaseLLM

logger = logging.getLogger(__name__)


class StageLLM(BaseLLM):
    llm_type: str = "stage_dispatch"
    handler: Any = None  # Callable[[], str]

    def __init__(self, handler: Callable[[], str], **kwargs: Any) -> None:
        super().__init__(model="stage-dispatch", temperature=0.0, handler=handler, **kwargs)

    def call(self, messages, tools=None, callbacks=None, available_functions=None,
             from_task=None, from_agent=None, response_model=None):
        result = self.handler()
        return f"Thought: The stage handler has finished.\nFinal Answer: {result}"

    def supports_function_calling(self) -> bool:
        return False

    def supports_stop_words(self) -> bool:
        return False

    def get_context_window_size(self) -> int:
        return 128_000


def build_stage_agent(role: str, goal: str, backstory: str, handler: Callable[[], str]) -> Agent:
    return Agent(role=role, goal=goal, backstory=backstory, llm=StageLLM(handler),
                 allow_delegation=False, verbose=False)
