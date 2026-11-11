"""Fixed read-only, parameter-bound EXPLAIN measurement; never arbitrary SQL."""
from datetime import date
from statistics import mean
import copy

from .catalog import EXPLAIN, Q
from .workload import digest, frequencies


class MeasurementError(RuntimeError):
    def __init__(self, details):
        self.details = details
        super().__init__("Incomplete measurement; completed samples retained in journal")


def bound_parameters(item):
    args = list(item["parameters"])
    q = item["template_id"]
    if q in (3, 4, 5):
        args[-2:] = [date.fromisoformat(x) if isinstance(x, str) else x for x in args[-2:]]
    return tuple(args)


def summarize_samples(samples):
    return {"mean_ms": mean(s["Execution Time"] for s in samples),
            "root_shared_hit_blocks": mean(s["Plan"].get("Shared Hit Blocks", 0) for s in samples),
            "root_shared_read_blocks": mean(s["Plan"].get("Shared Read Blocks", 0) for s in samples),
            "samples": samples}


def measure(conn, batch):
    if len(batch) != 24:
        raise ValueError("A measurement must contain the fixed batch of 24 queries")
    def explain(item):
        return conn.execute(EXPLAIN + Q[item["template_id"]], bound_parameters(item)).fetchone()[0][0]
    warmed, docs = 0, []
    try:
        for item in batch:
            explain(item)
            warmed += 1
        for item in batch:
            docs.append(explain(item))
    except Exception as exc:
        raise MeasurementError({"warmup_completed": warmed, "measured_completed": len(docs),
                                "partial_samples": docs, "queries": copy.deepcopy(batch),
                                "batch_sha256": digest(batch), "error_type": type(exc).__name__,
                                "sqlstate": getattr(exc, "sqlstate", None)}) from None
    return {**summarize_samples(docs), "batch_sha256": digest(batch), "queries": copy.deepcopy(batch)}


def observe(batch, measurement, indexes):
    # Explicit projection: experiment metadata and manifest are inaccessible to policies.
    return {"frequencies": frequencies(batch), "queries": copy.deepcopy(batch),
            "measurement": {k: copy.deepcopy(measurement[k]) for k in
                            ("mean_ms", "samples", "root_shared_hit_blocks", "root_shared_read_blocks")},
            "actual_indexes": list(indexes)}


def feedback(batch, before, after, decision, execution):
    return {"action": copy.deepcopy(decision["final_action"]), "status": execution["status"],
            "ddl_ms": execution["ddl_ms"], "frequencies": frequencies(batch),
            "before": observe(batch, before, [])['measurement'],
            "after": observe(batch, after, [])['measurement'],
            "actual_indexes": list(execution["actual_indexes"])}
