# PostgreSQL Autonomous Tuning Agent

一个可复现的 PostgreSQL 16 动态负载索引调优实验。Agent 在六个白名单候选索引中进行 CREATE、DROP 或 NOOP，候选索引预算为两个。模型只提出严格 JSON 动作，本地策略负责收益—成本验证和安全执行。

完整的设计、运行说明、策略公式和两组实验分析见 [docs/project.md](docs/project.md)。

## 仓库结构

```text
src/pg_tuning/              实验控制器、数据库、模型、策略、测量、审计和分析
sql/init.sql                200,000 行实验数据初始化 SQL
prompts/system.txt          固定模型提示词
manifests/                  seed 11 开发清单和 seed 29 正式清单
compatibility/              历史条件名称兼容映射
scripts/                    PostgreSQL 启动、完整实验、绘图和打包脚本
docs/project.md             唯一项目说明与实验报告
results/full-memory/        128MB 共享缓冲区正式结果
results/small-buffer-4mb/   4MB 共享缓冲区正式结果
```

本地生成的 `.env`、`.venv/`、`.local/`、`artifacts/`、缓存和打包输出不会进入代码仓库。

## 安装

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
export PYTHONPATH="$PWD/src"
cp .env.example .env
```

编辑 `.env`，填写专用数据库和模型接口配置。不要提交真实 `.env` 或 API 密钥。

## 启动专用 PostgreSQL 16

Docker：

```bash
set -a; source .env; set +a
docker compose up -d --wait
```

本地隔离集群：

```bash
bash scripts/bootstrap_postgres.sh start
export PG_DSN="host=$PWD/.local/pg16-socket port=55432 dbname=index_agent user=agent"
```

创建 4MB 小缓冲区新集群：

```bash
AGENT_PG_SHARED_BUFFERS=4MB bash scripts/bootstrap_postgres.sh start
```

## 运行完整实验

```bash
set -a
source .env
set +a
bash scripts/run_full_experiment.sh
```

该脚本在新的隔离工作目录中依次完成初始化、开发实验、冻结、三次正式重复和结果汇总。不要使用 `source scripts/run_full_experiment.sh`。

分步命令：

```bash
python -m pg_tuning manifests
python -m pg_tuning init
python -m pg_tuning run --mode dev --output artifacts/development/events.jsonl
python -m pg_tuning analyze --log artifacts/development/events.jsonl --output artifacts/development/analysis
python -m pg_tuning freeze --development-log artifacts/development/events.jsonl --output artifacts/freeze.json
python -m pg_tuning run --mode formal --freeze artifacts/freeze.json --output artifacts/formal/events.jsonl
python -m pg_tuning analyze --log artifacts/formal/events.jsonl --output artifacts/results
```

## 已保留结果

| 环境 | Agent 原始耗时 | Rule 原始耗时 | Agent 成本调整 | Rule 成本调整 | 审计 |
| --- | ---: | ---: | ---: | ---: | --- |
| 128MB | 0.835602 | 0.791134 | **0.994413** | 1.075337 | valid / complete |
| 4MB | 1.234569 | 1.193394 | **1.413247** | 1.532865 | valid / complete |

两组实验中，Rule 的纯查询耗时略优；Agent 执行更少 DDL，在预定义的查询+DDL 成本指标上更优。完整表格、动作轨迹和局限见 [docs/project.md](docs/project.md)。

## 打包

```bash
python scripts/package_submission.py
```

输出位于 `dist/`，只包含源码、可复现配置、单一说明文档和两组有效实验结果，不包含密钥、虚拟环境、数据库目录或运行缓存。
