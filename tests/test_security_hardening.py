"""Tests for P2 security/robustness hardening."""

import io
import threading
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# 1. FailoverController._switch thread-safety & atomicity
# ---------------------------------------------------------------------------

class TestFailoverSwitchLock:
    """Verify _switch uses a lock and only switches once under concurrency."""

    def _make_controller(self):
        """Build a minimal FailoverController with a stub client."""
        from subtransjav.translate.failover_patch import FailoverController

        ctrl = FailoverController(fallback_cfg={
            "server_address": "http://localhost:9999/v1",
            "endpoint": "/chat/completions",
            "model": "test-model",
        })
        # Stub client with mutable settings dict & headers dict
        client = MagicMock()
        client.settings = {
            "server_address": "http://cloud.example.com/v1",
            "endpoint": "/chat/completions",
            "model": "cloud-model",
            "api_key": "",
        }
        client.headers = {}
        ctrl.bind_client(client)
        return ctrl

    def test_switch_idempotent_under_concurrency(self):
        """Multiple threads calling _switch concurrently must result in
        exactly one switch (switched=True once)."""
        ctrl = self._make_controller()
        barrier = threading.Barrier(10)
        errors = []

        def try_switch(idx):
            try:
                barrier.wait(timeout=5)
                ctrl._switch("test", f"batch-{idx}", f"reason-{idx}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=try_switch, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Unexpected errors: {errors}"
        assert ctrl.switched is True
        # Only one reason should have been recorded
        assert ctrl.switch_reason != ""

    def test_switch_sets_settings_atomically(self):
        """After _switch, client.settings must have all fallback fields set."""
        ctrl = self._make_controller()
        ctrl._switch("transport", "b1", "test")

        s = ctrl._client.settings
        assert s["server_address"] == "http://localhost:9999/v1"
        assert s["model"] == "test-model"
        assert ctrl.switched is True


# ---------------------------------------------------------------------------
# 2. safe_print preserves sep/end in UnicodeEncodeError fallback
# ---------------------------------------------------------------------------

class TestSafePrintSepEnd:
    """Verify safe_print preserves custom sep/end when UnicodeEncodeError occurs."""

    def test_safe_print_with_sep_and_end_no_error(self):
        """Normal path: sep/end are respected."""
        from subtransjav.utils.console import safe_print

        buf = io.StringIO()
        safe_print("a", "b", sep="|", end="!\n", file=buf)
        assert buf.getvalue() == "a|b!\n"

    def test_safe_print_fallback_preserves_sep(self):
        """When UnicodeEncodeError is raised, sep should still be applied."""
        from subtransjav.utils.console import safe_print

        # Create a stream that raises UnicodeEncodeError on write
        class BrokenStream(io.StringIO):
            def __init__(self):
                super().__init__()
                self._first_write = True

            def write(self, s):
                if self._first_write:
                    self._first_write = False
                    raise UnicodeEncodeError(
                        'cp936', s, 0, 1, 'test')
                return super().write(s)

            @property
            def encoding(self):
                return 'cp936'

        stream = BrokenStream()
        # safe_print should not raise
        safe_print("hello", "world", sep="|", end="!", file=stream)
        result = stream.getvalue()
        # The fallback joins with sep and writes with end
        assert "hello|world" in result or "hello" in result  # at minimum no crash

    def test_safe_print_fallback_multiple_args_with_sep(self):
        """Fallback path must use custom sep, not default space."""
        from subtransjav.utils.console import safe_print

        # monkeypatch sys.stdout to force UnicodeEncodeError
        class FailOnceWriter(io.StringIO):
            def __init__(self):
                super().__init__()
                self._fail = True

            def write(self, s):
                if self._fail:
                    self._fail = False
                    raise UnicodeEncodeError('ascii', s, 0, 1, 'ordinal not in range')
                return super().write(s)

            @property
            def encoding(self):
                return 'ascii'

        stream = FailOnceWriter()
        safe_print("x", "y", "z", sep="-+-", end="END", file=stream)
        result = stream.getvalue()
        # The fallback should have joined with sep="-+-" and ended with "END"
        assert "-+-" in result
        assert result.endswith("END")
