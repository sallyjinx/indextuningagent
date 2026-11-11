# PostgreSQL 自治索引调优智能体：实现、运行与实验结果

## 1. 项目概述

本项目实现了一个面向 PostgreSQL 16 动态只读负载的自治索引调优智能体。系统只允许操作六个固定白名单 B-tree 索引，任意时刻最多保留两个候选索引。大语言模型负责提出严格 JSON 动作，本地策略依据历史 Before/After 收益、DDL 成本和负载漂移进行验证、拒绝或拆分，最后由白名单执行器执行。

项目比较三个条件：

- **No tuning**：始终 NOOP，不调用模型。
- **Rule baseline**：根据模板频次和谓词列使用固定启发式选择两个索引。
- **Autonomous Agent**：模型提议，本地收益—成本策略决策，白名单执行。

仓库只保留两组完整、通过审计的正式结果：

- `results/full-memory/`：PostgreSQL `shared_buffers=128MB`，工作集可完全进入共享缓冲区。
- `results/small-buffer-4mb/`：独立 PostgreSQL 集群，`shared_buffers=4MB`，小于约 9.95MiB 的数据表本体。

两组实验使用同一份 200,000 行数据，数据指纹均为 `2d94920b3dd78639206e0ed5540e3f1be566a97b5fedb4c8a89b21d964958572`。每组都重新运行开发实验、冻结成本先验和三次正式重复，没有混用正式反馈。

## 2. 数据、负载和候选索引

六个查询模板均计算 `sum(amount)`：Q1、Q2 分别按客户和商品等值过滤；Q3 按日期范围过滤；Q4、Q5 分别组合客户或商品与日期范围；Q6 同时按客户和商品过滤。每阶段有 24 条查询，三个阶段的频次如下。

| 阶段 | 主导条件 | Q1 | Q2 | Q3 | Q4 | Q5 | Q6 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A | customer | 10 | 2 | 2 | 8 | 1 | 1 |
| B | product | 2 | 10 | 2 | 1 | 8 | 1 |
| C | date | 2 | 2 | 10 | 4 | 4 | 2 |

| ID | 索引 | 列顺序 |
| --- | --- | --- |
| I1 | `idx_agent_customer` | `customer_id` |
| I2 | `idx_agent_product` | `product_id` |
| I3 | `idx_agent_date` | `order_date` |
| I4 | `idx_agent_customer_date` | `customer_id, order_date` |
| I5 | `idx_agent_product_date` | `product_id, order_date` |
| I6 | `idx_agent_customer_product` | `customer_id, product_id` |

正式实验固定种子 29并重复三次。每次重复都运行三个条件，顺序循环移位以降低运行顺序偏差。每个条件开始前清理候选索引和策略历史，同一条件的 A、B、C 阶段之间保留索引和历史。每阶段包含四个动作槽，每槽执行完整 Before 测量、至多一个动作和完整 After 测量。

每次测量先预热 24 条查询，再使用：

```sql
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON)
```

查询指标是 24 个 `Execution Time` 的算术平均值，单位为 ms/查询。Shared Hit/Read 只取根 Plan 值，不重复累加子节点。

## 3. Agent 设计

### 3.1 模型观察和安全边界

模型可以观察当前查询及绑定参数、模板频次、当前 EXPLAIN 样本、根计划缓冲区、实际索引、历史动作状态、DDL 时间和历史 Before/After 摘要。模型还可以观察仅由历史与当前频次计算出的 `workload_regime` 漂移摘要。

模型不可见正式种子、阶段标签、重复编号、未来负载，也不能直接执行 SQL。模型输出必须只包含 `action`、`index`、`reason`，动作限定为 CREATE、DROP 或 NOOP。执行器把 I1— I6 映射为固定 SQL，并在 DDL 后读取系统目录确认真实状态。

### 3.2 漂移和历史收益

当前频率分布为 `f`，历史记录为 `f_j`，频率相似度为：

```text
s_j = max(0, 1 - 0.5 × Σ_q |f(q)-f_j(q)|)
```

相邻负载相似度低于 0.75 时判定发生可观察漂移。最近漂移边界之前的历史额外乘 0.10，当前区间权重为 1.0。成功 CREATE 的收益为 `Before-After`；成功 DROP 反转符号，使收益仍表示保留索引的价值。

```text
w_j = regime_weight_j × s_j²
n   = Σ_j w_j
μ   = Σ_j(w_j × g_j) / n
σ   = sqrt(Σ_j(w_j × (g_j-μ)²) / n)
benefit(i) = (μ-σ) × n/(n+2)
```

该估计减去离散度，并用强度 2 收缩小样本。没有实测历史时，只把当前可由候选首列支持的查询耗时按 15% 计为探索先验。

### 3.3 DDL 成本和动作门槛

成本估计依次使用本次运行同索引同动作中位数、开发冻结的同索引成本、本次运行同类动作中位数、开发冻结的同类候选中位数，最后使用 CREATE=100ms、DROP=10ms 默认值。

收益视野固定为 `16×24=384` 条查询，成本系数为 1.25，普通动作必须同时满足：

```text
directional_gain > 0.01 ms/query
384 × directional_gain > 1.25 × predicted_ddl_cost
```

预算已满时，替换必须在不同槽位中先 DROP、后 CREATE，不能临时出现第三个候选索引。本地门槛拒绝后允许下一槽重新观察；模型主动 NOOP、模型/JSON 错误或 DDL 失败则停止当前阶段修改。

## 4. 环境与运行

### 4.1 安装

推荐 Python 3.12：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
export PYTHONPATH="$PWD/src"
```

复制本地配置模板。真实密钥只保存在 `.env`，该文件已被 `.gitignore` 排除：

```bash
cp .env.example .env
# 编辑 .env
set -a
source .env
set +a
```

### 4.2 PostgreSQL

Docker 方案：

```bash
docker compose up -d --wait
```

本地隔离集群方案：

```bash
bash scripts/bootstrap_postgres.sh start
export PG_DSN="host=$PWD/.local/pg16-socket port=55432 dbname=index_agent user=agent"
```

若系统没有 PostgreSQL 16，可先运行 `bash scripts/build_postgres16.sh`。小缓冲区新环境可设置：

```bash
AGENT_PG_SHARED_BUFFERS=4MB bash scripts/bootstrap_postgres.sh start
```

启动脚本只在新建集群时写入该参数，并在启动后用 `SHOW shared_buffers` 核验实际值。

### 4.3 完整实验

加载 `.env` 后运行：

```bash
bash scripts/run_full_experiment.sh
```

脚本创建隔离工作目录，依次执行数据库初始化、种子 11 开发实验、开发日志审计、冻结、种子 29 正式实验和正式汇总。失败只终止脚本并保留日志，不会关闭当前交互终端。

也可分步运行：

```bash
python -m pg_tuning manifests
python -m pg_tuning init
python -m pg_tuning run --mode dev --output artifacts/development/events.jsonl
python -m pg_tuning analyze --log artifacts/development/events.jsonl --output artifacts/development/analysis
python -m pg_tuning freeze --development-log artifacts/development/events.jsonl --output artifacts/freeze.json
python -m pg_tuning run --mode formal --freeze artifacts/freeze.json --output artifacts/formal/events.jsonl
python -m pg_tuning analyze --log artifacts/formal/events.jsonl --output artifacts/results
```

## 5. 全内存实验结果（128MB）

完整性审计结果为 `valid=true`、`complete=true`、`formal_ready=true`，包含 9 次条件运行、27 个阶段、108 个动作槽和 216 次测量。

### 5.1 阶段最终性能

| 阶段 | 条件 | 查询耗时（ms/查询） | Shared Hit | Shared Read |
| --- | --- | ---: | ---: | ---: |
| A | No tuning | 3.211778 ± 0.318514 | 1274.000 | 0 |
| A | Rule | **0.680931 ± 0.003838** | **359.083** | 0 |
| A | Agent | 0.748292 ± 0.030284 | 377.417 | 0 |
| B | No tuning | 3.190903 ± 0.207922 | 1274.000 | 0 |
| B | Rule | 0.676278 ± 0.007306 | 357.083 | 0 |
| B | Agent | **0.389750 ± 0.102644** | **228.000** | 0 |
| C | No tuning | 3.307500 ± 0.176125 | 1274.000 | 0 |
| C | Rule | **1.016194 ± 0.004340** | **892.583** | 0 |
| C | Agent | 1.368764 ± 0.354186 | 916.611 | 0 |

三个阶段等权平均后，No tuning、Rule 和 Agent 分别为 3.236727、0.791134 和 0.835602ms/查询。Agent 比 Rule 慢 5.62%，但比 No tuning 快 74.18%。所有 Shared Read 都为 0，说明采样主要反映预热后的共享缓冲区命中场景。

### 5.2 成本调整结果

```text
cost_adjusted_mean_ms
  = query_mean_ms + 1.25 × mean_tuning_ddl_ms / 384
```

| 阶段 | Agent：查询 / DDL调整 / 合计 | Rule：查询 / DDL调整 / 合计 | Agent 相对 Rule |
| --- | ---: | ---: | ---: |
| A | 0.748292 / 0.197124 / **0.945415** | 0.680931 / 0.308009 / **0.988940** | 好 4.40% |
| B | 0.389750 / 0.171941 / **0.561691** | 0.676278 / 0.271789 / **0.948067** | 好 40.75% |
| C | 1.368764 / 0.107369 / **1.476133** | 1.016194 / 0.272810 / **1.289005** | 差 14.52% |
| 平均 | 0.835602 / 0.158811 / **0.994413** | 0.791134 / 0.284203 / **1.075337** | 好 7.53% |

Agent 的 DDL 总时间为 439.082ms，Rule 为 785.764ms，Agent 少 44.12%。因此 Rule 的纯查询性能略好，但 Agent 在预定义的查询+DDL 综合指标上好 7.53%。该指标不包含模型时间，不能解释为端到端响应更快。

### 5.3 动作轨迹

规则基准三次重复均执行固定序列：A 阶段创建 I1、I4；B 阶段删除 I1、I4后创建 I2、I5；C 阶段删除 I2、I5后创建 I3、I4。因此轨迹稳定，但每次阶段切换都有删除旧索引造成的暂时回退，DDL 数量较多。

Agent 的三次轨迹不同：第一次形成 I1+I2，C 阶段成功替换为 I1+I3；第二次形成 I4+I5，C 阶段因门槛保持不变；第三次先形成 I4+I5，C 阶段替换为 I3+I5，但最后一槽删除 I5 后没有机会创建替代索引，最终只保留 I3。该未完成替换是 C 阶段方差较大的主要原因。

动作轨迹 SVG 位于 `results/full-memory/analysis/action-trajectory-comparison.svg`。

## 6. 小缓冲区实验结果（4MB）

该实验在全新隔离集群中运行，`shared_buffers=4MB`，小于 10,436,608 字节的数据表本体。完整性审计同样为 `valid=true`、`complete=true`、`formal_ready=true`，没有错误或警告。

### 6.1 阶段最终性能

| 阶段 | 条件 | 查询耗时（ms/查询） | Shared Hit | Shared Read |
| --- | --- | ---: | ---: | ---: |
| A | No tuning | 4.195889 ± 0.530525 | 400.958 | 873.042 |
| A | Rule | **0.972736 ± 0.121364** | 112.722 | 246.361 |
| A | Agent | 1.023819 ± 0.134739 | 125.486 | 269.597 |
| B | No tuning | 4.155986 ± 0.361948 | 401.125 | 872.875 |
| B | Rule | 0.955139 ± 0.125613 | 111.667 | 245.417 |
| B | Agent | **0.500500 ± 0.072477** | **73.222** | **156.792** |
| C | No tuning | 4.215000 ± 0.427912 | 387.667 | 886.333 |
| C | Rule | **1.652306 ± 0.238260** | **60.639** | **831.944** |
| C | Agent | 2.179389 ± 0.789851 | 76.681 | 948.528 |

总体纯查询耗时为 No tuning 4.188958、Rule 1.193394、Agent 1.234569ms/查询。Agent 比 Rule 慢 3.45%，但比 No tuning 快 70.53%。三种条件的 Shared Read 均明显大于 0，说明页面未完全驻留在 PostgreSQL 共享缓冲区；它们仍可能命中操作系统页缓存，因此不等同于物理磁盘读。

### 6.2 成本调整结果

| 阶段 | Agent：查询 / DDL调整 / 合计 | Rule：查询 / DDL调整 / 合计 | Agent 相对 Rule |
| --- | ---: | ---: | ---: |
| A | 1.023819 / 0.163174 / **1.186994** | 0.972736 / 0.350724 / **1.323460** | 好 10.31% |
| B | 0.500500 / 0.198186 / **0.698686** | 0.955139 / 0.334916 / **1.290055** | 好 45.84% |
| C | 2.179389 / 0.174673 / **2.354062** | 1.652306 / 0.332773 / **1.985079** | 差 18.59% |
| 平均 | 1.234569 / 0.178678 / **1.413247** | 1.193394 / 0.339471 / **1.532865** | 好 7.80% |

Agent 的 DDL 总时间为 494.008ms，Rule 为 938.570ms，Agent 少 47.37%。小缓冲区提高了三种条件的查询耗时并使 Shared Read 非零，但没有改变总体结论：Rule 的纯查询耗时略好，Agent 因更少 DDL 在成本调整指标上更好。

## 7. 局限与结论

实验只覆盖一个 200,000 行数据集、一个动态负载清单、单客户端和三次正式重复。Before/After 差值包含调度、缓存和并行计划波动，不是严格因果估计。4MB 实验中的 Shared Read 只表示 PostgreSQL 共享缓冲区未命中；主机操作系统缓存仍可能提供页面，且 `track_io_timing=off`，不能据此推断真实磁盘等待时间。

两组实验得出一致结论：

1. Rule 在纯查询执行时间上略优。
2. Agent 显著优于无调优，并在商品主导的 B 阶段超过 Rule。
3. Agent 使用更少的 CREATE/DROP，因此在查询+DDL 成本指标上优于 Rule。
4. Agent 的主要不足是 C 阶段替换不稳定，尤其可能在最后一个槽位开始无法完成的两槽替换。
5. 成本调整指标不包含约 51—53 秒的累计模型调用时间，不能用来声称端到端性能领先。

后续应禁止在剩余槽位不足时启动替换，增加开发期候选 DDL 成本样本，并压缩发送给模型的计划观察。

## 8. 结果文件说明

每个保留实验目录包含：

- `dataset.json`：数据指纹和行数。
- `freeze.json`：正式实验使用的源码、模型、数据库和开发日志冻结记录。
- `development/events.jsonl`：开发实验原始日志。
- `formal/events.jsonl`：正式实验原始日志。
- `analysis/integrity.json`：正式日志完整性审计。
- `analysis/formal_results.csv`：纯查询结果。
- `analysis/cost_adjusted_results.csv`：查询+DDL 成本调整结果。
- `analysis/costs.csv`：DDL、模型调用和 token 成本。
- `analysis/summary.json`：完整汇总。
- `analysis/phase_final.*`、`trajectory.*`：阶段结果和轨迹图。

原始 JSONL 是审计依据；CSV、JSON 和图形可通过分析器重新生成。
