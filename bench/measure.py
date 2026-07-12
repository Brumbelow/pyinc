"""Wall-clock measurement and deterministic work accounting."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar

from pyinc import Database

_T = TypeVar("_T")


@dataclass(frozen=True)
class WorkMetrics:
    """Per-scenario pyinc work captured around one measured operation."""

    query_executions: int
    query_reuses: int
    query_backdates: int
    resource_loads: int
    memo_nodes: int
    memo_node_delta: int
    dep_graph_edges: int
    dep_graph_edge_delta: int


@dataclass(frozen=True)
class ScenarioResult:
    target: str
    scenario: str
    engine: str
    seconds: float
    matches_fresh: bool
    query_executions: int | None = None
    query_reuses: int | None = None
    query_backdates: int | None = None
    resource_loads: int | None = None
    memo_nodes: int | None = None
    memo_node_delta: int | None = None
    dep_graph_edges: int | None = None
    dep_graph_edge_delta: int | None = None

    @classmethod
    def pyinc(
        cls,
        target: str,
        scenario: str,
        seconds: float,
        matches_fresh: bool,
        work: WorkMetrics,
    ) -> ScenarioResult:
        return cls(
            target=target,
            scenario=scenario,
            engine="pyinc",
            seconds=seconds,
            matches_fresh=matches_fresh,
            query_executions=work.query_executions,
            query_reuses=work.query_reuses,
            query_backdates=work.query_backdates,
            resource_loads=work.resource_loads,
            memo_nodes=work.memo_nodes,
            memo_node_delta=work.memo_node_delta,
            dep_graph_edges=work.dep_graph_edges,
            dep_graph_edge_delta=work.dep_graph_edge_delta,
        )

    @classmethod
    def comparator(
        cls,
        target: str,
        scenario: str,
        engine: str,
        seconds: float,
        matches_fresh: bool,
    ) -> ScenarioResult:
        return cls(target, scenario, engine, seconds, matches_fresh)

    def as_json(self) -> dict[str, object]:
        return {
            "target": self.target,
            "scenario": self.scenario,
            "engine": self.engine,
            "seconds": self.seconds,
            "matches_fresh": self.matches_fresh,
            "query_executions": self.query_executions,
            "query_reuses": self.query_reuses,
            "query_backdates": self.query_backdates,
            "resource_loads": self.resource_loads,
            "memo_nodes": self.memo_nodes,
            "memo_node_delta": self.memo_node_delta,
            "dep_graph_edges": self.dep_graph_edges,
            "dep_graph_edge_delta": self.dep_graph_edge_delta,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> ScenarioResult:
        def required_str(name: str) -> str:
            value = payload.get(name)
            if not isinstance(value, str):
                raise ValueError(f"benchmark worker field {name!r} must be a string")
            return value

        def optional_int(name: str) -> int | None:
            value = payload.get(name)
            if value is None:
                return None
            if type(value) is not int:
                raise ValueError(f"benchmark worker field {name!r} must be an integer or null")
            return value

        seconds = payload.get("seconds")
        matches_fresh = payload.get("matches_fresh")
        if not isinstance(seconds, int | float):
            raise ValueError("benchmark worker field 'seconds' must be numeric")
        if type(matches_fresh) is not bool:
            raise ValueError("benchmark worker field 'matches_fresh' must be a boolean")
        return cls(
            target=required_str("target"),
            scenario=required_str("scenario"),
            engine=required_str("engine"),
            seconds=float(seconds),
            matches_fresh=matches_fresh,
            query_executions=optional_int("query_executions"),
            query_reuses=optional_int("query_reuses"),
            query_backdates=optional_int("query_backdates"),
            resource_loads=optional_int("resource_loads"),
            memo_nodes=optional_int("memo_nodes"),
            memo_node_delta=optional_int("memo_node_delta"),
            dep_graph_edges=optional_int("dep_graph_edges"),
            dep_graph_edge_delta=optional_int("dep_graph_edge_delta"),
        )


def dependency_edge_count(db: Database) -> int:
    """Return the number of dependency edges, not the number of graph nodes."""
    return sum(len(node.dependency_labels) for node in db.dependency_graph())


def measure(fn: Callable[[], _T]) -> tuple[_T, float]:
    """Run ``fn`` once and return its value and wall time.

    Memory tracing is intentionally absent: instrumentation must not perturb the
    wall-clock sample used in the informational timing report.
    """
    start = time.perf_counter()
    value = fn()
    return value, time.perf_counter() - start


def measure_database(db: Database, fn: Callable[[], _T]) -> tuple[_T, float, WorkMetrics]:
    """Measure one database operation and collect its deterministic work delta."""
    nodes_before = db.statistics().node_count
    edges_before = dependency_edge_count(db)
    db.reset_statistics()
    value, seconds = measure(fn)
    stats = db.statistics()
    edges_after = dependency_edge_count(db)
    return (
        value,
        seconds,
        WorkMetrics(
            query_executions=stats.query_executions,
            query_reuses=stats.query_reuses,
            query_backdates=stats.query_backdates,
            resource_loads=stats.resource_loads,
            memo_nodes=stats.node_count,
            memo_node_delta=stats.node_count - nodes_before,
            dep_graph_edges=edges_after,
            dep_graph_edge_delta=edges_after - edges_before,
        ),
    )
