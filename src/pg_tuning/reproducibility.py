"""Freeze content before formal data are measured, then refuse configuration drift."""
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
from datetime import datetime, timezone
from statistics import median

from .workload import digest
from .conditions import canonical_condition

ROOT = Path(__file__).resolve().parents[2]


def development_ddl_cost_priors(events):
    """Candidate-level costs from successful autonomous DDL in development only."""
    samples = {"CREATE": {}, "DROP": {}}
    for event in events:
        if event.get("event") != "slot" or canonical_condition(event.get("condition")) != "autonomous":
            continue
        execution = event.get("execution", {})
        action = execution.get("action", {})
        kind, index = action.get("action"), action.get("index")
        value = execution.get("ddl_ms")
        if (execution.get("status") != "applied" or kind not in samples
                or not isinstance(index, str) or isinstance(value, bool)
                or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
            continue
        samples[kind].setdefault(index, []).append(float(value))
    return {
        kind: {
            index: {"median_ms": median(values), "samples": len(values)}
            for index, values in sorted(by_index.items())
        }
        for kind, by_index in samples.items() if by_index
    }


def source_hashes():
    paths = []
    for directory in ("src", "prompts", "sql", "scripts", "manifests", "compatibility"):
        paths += [p for p in (ROOT / directory).rglob("*") if p.is_file()
                  and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    paths += [ROOT / p for p in ("pyproject.toml", "requirements.lock", "compose.yaml") if (ROOT / p).exists()]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def machine_metadata():
    versions = {}
    for package in ("psycopg", "httpx", "matplotlib"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not installed"
    cpu = "unknown"
    if Path("/proc/cpuinfo").exists():
        cpu = next((l.split(":", 1)[1].strip() for l in Path("/proc/cpuinfo").read_text().splitlines()
                    if l.startswith("model name")), cpu)
    memory = next((l for l in Path("/proc/meminfo").read_text().splitlines() if l.startswith("MemTotal:")), "unknown")
    return {"python": platform.python_version(), "platform": platform.platform(),
            "machine": platform.machine(), "cpu": cpu, "logical_cpus": os.cpu_count(),
            "memory": memory, "packages": versions}


def freeze(path, development_log, model_config, dataset, db_metadata):
    from .analysis import audit
    events = [json.loads(line) for line in Path(development_log).read_text().splitlines() if line.strip()]
    if events and events[0].get("study_kind", "assignment") != "assignment":
        raise ValueError("Only assignment development can be frozen; post-formal exploration cannot become formal evidence")
    slots = [e for e in events if e["event"] == "slot"]
    if (not events or events[0].get("mode") != "dev" or events[0].get("seed") != 11
            or events[-1].get("status") != "completed" or len(slots) != 36):
        raise ValueError("Freeze requires a completed seed-11 development experiment with 36 slots")
    integrity = audit(events)
    if not integrity["valid"]:
        raise ValueError("Development integrity check failed: " + "; ".join(integrity["errors"]))
    if any(e["execution"]["status"] in ("model_error", "invalid_response", "failed") for e in slots):
        raise ValueError("Resolve development model/JSON/DDL errors before freezing")
    if not any(canonical_condition(e["condition"]) == "autonomous" and e["decision"]["model"]["called"] for e in slots):
        raise ValueError("Development must exercise the real model")
    hashes = source_hashes()
    expected = {"source_hashes": hashes, "model_config": model_config, "dataset": dataset}
    for key, value in expected.items():
        if events[0].get(key) != value:
            raise ValueError(f"Development {key} differs from current configuration; rerun development")
    if events[0].get("environment", {}).get("database") != db_metadata:
        raise ValueError("Development database metadata differs from current configuration")
    calibration = {
        "ddl_cost_priors": development_ddl_cost_priors(events),
        "source": "seed-11 autonomous successful applied DDL only",
    }
    frozen_content = {"source_hashes": hashes, "policy_calibration": calibration}
    record = {"schema_version": 2,
              "created_utc": datetime.now(timezone.utc).isoformat(), "source_hashes": hashes,
              "content_sha256": digest(frozen_content), "model_config": model_config, "dataset": dataset,
              "database": db_metadata, "machine": machine_metadata(),
              "policy_calibration": calibration,
              "development_log_sha256": hashlib.sha256(Path(development_log).read_bytes()).hexdigest(),
              "development_log": str(development_log), "formal_seed": 29,
              "formal_repeats": 3, "policy_input_excludes": ["seed", "phase", "repeat", "manifest"]}
    with Path(path).open("x") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return record


def verify_freeze(path, model_config, dataset, db_metadata):
    frozen = json.loads(Path(path).read_text())
    checks = {"source_hashes": source_hashes(), "model_config": model_config,
              "dataset": dataset, "database": db_metadata}
    for key, value in checks.items():
        if frozen[key] != value:
            raise ValueError(f"Frozen {key} differs; formal experiment prohibited")
    if frozen.get("schema_version", 1) >= 2:
        calibration = frozen.get("policy_calibration")
        if (not isinstance(calibration, dict)
                or calibration.get("source") != "seed-11 autonomous successful applied DDL only"):
            raise ValueError("Frozen policy calibration is missing or invalid")
        from .policy import PolicyConfig
        PolicyConfig(ddl_cost_priors=calibration.get("ddl_cost_priors"))
        expected_content = digest({"source_hashes": frozen["source_hashes"],
                                   "policy_calibration": calibration})
    else:
        expected_content = digest(frozen["source_hashes"])
    if frozen.get("content_sha256") != expected_content:
        raise ValueError("Frozen content hash differs; formal experiment prohibited")
    if hashlib.sha256(Path(frozen["development_log"]).read_bytes()).hexdigest() != frozen["development_log_sha256"]:
        raise ValueError("Frozen development log changed")
    return frozen


def claim_formal_run(frozen, log_path):
    # Project-wide, independent of the chosen freeze or output filename.
    guard = ROOT / "artifacts/formal-started.json"
    guard.parent.mkdir(parents=True, exist_ok=True)
    with guard.open("x") as f:
        json.dump({"log": str(log_path), "freeze_hash": frozen["content_sha256"],
                   "started_utc": datetime.now(timezone.utc).isoformat()}, f)
    return guard
