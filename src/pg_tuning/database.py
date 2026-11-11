"""Dedicated-database initialization, immutable dataset checks and safe DDL."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import time

import psycopg

from .catalog import BUDGET, CANDIDATES

ROOT = Path(__file__).resolve().parents[2]
LOCK_ID = 2026020016


class StateError(RuntimeError):
    pass


class UnrecoverableDatabaseError(RuntimeError):
    def __init__(self, result):
        self.result = result
        super().__init__(result.get("error", "Database state cannot be recovered"))


def connect(dsn):
    return psycopg.connect(dsn, autocommit=True, prepare_threshold=None, connect_timeout=10)


def require_pg16(conn):
    if not 160000 <= conn.info.server_version < 170000:
        raise StateError("PostgreSQL 16 is required; refusing to use another major version")


@contextmanager
def experiment_lock(dsn):
    # A separate session keeps the lease even when the DDL session is recovered.
    with connect(dsn) as conn:
        require_pg16(conn)
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,)).fetchone()[0]:
            raise StateError("Another tuning controller owns this experiment database")
        try:
            yield conn
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))


def validate_schema(conn):
    rows = conn.execute("""
        SELECT attname, format_type(atttypid, atttypmod), attnotnull
        FROM pg_attribute WHERE attrelid='public.agent_orders'::regclass
        AND attnum > 0 AND NOT attisdropped ORDER BY attnum
    """).fetchall()
    expected = [(n, t, True) for n, t in (
        ("order_id", "integer"), ("customer_id", "integer"),
        ("product_id", "integer"), ("order_date", "date"), ("amount", "integer"))]
    if rows != expected:
        raise StateError("Existing agent_orders schema differs from the assignment")
    pks = conn.execute("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conrelid='public.agent_orders'::regclass AND contype='p'""").fetchall()
    if pks != [("PRIMARY KEY (order_id)",)]:
        raise StateError("Primary key differs from the assignment")


def fingerprint(conn):
    validate_schema(conn)
    h = hashlib.sha256()
    count = 0
    # A streamed deterministic digest includes every field, including the frozen dates.
    with conn.cursor() as cur:
        cur.execute("SELECT order_id,customer_id,product_id,order_date,amount FROM public.agent_orders ORDER BY order_id")
        while rows := cur.fetchmany(4096):
            for oid, cid, pid, day, amount in rows:
                count += 1
                if oid != count or not (1 <= cid <= 1000 and 1 <= pid <= 1000
                        and "2024-01-01" <= str(day) <= "2024-12-30" and 100 <= amount <= 999):
                    raise StateError("Existing dataset violates assignment value ranges or primary-key sequence")
                h.update(f"{oid},{cid},{pid},{day},{amount}\n".encode())
    if count != 200000:
        raise StateError(f"Expected exactly 200000 existing rows, found {count}; never auto-regenerate")
    return {"row_count": count, "sha256": h.hexdigest(), "schema": "assignment-v1"}


def initialize(conn, record_path):
    require_pg16(conn)
    exists = conn.execute("SELECT to_regclass('public.agent_orders')").fetchone()[0] is not None
    path = Path(record_path)
    if not exists and path.exists():
        raise StateError("Recorded dataset is missing; refusing to regenerate experiment data")
    if not exists:
        # All four statements run on this one connection in an atomic transaction.
        with conn.transaction():
            for statement in (ROOT / "sql/init.sql").read_text().split(";"):
                if statement.strip():
                    conn.execute(statement)
    data = fingerprint(conn)
    if path.exists():
        prior = json.loads(path.read_text())
        if any(prior[k] != data[k] for k in data):
            raise StateError("Dataset differs from recorded initialization; refusing to modify it")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
    return {**data, "reused": exists}


def actual_indexes(conn):
    # Include reserved-name collisions on other tables. Never DROP an unrelated object.
    rows = conn.execute("""
        SELECT ni.nspname, ci.relname, nt.nspname, ct.relname, am.amname,
               ix.indisunique, ix.indisprimary, ix.indisvalid, ix.indisready,
               ix.indpred IS NULL, ix.indexprs IS NULL, ix.indnkeyatts=ix.indnatts,
               ARRAY(SELECT pg_get_indexdef(ci.oid, k, true)
                     FROM generate_series(1, ix.indnkeyatts) k),
               ci.relkind
        FROM pg_index ix JOIN pg_class ci ON ci.oid=ix.indexrelid
        JOIN pg_namespace ni ON ni.oid=ci.relnamespace
        JOIN pg_class ct ON ct.oid=ix.indrelid
        JOIN pg_namespace nt ON nt.oid=ct.relnamespace
        JOIN pg_am am ON am.oid=ci.relam
        WHERE (nt.nspname='public' AND ct.relname='agent_orders')
           OR (ni.nspname='public' AND ci.relname=ANY(%s))
    """, ([c["name"] for c in CANDIDATES.values()],)).fetchall()
    by_name = {c["name"]: (key, c) for key, c in CANDIDATES.items()}
    actual = []
    for ns, name, tns, table, am, unique, primary, valid, ready, nopred, noexpr, noinclude, cols, kind in rows:
        if primary and name not in by_name:
            if (tns, table, cols, valid) != ("public", "agent_orders", ["order_id"], True):
                raise StateError("Unexpected primary key index")
            continue
        if name not in by_name:
            raise StateError(f"Uncontrolled index on experiment table: {name}")
        key, candidate = by_name[name]
        if ((ns, tns, table, am, kind) != ("public", "public", "agent_orders", "btree", "i")
                or unique or primary or not all((valid, ready, nopred, noexpr, noinclude))
                or tuple(cols) != candidate["columns"]):
            raise StateError(f"Reserved candidate name has an unexpected definition: {name}")
        actual.append(key)
    if len(actual) > BUDGET:
        raise StateError("Actual candidate index count exceeds two")
    return sorted(actual)


def database_metadata(conn):
    settings = dict(conn.execute("""SELECT name,setting FROM pg_settings
        WHERE name IN ('server_version','server_encoding','block_size','shared_buffers',
        'effective_cache_size','work_mem','maintenance_work_mem','random_page_cost',
        'seq_page_cost','cpu_tuple_cost','cpu_index_tuple_cost','cpu_operator_cost',
        'max_parallel_workers','max_parallel_workers_per_gather','max_parallel_maintenance_workers',
        'jit','default_statistics_target','enable_seqscan','enable_indexscan',
        'enable_indexonlyscan','enable_bitmapscan','autovacuum','track_io_timing',
        'plan_cache_mode','TimeZone','lc_collate','lc_ctype') ORDER BY name""").fetchall())
    return {"server_version": conn.info.server_version, "database": conn.info.dbname,
            "settings": settings}


class Database:
    def __init__(self, dsn):
        self.dsn = dsn
        self.conn = connect(dsn)
        require_pg16(self.conn)

    def close(self):
        self.conn.close()

    def actual_indexes(self):
        return actual_indexes(self.conn)

    def recover(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.conn = connect(self.dsn)
        require_pg16(self.conn)
        return self.actual_indexes()

    def apply_checked(self, decision):
        from .policy import validate_action
        action = decision.get("final_action")
        result = {"action": action, "status": "noop", "ddl_ms": 0.0,
                  "attempted": False, "executed": False, "error": None,
                  "rejection_kind": None, "unauthorized_execution": False,
                  "budget_violation": False}
        current = self.actual_indexes()
        result["actual_indexes"] = current
        if decision.get("status") in ("model_error", "invalid_response"):
            result["status"] = decision["status"]
            return result
        result["attempted"] = isinstance(action, dict) and action.get("action") in ("CREATE", "DROP")
        try:
            action = validate_action(action)
        except (ValueError, TypeError) as exc:
            result.update(status="rejected", error=str(exc), rejection_kind="unknown_id" if
                          isinstance(action, dict) and (not isinstance(action.get("index"), str) or action.get("index") not in CANDIDATES)
                          and action.get("action") != "NOOP" else "invalid_action")
            return result
        kind, key = action["action"], action["index"]
        if kind == "NOOP":
            return result
        problem = ("duplicate" if kind == "CREATE" and key in current else
                   "absent" if kind == "DROP" and key not in current else
                   "budget" if kind == "CREATE" and len(current) >= BUDGET else None)
        if problem:
            result.update(status="rejected", rejection_kind=problem, error=problem)
            return result
        sql = CANDIDATES[key]["create_sql" if kind == "CREATE" else "drop_sql"]
        start = time.perf_counter()
        try:
            result["executed"] = True
            self.conn.execute(sql)
            result["ddl_ms"] = (time.perf_counter() - start) * 1000
            result["actual_indexes"] = self.actual_indexes()
            expected = sorted(set(current) | {key}) if kind == "CREATE" else sorted(set(current) - {key})
            if result["actual_indexes"] != expected:
                raise StateError("DDL completed but actual index state differs from expected state")
            result["status"] = "applied"
        except Exception as exc:
            if result["ddl_ms"] == 0:
                result["ddl_ms"] = (time.perf_counter() - start) * 1000
            # No DSN, credentials or arbitrary database error text in exported logs.
            result.update(status="failed", error=f"{type(exc).__name__}; sqlstate={getattr(exc, 'sqlstate', None)}")
            try:
                result["actual_indexes"] = self.recover()
            except Exception as recovery:
                result["error"] += f"; state recovery failed: {type(recovery).__name__}"
                raise UnrecoverableDatabaseError(result) from None
        return result

    def reset(self, on_result=None):
        results = []
        for key in self.actual_indexes():
            try:
                result = self.apply_checked({"final_action": {"action": "DROP", "index": key, "reason": "condition reset"}})
            except UnrecoverableDatabaseError as exc:
                if on_result:
                    on_result(exc.result)
                raise
            results.append(result)
            if on_result:
                on_result(result)
            if result["status"] != "applied":
                raise UnrecoverableDatabaseError(result)
        if self.actual_indexes():
            raise StateError("Reset failed to leave zero candidate indexes")
        return results
