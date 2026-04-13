### Coverage Mapper

### Purpose
- Build an instrumented solver binary.
- Prepare or discover the test seed set.
- Attribute covered functions to individual tests.
- Publish a merged `coverage_mapping.json.gz` that commit fuzzing can use.

### Contract Integration
Coverage mapping uses the same contract as commit fuzzing. The reusable workflow reads:
- `repository_url` and optional `tests_repository_url`
- `build_script` or `coverage_build_command`
- `coverage_binary_path` or `instrumented_binary_path`
- `tests_script` plus `seeds_dir`, or legacy `test_discovery_command`/`test_root`
- `coverage_target_job_count`
- `coverage_average_test_time_seconds`
- `coverage_mapper_command`
- `artifact_paths`

The shared brain owns checkout, validation, seed discovery, matrix generation, shard execution, and join logic.

### Minimal Contract

```yaml
solver_name: demo
repository_url: https://github.com/example/demo.git
build_script: build.sh
binary_path: build/demo
instrumented_binary_path: build/demo-cov
tests_script: tests.sh
seeds_dir: FUZZING_SEEDS
target_commands:
  - "{target_binary}"
coverage_mapper_command: python3 tools/coverage_mapper.py --build-dir {build_dir}
coverage_target_job_count: 4
coverage_average_test_time_seconds: 10.0
artifact_paths:
  - build/demo-cov
  - build/compile_commands.json
```

### Script Requirements
`build.sh --instrumented` must build the coverage binary and print the final line:

```text
BINARY_PATH=/absolute/path/to/binary
```

Compatibility aliases `--instrumentation` and `--coverage` are accepted by the bundled solver scripts.

`tests.sh` must populate `$FUZZING_SEEDS`:

```bash
#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$FUZZING_SEEDS"
find tests -name '*.smt' -o -name '*.smt2' -exec cp {} "$FUZZING_SEEDS"/ \;
```

The brain schedules only `.smt` and `.smt2` files relative to `FUZZING_SEEDS`.
The reusable workflows also resolve the production build and coverage mapping S3 prefixes from the contract so solver onboarding does not require workflow edits for the storage layout.
The resolved prefix fields are `build_artifact_s3_prefix` and `coverage_mapping_s3_prefix`.

### Commands

```bash
python3 scripts/solver_fuzzing_brain.py --contract contracts/solvers/demo.yml validate --json
python3 scripts/solver_fuzzing_brain.py --contract contracts/solvers/demo.yml checkout --json
python3 scripts/solver_fuzzing_brain.py --contract contracts/solvers/demo.yml build --mode instrumentation --json
python3 scripts/solver_fuzzing_brain.py --contract contracts/solvers/demo.yml coverage-matrix --output matrix.json
python3 scripts/solver_fuzzing_brain.py --contract contracts/solvers/demo.yml run-coverage-shard --start-index 1 --end-index 50
python3 scripts/solver_fuzzing_brain.py --contract contracts/solvers/demo.yml join-coverage --mappings-dir coverage-mappings
```

### Workflow
`.github/workflows/solver-coverage-mapper.yml` is solver-neutral. The cvc5, z3, and opensmt wrappers pass only the solver name and contract path, with legacy input names preserved for compatibility.
Those built-in solver contracts are the contract-first examples: `build_script`, `tests_script`, and `seeds_dir` are the canonical fields, while `test_discovery_command` and `test_root` remain compatibility fallbacks.

The daily check uses `.github/workflows/solver-coverage-daily-check.yml`, restores scheduled execution, counts tests through `scripts/coverage/count_tests.py --contract`, and triggers the generic mapper when S3 coverage state says the mapping is stale.
