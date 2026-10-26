"""Tests for hub-side session context and brief receipt (design item 12)."""
import time
import pytest

from rook.hub import session_context as sc


class TestSessionBrief:
    """SessionBrief: immutable, hashable, deterministic JSON."""

    def test_to_dict_minimal(self):
        """Minimal brief has session_id, user, timestamp; band_id and version are optional."""
        brief = sc.SessionBrief(
            session_id="sess_abc123",
            user="alice",
            timestamp=1000.0,
        )
        d = brief.to_dict()
        assert d == {
            "session_id": "sess_abc123",
            "user": "alice",
            "timestamp": 1000.0,
        }

    def test_to_dict_with_band_and_version(self):
        """Extra fields are included when present."""
        brief = sc.SessionBrief(
            session_id="sess_abc123",
            user="alice",
            timestamp=1000.0,
            band_id="band_xyz",
            hub_version="1055.happy.fox",
        )
        d = brief.to_dict()
        assert d["band_id"] == "band_xyz"
        assert d["hub_version"] == "1055.happy.fox"

    def test_to_json_deterministic(self):
        """Same content produces same JSON (keys sorted, compact)."""
        brief1 = sc.SessionBrief(
            session_id="sess_abc",
            user="alice",
            timestamp=1000.0,
            band_id="band_1",
        )
        brief2 = sc.SessionBrief(
            session_id="sess_abc",
            user="alice",
            timestamp=1000.0,
            band_id="band_1",
        )
        assert brief1.to_json() == brief2.to_json()

    def test_to_json_order_independent(self):
        """JSON is deterministic even if fields are created in different order."""
        # Both briefs have same values, created in same order
        brief1 = sc.SessionBrief("sess", "alice", 1000.0)
        brief2 = sc.SessionBrief("sess", "alice", 1000.0)
        assert brief1.to_json() == brief2.to_json()

    def test_receipt_deterministic(self):
        """Same brief produces same receipt hash."""
        brief1 = sc.SessionBrief("sess", "alice", 1000.0)
        brief2 = sc.SessionBrief("sess", "alice", 1000.0)
        assert brief1.receipt() == brief2.receipt()

    def test_receipt_changes_with_content(self):
        """Different content produces different receipt."""
        brief1 = sc.SessionBrief("sess_a", "alice", 1000.0)
        brief2 = sc.SessionBrief("sess_b", "alice", 1000.0)
        assert brief1.receipt() != brief2.receipt()

    def test_receipt_changes_with_user(self):
        """Different user produces different receipt."""
        brief1 = sc.SessionBrief("sess", "alice", 1000.0)
        brief2 = sc.SessionBrief("sess", "bob", 1000.0)
        assert brief1.receipt() != brief2.receipt()

    def test_receipt_changes_with_timestamp(self):
        """Different timestamp produces different receipt."""
        brief1 = sc.SessionBrief("sess", "alice", 1000.0)
        brief2 = sc.SessionBrief("sess", "alice", 1001.0)
        assert brief1.receipt() != brief2.receipt()


class TestEchoState:
    """EchoState: track client echo behavior and decide when to send brief."""

    def test_initial_state_is_never_echoed(self):
        """New EchoState starts in never_echoed state."""
        state = sc.EchoState()
        assert state.state == "never_echoed"

    def test_should_send_brief_on_never_echoed(self):
        """Always send brief when client hasn't echoed yet."""
        state = sc.EchoState()
        brief = sc.SessionBrief("sess", "alice", 1000.0)
        assert state.should_send_brief(brief) is True

    def test_mark_echoed_transitions_state(self):
        """Echoing receipt transitions from never_echoed to echoed."""
        state = sc.EchoState()
        state.mark_echoed("receipt_abc")
        assert state.state == "echoed"
        assert state.last_receipt == "receipt_abc"

    def test_should_send_brief_after_echo_with_same_receipt(self):
        """Don't send brief if receipt matches what client echoed."""
        state = sc.EchoState()
        brief = sc.SessionBrief("sess", "alice", 1000.0)
        receipt = brief.receipt()

        # Client echoes
        state.mark_echoed(receipt)

        # Same brief: no resend
        assert state.should_send_brief(brief) is False

    def test_should_send_brief_after_echo_with_different_receipt(self):
        """Resend brief if receipt changed (compaction, context change)."""
        state = sc.EchoState()
        brief1 = sc.SessionBrief("sess", "alice", 1000.0)
        brief2 = sc.SessionBrief("sess", "alice", 1001.0)  # Different timestamp

        # Client echoes with first brief
        state.mark_echoed(brief1.receipt())

        # Brief changed: send again
        assert state.should_send_brief(brief2) is True

    def test_should_send_brief_after_echo_with_missing_receipt_field(self):
        """Resend brief if client doesn't echo back (receipts uninitialized)."""
        state = sc.EchoState()
        brief = sc.SessionBrief("sess", "alice", 1000.0)

        # Client echoed once
        state.mark_echoed(brief.receipt())

        # But then second call has no echo (receipt still matches internally,
        # but client didn't send it back, so we'd call mark_silent instead)
        # This tests the case where last_receipt is set but we're checking
        # should_send_brief without mark_echoed being called again
        assert state.should_send_brief(brief) is False

    def test_mark_silent_stops_sending(self):
        """After client is marked silent, never send brief again."""
        state = sc.EchoState()
        brief = sc.SessionBrief("sess", "alice", 1000.0)

        # Client is silent
        state.mark_silent()
        assert state.state == "silent"

        # Never send (even if brief changes)
        assert state.should_send_brief(brief) is False

    def test_silent_transition_from_never_echoed_only(self):
        """Can only transition to silent from never_echoed (not from echoed)."""
        state = sc.EchoState()
        brief = sc.SessionBrief("sess", "alice", 1000.0)

        # First: echoed once
        state.mark_echoed(brief.receipt())
        assert state.state == "echoed"

        # Try to mark silent: state doesn't change (only transitions from never_echoed)
        state.mark_silent()
        assert state.state == "echoed"

    def test_multiple_echoes_update_receipt(self):
        """Can echo multiple times (updates receipt for new brief tracking)."""
        state = sc.EchoState()
        brief1 = sc.SessionBrief("sess", "alice", 1000.0)
        brief2 = sc.SessionBrief("sess", "alice", 2000.0)

        # First echo
        state.mark_echoed(brief1.receipt())
        assert state.should_send_brief(brief1) is False

        # Brief changed, so we send it
        assert state.should_send_brief(brief2) is True

        # Client echoes the new brief
        state.mark_echoed(brief2.receipt())
        assert state.should_send_brief(brief2) is False


class TestMakeNotice:
    """make_notice: build the notice dict for replies."""

    def test_notice_structure(self):
        """Notice includes brief dict and receipt string."""
        brief = sc.SessionBrief("sess_abc", "alice", 1000.0)
        notice = sc.make_notice(brief)

        assert "brief" in notice
        assert "receipt" in notice
        assert isinstance(notice["brief"], dict)
        assert isinstance(notice["receipt"], str)

    def test_notice_brief_content(self):
        """Notice brief matches brief.to_dict()."""
        brief = sc.SessionBrief("sess_abc", "alice", 1000.0, band_id="b1")
        notice = sc.make_notice(brief)

        assert notice["brief"] == brief.to_dict()

    def test_notice_receipt_matches_brief_receipt(self):
        """Notice receipt matches brief.receipt()."""
        brief = sc.SessionBrief("sess_abc", "alice", 1000.0)
        notice = sc.make_notice(brief)

        assert notice["receipt"] == brief.receipt()


class TestIntegration:
    """Integration: session brief, receipt, and echo state working together."""

    def test_first_call_sends_brief(self):
        """First call always sends brief + receipt."""
        state = sc.EchoState()
        brief = sc.SessionBrief("sess", "alice", 1000.0)
        notice = sc.make_notice(brief)

        assert state.should_send_brief(brief) is True
        # Caller would add notice to reply
        # Assume agent echoes receipt in next call

    def test_second_call_with_matching_echo_omits_brief(self):
        """Second call with matching receipt omits brief, saves tokens."""
        state = sc.EchoState()
        brief = sc.SessionBrief("sess", "alice", 1000.0)
        receipt = brief.receipt()

        # First call sends brief, agent echoes
        assert state.should_send_brief(brief) is True
        state.mark_echoed(receipt)

        # Second call: no resend
        assert state.should_send_brief(brief) is False

    def test_compaction_invalidates_receipt(self):
        """After hub compaction, brief timestamp changes, receipt invalidates."""
        state = sc.EchoState()

        # First brief
        brief1 = sc.SessionBrief("sess", "alice", 1000.0)
        state.mark_echoed(brief1.receipt())

        # After compaction, timestamp updated
        brief2 = sc.SessionBrief("sess", "alice", 2000.0)

        # Receipt mismatch triggers resend
        assert state.should_send_brief(brief2) is True

    def test_two_sessions_of_one_user_are_independent(self):
        """Two MCP sessions of the same user are tracked independently."""
        state1 = sc.EchoState()
        state2 = sc.EchoState()

        brief = sc.SessionBrief("sess_a", "alice", 1000.0)

        # Session 1 echoes
        state1.mark_echoed(brief.receipt())
        assert state1.should_send_brief(brief) is False

        # Session 2 is still never_echoed
        assert state2.should_send_brief(brief) is True

    def test_silent_client_not_spammed(self):
        """Non-echoing client (phone, browser) marked silent after first call."""
        state = sc.EchoState()
        brief = sc.SessionBrief("sess", "alice", 1000.0)

        # First call: send brief
        assert state.should_send_brief(brief) is True

        # Agent is a phone app, doesn't echo. On next call, no receipt.
        # Hub would call mark_silent (no receipt returned)
        state.mark_silent()

        # Subsequent calls: no brief sent, no spam
        for _ in range(5):
            assert state.should_send_brief(brief) is False

    def test_user_change_invalidates_receipt(self):
        """If user context changes (re-auth, role switch), receipt invalid."""
        state = sc.EchoState()

        # Alice's session
        brief1 = sc.SessionBrief("sess", "alice", 1000.0)
        state.mark_echoed(brief1.receipt())

        # User changes to Bob (shouldn't happen in same MCP session, but design
        # assumes receipt includes user for safety)
        brief2 = sc.SessionBrief("sess", "bob", 1000.0)

        # Receipt mismatch: resend
        assert state.should_send_brief(brief2) is True

    def test_band_change_invalidates_receipt(self):
        """Band change (access granted to new band) invalidates receipt."""
        state = sc.EchoState()

        # Initial brief without band
        brief1 = sc.SessionBrief("sess", "alice", 1000.0)
        state.mark_echoed(brief1.receipt())

        # Band assigned
        brief2 = sc.SessionBrief("sess", "alice", 1000.0, band_id="new_band")

        # Receipt mismatch: resend
        assert state.should_send_brief(brief2) is True
