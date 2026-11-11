"""Three selectors; the autonomous agent combines one model call with measured history."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import copy
import json
import math
from statistics import median

from .catalog import CANDIDATES, Q
from .model import ModelCallError


PREDICATE_COLUMNS = {
    1: ("customer_id",), 2: ("product_id",), 3: ("order_date",),
    4: ("customer_id", "order_date"), 5: ("product_id", "order_date"),
    6: ("customer_id", "product_id"),
}


def noop(reason="No change"):
    return {"action": "NOOP", "index": None, "reason": reason}


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def validate_action(value, candidates=None):
    """Exact object schema; no coercion, duplicate keys, NaN or SQL strings."""
    if isinstance(value, str):
        try:
            value = json.loads(value, object_pairs_hook=_unique_pairs,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid JSON constant")))
        except (TypeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("Invalid action JSON") from exc
    if not isinstance(value, dict) or set(value) != {"action", "index", "reason"}:
        raise ValueError("Action must contain exactly action, index and reason")
    if not isinstance(value["action"], str) or value["action"] not in {"CREATE", "DROP", "NOOP"}:
        raise ValueError("Unknown action")
    if not isinstance(value["reason"], str) or not value["reason"].strip() or len(value["reason"]) > 2000:
        raise ValueError("Reason must be a nonempty string of at most 2000 characters")
    try:
        value["reason"].encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError("Reason contains invalid Unicode") from exc
    allowed = set(candidates) if candidates is not None else {f"I{i}" for i in range(1, 7)}
    if value["action"] == "NOOP":
        if value["index"] is not None:
            raise ValueError("NOOP index must be null")
    elif not isinstance(value["index"], str) or value["index"] not in allowed:
        raise ValueError("Unknown candidate ID")
    return dict(value)


def no_call_record(action=None):
    return {"proposal": None, "final_action": action or noop(), "status": None,
            "model": {"called": False, "raw_output": None, "elapsed_ms": 0.0,
                      "usage": None, "config": None, "error": None},
            "policy": {"interventions": [], "evidence": {}}}


def h(query, candidate):
    columns = candidate["columns"]
    predicates = PREDICATE_COLUMNS[int(query)]
    if set(predicates) == set(columns):
        return 2
    return int(columns[0] in predicates)


def score(indexes, frequencies, candidates):
    return sum(int(f) * max((h(q, candidates[i]) for i in indexes), default=0)
               for q, f in frequencies.items())


def greedy_targets(frequencies, candidates, budget=2):
    selected = []
    while len(selected) < min(budget, len(candidates)):
        remaining = [i for i in sorted(candidates) if i not in selected]
        # sorted candidates plus max's stable first tie implements lower ID.
        selected.append(max(remaining, key=lambda i: score(selected + [i], frequencies, candidates)
                            - score(selected, frequencies, candidates)))
    return sorted(selected)


class NoTuningAgent:
    def select_action(self, observation, history, candidates, budget):
        return no_call_record(noop("No tuning condition"))


class RuleAgent:
    def select_action(self, observation, history, candidates, budget):
        # Deliberately never touches history or measurements.
        targets = set(greedy_targets(observation["frequencies"], candidates, budget))
        actual = set(observation["actual_indexes"])
        remove, add = sorted(actual - targets), sorted(targets - actual)
        if remove:
            action = {"action": "DROP", "index": remove[0], "reason": "Fixed greedy baseline: remove non-target"}
        elif add:
            action = {"action": "CREATE", "index": add[0], "reason": "Fixed greedy baseline: add target"}
        else:
            action = noop("Fixed greedy baseline: targets already present")
        return no_call_record(action)


@dataclass(frozen=True)
class PolicyConfig:
    horizon_batches: int = 16
    batch_size: int = 24
    cost_multiplier: float = 1.25
    min_gain_ms: float = 0.01
    prior_fraction: float = 0.15
    shrinkage: float = 2.0
    default_create_ms: float = 100.0
    default_drop_ms: float = 10.0
    cooldown_slots: int = 2
    max_calls: int = 12
    drift_similarity_threshold: float = 0.75
    old_history_weight: float = 0.10
    continue_after_policy_rejection: bool = True
    ddl_cost_priors: dict = field(default_factory=dict)

    def __post_init__(self):
        if not 0 <= self.drift_similarity_threshold <= 1:
            raise ValueError("drift_similarity_threshold must be between 0 and 1")
        if not 0 <= self.old_history_weight <= 1:
            raise ValueError("old_history_weight must be between 0 and 1")
        normalized = {}
        if not isinstance(self.ddl_cost_priors, dict):
            raise ValueError("ddl_cost_priors must be an object")
        for action, by_index in self.ddl_cost_priors.items():
            if action not in {"CREATE", "DROP"} or not isinstance(by_index, dict):
                raise ValueError("ddl_cost_priors may contain only CREATE/DROP objects")
            normalized[action] = {}
            for index, sample in by_index.items():
                if index not in CANDIDATES or not isinstance(sample, dict) or set(sample) != {"median_ms", "samples"}:
                    raise ValueError("invalid candidate DDL cost prior")
                value, count = sample["median_ms"], sample["samples"]
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or value < 0):
                    raise ValueError("DDL prior median_ms must be finite and nonnegative")
                if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                    raise ValueError("DDL prior samples must be a positive integer")
                normalized[action][index] = {"median_ms": float(value), "samples": count}
        object.__setattr__(self, "ddl_cost_priors", normalized)


def _frequencies(values):
    total = sum(values.values())
    return {str(q): values.get(str(q), values.get(q, 0)) / total if total else 0.0 for q in range(1, 7)}


def similarity(left, right):
    a, b = _frequencies(left), _frequencies(right)
    return max(0.0, 1.0 - 0.5 * sum(abs(a[q] - b[q]) for q in a))


def regime_weights(observation, history, config):
    """Downweight records before the most recent observable workload drift."""
    if not history:
        return [], {
            "drift_detected": False,
            "current_similarity": None,
            "current_regime_start": 0,
            "old_records": 0,
            "current_records": 0,
            "drift_similarity_threshold": config.drift_similarity_threshold,
            "old_history_weight": config.old_history_weight,
        }
    distributions = [item["frequencies"] for item in history] + [observation["frequencies"]]
    boundary = 0
    adjacent = []
    for position in range(1, len(distributions)):
        value = similarity(distributions[position - 1], distributions[position])
        adjacent.append(value)
        if value < config.drift_similarity_threshold:
            boundary = position
    weights = [
        config.old_history_weight if position < boundary else 1.0
        for position in range(len(history))
    ]
    return weights, {
        "drift_detected": boundary > 0,
        "current_similarity": adjacent[-1] if adjacent else None,
        "current_regime_start": boundary,
        "old_records": min(boundary, len(history)),
        "current_records": max(0, len(history) - boundary),
        "drift_similarity_threshold": config.drift_similarity_threshold,
        "old_history_weight": config.old_history_weight,
    }


def _measurement_view(value, *, with_samples=True):
    return {k: copy.deepcopy(value[k]) for k in (
        "mean_ms", "samples", "root_shared_hit_blocks", "root_shared_read_blocks")
            if k in value and (k != "samples" or with_samples)}


def policy_payload(observation, history, candidates, budget, config=None):
    """Allow-list boundary excludes controller labels and executable SQL."""
    config = config or PolicyConfig()
    obs = {"frequencies": copy.deepcopy(observation["frequencies"]),
           "templates": {str(q): {"sql": Q[int(q)], "predicate_columns": list(PREDICATE_COLUMNS[int(q)])}
                         for q, frequency in observation["frequencies"].items() if frequency > 0},
           "queries": [{"template_id": q["template_id"], "parameters": copy.deepcopy(q["parameters"])}
                       for q in observation["queries"]],
           "measurement": _measurement_view(observation["measurement"]),
           "actual_indexes": list(observation["actual_indexes"])}
    past = []
    for item in history:
        past.append({"action": validate_action(item["action"], candidates),
                     "status": item["status"], "ddl_ms": item["ddl_ms"],
                     "before": _measurement_view(item["before"], with_samples=False),
                     "after": _measurement_view(item["after"], with_samples=False),
                     "frequencies": copy.deepcopy(item["frequencies"]),
                     "actual_indexes": list(item["actual_indexes"])})
    _, workload_regime = regime_weights(obs, past, config)
    return {"observation": obs, "history": past,
            "derived_signals": {"workload_regime": workload_regime},
            "candidates": {i: {"name": c["name"], "columns": list(c["columns"])} for i, c in candidates.items()},
            "budget": budget}


class AutonomousTuningAgent:
    def __init__(self, model, config=None):
        self.model = model
        self.config = config or PolicyConfig()
        self.reset()

    def reset(self):
        self.calls = 0
        self.pending = None

    def _cost_estimate(self, action, index, history):
        exact = [x["ddl_ms"] for x in history if x["status"] == "applied" and
                 x["action"]["action"] == action and x["action"]["index"] == index]
        broad = [x["ddl_ms"] for x in history if x["status"] == "applied" and x["action"]["action"] == action]
        if exact:
            return {"ms": median(exact), "source": "current_run_exact", "samples": len(exact)}
        frozen = self.config.ddl_cost_priors.get(action, {})
        if index in frozen:
            return {"ms": frozen[index]["median_ms"], "source": "frozen_development_exact",
                    "samples": frozen[index]["samples"]}
        if broad:
            return {"ms": median(broad), "source": "current_run_same_action", "samples": len(broad)}
        frozen_values = [sample["median_ms"] for sample in frozen.values()]
        if frozen_values:
            return {"ms": median(frozen_values), "source": "frozen_development_same_action",
                    "samples": sum(sample["samples"] for sample in frozen.values())}
        value = self.config.default_create_ms if action == "CREATE" else self.config.default_drop_ms
        return {"ms": value, "source": "static_default", "samples": 0}

    def _cost(self, action, index, history):
        return self._cost_estimate(action, index, history)["ms"]

    def _estimate(self, index, obs, history, candidates, history_weights=None):
        records = []
        if history_weights is None:
            history_weights = [1.0] * len(history)
        for position, item in enumerate(history):
            if item["status"] != "applied" or item["action"]["index"] != index:
                continue
            sign = 1 if item["action"]["action"] == "CREATE" else -1
            gain = sign * (item["before"]["mean_ms"] - item["after"]["mean_ms"])
            weight = (similarity(obs["frequencies"], item["frequencies"]) ** 2
                      * history_weights[position])
            if weight > 0:
                records.append((gain, weight))
        if records:
            n = sum(w for _, w in records)
            raw = sum(g * w for g, w in records) / n
            uncertainty = math.sqrt(sum(w * (g - raw) ** 2 for g, w in records) / n)
            estimate = (raw - uncertainty) * n / (n + self.config.shrinkage)
            return {"benefit_ms_per_query": estimate, "raw_mean_ms": raw,
                    "uncertainty_ms": uncertainty, "effective_samples": n,
                    "samples": len(records), "source": "measured_history"}
        # Unknown index: a bounded exploratory prior from current query costs,
        # not from rule-baseline scores or future workload. Index-order matters.
        queries, samples = obs["queries"], obs["measurement"].get("samples", [])
        eligible = 0.0
        for query, sample in zip(queries, samples):
            if candidates[index]["columns"][0] in PREDICATE_COLUMNS[query["template_id"]]:
                eligible += float(sample["Execution Time"])
        if queries and samples:
            eligible /= len(queries)
        else:
            eligible = obs["measurement"]["mean_ms"] * sum(
                f for q, f in _frequencies(obs["frequencies"]).items()
                if candidates[index]["columns"][0] in PREDICATE_COLUMNS[int(q)])
        return {"benefit_ms_per_query": self.config.prior_fraction * eligible,
                "eligible_current_ms": eligible, "effective_samples": 0.0,
                "samples": 0, "source": "exploratory_current_cost_prior"}

    def _recent(self, index, history):
        return any(x["status"] == "applied" and x["action"]["index"] == index
                   for x in history[-self.config.cooldown_slots:]) if self.config.cooldown_slots else False

    def _consider(self, proposal, obs, history, candidates, budget):
        cfg = self.config
        history_weights, regime = regime_weights(obs, history, cfg)
        estimates = {
            i: self._estimate(i, obs, history, candidates, history_weights)
            for i in candidates
        }
        evidence = {"config": asdict(cfg), "workload_regime": regime, "estimates": estimates}
        interventions = []
        action = dict(proposal)
        actual = set(obs["actual_indexes"])
        if self.pending:
            pending = self.pending
            self.pending = None
            if pending["victim"] not in actual and pending["target"] not in actual and len(actual) < budget:
                # A validated fresh model output is still required for this step.
                action = {"action": "CREATE", "index": pending["target"],
                          "reason": "Complete previously justified replacement in a separate slot"}
                interventions.append("complete_pending_replacement")
                evidence["pending_replacement"] = pending
        kind, index = action["action"], action["index"]
        if kind == "NOOP":
            return action, {"interventions": interventions, "evidence": evidence}
        if (kind == "CREATE" and index in actual) or (kind == "DROP" and index not in actual):
            # Preserve structurally valid but impossible proposal for executor's
            # safety rejection rather than hiding it as successful tuning.
            return action, {"interventions": interventions, "evidence": evidence}
        if self._recent(index, history):
            interventions.append("cooldown")
            return noop("Recent action on this index; cooldown"), {"interventions": interventions, "evidence": evidence}
        horizon = cfg.horizon_batches * cfg.batch_size
        gain = estimates[index]["benefit_ms_per_query"]
        if kind == "CREATE" and len(actual) >= budget:
            victims = [i for i in actual if not self._recent(i, history)]
            if not victims:
                interventions.append("replacement_cooldown")
                return noop("All resident indexes are in cooldown"), {"interventions": interventions, "evidence": evidence}
            victim = min(victims, key=lambda i: (estimates[i]["benefit_ms_per_query"], i))
            loss = max(0.0, estimates[victim]["benefit_ms_per_query"])
            drop_cost = self._cost_estimate("DROP", victim, history)
            create_cost = self._cost_estimate("CREATE", index, history)
            cost = drop_cost["ms"] + create_cost["ms"]
            net = horizon * (gain - loss)
            evidence["replacement"] = {"target": index, "victim": victim,
                                       "predicted_saving_ms": net, "predicted_cost_ms": cost,
                                       "drop_cost": drop_cost, "create_cost": create_cost,
                                       "required_saving_ms": cfg.cost_multiplier * cost,
                                       "gain_difference_ms_per_query": gain - loss}
            if gain - loss > cfg.min_gain_ms and net > cfg.cost_multiplier * cost:
                self.pending = {"target": index, "victim": victim, "evidence": evidence["replacement"]}
                interventions.append("split_replacement_drop_first")
                action = {"action": "DROP", "index": victim, "reason": "Replacement expected savings exceed build/drop threshold; DROP first"}
            else:
                interventions.append("replacement_below_threshold")
                action = noop("Replacement expected savings do not cover cost threshold")
        else:
            directional_gain = gain if kind == "CREATE" else -gain
            cost_estimate = self._cost_estimate(kind, index, history)
            cost = cost_estimate["ms"]
            net = horizon * directional_gain
            evidence["decision"] = {"predicted_saving_ms": net, "predicted_cost_ms": cost,
                                    "cost_estimate": cost_estimate,
                                    "required_saving_ms": cfg.cost_multiplier * cost,
                                    "directional_gain_ms_per_query": directional_gain}
            if directional_gain <= cfg.min_gain_ms or net <= cfg.cost_multiplier * cost:
                interventions.append("below_benefit_cost_threshold")
                action = noop("Expected savings do not cover measured/default DDL cost threshold")
        return action, {"interventions": interventions, "evidence": evidence}

    def select_action(self, observation, history, candidates, budget):
        payload = policy_payload(observation, history, candidates, budget, self.config)
        # All decisions below consume only the allow-listed copy.
        obs, past = payload["observation"], payload["history"]
        result = no_call_record(noop("Model unavailable"))
        if self.calls >= self.config.max_calls:
            result["status"] = "model_error"
            result["model"]["error"] = "Model call budget exhausted"
            return result
        self.calls += 1
        try:
            result["model"] = self.model.complete(payload)
        except ModelCallError as exc:
            result["model"] = exc.metadata
            result["status"] = "model_error"
            return result
        except Exception as exc:
            result["model"] = {"called": True, "raw_output": None, "elapsed_ms": 0.0,
                               "usage": None, "config": None,
                               "error": f"{type(exc).__name__}: model adapter failed"}
            result["status"] = "model_error"
            return result
        try:
            proposal = validate_action(result["model"]["raw_output"], candidates)
        except ValueError as exc:
            result["status"] = "invalid_response"
            result["model"]["error"] = str(exc)
            return result
        result["proposal"] = proposal
        result["final_action"], result["policy"] = self._consider(proposal, obs, past, candidates, budget)
        return result
