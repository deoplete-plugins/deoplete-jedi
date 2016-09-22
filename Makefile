RPLUGIN_PATH := ./rplugin/python3/deoplete/sources

all: test

test: flake8

flake8:
	flake8

.PHONY: test flake8
