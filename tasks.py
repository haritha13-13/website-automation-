"""The four tasks of the pipeline, chained so each stage receives the previous stage's output."""
from __future__ import annotations

from crewai import Agent, Task


def build_tasks(prep: Agent, analysis: Agent, execution: Agent, verification: Agent) -> list[Task]:
    prepare = Task(
        description="Read, validate and normalize the Excel/CSV rows.",
        expected_output="JSON summary of validated and rejected rows", agent=prep)
    analyze = Task(
        description="Inspect each website and build a validated field mapping.",
        expected_output="JSON summary of execution plans", agent=analysis, context=[prepare])
    execute = Task(
        description="Fill the editors and save or publish after user approval.",
        expected_output="JSON summary of submissions", agent=execution, context=[analyze])
    verify = Task(
        description="Verify content, publishing status and article URL.",
        expected_output="JSON summary of verification results", agent=verification, context=[execute])
    return [prepare, analyze, execute, verify]
