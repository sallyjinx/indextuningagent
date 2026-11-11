"""Experiment-side workload generator; policies never import this module."""
from collections import Counter
from datetime import date, timedelta
import hashlib
import json
import random
from pathlib import Path

FREQUENCIES = ((10, 2, 2, 8, 1, 1), (2, 10, 2, 1, 8, 1), (2, 2, 10, 4, 4, 2))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def workload(seed):
    phases = []
    for phase_number, counts in enumerate(FREQUENCIES, 1):
        batch = []
        for q, count in enumerate(counts, 1):
            for _ in range(count):
                t = len(batch)
                c = 1 + (37*t + 17*seed) % 1000
                p = 1 + (53*t + 23*seed) % 1000
                d = date(2024, 1, 1) + timedelta(days=(11*t + 7*seed) % 335)
                e = d + timedelta(days=30)
                params = {1: (c,), 2: (p,), 3: (str(d), str(e)),
                          4: (c, str(d), str(e)), 5: (p, str(d), str(e)), 6: (c, p)}[q]
                batch.append({"template_id": q, "parameters": list(params)})
        random.Random(seed + phase_number).shuffle(batch)
        phases.append({"phase": phase_number, "queries": batch})
    return {"seed": seed, "phases": phases}


def save_manifests(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, seed in (("development", 11), ("formal", 29)):
        path = directory / f"{name}.json"
        payload = json.dumps(workload(seed), ensure_ascii=False, indent=2) + "\n"
        if path.exists() and path.read_text() != payload:
            raise ValueError(f"Refusing to overwrite a different manifest: {path}")
        path.write_text(payload)


def frequencies(batch):
    counts = Counter(item["template_id"] for item in batch)
    return {str(q): counts[q] for q in range(1, 7)}
