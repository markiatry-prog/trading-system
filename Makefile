.PHONY: test guards db-test ledger-check all

# Everything CI runs, runnable locally in one command.
all: guards test ledger-check

test:
	python -m pytest tests/

guards:
	python3 scripts/check_execution_boundary.py --selftest
	python3 scripts/check_execution_boundary.py .
	python3 scripts/check_no_legacy_dependency.py --selftest
	python3 scripts/check_no_legacy_dependency.py .

# Local mode by default. Set TRADING_MIGRATION_DSN for the real invariant.
ledger-check:
	python3 scripts/check_migration_ledger_parity.py --selftest
	python3 scripts/check_migration_ledger_parity.py

# pgTAP against an ephemeral local stack. Never against production:
# pgTAP must not be installed in a production application schema.
db-test:
	supabase db reset
	supabase test db
