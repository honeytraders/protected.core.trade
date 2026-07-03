.PHONY: build clean

build:
	python3 -m pip install --upgrade build
	python3 -m build

clean:
	rm -rf dist build src/*.egg-info
