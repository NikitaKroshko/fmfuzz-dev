### Commit Fuzzer

### Architecture
- Shared logic lives in `scripts/solver_fuzzing_brain.py`.
- Solver metadata lives in one YAML contract under `contracts/solvers/`.
- Solver wrappers call reusable workflows and pass `solver_name` plus `contract_path`.
- Solver-specific Python is only needed for domain-specific coverage parsing or changed-function analysis that the generic brain delegates to through contract commands.

### New Solver Contract
The preferred user-facing contract is:

```yaml
solver_name: demo
repository_url: https://github.com/example/demo.git
repository_path: demo

build_script: build.sh
production_binary_path: build/demo
coverage_binary_path: build/demo-cov

tests_script: tests.sh
seeds_dir: FUZZING_SEEDS

artifact_s3_prefix: solvers/demo/builds/v2

target_commands:
  - "{target_binary}"

coverage_mapper_command: python3 tools/coverage_mapper.py --build-dir {build_dir}
commit_prepare_command: python3 tools/prepare_commit.py

environment_requirements:
  packages:
    - cmake
    - python3
  env:
    - GCOV_PREFIX
artifact_paths:
  - build/demo
  - build/compile_commands.json
```

Compatibility fields still load:
- `build_command` and `coverage_build_command`
- `test_discovery_command` and `test_root`
- `tests_repository_url` and `tests_repository_path`
- `production_binary_path`, `coverage_binary_path`, or aliases `binary_path` and `instrumented_binary_path`

If `build_script` is present and `coverage_build_command` is omitted, the brain runs `build_script --instrumented` for coverage builds.

Reference solver fields are optional. Use `reference_setup_command` plus `reference_binary_path` when the reference can be downloaded or prepared directly. Use `reference_contract_path` when the reference is another solver contract; unprefixed paths are resolved from the fmfuzz repository root, so `contracts/solvers/z3.yml` works from any solver contract. Use `contract:z3.yml` only for a path relative to the current contract file.

### Script Contracts
`build.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

mode=production
case "${1:-}" in
  "" ) ;;
  --instrumented|--instrumentation|--coverage ) mode=coverage ;;
  * ) echo "unknown option: $1" >&2; exit 2 ;;
esac

# Build the solver here.
binary="$PWD/build/demo"
if [ "$mode" = coverage ]; then
  binary="$PWD/build/demo-cov"
fi

printf 'BINARY_PATH=%s\n' "$(cd "$(dirname "$binary")" && pwd)/$(basename "$binary")"
```

`tests.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

seed_dir="${FUZZING_SEEDS:-$PWD/FUZZING_SEEDS}"
mkdir -p "$seed_dir"

# Copy or link stable SMT-LIB seeds. The directory may contain subdirectories.
cp tests/regress/*.smt2 "$seed_dir"/
```

`FUZZING_SEEDS` semantics:
- The tests script must leave a directory or stable symlink at `$FUZZING_SEEDS`.
- Only `.smt` and `.smt2` files are scheduled by the generic seed path.
- Test names are relative to `FUZZING_SEEDS`.
- Legacy `test_discovery_command` and `test_root` remain compatibility fallbacks.

### Validation
Run this before wiring a new workflow:

```bash
python3 scripts/solver_fuzzing_brain.py --contract contracts/solvers/demo.yml validate --json
```

For fake-workspace script checks:

```bash
python3 scripts/solver_fuzzing_brain.py \
  --contract contracts/solvers/demo.yml \
  validate \
  --run-script-checks \
  --script-check-workspace /tmp/fmfuzz-demo-check \
  --json
```

Validation errors use `BrainError` or `ContractError` formatting with solver, step, command, hint, repository URL, and issue tracker when available.

### Production Flow
Scheduled commit fuzzing uses `.github/workflows/solver-commit-fuzzer.yml`:
1. `scripts/scheduling/fuzzer.py <solver> select --json` selects `(commit_to_fuzz, latest_build)` from S3 state.
2. The workflow checks out the repository at `latest_build`.
3. It resolves the production build and coverage mapping S3 prefixes from the contract and downloads the artifacts for `latest_build`.
4. It calls `prepare-commit` through the contract to create a commit-targeted matrix for `commit_to_fuzz`.
5. It runs `run-harness` over the matrix.
6. It calls `scripts/scheduling/fuzzer.py <solver> increment <commit>` after attempted fuzzing.

The reusable workflow consumes the brain-resolved `build_artifact_s3_prefix` and `coverage_mapping_s3_prefix` fields, so new solvers only need to keep their contract layout aligned.

Manual smoke fuzzing is still available through the wrapper input `smoke_test_limit`. The production path does not use `LOCAL_TEST_LIMIT`.

### Existing Solvers
`cvc5`, `z3`, and `opensmt` are the reference contract-first examples. They now use `build_script`, `tests_script`, and `seeds_dir`, with the reusable workflows resolving the S3 layout from the contract instead of solver-specific string assembly.
