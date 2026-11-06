#!/usr/bin/env bash
# P5R's O2 record, from WSL: ONE ssh connection to the node's compute host
# (through the login node, per ~/.ssh/config's compute-* entry), because on O2
# every new session is a login (and can be a Duo request). It carries the
# tunnel 127.0.0.1:8765 -> the node's 127.0.0.1:8642, hands the token to a
# private file here (never printed), and cancels the job when its stdin closes.
#
#   sbatch poc_node.sbatch            (on O2, from ~/plexora-poc, mode 700)
#   o2_session.sh start HOST JOB      (HOST from ~/plexora-poc/node.endpoint)
#   POC_NODE_TOKEN=$(cat ~/.plx-o2-node.token) node bench/run.mjs --poc P5R ...
#   o2_session.sh stop
set -u
umask 077
CTL="$HOME/.plx-o2-ctl"
case "${1:-}" in
start)
    HOST="$2"; JOB="$3"
    rm -f "$CTL"; mkfifo "$CTL"
    # Holds the fifo open: while it lives, the remote `cat` waits.
    setsid nohup sleep 14400 > "$CTL" 2>/dev/null &
    echo $! > "$HOME/.plx-o2-holder.pid"
    setsid nohup ssh -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
        -L 127.0.0.1:8765:127.0.0.1:8642 "ajn16@$HOST" \
        "cat ~/plexora-poc/node.token; echo; cat > /dev/null; scancel $JOB; echo cancelled $JOB" \
        < "$CTL" > "$HOME/.plx-o2-session.out" 2> "$HOME/.plx-o2-session.err" &
    echo $! > "$HOME/.plx-o2-ssh.pid"
    for i in $(seq 1 60); do
        [ -s "$HOME/.plx-o2-session.out" ] && break
        kill -0 "$(cat "$HOME/.plx-o2-ssh.pid")" 2>/dev/null || break
        sleep 2
    done
    if [ -s "$HOME/.plx-o2-session.out" ]; then
        head -1 "$HOME/.plx-o2-session.out" | tr -d "\n" > "$HOME/.plx-o2-node.token"
        echo "connected: token saved ($(wc -c < "$HOME/.plx-o2-node.token") chars), tunnel on 127.0.0.1:8765"
    else
        echo "not connected:"; grep -v -i "token" "$HOME/.plx-o2-session.err" | tail -5
    fi
    ;;
stop)
    kill "$(cat "$HOME/.plx-o2-holder.pid")" 2>/dev/null
    # EOF reaches the remote `cat`, which lets scancel run: wait for the
    # connection to end on its own before cutting it.
    for i in $(seq 1 30); do
        kill -0 "$(cat "$HOME/.plx-o2-ssh.pid")" 2>/dev/null || break
        sleep 1
    done
    tail -1 "$HOME/.plx-o2-session.out" | grep -v -E "^[0-9a-f]{32,}$"
    kill "$(cat "$HOME/.plx-o2-ssh.pid")" 2>/dev/null
    rm -f "$CTL" "$HOME"/.plx-o2-*
    ;;
esac
