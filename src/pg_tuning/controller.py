"""Experiment control only. Policies receive explicit, deep-copied projections."""
from datetime import datetime, timezone
import copy
import json
import os
from pathlib import Path
import sys

from .catalog import BUDGET, CANDIDATES
from .conditions import CONDITIONS, CONDITION_LABELS
from .database import UnrecoverableDatabaseError
from .measurement import MeasurementError, feedback, measure, observe
from .policy import NoTuningAgent, RuleAgent, no_call_record, noop


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents replacement, accidental reruns and cherry-picking.
        self.file = self.path.open("x", encoding="utf-8")

    def write(self, event, **data):
        record = {"event": event, "timestamp": datetime.now(timezone.utc).isoformat(), **data}
        self.file.write(json.dumps(record, ensure_ascii=True, allow_nan=False, default=str) + "\n")
        self.file.flush()
        os.fsync(self.file.fileno())

    def close(self):
        self.file.close()


def console_progress(message):
    """Write human-readable progress without contaminating JSON CLI output."""
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", file=sys.stderr, flush=True)


def _emit_progress(progress_fn, message):
    """Progress reporting must never change the experiment outcome."""
    if progress_fn is None:
        return
    try:
        progress_fn(message)
    except Exception:
        pass


def should_stop_modifications(condition, decision, execution, *,
                              continue_after_policy_rejection=False):
    """Stop on terminal outcomes, but optionally retry locally rejected proposals."""
    if condition == "no_tuning":
        return True
    decision_status = decision.get("status")
    execution_status = execution.get("status")
    if decision_status in {"model_error", "invalid_response"} or execution_status in {
        "failed", "model_error", "invalid_response"
    }:
        return True
    if condition != "autonomous" or not continue_after_policy_rejection:
        return execution_status != "applied"
    if execution_status == "applied":
        return False
    proposal = decision.get("proposal") or {}
    interventions = (decision.get("policy") or {}).get("interventions") or []
    policy_rejected = (
        proposal.get("action") in {"CREATE", "DROP"}
        and (bool(interventions) or execution_status == "rejected")
    )
    return not policy_rejected


def rotated_conditions(repeat):
    offset = (repeat - 1) % len(CONDITIONS)
    return CONDITIONS[offset:] + CONDITIONS[:offset]


def run_experiment(db, manifest, journal, agent_factory, *, repeats=3,
                   mode="formal", metadata=None, check_invariants=None, measure_fn=measure,
                   progress_fn=None):
    total_runs = repeats * len(CONDITIONS)
    total_slots = total_runs * len(manifest["phases"]) * 4
    completed_runs = 0
    completed_slots = 0
    continue_after_policy_rejection = bool(
        ((metadata or {}).get("policy_config") or {}).get(
            "continue_after_policy_rejection", False
        )
    )
    _emit_progress(
        progress_fn,
        f"实验开始 mode={mode} seed={manifest['seed']} repeats={repeats} "
        f"runs={total_runs} slots={total_slots}",
    )
    journal.write("experiment_start", mode=mode, seed=manifest["seed"], repeats=repeats, **(metadata or {}))
    active_run = None
    calls_total = 0
    try:
        for repeat in range(1, repeats + 1):
            for condition in rotated_conditions(repeat):
                active_run = {"repeat": repeat, "condition": condition}
                run_number = completed_runs + 1
                _emit_progress(
                    progress_fn,
                    f"运行开始 [{run_number}/{total_runs}] repeat={repeat} "
                    f"condition={CONDITION_LABELS[condition]}：检查环境并清理候选索引",
                )
                if check_invariants:
                    check_invariants()
                db.reset(on_result=lambda result: journal.write("reset", **active_run, execution=result))
                history = []
                agent = (agent_factory() if condition == "autonomous" else
                         RuleAgent() if condition == "rule" else NoTuningAgent())
                journal.write("run_start", **active_run, actual_indexes=db.actual_indexes(), history_length=0)
                calls_run = 0
                for phase in manifest["phases"]:
                    batch = phase["queries"]
                    stopped = condition == "no_tuning"
                    _emit_progress(
                        progress_fn,
                        f"阶段开始 repeat={repeat} condition={CONDITION_LABELS[condition]} "
                        f"phase={phase['phase']} stopped={stopped}",
                    )
                    for slot in range(1, 5):
                        identity = {**active_run, "phase": phase["phase"], "slot": slot}
                        _emit_progress(
                            progress_fn,
                            f"槽位 {completed_slots + 1}/{total_slots} "
                            f"({completed_slots / total_slots:.1%}) phase={phase['phase']} slot={slot}："
                            "开始 Before 测量（24条预热 + 24条采样）",
                        )
                        try:
                            before = measure_fn(db.conn, batch)
                        except MeasurementError as exc:
                            journal.write("measurement_error", **identity, side="before", details=exc.details)
                            raise
                        journal.write("slot_before", **identity, before=before, actual_indexes=db.actual_indexes())
                        _emit_progress(
                            progress_fn,
                            f"Before 完成 mean_ms={before['mean_ms']:.6f} "
                            f"indexes={db.actual_indexes()}",
                        )
                        decision = no_call_record(noop("Modification stopped for current batch"))
                        if not stopped:
                            observation = observe(batch, before, db.actual_indexes())
                            # Rule is given no measured history (not even a usable empty proxy).
                            policy_history = copy.deepcopy(history) if condition == "autonomous" else []
                            _emit_progress(
                                progress_fn,
                                ("正在调用模型并执行本地收益/成本评估" if condition == "autonomous"
                                 else "正在计算固定规则动作"),
                            )
                            decision = agent.select_action(observation, policy_history, copy.deepcopy(CANDIDATES), BUDGET)
                        journal.write("decision", **identity, decision=decision)
                        if decision["model"]["called"]:
                            calls_run += 1
                            calls_total += 1
                            _emit_progress(
                                progress_fn,
                                f"模型调用完成 elapsed_ms={decision['model']['elapsed_ms']:.1f} "
                                f"status={decision.get('status')} error={decision['model'].get('error')}",
                            )
                        if calls_run > 12 or calls_total > repeats * 12:
                            raise RuntimeError("Model call count exceeds hard experiment limit")
                        final_action = decision.get("final_action") or {}
                        _emit_progress(
                            progress_fn,
                            f"准备执行动作 action={final_action.get('action')} "
                            f"index={final_action.get('index')}",
                        )
                        try:
                            execution = db.apply_checked(decision)
                        except UnrecoverableDatabaseError as exc:
                            journal.write("action_result", **identity, execution=exc.result)
                            raise
                        journal.write("action_result", **identity, execution=execution)
                        _emit_progress(
                            progress_fn,
                            f"动作完成 status={execution['status']} ddl_ms={execution['ddl_ms']:.3f} "
                            f"indexes={execution['actual_indexes']}；开始 After 测量",
                        )
                        try:
                            after = measure_fn(db.conn, batch)
                        except MeasurementError as exc:
                            journal.write("measurement_error", **identity, side="after", details=exc.details)
                            raise
                        actual = db.actual_indexes()
                        journal.write("slot", **identity, before=before, after=after, decision=decision,
                                      execution=execution, actual_indexes=actual)
                        history.append(feedback(batch, before, after, decision, execution))
                        stopped = stopped or should_stop_modifications(
                            condition,
                            decision,
                            execution,
                            continue_after_policy_rejection=continue_after_policy_rejection,
                        )
                        completed_slots += 1
                        _emit_progress(
                            progress_fn,
                            f"槽位完成 [{completed_slots}/{total_slots} {completed_slots / total_slots:.1%}] "
                            f"after_ms={after['mean_ms']:.6f} stopped={stopped}",
                        )
                if check_invariants:
                    check_invariants()
                journal.write("run_end", **active_run, status="completed", model_calls=calls_run,
                              actual_indexes=db.actual_indexes())
                completed_runs += 1
                _emit_progress(
                    progress_fn,
                    f"运行完成 [{completed_runs}/{total_runs}] repeat={repeat} "
                    f"condition={CONDITION_LABELS[condition]} model_calls={calls_run} "
                    f"indexes={db.actual_indexes()}",
                )
                active_run = None
        if check_invariants:
            check_invariants()
        journal.write("experiment_end", status="completed", model_calls=calls_total)
        _emit_progress(
            progress_fn,
            f"实验完成 slots={completed_slots}/{total_slots} model_calls={calls_total}",
        )
        return True
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": "Experiment aborted; inspect last journal checkpoint and error type"}
        if active_run:
            journal.write("run_end", **active_run, status="failed", error=error)
        journal.write("experiment_end", status="failed", model_calls=calls_total, error=error)
        _emit_progress(
            progress_fn,
            f"实验中止 slots={completed_slots}/{total_slots} error_type={type(exc).__name__}",
        )
        return False
