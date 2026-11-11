"""Audit complete JSONL experiment records and derive reproducible results.

This module never invents missing measurements. Invalid/incomplete input still
produces a diagnostic summary, but ``formal_results`` is empty. Outputs are
tables and figures for separate analysis and writing; reports are not generated.
Synthetic test fixtures are not experiment results.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, stdev
from typing import Any

from .conditions import CONDITIONS, CONDITION_LABELS, normalize_events
from .controller import should_stop_modifications
STATUSES = {"applied", "noop", "rejected", "failed", "model_error", "invalid_response"}
IDS = {f"I{i}" for i in range(1, 7)}


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def read_events(path: str | Path | None) -> tuple[list[dict], list[str]]:
    if path is None or not Path(path).is_file():
        return [], ["No experiment log is available."]
    events, errors = [], []
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError("event is not an object")
                events.append(item)
            except (ValueError, json.JSONDecodeError) as exc:
                errors.append(f"line {line_number}: malformed event ({exc})")
    return events, errors


def _measurement_errors(measurement: Any, label: str) -> list[str]:
    errors = []
    if not isinstance(measurement, dict):
        return [f"{label}: missing measurement"]
    samples = measurement.get("samples", [])
    if not isinstance(samples, list) or len(samples) != 24:
        return [f"{label}: expected 24 complete EXPLAIN samples"]
    for i, sample in enumerate(samples):
        if not isinstance(sample, dict) or not _finite_number(sample.get("Execution Time")) or not isinstance(sample.get("Plan"), dict):
            errors.append(f"{label}: sample {i} has no valid Execution Time/root Plan")
            continue
        for field in ("Shared Hit Blocks", "Shared Read Blocks"):
            if not _finite_number(sample["Plan"].get(field, 0)):
                errors.append(f"{label}: sample {i} invalid {field}")
    if errors:
        return errors
    expected = {
        "mean_ms": mean(s["Execution Time"] for s in samples),
        "root_shared_hit_blocks": mean(s["Plan"].get("Shared Hit Blocks", 0) for s in samples),
        "root_shared_read_blocks": mean(s["Plan"].get("Shared Read Blocks", 0) for s in samples),
    }
    for key, value in expected.items():
        if not _finite_number(measurement.get(key)) or not math.isclose(measurement[key], value, rel_tol=1e-9, abs_tol=1e-9):
            errors.append(f"{label}: {key} differs from complete samples/root plans")
    return errors


def audit(events: list[dict], parse_errors: list[str] | None = None) -> dict:
    """Check sequence, completeness, arithmetic, action safety and call limits."""
    events = normalize_events(events)
    errors = list(parse_errors or [])
    warnings: list[str] = []
    starts = [e for e in events if e.get("event") == "experiment_start"]
    ends = [e for e in events if e.get("event") == "experiment_end"]
    if len(starts) != 1:
        errors.append("Expected exactly one experiment_start.")
    if len(ends) != 1 or (ends and ends[0].get("status") != "completed"):
        errors.append("Experiment has no unique completed experiment_end.")
    start = starts[0] if starts else {}
    continue_after_policy_rejection = bool(
        (start.get("policy_config") or {}).get("continue_after_policy_rejection", False)
    )
    mode = start.get("mode")
    study_kind = start.get("study_kind", "assignment")
    if study_kind not in ("assignment", "post_formal_exploration"):
        errors.append("Unknown study_kind; results cannot be treated as assignment evidence.")
    if study_kind == "post_formal_exploration" and mode != "dev":
        errors.append("Post-formal exploration requires dev mode and cannot be a formal assignment experiment.")
    repeats = start.get("repeats", 3 if mode == "formal" else 1)
    if not isinstance(repeats, int) or isinstance(repeats, bool) or repeats < 1:
        errors.append("Invalid repeat count.")
        repeats = 1
    if mode not in {"dev", "formal"}:
        errors.append("Experiment mode must explicitly be dev or formal.")
    if mode == "formal" and (start.get("seed") != 29 or repeats != 3):
        errors.append("Formal experiment requires seed 29 and exactly 3 repeats.")
    if mode == "dev" and start.get("seed") != 11:
        errors.append("Development experiment requires seed 11.")
    if mode == "formal" and not start.get("freeze"):
        errors.append("Formal experiment is missing its frozen implementation record.")
    if not start.get("manifest_sha256"):
        errors.append("Missing manifest content hash.")
    expected_manifest = None
    if start.get("seed") in {11, 29}:
        from .workload import digest, workload
        expected_manifest = workload(start["seed"])
        if start.get("manifest_sha256") != digest(expected_manifest):
            errors.append("Manifest hash does not match the deterministic assignment workload.")
    expected_runs = []
    for repeat in range(1, repeats + 1):
        shift = (repeat - 1) % 3
        order = CONDITIONS[shift:] + CONDITIONS[:shift]
        expected_runs.extend((repeat, condition) for condition in order)
    run_starts = [e for e in events if e.get("event") == "run_start"]
    actual_order = [(e.get("repeat"), e.get("condition")) for e in run_starts]
    if actual_order != expected_runs:
        errors.append("Run order/count differs from the required cyclic condition order.")
    slots = [e for e in events if e.get("event") == "slot"]
    expected_slots = len(expected_runs) * 12
    if len(slots) != expected_slots:
        errors.append(f"Expected {expected_slots} slots, found {len(slots)}.")
    expected_keys = [(r, c, p, s) for r, c in expected_runs for p in range(1, 4) for s in range(1, 5)]
    actual_keys = [(e.get("repeat"), e.get("condition"), e.get("phase"), e.get("slot")) for e in slots]
    if actual_keys != expected_keys:
        errors.append("Slot order differs from exact A/B/C, four-slot sequence (missing/duplicate/out-of-order slot).")
    active_run = None
    last_state: set[str] = set()
    stopped: dict[tuple, bool] = {}
    call_counts: Counter = Counter()
    run_end_keys = []
    status_counts = Counter()
    experiment_open = False
    seen_experiment_end = False
    checkpoints = defaultdict(dict)
    checkpoints_enabled = any(e.get("event") in {"slot_before", "decision", "action_result"} for e in events)
    for event_number, event in enumerate(events, 1):
        kind = event.get("event")
        if kind == "experiment_start":
            if experiment_open or event_number != 1:
                errors.append("experiment_start must be the first event.")
            experiment_open = True
            continue
        if not experiment_open or seen_experiment_end:
            errors.append(f"Event {event_number} falls outside experiment boundaries.")
        if kind == "run_start":
            if active_run is not None:
                errors.append("A run started before the previous run ended.")
            active_run = (event.get("repeat"), event.get("condition"))
            state = event.get("actual_indexes")
            if state != []:
                errors.append(f"{active_run}: condition did not start with verified empty indexes.")
            last_state = set()
        elif kind == "run_end":
            key = (event.get("repeat"), event.get("condition"))
            run_end_keys.append(key)
            if key != active_run or event.get("status") != "completed":
                errors.append(f"{key}: run ended unsuccessfully or without matching start.")
            active_run = None
        elif kind == "experiment_end":
            seen_experiment_end = True
            if active_run is not None:
                errors.append("Experiment ended with an unfinished run.")
        elif kind == "slot":
            key = (event.get("repeat"), event.get("condition"))
            phase_key = key + (event.get("phase"),)
            label = f"{key}/phase={event.get('phase')}/slot={event.get('slot')}"
            if key != active_run:
                errors.append(f"{label}: slot outside its run boundaries.")
            for side in ("before", "after"):
                errors.extend(_measurement_errors(event.get(side), f"{label}/{side}"))
            checkpoint_key = key + (event.get("phase"), event.get("slot"))
            if checkpoints_enabled:
                point = checkpoints.pop(checkpoint_key, {})
                if set(point) != {"slot_before", "decision", "action_result"}:
                    errors.append(f"{label}: missing checkpoint(s) before completed slot.")
                else:
                    if point["slot_before"].get("before") != event.get("before"):
                        errors.append(f"{label}: Before checkpoint differs from final slot.")
                    if point["decision"].get("decision") != event.get("decision"):
                        errors.append(f"{label}: decision checkpoint differs from final slot.")
                    if point["action_result"].get("execution") != event.get("execution"):
                        errors.append(f"{label}: DDL checkpoint differs from final slot.")
                    if set(point["slot_before"].get("actual_indexes", [])) != last_state:
                        errors.append(f"{label}: prior index state not preserved across slots/phases.")
            before_hash = (event.get("before") or {}).get("batch_sha256")
            after_hash = (event.get("after") or {}).get("batch_sha256")
            if before_hash is not None or after_hash is not None:
                if not before_hash or before_hash != after_hash:
                    errors.append(f"{label}: Before/After batch hashes differ.")
            if expected_manifest and event.get("phase") in {1, 2, 3}:
                from .workload import digest
                expected_batch = expected_manifest["phases"][event["phase"] - 1]["queries"]
                for side in ("before", "after"):
                    measurement = event.get(side) or {}
                    if measurement.get("batch_sha256") != digest(expected_batch):
                        errors.append(f"{label}/{side}: batch hash differs from assignment manifest.")
                    if measurement.get("queries") != expected_batch:
                        errors.append(f"{label}/{side}: bound query order/parameters differ from manifest.")
            decision = event.get("decision") or {}
            execution = event.get("execution") or {}
            action = decision.get("final_action") or execution.get("action") or {}
            status = execution.get("status")
            status_counts[status] += 1
            if status not in STATUSES:
                errors.append(f"{label}: unknown execution status.")
            if not _finite_number(execution.get("ddl_ms")):
                errors.append(f"{label}: missing/nonfinite DDL wall time.")
            actual = event.get("actual_indexes")
            if not isinstance(actual, list) or len(actual) != len(set(actual)) or not set(actual) <= IDS or len(actual) > 2:
                errors.append(f"{label}: invalid actual candidate state or budget exceeded.")
                actual_set = set()
            else:
                actual_set = set(actual)
            if execution.get("actual_indexes") != actual:
                errors.append(f"{label}: logged post-DDL state disagrees with verified state.")
            if execution.get("unauthorized_execution") is not False:
                errors.append(f"{label}: unauthorized execution is not proven zero.")
            if execution.get("budget_violation") is not False:
                errors.append(f"{label}: executed budget violation is not proven zero.")
            if execution.get("executed"):
                if action.get("action") not in {"CREATE", "DROP"} or action.get("index") not in IDS:
                    errors.append(f"{label}: executed action is outside the whitelist.")
                if action.get("action") == "CREATE" and (action.get("index") in last_state or len(last_state) >= 2):
                    errors.append(f"{label}: creation executed on duplicate/full candidate set.")
                if action.get("action") == "DROP" and action.get("index") not in last_state:
                    errors.append(f"{label}: absent-index deletion executed.")
            if status == "applied":
                if not execution.get("executed") or not execution.get("attempted"):
                    errors.append(f"{label}: applied action lacks DDL attempt/execution.")
                expected_state = last_state.copy()
                if action.get("action") == "CREATE":
                    expected_state.add(action.get("index"))
                elif action.get("action") == "DROP":
                    expected_state.discard(action.get("index"))
                else:
                    errors.append(f"{label}: applied action is not CREATE/DROP.")
                if actual_set != expected_state:
                    errors.append(f"{label}: applied DDL does not match actual state transition.")
            elif status in {"noop", "rejected", "model_error", "invalid_response"}:
                if execution.get("executed") or actual_set != last_state:
                    errors.append(f"{label}: nonexecuting status changed indexes or executed SQL.")
            model = decision.get("model") or {}
            called = model.get("called") is True
            if called:
                call_counts[key] += 1
                if key[1] != "autonomous":
                    errors.append(f"{label}: baseline called a model.")
                if not _finite_number(model.get("elapsed_ms")):
                    errors.append(f"{label}: missing model elapsed time.")
            was_stopped = stopped.get(phase_key, key[1] == "no_tuning")
            if key[1] == "autonomous" and not was_stopped and not called:
                errors.append(f"{label}: active autonomous slot did not call the model.")
            if was_stopped and (called or status != "noop" or action.get("action") != "NOOP"):
                errors.append(f"{label}: decision/modification occurred after this phase stopped.")
            if key[1] == "no_tuning" and (status != "noop" or actual_set):
                errors.append(f"{label}: no-tuning condition is not always NOOP.")
            stopped[phase_key] = was_stopped or should_stop_modifications(
                key[1],
                decision,
                execution,
                continue_after_policy_rejection=continue_after_policy_rejection,
            )
            last_state = actual_set
        elif kind in {"slot_before", "decision", "action_result"}:
            key = (event.get("repeat"), event.get("condition"), event.get("phase"), event.get("slot"))
            if key[:2] != active_run:
                errors.append(f"{key}: checkpoint outside its run boundaries.")
            point = checkpoints[key]
            required_previous = {"slot_before": set(), "decision": {"slot_before"},
                                 "action_result": {"slot_before", "decision"}}[kind]
            if set(point) != required_previous:
                errors.append(f"{key}: duplicated or out-of-order checkpoint {kind}.")
            point[kind] = event
        elif kind == "measurement_error":
            errors.append(f"Incomplete {event.get('side')} measurement; partial query samples retained.")
        elif kind in {"reset", "error", "checkpoint", "environment"}:
            # Reset DDL is separately recorded and not included in tuning costs.
            if kind == "reset" and active_run is not None:
                errors.append("Candidate reset occurred inside a condition run.")
        else:
            errors.append(f"Unknown event type: {kind!r}.")
    if checkpoints:
        errors.append("Unfinished slot checkpoints remain without a completed measurement pair.")
    if run_end_keys != expected_runs:
        errors.append("Completed run_end sequence/count does not match all expected runs.")
    if active_run is not None:
        errors.append("Log ends in an incomplete run.")
    for key, calls in call_counts.items():
        if calls > 12:
            errors.append(f"{key}: model call count {calls} exceeds 12.")
    total_calls = sum(call_counts.values())
    if mode == "formal" and total_calls > 36:
        errors.append(f"Formal model calls {total_calls} exceed 36.")
    for status in ("failed", "rejected", "model_error", "invalid_response"):
        if status_counts[status]:
            warnings.append(f"{status_counts[status]} slots had status {status}; retained explicitly in results.")
    measurement_count = sum(isinstance(e.get(side), dict) for e in slots for side in ("before", "after"))
    return {
        "valid": not errors,
        "complete": not errors,
        "formal_ready": mode == "formal" and study_kind == "assignment" and not errors,
        "mode": mode,
        **({"study_kind": study_kind} if "study_kind" in start else {}),
        "errors": list(dict.fromkeys(errors)),
        "warnings": warnings,
        "counts": {
            "run_starts": len(run_starts), "run_ends": len(run_end_keys),
            "phases": len({k[:3] for k in actual_keys}), "slots": len(slots),
            "measurements": measurement_count, "model_calls": total_calls,
        },
        "model_calls_by_run": {f"{r}:{c}": n for (r, c), n in sorted(call_counts.items())},
        "status_counts": dict(status_counts),
    }


def _costs(slots: list[dict]) -> dict:
    slots = normalize_events(slots)
    result: dict[str, Any] = {}
    for condition in CONDITIONS:
        rows = [r for r in slots if r.get("condition") == condition]
        out: dict[str, Any] = {
            "slots": len(rows), "create_attempts": 0, "create_successes": 0,
            "drop_attempts": 0, "drop_successes": 0, "create_submitted": 0, "drop_submitted": 0,
            "create_ddl_ms": 0.0, "drop_ddl_ms": 0.0, "ddl_ms": 0.0,
            "model_calls": 0, "model_ms": 0.0,
            "rejected": 0, "failed": 0, "model_error": 0, "invalid_response": 0,
            "unauthorized_execution": 0, "budget_violations": 0, "budget_rejections": 0,
        }
        known_tokens: Counter = Counter()
        unknown_usage = 0
        missing_tokens: Counter = Counter()
        for row in rows:
            decision, execution = row.get("decision", {}), row.get("execution", {})
            action = (decision.get("final_action") or execution.get("action") or {}).get("action", "")
            status = execution.get("status")
            elapsed = execution.get("ddl_ms", 0)
            elapsed = elapsed if _finite_number(elapsed) else 0
            out["ddl_ms"] += elapsed
            if action in {"CREATE", "DROP"}:
                key = action.lower()
                out[f"{key}_attempts"] += int(execution.get("attempted") is True)
                out[f"{key}_successes"] += int(status == "applied")
                out[f"{key}_submitted"] += int(execution.get("executed") is True)
                out[f"{key}_ddl_ms"] += elapsed
            if status in {"rejected", "failed", "model_error", "invalid_response"}:
                out[status] += 1
            out["unauthorized_execution"] += int(execution.get("unauthorized_execution") is True)
            out["budget_violations"] += int(execution.get("budget_violation") is True)
            out["budget_rejections"] += int(execution.get("rejection_kind") == "budget")
            model = decision.get("model") or {}
            if model.get("called"):
                out["model_calls"] += 1
                out["model_ms"] += model.get("elapsed_ms", 0) if _finite_number(model.get("elapsed_ms")) else 0
                usage = model.get("usage")
                if not isinstance(usage, dict) or not _finite_number(usage.get("total_tokens")):
                    unknown_usage += 1
                for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    if isinstance(usage, dict) and _finite_number(usage.get(name)):
                        known_tokens[name] += usage[name]
                    else:
                        missing_tokens[name] += 1
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            out[name] = "N/A" if missing_tokens[name] else known_tokens[name]
        out["calls_without_usage"] = unknown_usage
        out["known_token_subtotal"] = dict(known_tokens)
        result[condition] = out
    return result


def _cost_adjusted_results(aggregates: list[dict], slots: list[dict], metadata: dict) -> list[dict]:
    """Apply the policy's own amortization horizon and risk multiplier to DDL cost."""
    config = metadata.get("policy_config") or {}
    horizon_batches = config.get("horizon_batches", 16)
    batch_size = config.get("batch_size", 24)
    cost_multiplier = config.get("cost_multiplier", 1.25)
    if (not isinstance(horizon_batches, int) or isinstance(horizon_batches, bool)
            or not isinstance(batch_size, int) or isinstance(batch_size, bool)
            or horizon_batches <= 0 or batch_size <= 0
            or not _finite_number(cost_multiplier)):
        return []
    horizon_queries = horizon_batches * batch_size
    rows = []
    for aggregate in aggregates:
        condition, phase = aggregate["condition"], aggregate["phase"]
        phase_slots = [
            row for row in slots
            if row.get("condition") == condition and row.get("phase") == phase
        ]
        repeats = sorted({row.get("repeat") for row in phase_slots})
        ddl_by_repeat = []
        for repeat in repeats:
            ddl_by_repeat.append(sum(
                execution.get("ddl_ms", 0)
                for row in phase_slots if row.get("repeat") == repeat
                for execution in [row.get("execution") or {}]
                if _finite_number(execution.get("ddl_ms", 0))
            ))
        mean_ddl_ms = mean(ddl_by_repeat) if ddl_by_repeat else 0.0
        amortized = cost_multiplier * mean_ddl_ms / horizon_queries
        rows.append({
            "condition": condition,
            "phase": phase,
            "n": aggregate["n"],
            "query_mean_ms": aggregate["mean_ms"],
            "sample_stddev_ms": aggregate["sample_stddev_ms"],
            "mean_tuning_ddl_ms": mean_ddl_ms,
            "horizon_queries": horizon_queries,
            "cost_multiplier": cost_multiplier,
            "risk_adjusted_ddl_ms_per_query": amortized,
            "cost_adjusted_mean_ms": aggregate["mean_ms"] + amortized,
        })
    return rows


def _write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    if fields is None:
        fields = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in row.items()})


def _plots(trajectory: list[dict], aggregate: list[dict], out: Path, diagnostic: bool) -> list[str]:
    if not trajectory:
        return []
    # Avoid writing matplotlib's cache outside the dedicated project output.
    os.environ.setdefault("MPLCONFIGDIR", str(out / ".matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {"no_tuning": "#536c88", "rule": "#d38021", "autonomous": "#17877b"}
    figure, axes = plt.subplots(3, 1, figsize=(9, 6.5), sharex=True)
    for ax, condition in zip(axes, CONDITIONS):
        rows = [r for r in trajectory if r["condition"] == condition]
        for repeat in sorted({r["repeat"] for r in rows}):
            values = [r for r in rows if r["repeat"] == repeat]
            x = [(r["phase"] - 1) * 8 + (r["slot"] - 1) * 2 + (r["side"] == "after") + 1 for r in values]
            ax.plot(x, [r["mean_ms"] for r in values], marker="o", markersize=2.5,
                    linewidth=1, alpha=0.55 + repeat * 0.12, color=colors[condition], label=f"repeat {repeat}")
        ax.set_ylabel("Mean ms/query")
        ax.set_title(CONDITION_LABELS[condition], loc="left", fontsize=10)
        for boundary in (8.5, 16.5):
            ax.axvline(boundary, color="gray", linestyle="--", linewidth=.6)
        ax.grid(axis="y", alpha=.2)
        if rows:
            ax.legend(loc="upper right", fontsize=7, ncols=3)
    axes[-1].set_xticks(range(1, 25), [f"{i // 2 + 1}{'B' if i % 2 == 0 else 'A'}" for i in range(24)], fontsize=6)
    axes[-1].set_xlabel("Action slots 1-12; B=Before, A=After; dashed lines delimit phases")
    figure.suptitle("Before / After trajectory" + (" (DIAGNOSTIC, not formal results)" if diagnostic else ""), fontsize=11)
    figure.tight_layout()
    paths = []
    for extension in ("png", "pdf"):
        path = out / f"trajectory.{extension}"
        figure.savefig(path, dpi=190, bbox_inches="tight")
        paths.append(str(path))
    plt.close(figure)
    if aggregate:
        figure, ax = plt.subplots(figsize=(7.8, 3.6))
        if diagnostic:
            figure.suptitle("Phase-final performance (DIAGNOSTIC, not formal results)")
        for j, condition in enumerate(CONDITIONS):
            rows = sorted([r for r in aggregate if r["condition"] == condition], key=lambda r: r["phase"])
            xs = [r["phase"] + (j - 1) * .23 for r in rows]
            ax.bar(xs, [r["mean_ms"] for r in rows], width=.22, color=colors[condition], label=CONDITION_LABELS[condition],
                   yerr=[r["sample_stddev_ms"] or 0 for r in rows], capsize=3)
        ax.set_xticks([1, 2, 3], ["A", "B", "C"])
        ax.set_ylabel("Final After mean ms/query")
        ax.set_xlabel("Phase; error bars = sample standard deviation across repetitions")
        ax.legend(fontsize=8)
        ax.grid(axis="y", alpha=.2)
        figure.tight_layout()
        for extension in ("png", "pdf"):
            path = out / f"phase_final.{extension}"
            figure.savefig(path, dpi=190, bbox_inches="tight")
            paths.append(str(path))
        plt.close(figure)
    return paths


def analyze(log_path: str | Path | None, output_dir: str | Path, *, plots: bool = True) -> dict:
    """Write audited JSON, CSV tables, figures; return the summary dictionary."""
    out = Path(output_dir)
    events, parse_errors = read_events(log_path)
    events = normalize_events(events)
    starts = [e for e in events if e.get("event") == "experiment_start"]
    metadata = starts[0] if starts else {}
    exploratory = metadata.get("study_kind") == "post_formal_exploration"
    existing_summary = out / "summary.json"
    if exploratory and existing_summary.is_file():
        previous = json.loads(existing_summary.read_text(encoding="utf-8"))
        if not isinstance(previous, dict):
            raise ValueError("Cannot verify existing results; use a separate exploration output directory")
        previous_integrity = previous.get("integrity", {})
        if (previous.get("result_kind") == "formal" or
                isinstance(previous_integrity, dict) and previous_integrity.get("formal_ready") is True):
            raise ValueError("Exploration cannot overwrite formal results; use a separate output directory")
    integrity = audit(events, parse_errors)
    out.mkdir(parents=True, exist_ok=True)
    slots = [e for e in events if e.get("event") == "slot"]
    trajectory, phase_final, aggregates, transitions = [], [], [], []
    for row in slots:
        for side in ("before", "after"):
            measurement = row.get(side, {})
            trajectory.append({
                "repeat": row.get("repeat"), "condition": row.get("condition"),
                "phase": row.get("phase"), "slot": row.get("slot"), "side": side,
                "mean_ms": measurement.get("mean_ms"),
                "root_shared_hit_blocks": measurement.get("root_shared_hit_blocks"),
                "root_shared_read_blocks": measurement.get("root_shared_read_blocks"),
                "status": row.get("execution", {}).get("status"),
                "actual_indexes": row.get("actual_indexes"),
            })
        if row.get("slot") == 4:
            measurement = row.get("after", {})
            phase_final.append({
                "repeat": row.get("repeat"), "condition": row.get("condition"), "phase": row.get("phase"),
                "mean_ms": measurement.get("mean_ms"),
                "root_shared_hit_blocks": measurement.get("root_shared_hit_blocks"),
                "root_shared_read_blocks": measurement.get("root_shared_read_blocks"),
            })
    # Never let an incomplete/invalid log silently contribute to successful statistics.
    if integrity["complete"]:
        grouped: dict[tuple, list[dict]] = defaultdict(list)
        for row in phase_final:
            grouped[(row["condition"], row["phase"])].append(row)
        for (condition, phase), rows in sorted(grouped.items()):
            values = [r["mean_ms"] for r in rows]
            aggregates.append({
                "condition": condition, "phase": phase, "n": len(values),
                "mean_ms": mean(values), "sample_stddev_ms": stdev(values) if len(values) > 1 else None,
                "root_shared_hit_blocks": mean(r["root_shared_hit_blocks"] for r in rows),
                "root_shared_read_blocks": mean(r["root_shared_read_blocks"] for r in rows),
            })
    for row in slots:
        if row.get("condition") == "autonomous" and row.get("phase") in (2, 3):
            decision = row.get("decision", {})
            transitions.append({
                "repeat": row.get("repeat"), "phase": row.get("phase"), "slot": row.get("slot"),
                "before_ms": row.get("before", {}).get("mean_ms"), "after_ms": row.get("after", {}).get("mean_ms"),
                "proposal": decision.get("proposal"), "final_action": decision.get("final_action"),
                "policy": decision.get("policy"), "status": row.get("execution", {}).get("status"),
                "actual_indexes": row.get("actual_indexes"), "ddl_ms": row.get("execution", {}).get("ddl_ms"),
            })
    costs = _costs(slots)
    cost_adjusted = _cost_adjusted_results(aggregates, slots, metadata) if integrity["complete"] else []
    reset_events = [e for e in events if e.get("event") == "reset"]
    reset_cost_ms = sum(e.get("execution", e).get("ddl_ms", 0) for e in reset_events if _finite_number(e.get("execution", e).get("ddl_ms", 0)))
    summary = {
        "schema_version": 1,
        "source_log": str(Path(log_path).resolve()) if log_path else None,
        "source_sha256": hashlib.sha256(Path(log_path).read_bytes()).hexdigest() if log_path and Path(log_path).is_file() else None,
        "integrity": integrity, "metadata": metadata,
        "result_kind": ("formal" if integrity["formal_ready"] else
                        "exploratory" if integrity["complete"] and exploratory else
                        "development" if integrity["complete"] else "incomplete"),
        "formal_results": aggregates if integrity["formal_ready"] else [],
        "development_results": aggregates if integrity["complete"] and not integrity["formal_ready"] and not exploratory else [],
        "exploratory_results": aggregates if integrity["complete"] and exploratory else [],
        "phase_final_observations": phase_final,
        "costs": costs,
        "cost_adjusted_results": cost_adjusted,
        "evaluation_objective": {
            "primary": "cost_adjusted_mean_ms",
            "formula": "query_mean_ms + cost_multiplier * mean_tuning_ddl_ms / horizon_queries",
            "query_metric_preserved_separately": True,
            "model_wall_time_included": False,
        },
        "reset": {"events": len(reset_events), "ddl_ms": reset_cost_ms},
        "transitions": transitions, "figures": [],
        "metric_notes": {
            "query_time": "Arithmetic mean of 24 EXPLAIN Execution Time values, milliseconds per query; excludes warm-up, initialization, DDL and model wall time.",
            "cost_adjusted": "Policy-aligned query mean plus risk-adjusted tuning DDL amortized over the configured prediction horizon; raw query and DDL components remain separate.",
            "sample_stddev": "sqrt(sum((x-mean(x))**2)/(n-1)); unavailable for n=1.",
            "buffers": "Mean root Plan Shared Hit/Read Blocks per query; child nodes are not summed.",
            "tokens": "Each token field is N/A if any called response lacks that field; known_token_subtotal is separately disclosed.",
            "attempts": "CREATE/DROP attempts count final CREATE/DROP requests (execution.attempted), including preexecution rejections. Submitted DDL (executed) and successes are separate.",
            "reset_cost": "Condition-reset DDL is separate from tuning actions/costs.",
        },
    }
    _write_csv(out / "trajectory.csv", trajectory)
    _write_csv(out / "phase_final_observations.csv", phase_final)
    _write_csv(out / "formal_results.csv", summary["formal_results"], ["condition", "phase", "n", "mean_ms", "sample_stddev_ms", "root_shared_hit_blocks", "root_shared_read_blocks"])
    _write_csv(out / "development_results.csv", summary["development_results"], ["condition", "phase", "n", "mean_ms", "sample_stddev_ms", "root_shared_hit_blocks", "root_shared_read_blocks"])
    _write_csv(out / "exploratory_results.csv", summary["exploratory_results"], ["condition", "phase", "n", "mean_ms", "sample_stddev_ms", "root_shared_hit_blocks", "root_shared_read_blocks"])
    _write_csv(out / "cost_adjusted_results.csv", cost_adjusted, [
        "condition", "phase", "n", "query_mean_ms", "sample_stddev_ms",
        "mean_tuning_ddl_ms", "horizon_queries", "cost_multiplier",
        "risk_adjusted_ddl_ms_per_query", "cost_adjusted_mean_ms",
    ])
    _write_csv(out / "costs.csv", [{"condition": c, **costs[c]} for c in CONDITIONS])
    _write_csv(out / "transitions.csv", transitions)
    if plots and trajectory and all(_finite_number(t.get("mean_ms")) for t in trajectory):
        summary["figures"] = _plots(trajectory, aggregates, out, diagnostic=not integrity["formal_ready"])
    (out / "integrity.json").write_text(json.dumps(integrity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary
