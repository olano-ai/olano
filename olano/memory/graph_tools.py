"""Agent-facing tools for querying and updating the memory graph.

These tools allow an agent to explore its entity-relationship graph:
  - Search for entities by name or type
  - Get relationships (neighbors) for an entity
  - Add new entities or relationships during conversation
  - Summarize the graph as structured text
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# Module-level graph reference (set by builder when agent boots)
_graph_path: Path | None = None


def set_graph_path(path: Path) -> None:
    """Set the path to the agent's memory graph DB (called by builder)."""
    global _graph_path
    _graph_path = path


def _get_graph():
    """Get MemoryGraph instance, returning None if not configured."""
    from olano.memory.memory_graph import MemoryGraph

    if _graph_path is None:
        return None
    return MemoryGraph(_graph_path)


@tool
def graph_search_entities(
    name: Annotated[str, "Entity name to search (partial match supported)"] = "",
    entity_type: Annotated[str, "Filter by type: person, project, concept, tool, org, place"] = "",
) -> str:
    """Search the memory graph for entities by name or type."""
    graph = _get_graph()
    if graph is None:
        return "Memory graph not initialized for this agent."

    entities = graph.find_entities(name=name or None, entity_type=entity_type or None)
    if not entities:
        return f"No entities found matching name='{name}' type='{entity_type}'."

    lines = []
    for e in entities[:20]:
        desc = f" — {e.description}" if e.description else ""
        lines.append(f"- [{e.id}] **{e.name}** ({e.type}){desc}")
    return "\n".join(lines)


@tool
def graph_get_neighbors(
    entity_id: Annotated[str, "Entity ID to get neighbors for"],
    relation: Annotated[str, "Filter by relation type (optional, e.g. 'knows', 'works_on')"] = "",
    direction: Annotated[str, "Direction: out, in, or both"] = "both",
) -> str:
    """Get entities related to a given entity in the memory graph."""
    graph = _get_graph()
    if graph is None:
        return "Memory graph not initialized."

    neighbors = graph.get_neighbors(entity_id, relation=relation or None, direction=direction)
    if not neighbors:
        return f"No neighbors found for entity {entity_id}."

    lines = []
    for n in neighbors:
        arrow = "→" if n["direction"] == "out" else "←"
        e = n["entity"]
        lines.append(f"  {arrow} [{n['relation']}] **{e.name}** ({e.type})")
    return f"Neighbors of {entity_id}:\n" + "\n".join(lines)


@tool
def graph_add_entity(
    name: Annotated[str, "Entity name"],
    entity_type: Annotated[str, "Type: person, project, concept, tool, org, place"],
    description: Annotated[str, "Brief description"] = "",
) -> str:
    """Add or update an entity in the memory graph."""
    graph = _get_graph()
    if graph is None:
        return "Memory graph not initialized."

    entity_id = graph.upsert_entity(name, entity_type, description=description)
    return f"Entity saved: [{entity_id}] {name} ({entity_type})"


@tool
def graph_add_relationship(
    from_name: Annotated[str, "Source entity name"],
    from_type: Annotated[str, "Source entity type"],
    to_name: Annotated[str, "Target entity name"],
    to_type: Annotated[str, "Target entity type"],
    relation: Annotated[str, "Relationship type (e.g. knows, works_on, uses, is_a, reports_to)"],
) -> str:
    """Add a relationship between two entities in the memory graph.

    Entities are created if they don't exist.
    """
    graph = _get_graph()
    if graph is None:
        return "Memory graph not initialized."

    from_id = graph.upsert_entity(from_name, from_type)
    to_id = graph.upsert_entity(to_name, to_type)
    rel_id = graph.add_relationship(from_id, to_id, relation)
    return f"Relationship saved: {from_name} —[{relation}]→ {to_name} (id={rel_id})"


@tool
def graph_summary() -> str:
    """Get a summary of the memory graph: entity count, relationship count, and top entities."""
    graph = _get_graph()
    if graph is None:
        return "Memory graph not initialized."

    entity_count = graph.entity_count()
    rel_count = graph.relationship_count()

    if entity_count == 0:
        return "Memory graph is empty. Use graph_add_entity to populate it."

    # Get a sampling of entities
    entities = graph.find_entities()[:10]
    lines = [f"Memory Graph: {entity_count} entities, {rel_count} relationships"]
    lines.append("\nTop entities:")
    for e in entities:
        lines.append(f"  - {e.name} ({e.type})")
    return "\n".join(lines)


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
