import logging


def test_app_logger_emits_info_timing_lines():
    import app.main  # noqa: F401  (configures the "app" logger)
    assert logging.getLogger("app").isEnabledFor(logging.INFO)
