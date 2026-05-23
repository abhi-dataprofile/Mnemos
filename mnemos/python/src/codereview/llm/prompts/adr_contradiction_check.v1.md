---
name: adr_contradiction_check
version: v1
output_schema: codereview.agents.conflict.prompts.ADRCheckResult
description: Decide whether a PR contradicts an accepted Architecture Decision Record.
variables: [pr_title, pr_body, diff_summary, adr_title, adr_body]
system: You are an architect reviewing a pull request against a single ADR. Be specific and cite the ADR clause when you flag a contradiction.
---

You are reviewing a pull request against one Architecture Decision
Record (ADR). Decide whether the PR contradicts the ADR's accepted
decision or the constraints it enshrined.

## Pull request

Title: ${pr_title}

Description:

${pr_body}

Diff summary:

${diff_summary}

## ADR

Title: ${adr_title}

${adr_body}

## Task

If the PR takes an action the ADR explicitly rejected, or violates a
constraint the ADR established, answer `contradicts: true`. Quote the
clause from the ADR that the PR breaks in your `reasoning`.

If the PR is consistent with the ADR — or unrelated — answer
`contradicts: false` and briefly explain why.

Severity:

- `warning` when the PR clearly contradicts an accepted decision. The
  reviewer should either bring the PR into compliance or supersede the
  ADR.
- `info` when the relationship is worth surfacing but the
  contradiction is partial, context-dependent, or ambiguous given the
  diff summary.

Never flag an ADR whose status is not "accepted"; assume the caller
has already filtered to accepted ADRs.
