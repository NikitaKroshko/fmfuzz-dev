#!/bin/bash
# Simple coverage analysis script for a test range

set -e

START_INDEX=$1
END_INDEX=$2

echo "Running coverage analysis for tests ${START_INDEX}-${END_INDEX}"

cd opensmt/build

python3 ../../scripts/opensmt/coverage/coverage_mapper.py \
    --build-dir . \
    --opensmt-dir ../../opensmt \
    --start-index "${START_INDEX}" \
    --end-index "${END_INDEX}" \
    --output "coverage_mapping_${START_INDEX}_${END_INDEX}.json" || true

echo "Coverage analysis completed"
exit 0
