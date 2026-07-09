# SELF_ASSESSMENT: I expose the memory→concept-graph ingester so Iris can run/verify the mechanical brain-tab mirror on demand and start its background rescan in a live process.
"""
graph_tool — on-demand surface for brain/graph_ingest (the mechanical mirror).

graph_ingest_run: run one ingest pass now (params: force to re-ingest all
files ignoring the mtime cache; start_thread to also start the background
rescan loop in this process). This was the live-activation path on
2026-07-08 — the boot wiring in iris_runtime only fires on later restarts.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.tool_registry import register_tool


def _graph_ingest_run(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    cg = g.get("_concept_graph")
    if cg is None:
        return {"ok": False, "error": "concept graph not initialized"}
    from brain.graph_ingest import ingest_all, ingest_dynamic, start_ingest_thread
    root = Path(g.get("BASE_DIR") or ".")
    result = ingest_all(cg, root, force=bool(params.get("force")))
    result["dynamic"] = ingest_dynamic(cg, root)
    if params.get("start_thread"):
        result["thread_started"] = start_ingest_thread(cg, root)
    return result


register_tool(
    "graph_ingest_run",
    "Ingest profiles + memory notes (+wikilink edges) into the concept graph now; optional background rescan thread.",
    1,
    _graph_ingest_run,
)
