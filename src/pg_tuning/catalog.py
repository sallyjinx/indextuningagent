"""Assignment constants. No model-supplied text reaches SQL execution."""
PRED = {
    1: "customer_id = %s",
    2: "product_id = %s",
    3: "order_date >= %s AND order_date < %s",
    4: "customer_id = %s AND order_date >= %s AND order_date < %s",
    5: "product_id = %s AND order_date >= %s AND order_date < %s",
    6: "customer_id = %s AND product_id = %s",
}
Q = {k: "SELECT sum(amount) FROM public.agent_orders WHERE " + v for k, v in PRED.items()}
PREDICATE_COLUMNS = {
    1: ("customer_id",), 2: ("product_id",), 3: ("order_date",),
    4: ("customer_id", "order_date"), 5: ("product_id", "order_date"),
    6: ("customer_id", "product_id"),
}
_INDEXES = [
    ("idx_agent_customer", ("customer_id",)),
    ("idx_agent_product", ("product_id",)),
    ("idx_agent_date", ("order_date",)),
    ("idx_agent_customer_date", ("customer_id", "order_date")),
    ("idx_agent_product_date", ("product_id", "order_date")),
    ("idx_agent_customer_product", ("customer_id", "product_id")),
]
CANDIDATES = {
    f"I{i}": {"name": name, "columns": cols,
               "create_sql": f"CREATE INDEX {name} ON public.agent_orders USING btree ({', '.join(cols)})",
               "drop_sql": f"DROP INDEX public.{name}"}
    for i, (name, cols) in enumerate(_INDEXES, 1)
}
BUDGET = 2
EXPLAIN = "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) "
