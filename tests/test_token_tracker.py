import unittest

from app.core.config import settings
from app.services.token_tracker import PRICING_PER_1M, TurnTokenMetrics


class TokenTrackerPricingTests(unittest.TestCase):
    def test_configured_anthropic_model_has_pricing(self):
        self.assertIn(settings.ANTHROPIC_MODEL, PRICING_PER_1M)

    def test_configured_openai_model_has_pricing(self):
        self.assertIn(settings.OPENAI_MODEL, PRICING_PER_1M)

    def test_opus_55_cost_uses_configured_model_pricing(self):
        model = "claude-opus-5-5"
        metrics = TurnTokenMetrics(
            request_id="request",
            session_id="session",
            conversation_id="conversation",
            intent="test",
            model=model,
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
        )

        self.assertEqual(metrics.compute_cost(), 90.0)
        self.assertEqual(metrics.model, model)

    def test_gpt_55_cost_uses_configured_model_pricing(self):
        model = "gpt-5.5"
        metrics = TurnTokenMetrics(
            request_id="request",
            session_id="session",
            conversation_id="conversation",
            intent="test",
            model=model,
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
        )

        self.assertEqual(metrics.compute_cost(), 20.0)
        self.assertEqual(metrics.model, model)


if __name__ == "__main__":
    unittest.main()
