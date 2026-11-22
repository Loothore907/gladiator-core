"""Live tests (marked `live`) call real model APIs and cost money.
They are skipped unless GLADIATOR_LIVE=1 is set."""

import os

import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("GLADIATOR_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live test: set GLADIATOR_LIVE=1 to run (costs a few cents)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
