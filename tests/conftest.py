import os
import sys

# Make the repo root importable so tests can import the app modules directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Tests always run the app in self-host (BYOK) mode. app.py freezes
# BILLING_ENABLED at import time and load_dotenv never overrides an existing
# variable, so this must be set here — before any test module imports app — or
# the suite's behavior would depend on the developer's personal .env.
os.environ["BILLING_ENABLED"] = "0"
# No test may reach the real claude CLI (and spend the subscription's usage):
# a call a test forgot to stub fails at once instead.
os.environ["CLAUDE_BIN"] = "/nonexistent/claude-for-tests"

# The shared YouTube-account state (yt_account) lives in files: give the suite
# its own directory and no pacing delay, so a test never waits on, or leaves a
# cooldown for, another test or the developer's own output/ directory.
import tempfile  # noqa: E402

os.environ["YT_ACCOUNT_STATE_DIR"] = tempfile.mkdtemp(prefix="yt-account-tests-")
os.environ.setdefault("YT_ACCOUNT_MIN_INTERVAL", "0")


# Probes run in a thread in the suite: tests replace metering.probe_url_minutes,
# which a pool process would not see.
os.environ.setdefault("PROBE_PROCESSES", "0")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_owner_cache():
    """The owner check remembers callers per credential (cloud/owner_cache.py);
    a caller one test remembered must not answer for the next."""
    from cloud import owner_cache
    owner_cache.clear()
    yield
    owner_cache.clear()


@pytest.fixture(autouse=True)
def _private_model_state(tmp_path, monkeypatch):
    """Model picks and usage records go to the test's own files, never to the
    real output/ of the machine running the suite."""
    import claude_cli
    monkeypatch.setattr(claude_cli, "SETTINGS_FILE", str(tmp_path / ".models.json"))
    monkeypatch.setattr(claude_cli, "USAGE_FILE", str(tmp_path / ".usage.jsonl"))
    monkeypatch.delenv("USAGE_LOG", raising=False)
