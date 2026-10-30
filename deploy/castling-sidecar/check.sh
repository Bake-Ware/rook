#!/usr/bin/env bash
# Smoke-test the running 0.5 sidecar through its doors, as a person and as an
# agent: the worker is online, a shell.exec and a file.read go through dispatch
# and land in the journal, and the MCP door lists its tools. Prints no secret.
here=$(cd "$(dirname "$0")" && pwd)
. "$here/common.sh"
H="Authorization: Bearer $(cat "$SECRETS/owner.token")"
M="Authorization: Bearer $(cat "$SECRETS/mcp.token")"
fail=0
check() {   # check LABEL PYTHON-EXPR-ON-j JSON
    if printf '%s' "$3" | "$PY" -c "import json,sys; j=json.load(sys.stdin); sys.exit(0 if ($2) else 1)" 2>/dev/null; then
        printf 'ok    %s\n' "$1"
    else
        printf 'FAIL  %s\n      %s\n' "$1" "$(printf '%s' "$3" | head -c 400)"; fail=1
    fi
}
get() { curl -s --max-time 20 -H "$H" "$LOCAL_URL$1"; }
post() { curl -s --max-time 60 -H "$H" -d "$2" "$LOCAL_URL$1"; }

check "hub health" "j.get('ok')" "$(curl -s --max-time 5 "$LOCAL_URL/health")"
online="any(x.get('name')=='$WORKER_NAME' and x.get('online') for x in j['workers'])"
for _ in $(seq 45); do      # a fresh relay hears of the worker at its first keepalive (~20 s)
    w=$(get /workers)
    printf '%s' "$w" | "$PY" -c "import json,sys; j=json.load(sys.stdin); sys.exit(0 if ($online) else 1)" 2>/dev/null && break
    sleep 2
done
check "worker $WORKER_NAME online" "$online" "$w"
caps=$(get "/caps?worker=$WORKER_NAME")
check "worker serves shell.exec, file.read, agents-claude-code.turn" \
    "{'shell.exec','file.read','agents-claude-code.turn'} <= set(j['workers']['$WORKER_NAME']['caps']) - set(j['workers']['$WORKER_NAME'].get('unavailable') or {})" "$caps"
r=$(post /call "{\"worker\":\"$WORKER_NAME\",\"cap\":\"shell.exec\",\"args\":{\"argv\":[\"uname\",\"-n\"]},\"why\":\"Sidecar check: a shell call through the 0.5 hub.\"}")
check "shell.exec uname -n" "j.get('ok') and '$(uname -n)' in json.dumps(j['result'])" "$r"
r=$(post /call "{\"worker\":\"$WORKER_NAME\",\"cap\":\"file.read\",\"args\":{\"path\":\"$SRC/.castling-sha\"},\"why\":\"Sidecar check: read the installed commit.\"}")
check "file.read the installed commit" "j.get('ok') and '$(cut -c1-12 "$SRC/.castling-sha")' in json.dumps(j['result'])" "$r"
check "journal has the calls" "any(e.get('cap')=='shell.exec' for e in j.get('entries', j.get('journal', [])))" "$(get '/journal?limit=10')"
check "dashboard serves /" "True" "$(curl -s --max-time 5 -o /dev/null -w '{"code": %{http_code}}' "$LOCAL_URL/")"
r=$(curl -s --max-time 20 -H "$M" -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' "http://$MCP_BIND/mcp")
check "MCP door lists its tools" "'call' in [t['name'] for t in j['result']['tools']]" "$r"
exit $fail
