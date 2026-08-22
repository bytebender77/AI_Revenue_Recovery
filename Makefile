PY := python3

.PHONY: setup cohort describe gate train m4 ablation-features ablation-normalizer db-up db-down db-init run verify-chain audit test freeze clean

setup:
	$(PY) -m pip install -r requirements.txt

cohort:            ## regenerate dev + sealed test cohorts
	$(PY) -m rr.sim.cohort --out data

describe:          ## composition + latent-structure probe for the generated cohort
	$(PY) -m rr.sim.describe --data data

db-up:             ## start postgres
	docker compose up -d --wait db

db-down:
	docker compose down

db-init: db-up      ## (re)create the schema -- DROPS existing tables and the ledger chain
	$(PY) -c "from rr.db.conn import init_schema; init_schema(); print('  schema ready')"

data/dev_observed.jsonl:
	$(PY) -m rr.sim.cohort --out data

models/success_model.json: data/dev_observed.jsonl
	$(PY) -m rr.model.train --data data --out models

run: db-up data/dev_observed.jsonl   ## clean clone: docker compose up, then make run. No other steps.
	@$(PY) -c "from rr.db.conn import ensure_schema; ensure_schema()"
	$(PY) -m rr.pipeline.runner --data data --limit 500

verify-chain:
	$(PY) -m rr.db.verify

audit:             ## AUDIT=<payment_intent_id> make audit
	@psql "$${DATABASE_URL:-postgresql://rr:rr@localhost:5434/rr}" -v id="'$(AUDIT)'" -f sql/audit.sql

train: models/success_model.json   ## fit the success + organic models on dev exploration data

ablation-features:  ## feature cross v1 vs v2 on full dev
	$(PY) -m rr.model.train --feature-version v1 --suffix _v1 --data data --out models
	$(PY) -m rr.model.train --feature-version v2 --suffix _v2 --data data --out models
	$(PY) -m rr.eval.ablation_features --data data --models models

m4: train          ## full dev: every arm, calibration, per-cause, NO_ACTION breakdown
	$(PY) -m rr.eval.m4_report --data data --models models

gate:              ## run B0-B3 on the dev cohort and print the M1 gate verdict
	$(PY) -m rr.eval.gate --data data

test:
	$(PY) -m pytest -q

freeze:            ## re-pin the response model hash (deliberate act; note it in the changelog)
	$(PY) -c "import hashlib,pathlib; p=pathlib.Path('rr/sim/response_model.py'); \
	pathlib.Path('rr/sim/response_model.sha256').write_text(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n')"
	@cat rr/sim/response_model.sha256

clean:
	rm -f data/*.jsonl data/manifest.json data/test_cohort.SEALED.json
