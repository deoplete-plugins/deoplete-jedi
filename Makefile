RPLUGIN_PATH := ./rplugin/python3/deoplete/sources

TARGET := deoeplete_jedi.py deoplete_jedi/__init__.py  deoplete_jedi/cache.py  deoplete_jedi/helper.py  deoplete_jedi/profiler.py  deoplete_jedi/server.py  deoplete_jedi/utils.py  deoplete_jedi/utils.pyc  deoplete_jedi/worker.py

all: test

test: flake8

test/modules:
	@pip3 install -q -U --user -r ./tests/requirements.txt

flake8: test/modules
	flake8 --config=./.flake8 $(foreach dir,$(TARGET),$(RPLUGIN_PATH)/$(dir)) || true

.PHONY: test flake8
