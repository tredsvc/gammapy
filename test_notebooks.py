#!/usr/bin/env python
"""
Test if IPython notebooks work.
"""
import os
import sys
import logging
from pkg_resources import working_set
from gammapy.extern.pathlib import Path
from gammapy.scripts.jupyter import test_notebook
import yaml

logging.basicConfig(level=logging.INFO)

if 'GAMMAPY_EXTRA' not in os.environ:
    logging.info('GAMMAPY_EXTRA environment variable not set.')
    logging.info('Running notebook tests requires gammapy-extra.')
    logging.info('Exiting now.')
    sys.exit()


def get_notebooks():
    """Read `notebooks.yaml` info."""
    filename = str(
        Path(os.environ['GAMMAPY_EXTRA']) / 'notebooks' / 'notebooks.yaml')
    with open(filename) as fh:
        notebooks = yaml.safe_load(fh)
    return notebooks


def requirement_missing(notebook):
    """Check if one of the requirements is missing."""
    if notebook['requires'] is None:
        return False

    for package in notebook['requires'].split():
        try:
            working_set.require(package)
        except Exception as ex:
            return True
    return False


passed = True
yamlfile = get_notebooks()
dirnbs = Path(os.environ['GAMMAPY_EXTRA']) / 'notebooks'

for notebook in yamlfile:
    if not notebook['test']:
        logging.info(
            'Skipping notebook {} because test=false.'.format(notebook['name']))
        continue
    if requirement_missing(notebook):
        logging.info('Skipping notebook {} because requirement is missing.'.format(
            notebook['name']))
        continue

    filename = notebook['name'] + '.ipynb'
    path = dirnbs / filename

    if not test_notebook(path):
        passed = False
assert passed
