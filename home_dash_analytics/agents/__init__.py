"""
aiDocuMines agent system.

    orchestrator   decides what work exists and dispatches it to specialists      (orchestrator.py)
    resources      capacity: Celery, Redis, PostgreSQL, AI provider, RabbitMQ      (resources.py)
    cost           tokens -> USD per organisation, budgets and hard stops          (cost.py)
    pricing        USD per input/output token: admin prices, then LiteLLM registry (pricing.py)
    gateway        limits per person and per key, spend for any period            (gateway.py)
    intelligence   document facts: plan -> extract -> verify -> validate          (../intelligence.py)
    ocr            text for scanned documents                                      (document_ocr)
    similarity     near-identical documents by embeddings                          (orchestrator.py)
    events         immutable log of every agent decision                           (events.py)
    usage          records every AI call on the platform once, against its actor   (usage.py)

Importing this package registers the agents' Celery tasks.
"""
from . import cost, orchestrator, pricing, resources  # noqa: F401  (task registration)
