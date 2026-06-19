schema_version: 1

tasks:
  add_payments_endpoint:
    description: >
      Add a POST /payments endpoint to the billing service.
      Accepts amount, currency, and user_id. Returns a transaction_id.
    mutation_mode: evolve          # evolve = may edit spec_docs, tests, targets
    spec_docs:
      - example/docs/IMPLEMENTATION.md
      - example/docs/API_SCHEMA.md
    contracts:                     # stable, hash-pinned (subset of spec_docs)
      - example/docs/API_SCHEMA.md
    tests:
      - example/tests/test_payments.py
    contract_tests:                # tests that pin the contract (subset of tests)
      - example/tests/test_payments.py
    targets:
      - example/src/billing/routes.py
      - example/src/billing/models.py
    locked_files: []               # AGENTS.md is ALWAYS locked implicitly
    commit_prefix: "feat"
    max_autorepair_attempts: 3
    pr_labels: ["feature", "billing"]

  optimise_query_layer:
    description: >
      Optimise the database query layer in example/src/db/queries.py.
      Replace N+1 patterns with batch fetches. No API contract changes.
    mutation_mode: isolated        # isolated = ONLY files in targets may change
    spec_docs:
      - example/docs/IMPLEMENTATION.md
    tests:
      - example/tests/test_queries.py
    targets:
      - example/src/db/queries.py
    locked_files:
      - example/docs/IMPLEMENTATION.md
      - example/tests/test_queries.py
    commit_prefix: "perf"
    max_autorepair_attempts: 3
    pr_labels: ["performance"]
