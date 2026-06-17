schema_version: 1

tasks:
  add_payments_endpoint:
    description: >
      Add a POST /payments endpoint to the billing service.
      Accepts amount, currency, and user_id. Returns a transaction_id.
    mutation_mode: evolve          # evolve = may edit spec_docs, tests, targets
    spec_docs:
      - docs/IMPLEMENTATION.md
      - docs/API_SCHEMA.md
    contracts:                     # stable, hash-pinned (subset of spec_docs)
      - docs/API_SCHEMA.md
    tests:
      - tests/test_payments.py
    contract_tests:                # tests that pin the contract (subset of tests)
      - tests/test_payments.py
    targets:
      - src/billing/routes.py
      - src/billing/models.py
    locked_files: []               # AGENTS.md is ALWAYS locked implicitly
    commit_prefix: "feat"
    max_autorepair_attempts: 3
    pr_labels: ["feature", "billing"]

  optimise_query_layer:
    description: >
      Optimise the database query layer in src/db/queries.py.
      Replace N+1 patterns with batch fetches. No API contract changes.
    mutation_mode: isolated        # isolated = ONLY files in targets may change
    spec_docs:
      - docs/IMPLEMENTATION.md
    tests:
      - tests/test_queries.py
    targets:
      - src/db/queries.py
    locked_files:
      - docs/IMPLEMENTATION.md
      - tests/test_queries.py
    commit_prefix: "perf"
    max_autorepair_attempts: 3
    pr_labels: ["performance"]
