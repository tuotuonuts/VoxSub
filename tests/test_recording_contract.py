"""Recording reason is optional backend data, not a frontend-only protocol drift."""
import json

import pytest

from tests.test_contracts import CONTRACTS_DIR, iter_errors


@pytest.mark.parametrize('filename', ['commands.json', 'events.json', 'protocol.json'])
def test_recording_reason_state_contract(filename):
    doc = json.loads((CONTRACTS_DIR / filename).read_text(encoding='utf-8'))
    schema = doc['$defs']['StatePayload']
    payload = dict(running=False, paused=False, mode='a', state='idle', configGeneration=0)
    assert not list(iter_errors(payload, schema, root=doc))
    assert not list(iter_errors({**payload, 'recordingReason': 'resources closing'}, schema, root=doc))
    assert list(iter_errors({**payload, 'recordingReason': 42}, schema, root=doc))


def test_recording_reason_event_contract():
    doc = json.loads((CONTRACTS_DIR / 'events.json').read_text(encoding='utf-8'))
    schema = doc['events']['state']['payload']
    payload = dict(event='state', running=False, paused=False, mode='a', state='idle', configGeneration=0)
    assert not list(iter_errors(payload, schema, root=doc))
    assert not list(iter_errors({**payload, 'recordingReason': 'resources closing'}, schema, root=doc))
    assert list(iter_errors({**payload, 'recordingReason': {}}, schema, root=doc))
