# Colorable output
include mk/color.mk
# Snippets for debug and profiling
include mk/debug_code.mk

RPLUGIN_PATH := ./rplugin/python3/deoplete/sources/
MODULE_NAME := deoplete_jedi.py

DEOPLETE_JEDI := ${RPLUGIN_PATH}${MODULE_NAME}
HELPER := ${RPLUGIN_PATH}/deoplete_clang/helper.py
PROFILER := ${RPLUGIN_PATH}/deoplete_clang/profiler.py

all: autopep8

test: test_modules flake8

test_modules:
	pip3 install -U -r ./test/requirements.txt

flake8:
	flake8 -v --config=$(PWD)/.flake8 ${DEOPLETE_JEDI} ${HELPER} ${PROFILER} || true

autopep8: clean
	autopep8 -i ${DEOPLETE_JEDI}

clean:
	@echo "Cleanup debug code in ${CYELLOW}${DEOPLETE_JEDI}${CRESET}..."
	@sed -i ':a;N;$$!ba;s/\n        try:.*    def get_complete_position/\n    def get_complete_position/g' ${DEOPLETE_JEDI}
	@sed -i ':a;N;$$!ba;s/from profiler import timeit\n//g' ${DEOPLETE_JEDI}
	@sed -i ':a;N;$$!ba;s/from logging import getLogger\nlogger = getLogger(__name__)\n\n//g' ${DEOPLETE_JEDI}
	@sed -i 's/^    @timeit.*$$//g' ${DEOPLETE_JEDI}
	@sed -i 's/^        logger.*$$//g' ${DEOPLETE_JEDI}

set_debug:
	@sed -i ':a;N;$$!ba;s/\n\n    def get_complete_position/\n\n        ${SET_DEBUG}\n    def get_complete_position/g' ${DEOPLETE_JEDI}

import: set_debug
	@sed -i ':a;N;$$!ba;s/import jedi\n\n\nclass Source/import jedi\n\nfrom logging import getLogger\nlogger = getLogger(__name__)\n\n\nclass Source/g' ${DEOPLETE_JEDI}
	@sed -i ':a;N;$$!ba;s/\n\n\nclass Source/\n\nfrom profiler import timeit\n\nclass Source/g' ${DEOPLETE_JEDI}

timeit-get_complete_position: import
	@echo "Enable $(subst timeit-,,$@) @timeit decorator in ${CYELLOW}${DEOPLETE_JEDI}${CRESET}..."
	@sed -i ':a;N;$$!ba;s/\n\n    def $(subst timeit-,,$@)/\n\n    ${TIMEIT_GET_COMPLETE_POSITION}\n    def $(subst timeit-,,$@)/g' ${DEOPLETE_JEDI}

timeit-gather_candidates: import
	@echo "Enable $(subst timeit-,,$@) @timeit decorator in ${CYELLOW}${DEOPLETE_JEDI}${CRESET}..."
	@sed -i ':a;N;$$!ba;s/\n\n    def $(subst timeit-,,$@)/\n\n    ${TIMEIT_GATHER_CANDIDATES}\n    def $(subst timeit-,,$@)/g' ${DEOPLETE_JEDI}

.PHONY: autopep8 flake8 clean set_debug import_timeit
