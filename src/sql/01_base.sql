CREATE OR REPLACE TABLE loans AS
SELECT loan_id, payment_method_bank AS bank,
       CAST(loan_amount AS DOUBLE) AS loan_amount,
       CAST(total_amount_outstanding AS DOUBLE) AS total_amount_outstanding,
       CAST(strptime(overdue_at, '%m/%d/%Y') AS DATE) AS overdue_at,
       CAST(overdue_days AS INTEGER) AS overdue_days
FROM read_csv('data/PaymentsPM_Challenge_loan_info.csv', header=true, all_varchar=true);

CREATE OR REPLACE TABLE requests AS
SELECT payment_request_id, loan_id, status,
       CAST(amount AS DOUBLE) AS amount, failed_reason, failed_message,
       CAST(replace(created_at, 'Z', '') AS TIMESTAMP) AS created_utc,
       CAST(replace(created_at, 'Z', '') AS TIMESTAMP) - INTERVAL 6 HOUR AS created_cdmx,
       CAST(replace(completed_at, 'Z', '') AS TIMESTAMP) AS completed_utc,
       CAST(replace(chargeback_at, 'Z', '') AS TIMESTAMP) AS chargeback_utc
FROM read_csv('data/PaymentsPM_Challenge_payment_requests.csv', header=true, all_varchar=true);