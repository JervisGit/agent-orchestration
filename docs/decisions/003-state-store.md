# ADR-003: PostgreSQL + Redis for State and Memory

## Status
Accepted

## Context
Agent workflows need both fast ephemeral state (conversation context, session) and durable long-term storage (persistent facts, embeddings, audit logs). We also need vector search for knowledge retrieval.

## Decision
Use **PostgreSQL Flexible Server** (with pgvector extension) for durable state and **Azure Cache for Redis** for ephemeral state.

## Alternatives Considered

| Option | Pros | Cons |
|---|---|---|
| **PostgreSQL + Redis** | PG is versatile (relational + vector via pgvector), Redis is fast for sessions | Two services to manage |
| Cosmos DB | Managed, multi-model | Expensive at scale, no native vector search (need separate AI Search), less SQL tooling |
| Redis only | Simple, fast | Not durable for long-term, no vector search |

## Consequences
- Two data stores to operate (PG + Redis)
- pgvector enables vector search without a separate service
- Redis TTL handles automatic session cleanup
