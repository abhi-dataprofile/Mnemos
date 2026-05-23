---
name: semantic_conflict_check
version: v1
output_schema: codereview.agents.conflict.prompts.SemanticCheckResult
description: Decide whether a caller remains compatible with a changed symbol's new signature.
variables: [before_signature, after_signature, caller_qualified_name, caller_file_path, caller_snippet]
system: You are a code reviewer. Judge only what the provided snippet shows; do not speculate about callers you cannot see.
---

A Python function's signature changed in a pull request. You are given
one caller of that function and must decide whether the caller still
works against the new signature.

## Changed symbol

Before:

```python
${before_signature}
```

After:

```python
${after_signature}
```

## Caller

Qualified name: `${caller_qualified_name}`
File: `${caller_file_path}`

```python
${caller_snippet}
```

## Task

Decide whether the caller is still compatible with the new signature.

- If every argument the caller passes still lines up with the new
  parameter list — by position and by type — and the caller does not
  depend on the old return type in a way that would now break, answer
  `compatible: true`.
- Otherwise answer `compatible: false` and name the specific parameter,
  type, or return shape that breaks. Suggest a concrete one-line fix
  the PR author could apply at this call site.

Ignore stylistic differences. Only flag behavioural incompatibilities
that would surface as a runtime or type error.
