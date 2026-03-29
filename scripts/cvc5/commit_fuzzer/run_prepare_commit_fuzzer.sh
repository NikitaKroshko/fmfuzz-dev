#!/bin/bash

# Script to run commit coverage analysis for CVC5 commits
# Downloads coverage mapping artifact, gunzips it, and analyzes last N commits

set -e

# Default values
COMMITS_TO_ANALYZE=3
PYTHON_SCRIPT=""
COVERAGE_FILE="coverage_mapping.json"
COMPILE_COMMANDS=""
OUTPUT_MATRIX=""
TESTS_PER_JOB=1
MAX_JOBS=""
SKIP_COVERAGE_ENFORCEMENT=${SKIP_COVERAGE_ENFORCEMENT:-false}
MIN_OVERALL_COVERAGE=${MIN_OVERALL_COVERAGE:-80}

# Function to show usage
show_usage() {
    cat << EOF
Usage: $0 [OPTIONS] COMMITS_TO_ANALYZE

Run commit coverage analysis for CVC5 commits.

ARGUMENTS:
    COMMITS_TO_ANALYZE    Number of commits to analyze (default: 3)

OPTIONS:
    -p, --python-script PATH     Path to prepare_commit_fuzzer.py (default: ./commit_fuzzer/prepare_commit_fuzzer.py)
    -c, --coverage-file PATH     Path to coverage mapping JSON (default: coverage_mapping.json)
    -d, --compile-commands PATH  Path to compile_commands.json or directory
    -o, --output-matrix PATH     Output matrix to JSON file
    -t, --tests-per-job NUM      Number of tests per job (default: 1)
    -j, --max-jobs NUM           Maximum number of jobs to create
    -h, --help                   Show this help message

ENVIRONMENT VARIABLES:
    SKIP_COVERAGE_ENFORCEMENT    Set to 'true' to skip coverage enforcement (default: false)
    MIN_OVERALL_COVERAGE         Minimum coverage percentage required (default: 80)

EXAMPLES:
    # Analyze 3 commits with default settings
    $0 3

    # Analyze 1 commit with 4 tests per job, max 20 jobs
    $0 1 --tests-per-job 4 --max-jobs 20 --output-matrix matrix.json

    # Analyze 5 commits with custom coverage file
    $0 5 --coverage-file custom_coverage.json --compile-commands build/
EOF
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -p|--python-script)
            PYTHON_SCRIPT="$2"
            shift 2
            ;;
        -c|--coverage-file)
            COVERAGE_FILE="$2"
            shift 2
            ;;
        -d|--compile-commands)
            COMPILE_COMMANDS="$2"
            shift 2
            ;;
        -o|--output-matrix)
            OUTPUT_MATRIX="$2"
            shift 2
            ;;
        -t|--tests-per-job)
            TESTS_PER_JOB="$2"
            shift 2
            ;;
        -j|--max-jobs)
            MAX_JOBS="$2"
            shift 2
            ;;
        -h|--help)
            show_usage
            exit 0
            ;;
        -*)
            echo "Error: Unknown option $1" >&2
            show_usage
            exit 1
            ;;
        *)
            if [[ -z "$COMMITS_TO_ANALYZE" || "$COMMITS_TO_ANALYZE" == "3" ]]; then
                COMMITS_TO_ANALYZE="$1"
            else
                echo "Error: Multiple commit counts specified" >&2
                show_usage
                exit 1
            fi
            shift
            ;;
    esac
done

# Set default Python script if not provided
if [[ -z "$PYTHON_SCRIPT" ]]; then
    PYTHON_SCRIPT="$(dirname "$0")/commit_fuzzer/prepare_commit_fuzzer.py"
fi

# Validate arguments
if ! [[ "$COMMITS_TO_ANALYZE" =~ ^[0-9]+$ ]] || [[ "$COMMITS_TO_ANALYZE" -lt 1 ]]; then
    echo "Error: COMMITS_TO_ANALYZE must be a positive integer" >&2
    exit 1
fi

if ! [[ "$TESTS_PER_JOB" =~ ^[0-9]+$ ]] || [[ "$TESTS_PER_JOB" -lt 1 ]]; then
    echo "Error: TESTS_PER_JOB must be a positive integer" >&2
    exit 1
fi

if [[ -n "$MAX_JOBS" ]] && (! [[ "$MAX_JOBS" =~ ^[0-9]+$ ]] || [[ "$MAX_JOBS" -lt 1 ]]); then
    echo "Error: MAX_JOBS must be a positive integer" >&2
    exit 1
fi

echo "=========================================="
echo "CVC5 Commit Coverage Analysis"
echo "=========================================="

# Check if we're in a git repository
if ! git rev-parse --git-dir > /dev/null 2>&1; then
    echo "Error: Not in a git repository"
    exit 1
fi

# Check if we have the coverage mapping file
if [ ! -f "$COVERAGE_FILE" ]; then
    echo "Coverage mapping file not found: $COVERAGE_FILE"
    echo "Please ensure the coverage mapping artifact has been downloaded and extracted"
    echo "You can download it from the GitHub Actions artifacts or run the coverage analysis workflow first"
    exit 1
fi

# Auto-detect compile_commands.json in build directory if not provided
if [ -z "$COMPILE_COMMANDS" ]; then
    if [ -f "build/compile_commands.json" ]; then
        COMPILE_COMMANDS="build"
    elif [ -d "build" ]; then
        COMPILE_COMMANDS="build"
    fi
fi

# Get commits that changed files in src/ folder
echo "Getting commits that changed files in src/ folder..."

# Use the provided override if set, otherwise collect the requested number of recent commits.
if [ -n "$CVC5_COMMIT_HASH" ]; then
    echo "Using provided commit hash: $CVC5_COMMIT_HASH"
    COMMITS=("$CVC5_COMMIT_HASH")
else
    COMMITS=()
    while IFS= read -r commit; do
        COMMITS+=("$commit")
    done < <(git log --format=%H -n "$COMMITS_TO_ANALYZE")

    if [ ${#COMMITS[@]} -eq 0 ]; then
        echo "Error: No commits found in git history" >&2
        exit 1
    fi

    echo "Using last ${#COMMITS[@]} commit(s) from git history"
fi

COMMITS_TO_ANALYZE=${#COMMITS[@]}

echo "Analyzing last $COMMITS_TO_ANALYZE commits"
echo "Skip coverage enforcement: $SKIP_COVERAGE_ENFORCEMENT (threshold=${MIN_OVERALL_COVERAGE}%)"
echo ""

# Alternative: Dynamic commit discovery (commented out for now)
# COMMITS=()
# # Scan window: 5x requested commits to ensure enough src/ changes
# SCAN_LIMIT=$((COMMITS_TO_ANALYZE * 5))
# while IFS= read -r commit; do
#     if git show --name-only "$commit" 2>/dev/null | grep -q "^src/"; then
#         COMMITS+=("$commit")
#         if [ ${#COMMITS[@]} -ge $COMMITS_TO_ANALYZE ]; then
#             break
#         fi
#     fi
# done < <(git log --format="%H" -n $SCAN_LIMIT)

# if [ ${#COMMITS[@]} -eq 0 ]; then
#     echo "No commits found that changed src/ files"
#     exit 1
# fi

echo "Found commits that changed src/ files:"
echo "${COMMITS[@]}" | tr ' ' '\n' | nl -w1 -s'. '
echo ""

# Analyze each commit
COMMIT_COUNT=0
# Overall totals
TOTAL_FUNCS=0
TOTAL_WITH=0
TOTAL_WITHOUT=0
COMMITS_PROCESSED=0
for commit in "${COMMITS[@]}"; do
    COMMIT_COUNT=$((COMMIT_COUNT + 1))
    echo "=========================================="
    echo "ANALYZING COMMIT $COMMIT_COUNT/$COMMITS_TO_ANALYZE"
    echo "=========================================="
    
    COMMIT_MSG=$(git log --format="%s" -n 1 $commit)
    COMMIT_AUTHOR=$(git log --format="%an" -n 1 $commit)
    COMMIT_DATE=$(git log --format="%ad" -n 1 $commit)
    
    echo "Commit: $commit"
    echo "Message: $COMMIT_MSG"
    echo "Author: $COMMIT_AUTHOR"
    echo "Date: $COMMIT_DATE"
    echo ""
    
    # Run the coverage analysis (capture output for aggregation)
    TMP_OUT=$(mktemp)
    
    # Build the command with all arguments
    PYTHON_ARGS=()
    PYTHON_ARGS+=("$commit")
    PYTHON_ARGS+=("--coverage-json" "$COVERAGE_FILE")
    
    if [ -n "$COMPILE_COMMANDS" ]; then
        PYTHON_ARGS+=("--compile-commands" "$COMPILE_COMMANDS")
    fi
    
    if [ -n "$OUTPUT_MATRIX" ]; then
        PYTHON_ARGS+=("--output-matrix" "$OUTPUT_MATRIX")
    fi
    
    # Add matrix grouping arguments
    if [ "$TESTS_PER_JOB" != "1" ]; then
        PYTHON_ARGS+=("--tests-per-job" "$TESTS_PER_JOB")
    fi
    
    if [ -n "$MAX_JOBS" ]; then
        PYTHON_ARGS+=("--max-jobs" "$MAX_JOBS")
    fi
    
    echo "Running: python3 $PYTHON_SCRIPT ${PYTHON_ARGS[*]}"
    python3 "$PYTHON_SCRIPT" "${PYTHON_ARGS[@]}" | tee "$TMP_OUT"
    COMMITS_PROCESSED=$((COMMITS_PROCESSED + 1))
    # Parse summary line if present
    LINE=$(grep -E "Changed functions: [0-9]+; with coverage: [0-9]+; without: [0-9]+;" "$TMP_OUT" | tail -n 1 || true)
    if [ -n "$LINE" ]; then
        CF=$(echo "$LINE" | sed -n 's/.*Changed functions: \([0-9][0-9]*\);.*/\1/p')
        WC=$(echo "$LINE" | sed -n 's/.*with coverage: \([0-9][0-9]*\);.*/\1/p')
        WO=$(echo "$LINE" | sed -n 's/.*without: \([0-9][0-9]*\);.*/\1/p')
        TOTAL_FUNCS=$((TOTAL_FUNCS + CF))
        TOTAL_WITH=$((TOTAL_WITH + WC))
        TOTAL_WITHOUT=$((TOTAL_WITHOUT + WO))
    fi
    rm -f "$TMP_OUT"
    
    echo ""
    echo "----------------------------------------"
    echo ""
done

echo "=========================================="
echo "Analysis complete!"
echo "=========================================="

# Overall statistics
if [ "$TOTAL_FUNCS" -gt 0 ]; then
  COV_PCT=$(awk "BEGIN{printf \"%.1f\", 100*$TOTAL_WITH/$TOTAL_FUNCS}")
else
  COV_PCT=0.0
fi
echo "OVERALL SUMMARY: commits=${COMMITS_PROCESSED}; total_functions=${TOTAL_FUNCS}; with_coverage=${TOTAL_WITH}; without_coverage=${TOTAL_WITHOUT}; overall_coverage=${COV_PCT}%"

# Optionally enforce minimum coverage (default: enforce)
if [ "$SKIP_COVERAGE_ENFORCEMENT" != "true" ]; then
  # Convert to integer comparison by rounding down
  COV_INT=${COV_PCT%.*}
  if [ "$COV_INT" -lt "$MIN_OVERALL_COVERAGE" ]; then
    echo "Minimum overall coverage (${MIN_OVERALL_COVERAGE}%) not met: ${COV_PCT}%"
    exit 2
  fi
fi
