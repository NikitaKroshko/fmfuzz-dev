### Coverage Mapper

### Purpose
- Build an instrumented solver binary.
- Discover the relevant test set.
- Attribute covered functions to individual tests.

### Contract Integration
- Coverage builds now use the same solver contract as the commit fuzzer.
- The contract provides:
  - repository checkout information
  - the coverage build command
  - the coverage binary path
  - test discovery information
  - coverage matrix sizing metadata
  - the contract-declared coverage mapper command for the solver-local parser/runner
- The shared brain owns coverage matrix generation, shard execution, and shard joining.
- Solver-specific coverage code is limited to the mapper/parser that knows how to execute one test slice and interpret solver-local coverage output.

### Inputs
- Instrumented solver build from `build.sh --instrumentation`
- Test list from `test.sh --discover` or contract `test_root`

### Output
- JSON map: `src/path:demangled_signature:start_line` -> `[test_name, ...]`

### Algorithm
1. Discover tests for the selected solver workspace.
2. Reset coverage counters.
3. Run each test in isolation or in a bounded shard.
4. Export per-test coverage.
5. Normalize function identifiers.
6. Merge shard outputs into `coverage_mapping.json(.gz)`.

### Artifacts
- Sharded coverage mapping files
- Final merged `coverage_mapping.json(.gz)`
- Build logs and coverage byproducts from contract-declared artifact paths

### Guidance
- Keep only parsing or solver-native test execution differences outside the shared brain.
- Do not hardcode checkout URLs, build commands, shard scripts, or join scripts in workflows when the contract already defines the route.
