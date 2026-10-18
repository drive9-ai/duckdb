.PHONY: build test
PYTHON ?= python3
build:
	$(PYTHON) scripts/build.py
test: build
	$(PYTHON) -m unittest discover -s test -v
