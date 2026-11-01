#!/usr/bin/env python3
"""no-prod-data: ask before a coding agent pulls production rows into the conversation.

Claude Code PreToolUse hook. Whatever a command prints becomes part of the conversation and is sent to the
model provider. When that's customer data from production (names, emails, phone numbers, ID numbers,
orders), it has left your systems. This hook asks first when a command reads data from a production
database or data store. Schema and metadata (\\d, information_schema, EXPLAIN, count(*), --schema-only) are fine.

    python3 prodread.py --check 'psql "$DATABASE_URL" -c "select * from users limit 5"'
    python3 prodread.py --test
"""
import json
import os
import re
import shlex
import sys

PROD = re.compile(r"(?i)(?<![a-z])prod(uction)?(?![a-z])|\.rds\.amazonaws\.com|\.supabase\.(co|com)\b|pooler\.supabase|"
                  r"\.neon\.tech|\.psdb\.cloud|\.mongodb\.net|\.database\.azure\.com|\.cockroachlabs\.cloud|\.aivencloud\.com|"
                  r"\.upstash\.io|\.redis(labs)?\.(cloud|com)|--linked\b")

# the command itself, after env assignments and wrappers: "PGPASSWORD=x sudo psql" but not "echo psql"
AT = r"^\s*(?:(?:sudo|time|nohup|command|exec)\s+|env\s+|\w+=(?:\"[^\"]*\"|'[^']*'|\S+)\s+)*(?:\S*/)?"
# commands that read data, with the flag that carries their query text (if any)
CLIENTS = [
    (re.compile(AT + r"psql\b"), r"(?:-c|--command)"),
    (re.compile(AT + r"(?:mysql|mariadb)(?![\w-])"), r"(?:-e|--execute)"),
    (re.compile(AT + r"mongosh?\b"), r"(?:--eval)"),
    (re.compile(AT + r"redis-cli\b"), None),
    (re.compile(AT + r"(?:sqlcmd|clickhouse-client|cqlsh|snowsql)\b"), r"(?:-q|-Q|--query|-e|--execute)"),
    (re.compile(AT + r"bq\s+query\b"), None),
]
DUMPS = re.compile(AT + r"(?:(pg_dump|pg_dumpall|mysqldump|mariadb-dump|mongodump|mongoexport)\b|bq\s+extract\b|supabase\s+db\s+dump\b"
                   r"|heroku\s+pg:(psql|pull)\b|fly(ctl)?\s+(postgres|pg)\s+connect\b|railway\s+connect\b|pscale\s+shell\b"
                   r"|gcloud\s+sql\s+connect\b|aws\s+(--\S+\s+\S+\s+)*rds-data\s+execute-statement\b"
                   r"|aws\s+(--\S+\s+\S+\s+)*dynamodb\s+(scan|query|get-item|batch-get-item|execute-statement)\b"
                   r"|aws\s+(--\S+\s+\S+\s+)*s3\s+(cp|sync)\s+s3://)")
SCHEMA_ONLY_DUMP = re.compile(r"(?:^|\s)(?:-s|--schema-only|--no-data|-d)(?:\s|$)")
KUBECTL_DB = re.compile(AT + r"(?:kubectl|docker(?:\s+compose)?|podman)\b.*\bexec\b.*?(?:--\s*|\s)(?:sh\s+-c\s+)?['\"]?"
                        r"(psql|mysql|mariadb|mongosh?|redis-cli)\b")
REDIS_META = re.compile(r"(?i)^\s*(ping|info|dbsize|config\s+get|memory\s+stats|slowlog|client\s+list|latency|time)\b")
REDIS_ARGS = re.compile(r"\bredis-cli\b(?:\s+-[a-zA-Z]+(?:\s+(?!-)\S+)?|\s+--\S+(?:\s+(?!-)\S+)?|\s+\S+://\S+)*\s+(\S.*)$")
# statements that only look at structure or counts
META_SQL = re.compile(r"""(?ix)^\s*(
      \\[a-z+]*\S*                                   # psql meta commands: \\d, \\dt+, \\l, \\dn
    | explain(?!\s+(\(.*analyze|analyze))\b          # plans, not EXPLAIN ANALYZE (it runs the query)
    | show\s | describe\s | desc\s | use\s | set\s
    | select\s+(count\s*\(\s*\*?\s*\)|1|version\s*\(\)|now\s*\(\)|current_\w+(\s*\(\))?)\s*(from\s+[\w."]+\s*)?(where\s+[^;]*)?$
    | select\b.*\bfrom\s+(information_schema|pg_catalog|pg_stat\w*|pg_\w+|mysql\.|sys\.|sqlite_master)
    | db\.(getCollectionNames|stats|getCollectionInfos|\w+\.(countDocuments|estimatedDocumentCount|getIndexes|stats))\s*\(
    | show\s+(dbs|collections|databases|tables)
    )""")

ASK = """no-prod-data: this looks like it reads data from production ({target}).

Whatever it prints becomes part of this conversation and is sent to the model provider. If it's customer data (names, emails, phone numbers, ID numbers, orders), that's a data transfer the company probably hasn't approved (PDPA, GDPR).

Prefer, in order: staging or a sanitized copy; aggregates instead of rows (count, group by); schema only (\\d, information_schema, --schema-only); or ask the user to run it and paste a redacted result. If the user has explicitly agreed to this query, go ahead and select only the columns you need, with a small LIMIT."""


def env_value(name, cwd):
    """Value of an env var from the environment or the project's .env files (never printed)."""
    if os.environ.get(name):
        return os.environ[name]
    for f in (".env", ".env.local", ".env.development", ".env.production"):
        try:
            with open(os.path.join(cwd, f), encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = re.match(rf"\s*(?:export\s+)?{re.escape(name)}\s*=\s*['\"]?([^'\"\n]+)", line)
                    if m:
                        return m.group(1)
        except OSError:
            continue
    return ""


def prod_target(segment, cwd):
    """Why we think this segment talks to production, or None."""
    m = PROD.search(segment)
    if m:
        return f"'{m.group(0)}' in the command"
    for var in re.findall(r"\$\{?([A-Z_][A-Z0-9_]*)\}?", segment):
        if PROD.search(env_value(var, cwd)):
            return f"${var} points at a production host"
    return None


def query_texts(segment, flag):
    """The SQL or JS the command will run, if it's visible in the command line."""
    texts = []
    if flag:
        for m in re.finditer(rf"(?:^|\s){flag}(?:\s+|=)(\"(?:[^\"\\]|\\.)*\"|'[^']*'|\S+)", segment):
            q = m.group(1)
            texts.append(q[1:-1] if q[:1] in "\"'" and q[-1:] == q[:1] else q)
    heredoc = re.search(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)\n\s*\1\s*$", segment, re.S)
    if heredoc:
        texts.append(heredoc.group(2))
    return texts


def only_metadata(texts):
    statements = [s for t in texts for s in re.split(r";\s*", t) if s.strip()]
    return bool(statements) and all(META_SQL.match(s.strip()) for s in statements)


def segments(command):
    # keep heredocs attached to their command; split the rest on ; && || and newlines
    parts, buf = [], []
    for line in command.split("\n"):
        buf.append(line)
        if re.search(r"<<-?\s*['\"]?\w+['\"]?", "\n".join(buf)) and not re.search(r"\n\s*\w+\s*$", "\n".join(buf)):
            continue
        parts.append("\n".join(buf))
        buf = []
    if buf:
        parts.append("\n".join(buf))
    out = []
    for p in parts:
        if "<<" in p:
            out.append(p)
        else:
            out += [s for s in re.split(r"\s*(?:;|&&|\|\|?)\s*", p) if s.strip()]
    return out


def check_command(command, cwd="."):
    """-> reason string if we should ask, else None"""
    for seg in segments(command):
        if re.match(r"\s*#", seg):
            continue
        target = prod_target(seg, cwd)
        if not target:
            continue
        if DUMPS.search(seg):
            if SCHEMA_ONLY_DUMP.search(seg) and re.search(r"pg_dump|mysqldump|mariadb-dump|supabase\s+db\s+dump", seg):
                continue
            return ASK.format(target=target)
        if KUBECTL_DB.search(seg):
            return ASK.format(target=target)
        for rx, flag in CLIENTS:
            if not rx.search(seg):
                continue
            if "redis-cli" in seg:
                m = REDIS_ARGS.search(seg)
                if m and REDIS_META.match(m.group(1)):
                    break
                return ASK.format(target=target)
            texts = query_texts(seg, flag)
            if texts and only_metadata(texts):
                break
            return ASK.format(target=target)
    return None


def check_mcp(tool_name, tool_input):
    """MCP database tools whose server name says production."""
    m = re.match(r"mcp__([^_].*?)__(.+)$", tool_name or "")
    if not m:
        return None
    server, tool = m.groups()
    if not PROD.search(server) or not re.search(r"(?i)query|sql|select|find|read|scan|get|execute|run", tool):
        return None
    text = " ".join(str(v) for v in (tool_input or {}).values() if isinstance(v, str))
    if text and only_metadata([text]):
        return None
    return ASK.format(target=f"MCP server '{server}'")


def handle(data):
    if data.get("hook_event_name") != "PreToolUse":
        return
    name, ti = data.get("tool_name"), data.get("tool_input") or {}
    reason = check_command(ti.get("command") or "", data.get("cwd") or os.getcwd()) if name == "Bash" else check_mcp(name, ti)
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                                 "permissionDecisionReason": reason}}))


def test():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, ".env"), "w") as fh:
            fh.write("DATABASE_URL=postgresql://app:x@db-prod.abc123.ap-southeast-1.rds.amazonaws.com:5432/app\n"
                     "STAGING_URL=postgresql://app:x@localhost:5432/app\n")
        ask = [
            'psql "$DATABASE_URL" -c "select * from users limit 5"',
            "psql postgresql://ro@db.abcd.supabase.co:5432/postgres -c 'select email, phone from customers'",
            "psql -h prod-db.internal -U app app",
            "PGPASSWORD=x psql -h orders-production.cluster-xyz.rds.amazonaws.com -c 'SELECT * FROM orders'",
            "mysql -h prod-mysql -e 'select * from customers'",
            "mongosh 'mongodb+srv://u:p@cluster0.ab12c.mongodb.net/shop' --eval 'db.users.find().limit(5)'",
            "pg_dump $DATABASE_URL > dump.sql",
            "supabase db dump --linked -f data.sql --data-only",
            "kubectl --context prod-eks exec -it deploy/api -- psql -c 'select * from users'",
            "aws dynamodb scan --table-name prod-users --profile prod",
            "redis-cli -h cache.prod.internal GET session:123",
            "heroku pg:psql -a myapp-production",
            "docker compose exec db-prod psql -U app -c 'select * from users'",
            "aws --profile prod dynamodb scan --table-name users",
            "cd api && psql $DATABASE_URL",
            "echo 'select * from users' | psql $DATABASE_URL",
            "psql $DATABASE_URL <<'SQL'\nselect id, email from users where id = 5;\nSQL",
            "psql $DATABASE_URL -c 'explain analyze select * from users'",
        ]
        fine = [
            'psql "$DATABASE_URL" -c "\\d users"',
            "psql $DATABASE_URL -c 'select count(*) from users'",
            "psql $DATABASE_URL -c \"select column_name, data_type from information_schema.columns where table_name='users'\"",
            "psql $DATABASE_URL -c 'explain select * from users where id = 1'",
            "pg_dump --schema-only $DATABASE_URL > schema.sql",
            "mysqldump --no-data -h prod-mysql shop > schema.sql",
            "psql $STAGING_URL -c 'select * from users'",
            "psql -h localhost -c 'select * from users'",
            "redis-cli -h cache.prod.internal INFO memory",
            "mongosh 'mongodb+srv://u:p@cluster0.ab12c.mongodb.net/shop' --eval 'db.users.countDocuments()'",
            "grep -r production src/",
            "npm run build -- --mode production",
            "kubectl --context prod-eks get pods",
            "echo 'psql prod' # just text",
            "cat notes.md | grep 'psql prod'",
        ]
        for c in ask:
            assert check_command(c, d), c
        for c in fine:
            assert not check_command(c, d), c
    assert check_mcp("mcp__postgres-prod__query", {"sql": "select * from users"})
    assert not check_mcp("mcp__postgres-prod__query", {"sql": "select count(*) from users"})
    assert not check_mcp("mcp__postgres-staging__query", {"sql": "select * from users"})
    assert not check_mcp("Bash", {})
    print("ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--test"]:
        test()
    elif sys.argv[1:2] == ["--check"]:
        r = check_command(" ".join(sys.argv[2:]))
        print(r or "fine")
        sys.exit(1 if r else 0)
    else:
        try:
            handle(json.load(sys.stdin))
        except Exception as e:  # a broken hook must never break the session
            print(f"no-prod-data: {e}", file=sys.stderr)
