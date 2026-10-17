# Documentation and Work-Record Guidelines

## 1. Sources of truth and writing style

Requirements answer what the system must do; design explains how and why; tests explain how behavior is proven; the backlog records known gaps. Do not repeat the full configuration, interface contract, or command set in every file.

Documentation is written in English, while code identifiers and protocol fields retain their original spelling. Write for someone who did not participate in the conversation: name the actor, trigger, result, and boundary explicitly. Avoid phrases such as “this turn” or references that depend on chat history.

Primary documents contain only current information. Do not append historical versions, change logs, test-count evolution, or abandoned designs. Keep the current rationale and tradeoffs for significant choices. Git, pull requests, and test systems hold historical diffs, discussion, and detailed run output.

Distinguish current implementation, normative requirements, and future work. State whether a numeric limit comes from configuration or a requirement. Provider parameters, timeouts, and demo observations are not performance guarantees.

## 2. Where to update documentation

| Change impact | Update location |
| --- | --- |
| User behavior, scope, or quality objective | `requirements.md`, plus related acceptance cases. |
| Technology choice, layering, cross-module interface, error code, schema, or permission | `system-design.md`; update requirements and tests when necessary. |
| Task breakdown, dependency, blocker, or completion condition | Current pull request, issue, or handoff; do not maintain another plan document. |
| Test, category, command, gate, or manual scenario | `testing-and-validation.md`, using the consistent module → test/tool/scenario → expected-result format; mark incomplete evidence explicitly. |
| Configuration, operation, deployment, or troubleshooting | Setup/scenario sections of `testing-and-validation.md`; update `system-design.md` for architectural effects. |
| Established rule or automated gate | Root `AGENTS.MD` and the actual tool configuration; update `improvement-backlog.md`. |
| Known unresolved gap | `improvement-backlog.md`, including location, impact, recommendation, and validation exit; update the existing ID. |
| Internal implementation with no behavior or interface change | Usually no requirements/design edit; state “no documentation contract change” in the handoff. |

Documentation cleanup does not itself authorize changes to README, root `AGENTS.MD`, or tool configuration. Those files change only when the task scope includes them.

## 3. Maintaining current status

Update the existing entry for one task, issue, or requirement; do not append duplicates or reuse IDs. Completed work may be removed from the current backlog because Git preserves history. Work that has not been accepted must not be marked complete. Leave undefined requirements blank instead of inventing assumptions.

Use these task states: pending clarification, pending implementation, in progress, pending acceptance, blocked, and complete. A blocked item names the missing condition. Ownership, the next action, and existing evidence need only be sufficient for another engineer to continue; daily status reports are not required.

## 4. Minimum handoff and validation evidence

Every delivery records the following in its pull request, issue, or final report. If none exists, use a temporary handoff file rather than appending run logs to primary documentation.

```text
Task / requirement ID:
Code location: commit ID, or working-tree scope when uncommitted
Completed behavior:
Interface / data impact:
Validation: command, environment, pass/fail/skip/not-run, and reason
Evidence location: test output or controlled artifact path
Remaining issues / next step:
Manual acceptance: scenario, expected result, actual result, reviewer; state not run explicitly
```

Validation artifacts may record runtime, model/configuration summaries, and audio-source information for reproducibility. They are evidence, not historical versions of the primary documents. Never store secrets, complete environments, or unnecessary personal data. Restrict access to recordings and conversations. Do not report a check as passed unless it was actually run.

## 5. Definition of done

Applicable requirements and contracts agree; necessary checks have run and have evidence; risks and omitted checks are explicit; the current task or backlog entry is updated; links work; and primary documentation contains no stale description. Work requiring human acceptance remains pending acceptance until a person signs it off; an AI summary is not a substitute.
