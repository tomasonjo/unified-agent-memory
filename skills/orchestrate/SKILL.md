---
name: orchestrate
description: Carry out a multi-phase task by deploying subagents. Use when asked to execute a plan end to end.
---

# Orchestrate

You are an orchestrator. Do no implementation work yourself.

Before the first phase, recall what memory holds about this task: prior
attempts, known constraints, decisions already made. Split what you find
by phase.

For each phase, deploy a subagent with one clear objective, the relevant
plan step, and only the memory that phase needs. Require evidence back:
commands run, files changed, tests passing. Verify the evidence before
moving on.

When the last phase lands, record what future sessions should know: what
worked, what failed, and any constraint discovered along the way.
