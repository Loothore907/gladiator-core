"""Offline fake 'models' for testing the engine without spending money.

    scripted:golden   plays v1's known-correct actions (should pass every v1 case)
    scripted:invalid  always submits '{}' (should fail everything)
    scripted:silent   never calls the tool (tests the no-action path)
    scripted:outage   every call fails like an overloaded provider (tests that outages aren't scored)

The golden answers are operator data (tests/fixtures/golden_actions.json).
The fake model recognises which case it is playing by comparing the public
packet it receives with each case's public packet, so nothing private is
ever added to a prompt.
"""

import json
import threading
from pathlib import Path

from providers.base import TOOL_NAME, Chat, Provider, ProviderError, Reply, Usage

GOLDEN = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "golden_actions.json"
_by_packet = None
_lock = threading.Lock()  # both contestants may start at the same moment


def _golden_by_packet():
    global _by_packet
    with _lock:
        if _by_packet is None:
            import modules

            table = {}
            for case_id, actions in json.loads(GOLDEN.read_text(encoding="utf-8")).items():
                packet = modules.load("v1_case", {"case": case_id}).start()
                table[json.dumps(packet, sort_keys=True)] = actions
            _by_packet = table
    return _by_packet


class ScriptedProvider(Provider):
    name = "scripted"

    def open_chat(self, system, first_message, settings, tools=None):
        return ScriptedChat(self.model, first_message)


class ScriptedChat(Chat):
    def __init__(self, mode, first_message):
        self.mode = mode
        self.first = first_message
        self.turn = 0
        if mode == "golden":
            packet = json.loads(first_message).get("case")
            self.script = _golden_by_packet().get(json.dumps(packet, sort_keys=True))
            if self.script is None:
                raise ProviderError("scripted:golden has no answers for this case")
        elif mode not in ("invalid", "silent", "outage"):
            raise ProviderError(f"unknown scripted mode '{mode}' (use golden, invalid, silent or outage)")

    def approx_input_chars(self):
        return len(self.first)

    def _reply(self):
        if self.mode == "outage":
            raise ProviderError("scripted HTTP 503: simulated provider outage")
        usage = Usage(input_tokens=1000, output_tokens=50)
        if self.mode == "silent":
            return Reply(None, "I will not call the tool.", "end_turn", usage)
        if self.mode == "invalid":
            return Reply("{}", "", "tool_use", usage, tool_name=TOOL_NAME, tool_args={"action_json": "{}"})
        action = self.script[self.turn] if self.turn < len(self.script) else '{"op":"finish"}'
        self.turn += 1
        return Reply(action, "", "tool_use", usage, tool_name=TOOL_NAME, tool_args={"action_json": action})

    def ask(self):
        return self._reply()

    def answer_tool(self, observation_text):
        return self._reply()
