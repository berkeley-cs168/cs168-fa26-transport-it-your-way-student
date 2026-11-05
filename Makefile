SHELL := /bin/bash
PYTHON ?= python3

SCENARIO ?= leo
WORKLOAD ?= basic_workload
PROTOCOL ?= protocol.py
# A workload can be a card name or a path to a card; either way the run is
# named after it, so WORKLOAD=workload.yaml lands in runs/leo-workload.
OUT ?= runs/$(SCENARIO)-$(basename $(notdir $(WORKLOAD)))
# PCAP=1 writes a pcap per network device, to open in Wireshark
PCAP ?=
# VIS=1 writes vis.html, a view of the trial to open in a browser
VIS ?=
# SEED=n runs the card with a different seed
SEED ?=

RUN_FLAGS = --scenario $(SCENARIO) --workload $(WORKLOAD) --protocol $(PROTOCOL) \
	--out $(OUT) $(if $(PCAP),--pcap) $(if $(VIS),--vis) $(if $(SEED),--seed $(SEED))

.PHONY: help setup doctor run run-local test clean-runs

help:
	@echo "make setup                       build ns-3 and the simulator, once (a few minutes)"
	@echo "make doctor                      check this computer can run trials"
	@echo "make run SCENARIO=leo            one trial of protocol.py in ns-3"
	@echo "       WORKLOAD=workload.yaml    your own workload instead of basic_workload"
	@echo "       PROTOCOL=example/baseline_stopwait    a different protocol"
	@echo "       PCAP=1 for a pcap per device (Wireshark), VIS=1 for vis.html, SEED=2"
	@echo "make run-local ...               same trial over real sockets on 127.0.0.1, no shaping"
	@echo "make test                        check this checkout works"
	@echo "make clean-runs                  delete runs/"
	@echo ""
	@echo "current: SCENARIO=$(SCENARIO) WORKLOAD=$(WORKLOAD) PROTOCOL=$(PROTOCOL)"

setup:
	PYTHON=$(PYTHON) tools/setup-ns3.sh

doctor:
	@PYTHON=$(PYTHON) tools/doctor.sh

run:
	$(PYTHON) -m transportlab run $(RUN_FLAGS) --runner ns3

run-local:
	$(PYTHON) -m transportlab run $(RUN_FLAGS) --runner loopback

test:
	$(PYTHON) -m pytest -q tests

clean-runs:
	rm -rf runs
