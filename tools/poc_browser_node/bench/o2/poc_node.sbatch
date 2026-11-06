#!/bin/bash
#SBATCH --job-name=plx-poc-node
#SBATCH --partition=short
#SBATCH --time=3:00:00
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --output=%x-%j.out
#
# The browser-node P5R record's data node on O2. Runs from a private folder
# (mode 700); the token is read from a mode-600 file into the environment and
# never appears on a command line; the node binds the compute node's loopback
# and allows exactly the test page's origin.
set -euo pipefail
umask 077
HERE="$HOME/plexora-poc"
cd "$HERE"
export PLEXORA_NODE_TOKEN="$(cat "$HERE/node.token")"
export PLEXORA_DATA_PATH="/n/scratch/users/a/ajn16/plexora-poc-root"
mkdir -p "$PLEXORA_DATA_PATH"
IMAGE=/n/scratch/users/a/ajn16/LAMP_NLE012/registration/NLU337.ome.tif
TABLE=$(ls /n/scratch/users/a/ajn16/LAMP_NLE012/quantification/*NLU337*cell*.csv 2>/dev/null | head -1 || true)
SERVE=(--serve "image:img=$IMAGE")
[ -n "$TABLE" ] && SERVE+=(--serve "table:cells=$TABLE")
{
    echo "host=$(hostname -f)"
    echo "port=8642"
    echo "job=${SLURM_JOB_ID:-manual}"
    echo "table=${TABLE:-none}"
} > "$HERE/node.endpoint"
source /home/ajn16/miniconda3/etc/profile.d/conda.sh
conda activate plexora
exec plexora node serve --host 127.0.0.1 --port 8642 --node-id poc-o2 \
    "${SERVE[@]}" --allow-origin http://127.0.0.1:8650
