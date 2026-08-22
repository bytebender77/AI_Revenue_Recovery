PY := python3

.PHONY: setup cohort describe test freeze clean

setup:
	$(PY) -m pip install -r requirements.txt

cohort:            ## regenerate dev + sealed test cohorts
	$(PY) -m rr.sim.cohort --out data

describe:          ## composition + latent-structure probe for the generated cohort
	$(PY) -m rr.sim.describe --data data

test:
	$(PY) -m pytest -q

freeze:            ## re-pin the response model hash (deliberate act; note it in the changelog)
	$(PY) -c "import hashlib,pathlib; p=pathlib.Path('rr/sim/response_model.py'); \
	pathlib.Path('rr/sim/response_model.sha256').write_text(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n')"
	@cat rr/sim/response_model.sha256

clean:
	rm -f data/*.jsonl data/manifest.json data/test_cohort.SEALED.json
