---
name: schedule-brief
description: Brief the user on the day ahead and the day just passed: calendar, reminders, and what needs preparing. Use when the user asks "what is on today", for a morning brief, or to plan tomorrow. Read-only: never creates, moves, or cancels anything.
allowed-tools: calendar reminders recall
channel: api
version: 1
permissions:
  tools:
    - calendar
    - reminders
    - recall
  network: []
  files: []
tests: [tests/test_skills_benchmark.py]
metadata:
  author: nanoMuse
---

# Schedule brief

Read-only. If the calendar is not configured, say so and stop.

## Gather

1. **Calendar**: `calendar` action=agenda for today, then for tomorrow. Note what takes the most time, what repeats, and anything that needs preparing.
2. **Reminders**: `reminders` action=list; the ones due today or overdue, in time order.
3. **Memory**: `recall` "schedule" for how the user likes briefs (length, tone, what to leave out).

## Write

A short brief: the day in time order, then what needs preparing, then anything overdue. Times as the user wrote them; do not convert time zones unless asked. Never create, move, or cancel a calendar event or a reminder: this skill only reads, and says what it found. If the user wants a change, ask first and let the change be its own action.
