CREATE TABLE public.agent_orders (
  order_id integer PRIMARY KEY,
  customer_id integer NOT NULL,
  product_id integer NOT NULL,
  order_date date NOT NULL,
  amount integer NOT NULL
);
SELECT setseed(0.2026);
INSERT INTO public.agent_orders
SELECT g, 1 + floor(random() * 1000)::int,
       1 + floor(random() * 1000)::int,
       DATE '2024-01-01' + floor(random() * 365)::int,
       100 + floor(random() * 900)::int
FROM generate_series(1, 200000) AS t(g);
ANALYZE public.agent_orders;
