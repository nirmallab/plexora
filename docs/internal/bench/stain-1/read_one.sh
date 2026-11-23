#!/bin/bash
# read_one.sh <question.txt> <answer file>: one blind Sonnet reading.
q="$1"; a="$2"; cd "$(dirname "$q")" || exit 1
claude -p --model "${MODEL:-claude-sonnet-5-5}" --effort high --allowedTools Read --output-format text "$(cat question.txt)" > "$a.tmp" 2>>reader.err && mv "$a.tmp" "$a"
