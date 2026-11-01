# no-prod-data

[ภาษาไทย](README.th.md) · From the maker of [safe-migrations](https://github.com/nuttakulsv/safe-migrations) and [thai-pii-scan](https://github.com/nuttakulsv/thai-pii-scan)

You ask your agent why an order is stuck. It reads `.env`, finds `DATABASE_URL`, and runs:

```
psql "$DATABASE_URL" -c "select * from orders o join customers c on c.id = o.customer_id order by o.created_at desc limit 50"
```

`DATABASE_URL` points at production. Fifty customers' names, emails, phone numbers and addresses are now in the conversation, and on their way to the model provider. Nothing was deleted, nothing broke, and you still have a data protection problem (PDPA, GDPR).

no-prod-data is a Claude Code hook that asks before a command pulls rows out of a production database or data store. Schema and counts go through, because that's usually all the agent needs.

## What it asks about

**Commands that read data:** `psql`, `mysql`/`mariadb`, `mongosh`, `redis-cli` reads (`GET`, `HGETALL`, `KEYS`...), `sqlcmd`, `clickhouse-client`, `cqlsh`, `snowsql`, `bq query`, dumps (`pg_dump`, `mysqldump`, `mongodump`, `mongoexport`, `supabase db dump`), `heroku pg:psql`, `fly postgres connect`, `railway connect`, `pscale shell`, `gcloud sql connect`, `aws dynamodb scan/query/get-item`, `aws rds-data`, `aws s3 cp s3://...`, and `kubectl exec` / `docker exec` into a database client. Piped input (`echo "select..." | psql`) counts too.

**...when they point at production:** `prod`/`production` in the host, context, profile or app name; managed database hosts (`*.rds.amazonaws.com`, `*.supabase.co`, `*.neon.tech`, `*.psdb.cloud`, `*.mongodb.net`, Azure, Aiven, Upstash, Redis Cloud); `supabase --linked`. It also looks up `$DATABASE_URL`-style variables in your environment and the project's `.env` files, so `psql $DATABASE_URL` is caught when that URL is production (without ever printing it).

**MCP database tools** too, when the server's name says it's production (`mcp__postgres-prod__query`).

**Let through:** `\d`, `\dt`, `information_schema` and `pg_catalog` queries, `SHOW`, `DESCRIBE`, `EXPLAIN` (but not `EXPLAIN ANALYZE`, which runs the query), `select count(*)`, `--schema-only` / `--no-data` dumps, `redis-cli INFO`/`PING`/`DBSIZE`, `countDocuments()`, and anything not aimed at production.

When it asks, the agent sees why and what to do instead:

```
no-prod-data: this looks like it reads data from production ($DATABASE_URL points at a production host).

Whatever it prints becomes part of this conversation and is sent to the model provider. ...
Prefer, in order: staging or a sanitized copy; aggregates instead of rows (count, group by);
schema only; or ask the user to run it and paste a redacted result.
```

It's `ask`, not `deny`: sometimes reading one production record is the right call, and you get to decide.

## Install

Claude Code (hook and skill):

```
/plugin marketplace add nuttakulsv/no-prod-data
/plugin install no-prod-data@no-prod-data
```

The skill on its own (Codex, Cursor and others) covers how to debug production without pulling rows: structure first, aggregates instead of rows, one record by id with only the needed columns, never copy prod rows into fixtures or replies:

```
npx skills add nuttakulsv/no-prod-data
```

Check a command by hand:

```
python3 hooks/prodread.py --check 'psql "$DATABASE_URL" -c "select * from users limit 5"'
```

## Limits

- It recognizes production by name and host. A production database called `db1` on a private IP looks like any other; name your hosts, contexts and profiles, or put the URL in an env var whose value contains `prod`.
- It reads the command line. A script that queries production on its own (`python debug.py`) gets through.
- A hook is one layer. The stronger ones are a read-only role, a replica with masked columns, and keeping production credentials out of the agent's environment entirely.
- Writes and destructive commands are a different problem: see [safe-migrations](https://github.com/nuttakulsv/safe-migrations).

## Tests

```
python3 hooks/prodread.py --test
```

## License

MIT
