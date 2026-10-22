--One row per debit, with its context and buckets

CREATE OR REPLACE TABLE debits AS
SELECT
  r.payment_request_id,
  r.loan_id,
  r.amount,
  r.status,
  r.completed_utc,
  a.attempt_day,
  a.attempt_no,
  a.n_requests,
  a.prev_outcome,
  a.ever_paid_before,
  a.payday_window,
  l.bank,
  CASE WHEN a.attempt_no = 1   THEN '1'
       WHEN a.attempt_no <= 5  THEN '2-5'
       WHEN a.attempt_no <= 20 THEN '6-20'
       ELSE '21+' END                                   AS attempt_bucket,
  CASE WHEN a.days_overdue_at_attempt <= 90  THEN '1-90'
       WHEN a.days_overdue_at_attempt <= 180 THEN '91-180'
       WHEN a.days_overdue_at_attempt <= 365 THEN '181-365'
       ELSE '365+' END                                  AS overdue_segment,
  CASE WHEN a.gap_days IS NULL THEN 'first'
       WHEN a.gap_days < 4     THEN '<4'
       WHEN a.gap_days < 10    THEN '4-9'
       ELSE '10+' END                                   AS gap_bucket,
  CASE WHEN r.amount < 100  THEN '<100'
       WHEN r.amount < 500  THEN '100-499'
       WHEN r.amount < 2000 THEN '500-1999'
       ELSE '2000+' END                                 AS amount_bucket,
  r.status IN ('successful', 'chargeback')              AS is_success,
  r.status = 'chargeback'                               AS is_chargeback
FROM requests r
JOIN attempts a ON a.loan_id = r.loan_id AND a.created_utc = r.created_utc
JOIN loans l    ON l.loan_id = r.loan_id
WHERE a.attempt_day < (SELECT as_of FROM params);



--Success odds in four levels. Each level shrinks toward its parent with a weight of 10 (α + β = 10 from the spec).

-- Level 0: everything
CREATE OR REPLACE TABLE odds_l0 AS
SELECT count(*) AS n, sum(is_success::INT) AS s, avg(is_success::INT) AS p_success
FROM debits;

-- Level 1: previous outcome × paid before × how overdue
CREATE OR REPLACE TABLE odds_l1 AS
SELECT prev_outcome, ever_paid_before, overdue_segment,
       count(*) AS n, sum(is_success::INT) AS s,
       (sum(is_success::INT) + 10 * (SELECT p_success FROM odds_l0)) / (count(*) + 10) AS p_success
FROM debits
GROUP BY ALL;

-- Level 2: + attempt number, payday window, days since previous attempt
CREATE OR REPLACE TABLE odds_l2 AS
SELECT d.prev_outcome, d.ever_paid_before, d.overdue_segment,
       d.attempt_bucket, d.payday_window, d.gap_bucket,
       count(*) AS n, sum(d.is_success::INT) AS s,
       (sum(d.is_success::INT) + 10 * any_value(p.p_success)) / (count(*) + 10) AS p_success
FROM debits d
JOIN odds_l1 p USING (prev_outcome, ever_paid_before, overdue_segment)
GROUP BY ALL;

-- Level 3: + amount bucket
CREATE OR REPLACE TABLE odds_l3 AS
SELECT d.prev_outcome, d.ever_paid_before, d.overdue_segment,
       d.attempt_bucket, d.payday_window, d.gap_bucket, d.amount_bucket,
       count(*) AS n, sum(d.is_success::INT) AS s,
       (sum(d.is_success::INT) + 10 * any_value(p.p_success)) / (count(*) + 10) AS p_success
FROM debits d
JOIN odds_l2 p USING (prev_outcome, ever_paid_before, overdue_segment, attempt_bucket, payday_window, gap_bucket)
GROUP BY ALL;



--Chargeback odds in three levels. Only successful debits that came back at least 30 days before the decision day count here, because chargebacks can still arrive for anything newer (95.7% arrive within 30 days).
CREATE OR REPLACE VIEW matured_successes AS
SELECT * FROM debits
WHERE is_success
  AND completed_utc <= CAST((SELECT as_of FROM params) AS TIMESTAMP) - INTERVAL 30 DAY;

CREATE OR REPLACE TABLE cb_l0 AS
SELECT count(*) AS n, sum(is_chargeback::INT) AS c, avg(is_chargeback::INT) AS p_cb
FROM matured_successes;

CREATE OR REPLACE TABLE cb_l1 AS
SELECT bank, count(*) AS n, sum(is_chargeback::INT) AS c,
       (sum(is_chargeback::INT) + 10 * (SELECT p_cb FROM cb_l0)) / (count(*) + 10) AS p_cb
FROM matured_successes
GROUP BY ALL;

CREATE OR REPLACE TABLE cb_l2 AS
SELECT m.bank, m.amount_bucket, count(*) AS n, sum(m.is_chargeback::INT) AS c,
       (sum(m.is_chargeback::INT) + 10 * any_value(b.p_cb)) / (count(*) + 10) AS p_cb
FROM matured_successes m
JOIN cb_l1 b USING (bank)
GROUP BY ALL;