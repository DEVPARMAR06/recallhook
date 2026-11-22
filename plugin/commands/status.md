---
description: Check whether recallhook memory is working, and how to fix problems
allowed-tools: Bash(recallhook status:*)
---

## Current recallhook status

!`recallhook status 2>&1 || true`

## Your task

Summarize the status above in two or three short lines. If any line starts with [FAIL], give the exact fix command it names. If the command was not found, tell the user to run `pipx install recallhook` and then `recallhook init --no-hook`. Do not run any other commands.
