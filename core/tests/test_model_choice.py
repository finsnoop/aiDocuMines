"""The platform's configured default model decides cost: it wins over the built-in fallback list."""
from django.test import TestCase

from core.llm_providers import pick_chat_model
from system_settings.models import PlatformGlobalSettings

OFFERED = ["gpt-4o-mini", "gpt-5.6-terra", "gpt-5.5", "text-embedding-3-small", "openai/*"]


class ModelChoiceTests(TestCase):
    def test_platform_default_wins_over_the_fallback_list(self):
        settings = PlatformGlobalSettings.load()
        settings.default_llm_model = "gpt-4o-mini"
        settings.save()
        self.assertEqual(pick_chat_model(OFFERED), "gpt-4o-mini")
        self.assertEqual(pick_chat_model(["openai/gpt-4o-mini", "openai/gpt-5.5"]), "openai/gpt-4o-mini")

    def test_an_explicit_choice_wins_over_the_platform_default(self):
        settings = PlatformGlobalSettings.load()
        settings.default_llm_model = "gpt-4o-mini"
        settings.save()
        self.assertEqual(pick_chat_model(OFFERED, "gpt-5.5"), "gpt-5.5")

    def test_without_a_platform_default_current_models_come_first(self):
        self.assertEqual(pick_chat_model(OFFERED), "gpt-5.6-terra")
        self.assertEqual(pick_chat_model(["gpt-4o-mini", "text-embedding-3-small"]), "gpt-4o-mini")

    def test_a_platform_default_that_is_not_offered_is_skipped(self):
        settings = PlatformGlobalSettings.load()
        settings.default_llm_model = "retired-model"
        settings.save()
        self.assertEqual(pick_chat_model(OFFERED), "gpt-5.6-terra")
