--Failure-code map
CREATE OR REPLACE TABLE code_buckets AS
SELECT * FROM (VALUES
  ('4',  'FUNDS'),
  ('11', 'WAIT'),
  ('2',  'BLOCKED'),
  ('8',  'REVOKED'),
  ('10', 'REVOKED'),
  ('1',  'HARD'),
  ('3',  'HARD'),
  ('6',  'HARD')
) AS t(failed_reason, bucket);


--Group requests into attempts
CREATE OR REPLACE TABLE attempts_base AS
WITH g AS (
  SELECT
    loan_id,
    created_utc,
    min(created_cdmx)                                               AS created_cdmx,
    count(*)                                                        AS n_requests,
    list(amount ORDER BY amount DESC)                               AS amounts,
    sum(amount)                                                     AS amount_total,
    max(completed_utc)                                              AS result_utc,
    count(*) FILTER (WHERE status IN ('successful', 'chargeback'))  AS n_success,
    count(*) FILTER (WHERE status = 'chargeback')                   AS n_chargeback,
    coalesce(sum(amount) FILTER (WHERE status IN ('successful', 'chargeback')), 0) AS success_amount,
    coalesce(sum(amount) FILTER (WHERE status = 'successful'), 0)   AS net_collected,
    min(failed_reason)                                              AS failed_reason
  FROM requests
  GROUP BY loan_id, created_utc
)
SELECT
  g.*,
  CASE WHEN g.n_success > 0 THEN 'SUCCESS'
       ELSE coalesce(b.bucket, 'UNKNOWN') END                       AS outcome
FROM g
LEFT JOIN code_buckets b USING (failed_reason);



--add the “look back” columns
CREATE OR REPLACE TABLE attempts AS
SELECT
  a.*,
  CAST(a.created_cdmx AS DATE)                                       AS attempt_day,
  row_number() OVER w                                                AS attempt_no,
  coalesce(lag(a.outcome) OVER w, 'FIRST')                           AS prev_outcome,
  lag(a.result_utc) OVER w                                           AS prev_result_utc,
  a.created_utc < lag(a.result_utc) OVER w                           AS sent_while_prev_pending,
  date_diff('second', lag(a.created_utc) OVER w, a.created_utc) / 86400.0 AS gap_days,
  coalesce(sum(CASE WHEN a.n_success > 0 THEN 1 ELSE 0 END) OVER (
      PARTITION BY a.loan_id ORDER BY a.created_utc
      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) > 0      AS ever_paid_before,
  date_diff('day', l.overdue_at, CAST(a.created_cdmx AS DATE))       AS days_overdue_at_attempt,
  (day(a.created_cdmx) BETWEEN 12 AND 15
     OR day(a.created_cdmx) >= 28
     OR day(a.created_cdmx) <= 2)                                    AS payday_window
FROM attempts_base a
JOIN loans l USING (loan_id)
WINDOW w AS (PARTITION BY a.loan_id ORDER BY a.created_utc);