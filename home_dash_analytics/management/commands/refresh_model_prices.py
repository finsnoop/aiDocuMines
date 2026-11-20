"""Fetch LiteLLM's latest AI model prices now (a daily task does this automatically).

    docker exec aidocumines_web python manage.py refresh_model_prices
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Refresh AI model prices from LiteLLM's price registry."

    def handle(self, *args, **options):
        from home_dash_analytics.agents.pricing import refresh
        try:
            count = refresh()
        except Exception as exc:
            raise CommandError(f"Could not refresh prices ({type(exc).__name__}); the bundled prices stay in use.")
        self.stdout.write(f"Prices updated for {count} models.")
