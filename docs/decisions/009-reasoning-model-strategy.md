# ADR-009: Reasoning Model Strategy

## Status
Accepted

## Context
During Phase 5 (email assistant deepening), the question arose of whether to surface
agent "reasoning" in the UI — showing *why* an agent made a particular decision, not
just the reply it produced. Two distinct approaches exist:

**Scratchpad prompting** — instruct any chat model to write `<think>...</think>` before
its reply. The thinking is extracted, stripped from the final output, and streamed to the
UI as a collapsible "Agent reasoning" accordion.

**Native reasoning models** — OpenAI o1 / o3 / o4-mini, Anthropic Claude with extended
thinking. These models perform a private chain-of-thought in a dedicated reasoning token
budget before producing any output. The reasoning is genuinely driving the answer, not
post-hoc narration.

## Decision
**Phase 5:** Implement scratchpad prompting (`show_reasoning: true` in manifest) using
`gpt-4.1-mini`. Enabled per-agent in the manifest; CoT is extracted via regex and emitted
as a `type=reasoning` SSE event that renders in the UI.

**Future (Phase 7+):** When an agent is configured with an o3/o4 model, capture the
`reasoning` summary from the API response and surface it using the same SSE/UI path.
No frontend changes needed — the `type=reasoning` event is already consumed identically.

## Difference Between the Two Approaches

| | Scratchpad (`show_reasoning: true`) | Native reasoning (o1/o3/o4) |
|---|---|---|
| How it works | Model writes `<think>…</think>` in output | Model reasons in a private hidden token budget |
| Faithfulness | Low — stated reasoning is performative, may not reflect the model's actual computation | Higher — the internal chain meaningfully drives the answer |
| Token cost | No overhead (thinking is part of the response) | Reasoning tokens billed separately; 5–20× output for hard problems |
| Latency | Same as normal completion | 3–10× longer for o1; o4-mini is faster |
| Visibility | Full text visible | Summary only (`reasoning_effort` param) — full tokens not exposed by API |
| Best for | Audit trails, demos, simple SOPs | Complex compliance decisions, multi-step legal reasoning, ambiguous evidence |
| Model support | Any chat model | OpenAI o1/o3/o4-mini, Anthropic Claude 3.7+ extended thinking |

## Manifest Configuration

```yaml
# Scratchpad — any model, zero cost overhead
- name: penalty_waiver
  model: gpt-4.1-mini
  show_reasoning: true   # emits <think> block as type=reasoning SSE event

# Native reasoning — future, Phase 7+
- name: penalty_waiver
  model: o4-mini
  reasoning_effort: medium   # "low" | "medium" | "high" (maps to OpenAI param)
```

## Implementation Notes
- `show_reasoning: bool` is an `AgentConfig` field in `ao-core/ao/config/manifest.py`
- In `ManifestExecutor._make_specialist_node()`, when `show_reasoning=True`:
  1. System prompt includes instruction to wrap reasoning in `<think>…</think>`
  2. After completion, `<think>…</think>` is regex-extracted and stripped from reply
  3. Thinking text is pushed to the SSE token queue as `{"node": ..., "reasoning": text}`
  4. SSE generator emits `type=reasoning` event; frontend renders collapsible accordion
- The `type=reasoning` SSE event path is model-agnostic — whether the text comes from
  scratchpad extraction or a future `response.reasoning` field, the UI path is identical

## Consequences
- `show_reasoning: true` adds no latency and no token cost — suitable for all agents
- Scratchpad reasoning is **not faithful** — treat it as a structured audit log, not ground truth
- Switching a single agent from `gpt-4.1-mini` to `o4-mini` is a one-line manifest change;
  all surrounding infrastructure (tools, HITL, policy checks, Langfuse tracing) is unchanged
- Reasoning tokens for o3/o4 are not exposed at the token level by the OpenAI API;
  only a `reasoning` summary string is returned — this is sufficient for UI display
