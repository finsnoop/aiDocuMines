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


class LocalModelFallbackTests(TestCase):
    """A local Ollama model runs on the server's CPU: it is used only when someone chose it."""

    def test_fallback_never_picks_a_local_model(self):
        offered = ["ollama/llama3.2:3b", "anthropic/claude-sonnet-4-5", "openai/*"]
        self.assertEqual(pick_chat_model(offered), "anthropic/claude-sonnet-4-5")
        self.assertIsNone(pick_chat_model(["ollama/llama3.2:3b", "llama3.2:3b"]))

    def test_a_chosen_local_model_is_still_used(self):
        self.assertEqual(pick_chat_model(["ollama/llama3.2:3b"], "ollama/llama3.2:3b"),
                         "ollama/llama3.2:3b")
        settings = PlatformGlobalSettings.load()
        settings.default_llm_model = "ollama/llama3.2:3b"
        settings.save()
        self.assertEqual(pick_chat_model(["ollama/llama3.2:3b"]), "ollama/llama3.2:3b")

    def test_ollama_as_the_chosen_provider_lists_its_own_models(self):
        self.assertEqual(pick_chat_model(["llama3.2:3b"], allow_local=True), "llama3.2:3b")
