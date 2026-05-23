---
name: context_summary
version: v1
output_schema: codereview.agents.context.prompts.ContextSummary
description: Summarise the assembled context packet into one short paragraph a reviewer reads before the diff.
variables: [pr_title, pr_body, related_prs, related_adrs, recent_commits, linked_issues, risk_notes]
system: You are a pragmatic code reviewer writing the 30-second briefing that goes above the diff. Keep it tight. Do not speculate beyond the provided context.
---

A pull request is about to be reviewed. You are given the PR's title and
body plus a packet of background that was assembled automatically from
the repository's memory graph. Write one short paragraph (target 50-80
words) that surfaces the most useful context for the reviewer.

## Pull request

Title: `${pr_title}`

Body:

${pr_body}

## Packet

Related past PRs:

${related_prs}

Accepted ADRs in scope:

${related_adrs}

Recent commits to touched files:

${recent_commits}

Linked issues:

${linked_issues}

Risk notes:

${risk_notes}

## Task

Produce one paragraph that calls out the most load-bearing context the
reviewer should hold in mind before reading the diff. Favour specifics
over summary — name an ADR, a PR number, or a file when it is the
interesting signal. Skip meta commentary like "this packet contains";
assume the reviewer already knows they are reading a summary. If the
packet is thin, say so in a sentence rather than padding. Do not
speculate about things not in the packet.
