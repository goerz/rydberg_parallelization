define PRINT_HELP_PYSCRIPT
import re, sys

for line in sys.stdin:
	match = re.match(r'^([a-zA-Z_-]+):.*?## (.*)$$', line)
	if match:
		target, help = match.groups()
		print("%-20s %s" % (target, help))
endef
export PRINT_HELP_PYSCRIPT

help:
	@python -c "$$PRINT_HELP_PYSCRIPT" < $(MAKEFILE_LIST)

.venv/bin/jupyter:
	conda env create -p ./.venv -f environment.yml

venv: ./.venv/bin/jupyter  ## create the local virtual environment

jupyter-notebook: .venv/bin/jupyter  ## run a notebook server
	.venv/bin/jupyter notebook --no-browser

jupyter-lab: ./.venv/bin/jupyter  ## run a jupyterlab server
	.venv/bin/jupyter lab --no-browser

dist-clean:  ## remove env files
	rm -rf .venv

PREFIX = $(shell pwd)/.venv
export PREFIX
