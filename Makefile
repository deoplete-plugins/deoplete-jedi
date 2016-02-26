MAKEFLAGS := -j 1

# Colorable output
include mk/color.mk
# Snippets for debug and profiling
include mk/debug_code.mk

RPLUGIN_PATH := ./rplugin/python3/deoplete/sources/
MODULE_NAME := deoplete_jedi.py

DEOPLETE_JEDI := ${RPLUGIN_PATH}${MODULE_NAME}
HELPER := ${RPLUGIN_PATH}/deoplete_jedi/helper.py
PROFILER := ${RPLUGIN_PATH}/deoplete_jedi/profiler.py

all: autopep8

test: flake8

lint: lint_modules flake8

lint_modules:
	pip3 install -U -r ./tests/requirements.txt

flake8:
	flake8 -v --config=$(PWD)/.flake8 ${DEOPLETE_JEDI} ${HELPER} ${PROFILER} || true

autopep8: clean
	autopep8 -i ${DEOPLETE_JEDI}

clean:
	@echo "Cleanup debug code in ${CYELLOW}${DEOPLETE_JEDI}${CRESET}..."
	@sed -i ':a;N;$$!ba;s/\n        try:.*    def get_complete_position/\n    def get_complete_position/g' ${DEOPLETE_JEDI}
	@sed -i ':a;N;$$!ba;s/${IMPORT_LOGGER}\n\n//g' ${DEOPLETE_JEDI}
	@sed -i ':a;N;$$!ba;s/${IMPORT_TIMEIT}\n//g' ${DEOPLETE_JEDI}
	@sed -i ':a;N;$$!ba;s/${IMPORT_PYVMMONITOR}\n//g' ${DEOPLETE_JEDI}
	@sed -i 's/^    @timeit.*$$//g' ${DEOPLETE_JEDI}
	@sed -i 's/^    @pyvmmonitor.*$$//g' ${DEOPLETE_JEDI}
	@sed -i 's/^        logger.*$$//g' ${DEOPLETE_JEDI}

set_debug:
	@sed -i ':a;N;$$!ba;s/\n\n    def get_complete_position/\n\n        ${SET_DEBUG}\n    def get_complete_position/g' ${DEOPLETE_JEDI}

import_logger: set_debug
	@sed -i ':a;N;$$!ba;s/import jedi\n\n\nclass Source/import jedi\n\n${IMPORT_LOGGER}\n\n\nclass Source/g' ${DEOPLETE_JEDI}

import_timeit: import_logger
	@sed -i ':a;N;$$!ba;s/\n\n\nclass Source/\n\n${IMPORT_TIMEIT}\n\nclass Source/g' ${DEOPLETE_JEDI}

import_pyvmmonitor:
	@sed -i ':a;N;$$!ba;s/\n\n\nclass Source/\n\n${IMPORT_PYVMMONITOR}\n\nclass Source/g' ${DEOPLETE_JEDI}

timeit-get_complete_position: import_timeit
	@echo "Enable $(subst timeit-,,$@) @timeit decorator in ${CYELLOW}${DEOPLETE_JEDI}${CRESET}..."
	@sed -i ':a;N;$$!ba;s/\n\n    def $(subst timeit-,,$@)/\n\n    ${TIMEIT_GET_COMPLETE_POSITION}\n    def $(subst timeit-,,$@)/g' ${DEOPLETE_JEDI}

timeit-gather_candidates: import_timeit
	@echo "Enable $(subst timeit-,,$@) @timeit decorator in ${CYELLOW}${DEOPLETE_JEDI}${CRESET}..."
	@sed -i ':a;N;$$!ba;s/\n\n    def $(subst timeit-,,$@)/\n\n    ${TIMEIT_GATHER_CANDIDATES}\n    def $(subst timeit-,,$@)/g' ${DEOPLETE_JEDI}

pyvmmonitor-get_complete_position: import_pyvmmonitor
	@echo "Enable $(subst pyvmmonitor-,,$@) @pyvmmonitor decorator in ${CYELLOW}${DEOPLETE_JEDI}${CRESET}..."
	@sed -i ':a;N;$$!ba;s/\n\n    def $(subst pyvmmonitor-,,$@)/\n\n    ${PYVMMONITOR_DECORATOR}\n    def $(subst pyvmmonitor-,,$@)/g' ${DEOPLETE_JEDI}

pyvmmonitor-gather_candidates: import_pyvmmonitor
	@echo "Enable $(subst pyvmmonitor-,,$@) @pyvmmonitor decorator in ${CYELLOW}${DEOPLETE_JEDI}${CRESET}..."
	@sed -i ':a;N;$$!ba;s/\n\n    def $(subst pyvmmonitor-,,$@)/\n\n    ${PYVMMONITOR_DECORATOR}\n    def $(subst pyvmmonitor-,,$@)/g' ${DEOPLETE_JEDI}

.PHONY: autopep8 flake8 clean set_debug import_timeit
