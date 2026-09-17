# Bundles live in data/<SET>/ -- see GET_DATA.md.
SET ?= Market-cloudbed-1
SUB ?= starter
PY  ?= python
# which agent module run.py loads, e.g. AGENT=agents.routed (default: run.py's)
AGENT_ARG := $(if $(AGENT),--agent $(AGENT),)

.PHONY: help validate dev score cost docker
.DEFAULT_GOAL := help

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

validate: ## run your agent on 2 dev cases and check the output shape
	VAL_AGENT=$(or $(AGENT),agents.routed) $(PY) scripts/validate_submission.py --submission $(SUB) \
	  --dataset data/$(SET) --queries data/$(SET)/dev/query_dev.csv

dev: ## run your agent over all 70 cases -> out/dev/ (N=20 for the first 20)
	cd $(SUB) && $(PY) run.py --dataset ../data/$(SET) \
	  --queries ../data/$(SET)/dev/query_dev.csv --out ../out/dev $(AGENT_ARG) $(if $(N),--limit $(N),)

score: ## score out/dev/ against the answers
	cd $(SUB) && $(PY) score.py --predictions ../out/dev/predictions.csv \
	  --queries ../data/$(SET)/dev/query_dev.csv

cost: ## dollars per case and per model for out/dev/, from its usage.jsonl
	cd $(SUB) && $(PY) cost.py ../out/dev/usage.jsonl

docker: ## build your image and run 2 dev cases exactly as we will (uses FEATHERLESS_API_KEY)
	docker build -t rca-submission ..
	rm -rf out/docker && mkdir -p out/docker
	docker run --rm --cpus 2 --memory 8g -e FEATHERLESS_API_KEY -e FEATHERLESS_BASE_URL \
	  -v "$(CURDIR)/data/$(SET)":/data:ro -v "$(CURDIR)/out/docker":/out rca-submission \
	  python run.py --dataset /data --queries /data/dev/query_dev.csv --out /out --limit 2 $(AGENT_ARG)
	@ls out/docker/evidence | wc -l | xargs echo "evidence files:"
