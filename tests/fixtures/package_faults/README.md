# Package fault fixtures

The executable fault corpus is defined in `benchmarks/fault_localization.jsonl`. Each row copies
one licensed public benchmark Skill into an isolated temporary workspace and applies explicit,
auditable file operations. This avoids checking in 30 redundant package copies while preserving
the exact mutated source, expected diagnostic family, and ground-truth package path for every case.
