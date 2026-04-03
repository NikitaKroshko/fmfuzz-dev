### Commit Fuzzer

### Architecture
- The shared logic lives in `scripts/solver_fuzzing_brain.py`.
- Solver metadata lives in one YAML contract under `contracts/solvers/`.
- Solver repositories provide thin entrypoints:
  - `build.sh`
  - `test.sh`
  - a thin workflow wrapper
- Solver-specific Python routes should not be added to the shared brain.

### Contract
- Source of truth: `contracts/solvers/<solver>.yml`
- Required fields:
  - `solver_name`
  - `repository_url`
  - `repository_path`
  - `build_command`
  - `coverage_build_command`
  - `production_binary_path`
  - `coverage_binary_path`
  - one of `test_discovery_command` or `test_root`
  - `target_commands`
- Optional fields:
  - `issues_url`
  - `tests_repository_url`
  - `tests_repository_path`
  - `oracle_command`
  - `environment_requirements`
  - `artifact_paths`
  - `artifact_s3_bucket`
  - `artifact_s3_prefix`

### Script Contracts
- `build.sh`
  - no arguments: production build
  - `--instrumentation`: coverage or instrumented build
  - `--coverage`: compatibility alias
  - final stdout line must be `BINARY_PATH=/abs/path/to/binary`
- `test.sh`
  - `--discover` prints newline-separated test paths or stable identifiers
  - output must be machine-readable on stdout
  - logs belong on stderr

### Brain Responsibilities
1. Load and validate the YAML contract.
2. Resolve same-repo or split-repo workspaces.
3. Check out solver and optional test repositories.
4. Run production or coverage builds through the contract.
5. Parse `BINARY_PATH=...` and verify the binary exists and is executable.
6. Discover tests through `test_discovery_command` or `test_root`.
7. Run the shared commit harness with contract-provided target commands.
8. Run the oracle if the contract defines one.
9. Collect local artifacts and optionally upload them to S3.

### Onboarding A New Solver
1. Add `contracts/solvers/<solver>.yml`.
2. Add or adapt `scripts/<solver>/build.sh`.
3. Add or adapt `scripts/<solver>/test.sh`.
4. Add a thin workflow wrapper that points to the contract.
5. If the solver needs commit-selection or coverage-specific helpers, keep them outside the shared brain.

### Errors
- Contract failures are explicit and fail fast.
- Error messages include:
  - solver name
  - repository URL
  - step name
  - exact command
  - exit code
  - artifact or log location
  - issue tracker URL when available
- GitHub issue URLs are derived automatically when `issues_url` is omitted.

### Artifacts
- Local artifact collection always runs.
- S3 upload is optional and contract-driven.
- Upload failures do not hide the underlying local result.
- Hidden sentinel files inside worker bug folders are ignored and are not treated as bug artifacts.

### Current Baseline
- `cvc5` is the reference contract and parity target.
- `z3` and `opensmt` use the same shared brain with solver-specific metadata and thin wrappers only.
