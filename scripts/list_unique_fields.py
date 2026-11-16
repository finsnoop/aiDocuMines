#!/usr/bin/env python3
import os
import sys

# Add the project root to sys.path (so aiDocuMines can be found)
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))
sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'aiDocuMines.settings')

import django
from django.apps import apps

django.setup()

print("\nModels with UNIQUE constraints in aiDocuMines:\n")

for model in apps.get_models():
    unique_fields = [f.name for f in model._meta.fields if getattr(f, 'unique', False)]
    unique_together = getattr(model._meta, 'unique_together', None)

    if unique_fields or unique_together:
        print(f"Model: {model.__module__}.{model.__name__}")
        if unique_fields:
            print(f"   • unique=True fields: {unique_fields}")
        if unique_together:
            print(f"   • unique_together: {unique_together}")
        print()

