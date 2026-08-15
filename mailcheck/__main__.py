"""Enables `python -m mailcheck`, which works regardless of whether the
Scripts directory is on PATH."""

from .cli import main

main()
