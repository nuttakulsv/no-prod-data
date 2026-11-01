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

# the command itself, after env assignments and wrappers: "PGPASSWORD=x timeout 30 psql" but not "echo psql"
WRAP = (r"(?:(?:sudo|time|nohup|command|exec|nice(?:\s+-n\s*-?\d+)?|ionice(?:\s+-\S+)*|stdbuf(?:\s+-\S+)*"
        r"|xargs(?:\s+-\S+)*|timeout(?:\s+-\S+)*\s+\d+[smhd]?)\s+|env(?:\s+-\S+)*\s+|\w+=(?:\"[^\"]*\"|'[^']*'|\S+)\s+)*")
# also after a subshell, group, negation or shell keyword: (psql ...), { psql ...; }, ! psql, then psql
AT = r"^\s*(?:[({!]\s*|(?:then|do|else|elif|if|while|until)\s+)*" + WRAP + r"(?:\S*/)?"
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
# any data client as a word, wherever it sits: the fallback when a wrapper we don't know hides the command position
CLIENT_WORD = re.compile(r"(?<![\w.-])(?:\S*/)?(?:psql|mysql|mariadb|mongosh?|redis-cli|sqlcmd|clickhouse-client|cqlsh|snowsql|pg_dump|"
                         r"pg_dumpall|mysqldump|mariadb-dump|mongodump|mongoexport)(?![\w-])")
# other places a client can get its queries from: a file, a redirect, a pipe
EXTRA_INPUT = re.compile(r"(?:^|\s)(?:-f|--file)(?:\s+|=)\S+|(?<!<)<(?!<)\s*\S+")
REDIS_META = re.compile(r"(?i)^\s*(ping|info|dbsize|config\s+get|memory\s+stats|slowlog|client\s+list|latency|time)\b[^;|&]*$")
REDIS_ARGS = re.compile(r"\bredis-cli\b(?:\s+-[a-zA-Z]+(?:\s+(?!-)\S+)?|\s+--\S+(?:\s+(?!-)\S+)?|\s+\S+://\S+)*\s+(\S.*)$")
# shell code hidden inside another command: bash -c "...", eval "...", ssh host "...", $(...), `...`
QUOTED = r"(\"(?:[^\"\\]|\\.)*\"|'[^']*')"
INNER = [re.compile(r"\b(?:bash|sh|zsh|dash|ksh)\s+(?:-\w+\s+)*-\w*c\w*\s+" + QUOTED), re.compile(r"\beval\s+" + QUOTED),
         re.compile(r"\bssh\s+(?:-\S+(?:\s+(?!-)\S+)?\s+)*\S+\s+" + QUOTED), re.compile(r"\$\(((?:[^()]|\([^()]*\))*)\)"),
         re.compile(r"`([^`]*)`"), re.compile(r"[<>]\(((?:[^()]|\([^()]*\))*)\)"), re.compile(r"\bwatch\s+(?:-\S+(?:\s+(?!-)\S+)?\s+)*" + QUOTED)]
# commands that only print or search text: a client name inside their quoted arguments is just text
# Only commands that print or search text and can't run other programs (not awk, sed, find, sort, rg, git: they can).
HARMLESS = {"echo", "printf", "grep", "egrep", "fgrep", "cat", "head", "tail", "wc", "ls", "which", "type"}
# wrappers that run the command after them, and their options that take a value
# Any other option (and any --long option) means we don't know what runs, so argv0() gives up and nothing is harmless.
# env -S is left out on purpose: it splits a string into a command and runs it.
WRAPPERS = {  # name: (options that take a value, options that don't)
    "sudo": ("-u -g -h -p -C -D -r -t -U -R -T", "-E -H -n -S -b -i -k -K -P -s -A -B -e"),
    "doas": ("-u -C", "-n -s -L"),
    "env": ("-u -C", "-i -0 -v"),
    "time": ("-o -f", "-p -a -v -q"),
    "nohup": ("", ""),
    "command": ("", "-p"),
    "exec": ("-a", "-c -l"),
    "nice": ("-n", ""),
    "ionice": ("-c -n -p -P -u", "-t"),
    "stdbuf": ("-i -o -e", ""),
    "timeout": ("-k -s", "-v -f"),
    "xargs": ("-I -i -L -l -n -P -s -d -E -a -e", "-0 -r -t -p -x"),
}
SHELL_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "(", "time"}
ASSIGN = re.compile(r"(?:^|[\s;&|])(?:export\s+)?([A-Za-z_]\w*)=(\"[^\"]*\"|'[^']*'|[^\s;&|]+)")

# statements that only look at structure or counts; each must match the WHOLE statement
# WHERE: plain "column op literal" comparisons only. No casts or functions: "where email::int = 1" fails with an error
# message that contains the email.
# literals can't contain SQL keywords, and column names can't be quoted: MySQL reads "..." as a string, Postgres as a name
_LITERAL = r"(?:'(?:(?!\b(?:union|select|from|into|join|copy)\b)[^'\\])*'|-?\d+(?:\.\d+)?|true|false|null)"
_COND = rf"[A-Za-z_][\w.]*\s*(?:(?:=|!=|<>|<=|>=|<|>|(?:not\s+)?i?like)\s*{_LITERAL}|is\s+(?:not\s+)?null)"
_WHERE = rf"(?:\s+where\s+{_COND}(?:\s+(?:and|or)\s+{_COND})*)?"
# select lists for catalog queries: plain column names only, so no function calls like pg_read_file()
_SELECT_LIST = r"(?:(?!\b(?:select|from|union|into)\b)[\w\s,.*])+?"
META_SQL = re.compile(r"(?is)^\s*(?:"
    r"\\(?:d[tivmnsufxSEgcpTo]*\+?|l\+?|conninfo)(?:\s+[\w.\"*]+)?"     # psql: \d users, \dt+, \l, \conninfo (not \copy, \o, \g, \!, \i)
    r"|explain\s+(?!.*\banaly[sz]e\b)[^\\]+"                          # plans, not EXPLAIN ANALYZE (it runs the query)
    r"|(?:show|describe|desc)\s+[\w.\"` ]+|(?:set|use)\s+[\w.\" =:-]+"
    rf"|select\s+(?:count\s*\(\s*\*?\s*\)|1|version\s*\(\s*\)|now\s*\(\s*\)|current_\w+(?:\s*\(\s*\))?)(?:\s+from\s+[\w.\"]+{_WHERE})?"
    rf"|select\s+{_SELECT_LIST}\s+from\s+(?:information_schema|pg_catalog)\.[\w\"]+{_WHERE}(?:\s+order\s+by\s+[\w\s,.\"]+)?"
    rf"|select\s+{_SELECT_LIST}\s+from\s+(?:pg_stat_\w+|pg_tables|pg_indexes|pg_class|sqlite_master){_WHERE}"
    r"|db\.(?:getCollectionNames|getCollectionInfos|stats|serverStatus|version|hostInfo)\(\s*\)"
    r"|db\.(?:getCollection\(\s*[\"'][\w.-]+[\"']\s*\)|\w+)\.(?:countDocuments|estimatedDocumentCount|getIndexes|stats)\(\s*(?:\{\s*\})?\s*\)"
    r"|show\s+(?:dbs|collections|databases|tables)"
    r")\s*;?\s*$")

ASK = """no-prod-data: this looks like it reads data from production ({target}).

Whatever it prints becomes part of this conversation and is sent to the model provider. If it's customer data (names, emails, phone numbers, ID numbers, orders), that's a data transfer the company probably hasn't approved (PDPA, GDPR).

Prefer, in order: staging or a sanitized copy; aggregates instead of rows (count, group by); schema only (\\d, information_schema, --schema-only); or ask the user to run it and paste a redacted result. If the user has explicitly agreed to this query, go ahead and select only the columns you need, with a small LIMIT."""


def env_values(name, cwd, assigned=None):
    """Every value this variable might have: assigned anywhere in the same command, in the environment, or in
    the project's .env files. If any of them is production, we treat it as production."""
    values = list((assigned or {}).get(name, []))
    if os.environ.get(name):
        values.append(os.environ[name])
    for f in (".env", ".env.local", ".env.development", ".env.production"):
        try:
            with open(os.path.join(cwd, f), encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = re.match(rf"\s*(?:export\s+)?{re.escape(name)}\s*=\s*['\"]?([^'\"\n]+)", line)
                    if m:
                        values.append(m.group(1))
        except OSError:
            continue
    return values


def prod_target(segment, cwd, assigned=None, context=""):
    """Why we think this segment talks to production, or None."""
    m = PROD.search(segment) or PROD.search(context)
    if m:
        return f"'{m.group(0)}' in the command"
    for var in re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", segment):
        if any(PROD.search(v) for v in env_values(var, cwd, assigned)):
            return f"${var} points at a production host"
    return None


def unquote(q):
    return q[1:-1] if len(q) >= 2 and q[0] in "\"'" and q[-1] == q[0] else q


def query_texts(segment, flag):
    """The SQL or JS the command will run, if it's visible in the command line."""
    texts = []
    if flag:
        for m in re.finditer(rf"(?:^|\s){flag}(?:\s+|=)(\"(?:[^\"\\]|\\.)*\"|'[^']*'|\S+)", segment):
            texts.append(unquote(m.group(1)))
    heredoc = re.search(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)\n\s*\1\s*$", segment, re.S)
    if heredoc:
        texts.append(heredoc.group(2))
    return texts


def only_metadata(texts):
    statements = [s for t in texts for s in re.split(r";|\n", t) if s.strip()]
    return bool(statements) and all(META_SQL.match(s) for s in statements)


def segments(command):
    """-> [(segment, reads_from_a_pipe)]. Heredocs stay attached to their command."""
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
            out.append((p, False))
            continue
        for chunk in re.split(r"\s*(?:;|&&|\|\||(?<![>&<\d])&(?![&>]))\s*", p):
            pieces = [x for x in re.split(r"\s*\|\s*", chunk) if x.strip()]
            out += [(x, i > 0) for i, x in enumerate(pieces)]
    return out


def expand(command, depth=0):
    """The command plus any shell code nested inside it, with the outer text as context."""
    found = [(command, "")]
    if depth >= 3:
        return found
    for rx in INNER:
        for m in rx.finditer(command):
            inner = unquote(m.group(1))
            found += [(c, ctx + " " + command) for c, ctx in expand(inner, depth + 1)]
    return found


def argv0(text):
    """The program a shell command line actually runs, read the way the shell reads it (quotes, NAME=value
    prefixes, wrappers and their options). None if it can't be parsed."""
    try:
        lex = shlex.shlex(text, posix=True)
        lex.whitespace_split = True
        lex.commenters = ""
        tokens = list(lex)
    except ValueError:
        return None
    i = 0
    while i < len(tokens):
        tok = tokens[i].lstrip("({!")
        if not tok or tok in SHELL_KEYWORDS or re.match(r"^[A-Za-z_]\w*=", tok):
            i += 1
            continue
        name = tok.rsplit("/", 1)[-1]
        if name not in WRAPPERS:
            return name
        takes_value, no_value = (x.split() for x in WRAPPERS[name])
        i += 1
        while i < len(tokens) and tokens[i].startswith("-"):
            opt = tokens[i]
            i += 1
            if opt == "--":
                break
            if opt in no_value:
                continue
            if opt in takes_value:
                i += 1
            elif opt[:2] in takes_value and not opt.startswith("--"):
                continue  # value attached: -uroot, -oL
            else:
                return None  # an option we don't know: can't tell what actually runs
        if name == "timeout" and i < len(tokens) and re.match(r"^\d", tokens[i]):
            i += 1
    return None


def shell_words(text):
    """Roughly what the shell will run: quotes and backslash escapes removed, so ps"q"l and p\\sql read as psql."""
    return re.sub(r"\\(.)", r"\1", re.sub(r"[\"']", "", text))


def check_segment(seg, piped, cwd, assigned, context, shell_pipe=False):
    if re.match(r"\s*#", seg):
        return None
    target = prod_target(seg, cwd, assigned, context)
    if not target:
        return None
    norm = shell_words(seg.split("<<")[0])
    first = re.match(AT + r"(\S+)", norm)
    if first and re.search(r"[$`]", first.group(1)):
        return ASK.format(target=target)  # the command itself comes from a variable: can't tell what it runs
    if DUMPS.search(norm):
        if SCHEMA_ONLY_DUMP.search(seg) and re.search(r"pg_dump|mysqldump|mariadb-dump|supabase\s+db\s+dump", seg):
            return None
        return ASK.format(target=target)
    if KUBECTL_DB.search(norm):
        return ASK.format(target=target)
    for rx, flag in CLIENTS:
        if not rx.search(norm):
            continue
        if piped or EXTRA_INPUT.search(seg.split("<<")[0]):
            return ASK.format(target=target)
        if "redis-cli" in seg:
            m = REDIS_ARGS.search(seg)
            return None if m and REDIS_META.match(m.group(1)) else ASK.format(target=target)
        texts = query_texts(seg, flag)
        return None if texts and only_metadata(texts) else ASK.format(target=target)
    # what the shell will run: quote characters dropped, contents kept, unless the command only prints or searches text
    head = seg.split("<<")[0]
    structure = re.sub(QUOTED, "", head)  # subshells, groups, substitutions outside quotes run code of their own
    if argv0(head) in HARMLESS and not shell_pipe and not re.search(r"[(){}`]|\$\(|[<>]\(", structure):
        return None  # it only prints or searches its arguments; command substitutions are checked separately
    if CLIENT_WORD.search(shell_words(head)):
        return ASK.format(target=target)
    return None


def check_command(command, cwd="."):
    """-> reason string if we should ask, else None"""
    assigned = {}
    for k, v in ASSIGN.findall(command):
        assigned.setdefault(k, []).append(unquote(v))
    for text, context in expand(command):
        segs = segments(text)
        # default-deny: if anything in a pipeline isn't a known print/search command, nothing in it is harmless
        shell_pipe = any(piped and argv0(seg.split("<<")[0]) not in HARMLESS for seg, piped in segs)
        for seg, piped in segs:
            reason = check_segment(seg, piped, cwd, assigned, context, shell_pipe)
            if reason:
                return reason
    return None


DATA_SERVER = re.compile(r"(?i)postgres|\bpg\b|pg[-_]|mysql|maria|mongo|redis|supabase|sql|\bdb\b|db[-_]|[-_]db|database|bigquery|"
                         r"snowflake|clickhouse|dynamo|firestore|elastic|neon|planetscale|warehouse")
METADATA_TOOL = re.compile(r"(?i)^(list|describe|get)_?(tables?|schemas?|collections?|databases?|columns?|indexes|table_schema|"
                           r"table_info|schema)$|^explain$")
QUERY_KEY = re.compile(r"(?i)^(sql|query|statement|command|q|code|filter|pipeline|expression)$")
QUERY_TOOL = re.compile(r"(?i)query|sql|select|find|scan|execute|export|dump")
LOOKS_LIKE_QUERY = re.compile(r"(?is)\b(select|insert|update|delete|copy|with|explain|show)\b.+|\.(find|aggregate)\s*\(|\\\w")


def strings_under(value, key=""):
    """Every string in a nested tool input, with the key it was under."""
    if isinstance(value, str):
        yield key, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from strings_under(v, str(k))
    elif isinstance(value, list):
        for v in value:
            yield from strings_under(v, key)


def check_mcp(tool_name, tool_input):
    """MCP tools on a production data store: ask unless the call clearly only reads structure."""
    m = re.match(r"mcp__(.+?)__(.+)$", tool_name or "")
    if not m:
        return None
    server, tool = m.groups()
    if not PROD.search(server):
        return None
    inputs = list(strings_under(tool_input or {}))
    queries = [v for k, v in inputs if QUERY_KEY.match(k) or LOOKS_LIKE_QUERY.search(v)]
    if not (DATA_SERVER.search(server) or QUERY_TOOL.search(tool) or queries):
        return None
    if METADATA_TOOL.match(tool) and not queries:
        return None
    if queries and only_metadata(queries):
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
            # ways around an allowlist
            "psql $DATABASE_URL -c \"select table_name from information_schema.tables union select email from users\"",
            "psql $DATABASE_URL -c \"select table_name from information_schema.tables where 1=1 union select email from users\"",
            "psql $DATABASE_URL -c \"select (select email from users limit 1) from information_schema.tables\"",
            "psql $DATABASE_URL -c \"select count(*) from users where id in (select id from users)\"",
            "psql $DATABASE_URL -c '\\copy users to stdout'",
            "psql $DATABASE_URL -c '\\d users' -c 'select * from users'",
            "psql $DATABASE_URL -c '\\d users' -f dump.sql",
            "psql $DATABASE_URL -c '\\d users' < query.sql",
            "cat query.sql | psql $DATABASE_URL -c '\\d users'",
            "mongosh 'mongodb+srv://u:p@c.ab12c.mongodb.net/shop' --eval 'db.stats()\ndb.users.find()'",
            "timeout 30 psql \"$DATABASE_URL\" -c 'select * from users'",
            "nice -n 10 psql $DATABASE_URL -c 'select * from users'",
            "bash -c \"psql $DATABASE_URL -c 'select * from users'\"",
            "eval \"psql $DATABASE_URL -c 'select * from users'\"",
            "echo $(psql $DATABASE_URL -c 'select * from users')",
            "ssh prod-bastion \"psql -h 10.0.0.5 -c 'select * from users'\"",
            "export DB=postgresql://app@db-prod.internal/app; psql $DB -c 'select * from users'",
            "DB=postgresql://app@orders.production.internal/app && psql \"$DB\" -c 'select 1; select * from orders'",
            "psql $DATABASE_URL <<'SQL'\nselect id, email from users where id = 5;\nSQL",
            "psql $DATABASE_URL -c 'explain analyze select * from users'",
            "psql $DATABASE_URL -c 'explain analyse select * from users'",
            "psql $DATABASE_URL -c 'explain select 1 \\g out.txt'",
            "psql $DATABASE_URL -c \"select pg_read_file('/etc/passwd') from information_schema.tables\"",
            "psql $DATABASE_URL -c \"select count(*) from users where email::int = 1\"",
            "psql $DATABASE_URL -c \"select count(*) from users where cast(email as int) = 1\"",
            "DB=localhost; DB=postgresql://x@db-prod.internal/app; psql $DB -c 'select * from users'",
            "watch -n 5 psql $DATABASE_URL -c 'select * from users'",
            "mywrapper psql $DATABASE_URL -c '\\d users'",
            "watch -n 5 \"psql $DATABASE_URL -c 'select * from users'\"",
            "parallel \"psql $DATABASE_URL -c 'select * from users'\"",
            "foo 'psql' $DATABASE_URL",
            "echo x | awk 'BEGIN{system(\"psql $DATABASE_URL -c select\")}'",
            "find . -name x -exec psql $DATABASE_URL -c 'select * from users' \\;",
            "ps\"q\"l $DATABASE_URL -c 'select * from users'",
            "p\\sql $DATABASE_URL -c 'select * from users'",
            "CMD=psql; $CMD $DATABASE_URL -c 'select * from users'",
            "echo 'psql $DATABASE_URL -c \"select * from users\"' | sh",
            "echo hi & psql $DATABASE_URL -c 'select * from users'",
            "(psql $DATABASE_URL -c 'select * from users')",
            "if true; then psql $DATABASE_URL -c 'select * from users'; fi",
            "echo 'psql $DATABASE_URL -c \"select * from users\"' | env sh",
            "echo 'psql $DATABASE_URL -c \"select * from users\"' | sudo -u postgres bash",
            "echo 'psql $DATABASE_URL -c \"select * from users\"' | timeout 5 sh",
            "echo 'psql $DATABASE_URL -c \"select * from users\"' | tee /dev/stderr | command bash",
            "'psql' $DATABASE_URL -c 'select * from users'",
            "\"e\"cho hi; \"ps\"ql $DATABASE_URL -c 'select * from users'",
            "cat <(psql $DATABASE_URL -c 'select * from users')",
            "echo hi > >(psql $DATABASE_URL)",
            "env -S \"psql $DATABASE_URL -c 'select * from users'\" echo hi",
            "sudo --preserve-env=PATH cat 'psql $DATABASE_URL -c \"select * from users\"'",
            "psql $DATABASE_URL -c \"select count(*) from users where \\\"x\\\" = 'a'\"",
            "psql $DATABASE_URL -c \"select count(*) from users where status = 'a union select email from users'\"",
        ]
        fine = [
            'psql "$DATABASE_URL" -c "\\d users"',
            "psql $DATABASE_URL -c 'select count(*) from users'",
            "psql $DATABASE_URL -c \"select column_name, data_type from information_schema.columns where table_name='users'\"",
            "psql $DATABASE_URL -c 'explain select * from users where id = 1'",
            "psql $DATABASE_URL -c \"select count(*) from users where status = 'paid' and amount > 100\"",
            "psql $DATABASE_URL -c \"select table_name from information_schema.tables where table_schema = 'public'\"",
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
    assert check_mcp("mcp__postgres-prod__query", {"args": {"sql": "select * from users"}}), "nested input"
    assert check_mcp("mcp__db-prod__run", {}), "no recognizable query on a production data store"
    assert check_mcp("mcp__supabase-prod__execute_sql", {"query": "select count(*) from users; select * from users"})
    assert not check_mcp("mcp__postgres-prod__query", {"sql": "select count(*) from users"})
    assert not check_mcp("mcp__postgres-prod__list_tables", {})
    assert not check_mcp("mcp__postgres-staging__query", {"sql": "select * from users"})
    assert not check_mcp("mcp__sentry-prod__get_issue", {"id": "1"}), "not a data store"
    assert check_mcp("mcp__postgres-prod__query", {"sql": "select count(*) from users", "extra": "select * from users"})
    assert check_mcp("mcp__acme-prod__run_query", {"text": "select * from users"}), "query tool on a prod server"
    assert check_mcp("mcp__acme-prod__tool", {"input": "select email from users"}), "query-looking input on a prod server"
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
