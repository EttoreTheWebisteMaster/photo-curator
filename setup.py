# Shim for old pip versions (< 21.3) that cannot do an editable install from pyproject.toml alone.
# The real configuration lives in pyproject.toml.
from setuptools import setup

setup()
