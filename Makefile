# stay-on-the-line

PY ?= python3
PORT ?= 8080
BPORT ?= 9000

.PHONY: help test test1 test2 check serve demo clean

help:
	@echo "make test    - both test suites"
	@echo "make check   - the part 1 marking harness (spawns a server)"
	@echo "make serve   - run the HTTP/1.1 calculator on port $(PORT)"
	@echo "make demo    - part 2 end to end, with hexdumps"

test: test1 test2

test1:
	$(PY) -m unittest discover -s part1-http11/tests -t part1-http11/tests

test2:
	$(PY) -m unittest discover -s part2-binary/tests -t part2-binary/tests

check:
	$(PY) part1-http11/check.py --spawn --port $(PORT)

serve:
	$(PY) part1-http11/server11.py --port $(PORT)

demo:
	@cd part2-binary && ./bserve ./www $(BPORT) & \
	sleep 1; \
	cd part2-binary && ./bcurl -v localhost:$(BPORT)/hello.txt; \
	echo; echo "--- three URLs, one connection ---"; \
	cd part2-binary && ./bcurl -v localhost:$(BPORT)/index.html /about.html /assets/data.json >/dev/null; \
	echo; echo "--- an unknown frame type, skipped by length ---"; \
	cd part2-binary && ./bcurl --send-unknown localhost:$(BPORT)/about.html >/dev/null; \
	kill %1 2>/dev/null || true

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
