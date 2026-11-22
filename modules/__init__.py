"""Test types: the kinds of test the engine knows how to run (core code, reviewed by maintainers).

Challenges (community data) declare which test type they use, e.g. `test_type: v1-simulator@1`.
The `@N` is the test type's interface version: a breaking change to a test type gets a new number,
so existing challenges keep running on the version they were written for.

To add a test type:
  1. Create modules/my_type.py with a class that subclasses modules.base.Module.
  2. Register it below as "my-type@1": "modules.my_type:MyType".
"""

import importlib

TEST_TYPES = {
    "v1-simulator@1": "modules.v1_case:V1Case",
    "workflow@1": "modules.workflow:Workflow",
}

# Older spellings still accepted in inline (unpackaged) suite nodes.
ALIASES = {"v1_case": "v1-simulator@1"}


def resolve(name: str) -> str:
    name = ALIASES.get(name, name)
    if name not in TEST_TYPES:
        raise ValueError(f"Unknown test type '{name}'. Known: {sorted(TEST_TYPES)}")
    return name


def load(name: str, params: dict, instance: dict | None = None):
    """instance: per-attempt {seed, difficulty}; only passed to test types that generate instances."""
    path, cls_name = TEST_TYPES[resolve(name)].split(":")
    cls = getattr(importlib.import_module(path), cls_name)
    kwargs = dict(params or {})
    if getattr(cls, "uses_instances", False) and instance:
        kwargs.update(instance)
    return cls(**kwargs)
