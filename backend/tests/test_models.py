import pytest
from pydantic import ValidationError

from app.models import Confidence, Decision, DecisionIn, Status

VALID = {
    "title": "Fixed retry intervals",
    "decision": "Use fixed retry intervals",
    "rejected": "Exponential backoff",
    "reason": "Thundering herd at 1000+ concurrent requests",
    "future_implication": "No exponential backoff in retry logic",
    "source": {"type": "pr", "url": "https://github.com/o/r/pull/42", "ref": "42"},
    "decided_by": ["alice"],
    "files": ["src/api/retry.py"],
}


def test_valid_input_gets_defaults():
    d = DecisionIn(**VALID)
    assert d.confidence == Confidence.MEDIUM
    assert d.security_relevant is False


def test_stored_decision_defaults():
    d = Decision(id="KEEP-D-001", **VALID)
    assert d.status == Status.ACTIVE
    assert d.needs_sync is False
    assert d.created_at.tzinfo is not None


@pytest.mark.parametrize("field", ["title", "decision", "reason", "future_implication", "source"])
def test_required_fields(field):
    data = {k: v for k, v in VALID.items() if k != field}
    with pytest.raises(ValidationError):
        DecisionIn(**data)


def test_needs_someone_who_decided():
    with pytest.raises(ValidationError):
        DecisionIn(**{**VALID, "decided_by": []})


def test_bad_enum_rejected():
    with pytest.raises(ValidationError):
        DecisionIn(**{**VALID, "confidence": "VERY HIGH"})
