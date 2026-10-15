# AutoHeal developer workflow. Run `make help` for the list.
# On Windows, run from Git Bash or WSL (GNU make required).

SHELL := bash
NS      ?= autoheal
REL     ?= autoheal
TAG     ?= local
CLUSTER ?= minikube          # minikube | kind
SERVICES := gateway tasks control
PY      ?= python

.DEFAULT_GOAL := help
.PHONY: help install test lint fmt dashboard build compose-up compose-down cluster-up cluster-down \
        images-load monitoring-up deploy undeploy status port-forward demo loadtest e2e clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- develop
install: ## Install dev/test dependencies into the active virtualenv
	$(PY) -m pip install -r requirements-dev.txt

test: ## Run the unit/integration test suite with coverage
	$(PY) -m pytest --cov --cov-report=term-missing

lint: ## Lint Python, the Helm chart and rendered manifests
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .
	helm lint helm/autoheal
	helm template $(REL) helm/autoheal -n $(NS) | kubeconform -strict -summary -ignore-missing-schemas -

fmt: ## Auto-format and auto-fix lint issues
	$(PY) -m ruff format .
	$(PY) -m ruff check . --fix

dashboard: ## Regenerate the Grafana dashboard JSON
	$(PY) scripts/build_dashboard.py

# ---------------------------------------------------------------- containers
build: ## Build all service images (autoheal-<svc>:$(TAG))
	@for s in $(SERVICES); do \
	  docker build -f services/$$s/Dockerfile -t autoheal-$$s:$(TAG) --build-arg VERSION=$(TAG) . || exit 1; \
	done

compose-up: ## Run the full stack locally with Docker Compose
	docker compose up --build -d
	@echo "gateway http://localhost:8080/docs | control http://localhost:8090 | grafana http://localhost:3000 | prometheus http://localhost:9090"

compose-down: ## Stop the Compose stack and remove volumes
	docker compose down -v

# ---------------------------------------------------------------- kubernetes
cluster-up: ## Create a local cluster with metrics-server + a policy-enforcing CNI
ifeq ($(strip $(CLUSTER)),kind)
	kind create cluster --name autoheal --config deploy/k8s/kind-config.yaml
	kubectl apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.29.3/manifests/calico.yaml
	kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml
	kubectl -n kube-system patch deploy metrics-server --type=json \
	  -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
else
	minikube start -p autoheal --cpus=4 --memory=6g --cni=calico
	minikube -p autoheal addons enable metrics-server
endif

cluster-down: ## Delete the local cluster
ifeq ($(strip $(CLUSTER)),kind)
	kind delete cluster --name autoheal
else
	minikube delete -p autoheal
endif

images-load: build ## Build images and load them into the local cluster
	@for s in $(SERVICES); do \
	  if [ "$(strip $(CLUSTER))" = "kind" ]; then kind load docker-image autoheal-$$s:$(TAG) --name autoheal; \
	  else minikube -p autoheal image load autoheal-$$s:$(TAG); fi; \
	done

monitoring-up: ## Install kube-prometheus-stack (Prometheus, Grafana, Alertmanager, kube-state-metrics)
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
	helm repo update prometheus-community
	helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
	  -n monitoring --create-namespace -f deploy/k8s/kube-prometheus-stack-values.yaml --wait --timeout 10m

deploy: ## Install/upgrade AutoHeal (add VALUES=helm/autoheal/values-local.yaml when monitoring is installed)
	kubectl apply -f deploy/k8s/namespace.yaml
	helm upgrade --install $(REL) helm/autoheal -n $(NS) --set image.tag=$(TAG) \
	  $(if $(VALUES),-f $(VALUES)) --wait --timeout 5m

undeploy: ## Remove the AutoHeal release
	helm uninstall $(REL) -n $(NS)

status: ## Pods, HPAs and recent events
	kubectl -n $(NS) get deploy,pods,hpa,pdb -o wide
	kubectl -n $(NS) get events --sort-by=.lastTimestamp | tail -20

port-forward: ## Gateway on :8080, control dashboard on :8090, Grafana on :3000
	@echo "Chaos token: $$(kubectl -n $(NS) get secret $(REL)-secrets -o jsonpath='{.data.chaos-token}' | base64 -d)"
	kubectl -n $(NS) port-forward svc/$(REL)-gateway 8080:80 & \
	kubectl -n $(NS) port-forward svc/$(REL)-control 8090:80 & \
	(kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80 2>/dev/null || true) & \
	wait

demo: ## Run every self-healing scenario and print recovery times (needs port-forward)
	CHAOS_TOKEN=$$(kubectl -n $(NS) get secret $(REL)-secrets -o jsonpath='{.data.chaos-token}' | base64 -d) \
	  $(PY) scripts/chaos_demo.py all

loadtest: ## Step load against the gateway to trigger autoscaling (needs port-forward)
	mkdir -p loadtest/reports
	AUTOHEAL_STEP_LOAD=1 locust -f loadtest/locustfile.py --host http://localhost:8080 --headless \
	  --html loadtest/reports/step-load.html

e2e: ## End-to-end test against the current cluster
	./scripts/e2e.sh

clean: ## Remove caches and reports
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov loadtest/reports
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
