"""Disable cross-request AI caches and untrusted pickle-backed disk deserialization."""
import os
import tempfile


def configure_ai_cache():
    # DSPy opens a default disk cache at import. Give it a fresh private directory
    # before importing, so no pre-existing pickle is ever considered trusted.
    os.environ["DSPY_CACHEDIR"] = tempfile.mkdtemp(prefix="aidoc-dspy-")
    import dspy
    dspy.configure_cache(enable_disk_cache=False, enable_memory_cache=False, restrict_pickle=True)
    # Every DSPy model call is checked by the AI gateway and recorded in the cost ledger.
    from home_dash_analytics.agents.usage import instrument_dspy
    instrument_dspy()
