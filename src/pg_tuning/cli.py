import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path

from .controller import Journal, console_progress, run_experiment
from .database import Database, connect, database_metadata, experiment_lock, fingerprint, initialize
from .model import ChatModel, ModelConfig
from .policy import AutonomousTuningAgent, PolicyConfig
from .reproducibility import claim_formal_run, freeze, machine_metadata, source_hashes, verify_freeze
from .workload import digest, save_manifests, workload


def main(argv=None):
    parser = argparse.ArgumentParser(description="PostgreSQL 16 autonomous tuning assignment")
    parser.add_argument("--dsn", default=os.environ.get("PG_DSN"), help="Prefer PG_DSN environment; never logged")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("manifests")
    init = sub.add_parser("init")
    init.add_argument("--record", default="artifacts/dataset.json")
    fr = sub.add_parser("freeze")
    fr.add_argument("--development-log", default="artifacts/development/events.jsonl")
    fr.add_argument("--output", default="artifacts/freeze.json")
    run = sub.add_parser("run")
    run.add_argument("--mode", choices=("dev", "formal"), required=True)
    run.add_argument("--output", required=True, help="New log file; existing files are never overwritten")
    run.add_argument("--freeze", default="artifacts/freeze.json")
    analysis = sub.add_parser("analyze")
    analysis.add_argument("--log")
    analysis.add_argument("--output", default="artifacts/results")
    args = parser.parse_args(argv)
    if args.command == "manifests":
        console_progress("正在生成开发与正式工作负载 manifest")
        save_manifests("manifests")
        console_progress("manifest 生成完成")
        return 0
    if args.command == "analyze":
        from .analysis import analyze
        console_progress(f"开始审计并汇总日志 log={args.log} output={args.output}")
        result = analyze(args.log, args.output)
        console_progress(
            f"分析完成 complete={result['integrity']['complete']} result_kind={result['result_kind']}"
        )
        print(json.dumps({"output": args.output, "complete": result['integrity']['complete'], "result_kind": result['result_kind']}, ensure_ascii=False))
        return 0 if result['integrity']['complete'] or not args.log else 1
    if not args.dsn:
        parser.error("PG_DSN is required")
    console_progress(f"正在连接专用 PostgreSQL，准备执行 command={args.command}")
    with experiment_lock(args.dsn) as lease:
        if args.command == "init":
            console_progress("正在初始化或校验20万行实验数据")
            print(json.dumps(initialize(lease, args.record)))
            console_progress("数据初始化/校验完成")
            return 0
        config = ModelConfig.from_env()
        dataset = fingerprint(lease)
        record = Path("artifacts/dataset.json")
        if not record.exists() or json.loads(record.read_text()) != dataset:
            raise ValueError("Dataset does not match artifacts/dataset.json; run init for first initialization only")
        db_info = database_metadata(lease)
        if args.command == "freeze":
            console_progress(f"开始冻结开发实验 log={args.development_log}")
            result = freeze(args.output, args.development_log, config.public(), dataset, db_info)
            console_progress(f"冻结完成 output={args.output}")
            print(json.dumps({"freeze": args.output, "sha256": result["content_sha256"]}))
            return 0
        frozen = verify_freeze(args.freeze, config.public(), dataset, db_info) if args.mode == "formal" else None
        policy_settings = PolicyConfig(
            ddl_cost_priors=(frozen or {}).get("policy_calibration", {}).get("ddl_cost_priors", {})
        )
        exploratory = args.mode == "dev" and Path("artifacts/formal-started.json").exists()
        manifest = workload(29 if args.mode == "formal" else 11)
        # Formal guard is project-wide; neither freeze nor log paths can bypass it.
        if args.mode == "formal":
            claim_formal_run(frozen, args.output)
        console_progress(f"实验日志将写入 {args.output}")
        journal = Journal(args.output)
        db = Database(args.dsn)
        model = ChatModel(config)
        def check():
            if fingerprint(lease) != dataset or database_metadata(lease) != db_info:
                raise ValueError("Dataset or PostgreSQL settings changed during experiment")
            if frozen:
                verify_freeze(args.freeze, config.public(), dataset, db_info)
        try:
            success = run_experiment(db, manifest, journal, lambda: AutonomousTuningAgent(model, policy_settings),
                                     repeats=3 if args.mode == "formal" else 1,
                                     mode=args.mode, check_invariants=check,
                                     progress_fn=console_progress,
                                     metadata={"manifest_sha256": digest(manifest), "freeze": frozen,
                                               "source_hashes": source_hashes(),
                                               "environment": {**machine_metadata(), "database": db_info},
                                               "dataset": dataset, "model_config": config.public(),
                                               "study_kind": "post_formal_exploration" if exploratory else "assignment",
                                               "policy_config": asdict(policy_settings)})
        finally:
            model.close()
            db.close()
            journal.close()
        print(json.dumps({"log": args.output, "completed": success}))
        return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
