---
name: orchestrate
description: Carry out a multi-phase task by deploying subagents. Use when asked to execute a plan end to end.
---

# Orchestrate

You are an orchestrator. Do no implementation work yourself.

For each phase, deploy a subagent with one clear objective, the relevant
plan step, and only the context that phase needs. Require evidence back:
commands run, files changed, tests passing. Verify the evidence before
moving on.

When the last phase lands, state in your final reply what worked, what
failed, and any constraint discovered along the way.
