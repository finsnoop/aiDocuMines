"""
Import heavy libraries (PyTorch, Transformers, spaCy, Presidio, unstructured) only when they are used.

Every web worker, Celery worker, the beat scheduler and the file monitor import all task modules at
start-up. When those modules imported these libraries at the top, each process held ~850 MB before
doing any work. With these helpers a process only pays for a library once it actually runs a job
that needs it.

    spacy = LazyModule("spacy")                                  # spacy.load(...) imports on first use
    partition_pdf = lazy_callable("unstructured.partition.pdf", "partition_pdf")
"""
from __future__ import annotations

import importlib
import threading


class LazyModule:
    """A module imported on first attribute access."""

    def __init__(self, name: str):
        self._name = name
        self._module = None
        self._lock = threading.Lock()

    def _load(self):
        if self._module is None:
            with self._lock:
                if self._module is None:
                    self._module = importlib.import_module(self._name)
        return self._module

    def __getattr__(self, attr):
        return getattr(self._load(), attr)

    def __repr__(self):
        return f"<lazy module {self._name}{' (loaded)' if self._module else ''}>"


def lazy_callable(module: str, name: str):
    """A function or class from `module` that is imported the first time it is called."""
    target = {}

    def call(*args, **kwargs):
        if "fn" not in target:
            target["fn"] = getattr(importlib.import_module(module), name)
        return target["fn"](*args, **kwargs)

    call.__name__ = name
    call.__qualname__ = name
    call.__doc__ = f"{module}.{name}, imported on first use (core/lazy_imports.py)."
    return call
