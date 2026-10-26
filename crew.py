"""Connects the four agents and their tasks into one sequential crew."""
from __future__ import annotations

from crewai import Agent, Crew, Process

from tasks import build_tasks


def build_crew(prep: Agent, analysis: Agent, execution: Agent, verification: Agent) -> Crew:
    return Crew(
        agents=[prep, analysis, execution, verification],
        tasks=build_tasks(prep, analysis, execution, verification),
        process=Process.sequential,
        verbose=False,
    )
