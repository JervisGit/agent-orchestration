# ADR-001: LangGraph as Orchestration Framework

## Status
Accepted

## Context
We need a framework to orchestrate agentic workflows across multiple DSAI applications, each potentially using different patterns (linear, multi-agent, planner-based). The framework must support HITL, checkpointing, and state machines natively.

## Decision
Use **LangGraph** as the primary orchestration engine, wrapped behind an abstract `OrchestrationEngine` interface for future swappability.

## Alternatives Considered

| Framework | Pros | Cons |
|---|---|---|
| **LangGraph** | Native HITL, checkpointing, state machines, Python-native, active community | Tied to LangChain ecosystem |
| Semantic Kernel | Microsoft-backed, .NET + Python, good Azure integration | Less mature agentic patterns, weaker HITL |
| AutoGen | Strong multi-agent patterns | Less flexible single-agent flows, heavier setup |
| CrewAI | Simple multi-agent API | Less control over state, weaker checkpointing |

## Consequences
- Dependency on LangChain ecosystem (langchain-core, langgraph)
- Abstract interface allows migration if needed
- Team needs to learn LangGraph's state graph model
