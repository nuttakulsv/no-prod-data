---
name: no-prod-data
description: Use when debugging or investigating anything that involves a production database, data warehouse, cache, logs or data export - SQL against prod, psql/mysql/mongosh sessions, dumps, DynamoDB scans, kubectl exec into a database pod, MCP database tools. Keeps customer data out of the conversation.
---

# Keep production data out of the conversation

Everything a command prints becomes part of this conversation and goes to the model provider. Customer rows from production (names, emails, phone numbers, national ID numbers, addresses, orders, messages) should not. Under PDPA and GDPR that's a transfer of personal data the company has to have a basis for.

## How to debug without pulling rows

1. **Start with structure.** `\d table`, `information_schema`, `EXPLAIN` (not `EXPLAIN ANALYZE`, which runs the query), `pg_dump --schema-only`.
2. **Use aggregates instead of rows.** `select status, count(*) from orders where created_at > now() - interval '1 day' group by 1` tells you what's wrong without showing anyone's order.
3. **Use staging or a sanitized copy** for anything that needs real-looking rows.
4. **When you need one specific record**, select only the columns that matter, by id, with `limit 1`, and leave out personal fields (name, email, phone, address, ID number) unless they're the bug.
5. **Or ask the user to run it** and paste a result with personal data removed.
6. **Never** copy production rows into files, fixtures, tests, issues, commit messages or your final reply.
7. Read-only doesn't mean safe: a `select *` on prod is still a data export into this conversation.

If the user explicitly says it's fine to read specific production data, do exactly that query and nothing broader.
