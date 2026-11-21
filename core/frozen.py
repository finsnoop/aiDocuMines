"""
Frozen documents: point-in-time copies that must never change, such as a document set as a benchmark
(contracts.Benchmark). The copy has its own bytes (core.ingest.MatterBatch.add_snapshot) and is opened read-only;
saves from the editor or the Word add-in are refused. Its SHA-256 is kept on the benchmark so anyone can verify it.
"""
FROZEN_SERVICES = {"benchmark"}


class DocumentFrozen(Exception):
    """This document is a frozen point-in-time copy."""


def is_frozen(file_obj) -> bool:
    return bool(file_obj is not None and getattr(file_obj, "service_id", None) in FROZEN_SERVICES)


def assert_not_frozen(file_obj):
    if is_frozen(file_obj):
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied("This is a frozen benchmark copy. Edit the original document and set it as a new benchmark.")
