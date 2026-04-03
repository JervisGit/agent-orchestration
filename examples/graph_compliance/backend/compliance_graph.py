"""Synthetic compliance knowledge graph built with NetworkX.

This replaces Neo4j for local development and testing.
In production, the tool callables in app.py would call Neo4j Cypher queries
instead — the agents and the manifest remain unchanged.

ADR-015: NetworkX is used here because:
  - Zero infrastructure: no Docker container, no server, no schema migration
  - Identical graph API to what NetworkX offers; easy to understand + explain
  - The tool interface (find_entity, get_neighbors, find_path, get_risk_indicators)
    is identical whether backed by NetworkX or Neo4j — only the implementation changes
  - For demo purposes the synthetic data covers a realistic compliance scenario:
    a shell company structure with circular ownership and shared directors

Entity types: Company, Person, Account
Relationship types: OWNS, CONTROLS, DIRECTOR_OF, SHARED_DIRECTOR_WITH,
                    HAS_ACCOUNT, TRANSACTED_WITH
"""

from __future__ import annotations

import networkx as nx

# ── Graph construction ────────────────────────────────────────────────

_G: nx.DiGraph = nx.DiGraph()

# ── Entities ─────────────────────────────────────────────────────────

ENTITIES: list[dict] = [
    # Companies
    {"id": "C001", "type": "Company", "name": "Apex Holdings Pte Ltd",      "country": "SG", "risk_level": "HIGH",   "flags": ["circular_ownership", "shell_company"]},
    {"id": "C002", "type": "Company", "name": "BlueStar Trading Ltd",        "country": "SG", "risk_level": "MEDIUM", "flags": ["frequent_director_change"]},
    {"id": "C003", "type": "Company", "name": "Nexus Capital Pte Ltd",       "country": "VG", "risk_level": "HIGH",   "flags": ["offshore", "shell_company"]},
    {"id": "C004", "type": "Company", "name": "Meridian Logistics Pte Ltd",  "country": "SG", "risk_level": "LOW",    "flags": []},
    {"id": "C005", "type": "Company", "name": "Cascade Investments Ltd",     "country": "KY", "risk_level": "HIGH",   "flags": ["offshore", "circular_ownership"]},
    {"id": "C006", "type": "Company", "name": "TrueNorth Advisory Pte Ltd",  "country": "SG", "risk_level": "LOW",    "flags": []},
    {"id": "C007", "type": "Company", "name": "Phoenix Resources Corp",      "country": "HK", "risk_level": "MEDIUM", "flags": ["frequent_large_transactions"]},
    # Persons
    {"id": "P001", "type": "Person", "name": "Tan Wei Liang",                "nationality": "SG", "risk_level": "MEDIUM", "flags": ["multiple_directorships", "pep_adjacent"]},
    {"id": "P002", "type": "Person", "name": "Sarah Chen Mei Ling",          "nationality": "SG", "risk_level": "LOW",    "flags": []},
    {"id": "P003", "type": "Person", "name": "Raj Kumar Nair",               "nationality": "IN", "risk_level": "LOW",    "flags": []},
    {"id": "P004", "type": "Person", "name": "James Lim Boon Huat",         "nationality": "SG", "risk_level": "HIGH",   "flags": ["pep", "sanctions_adjacent"]},
    {"id": "P005", "type": "Person", "name": "Fatima Al-Hassan",             "nationality": "AE", "risk_level": "MEDIUM", "flags": ["offshore_accounts"]},
    # Accounts
    {"id": "A001", "type": "Account", "name": "Apex DBS Corp Acct #8821",    "bank": "DBS",     "risk_level": "HIGH",    "flags": ["large_cash_deposits", "structuring_risk"]},
    {"id": "A002", "type": "Account", "name": "BlueStar OCBC Acct #4412",    "bank": "OCBC",    "risk_level": "LOW",     "flags": []},
    {"id": "A003", "type": "Account", "name": "Nexus Cayman Acct #9901",     "bank": "Cayman",  "risk_level": "HIGH",    "flags": ["offshore_account", "suspicious_outflows"]},
    {"id": "A004", "type": "Account", "name": "James Lim Personal #3371",   "bank": "UOB",     "risk_level": "HIGH",    "flags": ["structuring_risk", "pep_account"]},
]

# Index by id for fast lookup
_ENTITY_BY_ID: dict[str, dict] = {e["id"]: e for e in ENTITIES}

# ── Relationships ─────────────────────────────────────────────────────

EDGES: list[tuple[str, str, str, dict]] = [
    # (from_id, to_id, relationship_type, attributes)
    ("C001", "C002", "OWNS",             {"pct": 60, "since": "2019"}),
    ("C001", "C005", "OWNS",             {"pct": 100, "since": "2021"}),
    ("C005", "C003", "OWNS",             {"pct": 80, "since": "2022"}),
    ("C003", "C001", "OWNS",             {"pct": 40, "since": "2023"}),   # circular!
    ("C002", "C007", "OWNS",             {"pct": 35, "since": "2020"}),
    ("C001", "C004", "CONTROLS",         {"type": "operational"}),
    ("P001", "C001", "DIRECTOR_OF",      {"appointed": "2018-03-01"}),
    ("P001", "C002", "DIRECTOR_OF",      {"appointed": "2019-06-15"}),
    ("P001", "C005", "DIRECTOR_OF",      {"appointed": "2021-01-10"}),   # 3 directorships
    ("P002", "C004", "DIRECTOR_OF",      {"appointed": "2017-11-20"}),
    ("P003", "C006", "DIRECTOR_OF",      {"appointed": "2020-04-05"}),
    ("P004", "C001", "CONTROLS",         {"type": "beneficial_owner"}),
    ("P004", "C003", "CONTROLS",         {"type": "beneficial_owner"}),
    ("P005", "C007", "DIRECTOR_OF",      {"appointed": "2022-09-01"}),
    ("C001", "A001", "HAS_ACCOUNT",      {"opened": "2018-05-01"}),
    ("C002", "A002", "HAS_ACCOUNT",      {"opened": "2019-03-15"}),
    ("C003", "A003", "HAS_ACCOUNT",      {"opened": "2021-07-20"}),
    ("P004", "A004", "HAS_ACCOUNT",      {"opened": "2015-01-01"}),
    ("A001", "A003", "TRANSACTED_WITH",  {"total_sgd": 4_200_000, "tx_count": 47, "period": "2022-2024"}),
    ("A001", "A004", "TRANSACTED_WITH",  {"total_sgd": 650_000,   "tx_count": 12, "period": "2023-2024"}),
    ("A002", "A001", "TRANSACTED_WITH",  {"total_sgd": 1_100_000, "tx_count": 23, "period": "2021-2024"}),
    ("C001", "C002", "SHARED_DIRECTOR_WITH", {"person": "P001"}),
    ("C001", "C005", "SHARED_DIRECTOR_WITH", {"person": "P001"}),
]

# Populate the graph
for entity in ENTITIES:
    _G.add_node(entity["id"], **entity)

for src, dst, rel, attrs in EDGES:
    _G.add_edge(src, dst, relationship=rel, **attrs)

# ── Tool callables ────────────────────────────────────────────────────

def find_entity(name: str) -> dict:
    """Find entities whose name contains the query string (case-insensitive)."""
    q = name.lower()
    matches = [
        e for e in ENTITIES
        if q in e["name"].lower()
    ]
    if not matches:
        return {"found": False, "results": [], "message": f"No entity found matching '{name}'."}
    return {
        "found": True,
        "results": [
            {
                "id": e["id"],
                "name": e["name"],
                "type": e["type"],
                "risk_level": e["risk_level"],
                "flags": e["flags"],
                **{k: v for k, v in e.items() if k not in ("id", "name", "type", "risk_level", "flags")},
            }
            for e in matches
        ],
    }


def get_neighbors(entity_id: str, relationship_type: str = "") -> dict:
    """Get direct neighbours of an entity, optionally filtered by relationship type."""
    if entity_id not in _G:
        return {"found": False, "entity_id": entity_id, "message": "Entity not found in graph."}

    entity = _ENTITY_BY_ID.get(entity_id, {"id": entity_id})
    outgoing = []
    for dst in _G.successors(entity_id):
        edge = _G[entity_id][dst]
        rel = edge.get("relationship", "UNKNOWN")
        if relationship_type and rel != relationship_type.upper():
            continue
        dst_entity = _ENTITY_BY_ID.get(dst, {"id": dst, "name": dst})
        outgoing.append({
            "entity": {"id": dst_entity["id"], "name": dst_entity["name"], "type": dst_entity.get("type"), "risk_level": dst_entity.get("risk_level")},
            "relationship": rel,
            "attributes": {k: v for k, v in edge.items() if k != "relationship"},
        })

    incoming = []
    for src in _G.predecessors(entity_id):
        edge = _G[src][entity_id]
        rel = edge.get("relationship", "UNKNOWN")
        if relationship_type and rel != relationship_type.upper():
            continue
        src_entity = _ENTITY_BY_ID.get(src, {"id": src, "name": src})
        incoming.append({
            "entity": {"id": src_entity["id"], "name": src_entity["name"], "type": src_entity.get("type"), "risk_level": src_entity.get("risk_level")},
            "relationship": rel,
            "attributes": {k: v for k, v in edge.items() if k != "relationship"},
        })

    return {
        "found": True,
        "entity": {"id": entity_id, "name": entity.get("name", entity_id)},
        "outgoing": outgoing,
        "incoming": incoming,
        "total_connections": len(outgoing) + len(incoming),
    }


def find_path(from_entity_id: str, to_entity_id: str) -> dict:
    """Find the shortest path between two entities in the compliance graph."""
    if from_entity_id not in _G:
        return {"found": False, "message": f"Entity '{from_entity_id}' not found."}
    if to_entity_id not in _G:
        return {"found": False, "message": f"Entity '{to_entity_id}' not found."}
    try:
        path_ids = nx.shortest_path(_G, source=from_entity_id, target=to_entity_id)
    except nx.NetworkXNoPath:
        return {"found": False, "message": f"No path found between {from_entity_id} and {to_entity_id}."}

    path_entities = []
    for eid in path_ids:
        e = _ENTITY_BY_ID.get(eid, {"id": eid, "name": eid})
        path_entities.append({"id": eid, "name": e.get("name", eid), "type": e.get("type"), "risk_level": e.get("risk_level")})

    edges_on_path = []
    for i in range(len(path_ids) - 1):
        edge = _G[path_ids[i]][path_ids[i + 1]]
        edges_on_path.append(edge.get("relationship", "UNKNOWN"))

    return {
        "found": True,
        "path_length": len(path_ids) - 1,
        "entities": path_entities,
        "relationships": edges_on_path,
        "summary": " → ".join(
            f"{e['name']} [{r}]" if r else e["name"]
            for e, r in zip(path_entities, edges_on_path + [""])
        ).rstrip(" []"),
    }


def get_risk_indicators(entity_id: str) -> dict:
    """Get all risk flags and scores for an entity, including second-degree exposure."""
    if entity_id not in _G:
        return {"found": False, "entity_id": entity_id, "message": "Entity not found."}

    entity = _ENTITY_BY_ID.get(entity_id, {})
    own_flags = entity.get("flags", [])
    own_risk = entity.get("risk_level", "UNKNOWN")

    # Second-degree: flags from direct neighbours
    second_degree: list[dict] = []
    for nbr_id in list(_G.successors(entity_id)) + list(_G.predecessors(entity_id)):
        nbr = _ENTITY_BY_ID.get(nbr_id, {})
        nbr_flags = nbr.get("flags", [])
        if nbr_flags or nbr.get("risk_level") == "HIGH":
            second_degree.append({
                "entity_id": nbr_id,
                "entity_name": nbr.get("name", nbr_id),
                "risk_level": nbr.get("risk_level"),
                "flags": nbr_flags,
            })

    # Circular ownership detection (simple: check if entity can reach itself)
    try:
        cycle_path = nx.find_cycle(_G, source=entity_id)
        cycle_detected = True
        cycle_nodes = list({u for u, v, *_ in cycle_path} | {v for u, v, *_ in cycle_path})
        cycle_entity_names = [_ENTITY_BY_ID.get(n, {}).get("name", n) for n in cycle_nodes]
    except nx.NetworkXNoCycle:
        cycle_detected = False
        cycle_entity_names = []

    overall_score = (
        3 if own_risk == "HIGH" else
        2 if own_risk == "MEDIUM" else
        1
    ) + (1 if second_degree else 0) + (2 if cycle_detected else 0)

    return {
        "found": True,
        "entity_id": entity_id,
        "entity_name": entity.get("name", entity_id),
        "own_risk_level": own_risk,
        "own_flags": own_flags,
        "second_degree_exposure": second_degree,
        "circular_ownership_detected": cycle_detected,
        "circular_ownership_entities": cycle_entity_names,
        "composite_risk_score": overall_score,
        "risk_summary": (
            "CRITICAL — circular ownership + high-risk connections"
            if cycle_detected and second_degree else
            "HIGH — direct flags present"
            if own_risk == "HIGH" else
            "MEDIUM — indirect exposure via connections"
            if second_degree else
            "LOW — no significant risk indicators"
        ),
    }


# ── Graph statistics (for /api/graph/stats endpoint) ─────────────────

def graph_stats() -> dict:
    return {
        "node_count": _G.number_of_nodes(),
        "edge_count": _G.number_of_edges(),
        "entity_types": {
            t: sum(1 for e in ENTITIES if e["type"] == t)
            for t in ("Company", "Person", "Account")
        },
        "high_risk_entities": [
            {"id": e["id"], "name": e["name"], "type": e["type"]}
            for e in ENTITIES if e["risk_level"] == "HIGH"
        ],
    }
