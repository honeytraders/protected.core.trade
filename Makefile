.PHONY: build clean verify

build:
	python3 -m pip install --upgrade build
	python3 -m build

verify:
	python3 -m py_compile src/honeytrade_core/strategy.py src/honeytrade_core/license_guard.py

clean:
	rm -rf dist build src/*.egg-info
