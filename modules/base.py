"""The interface every test module implements.

A module is one self-contained test. The runner creates a FRESH instance for
each contestant on each node, so no state ever leaks between models.

Rules:
  - start() returns the PUBLIC packet: everything the model is allowed to see.
  - step() receives the model's action (a JSON string) and returns the
    simulator's observation (a dict). The model sees that observation.
  - grade() is operator-only. It can look at private answers.
  - Private references (answer keys, hidden faults) must never be returned
    from start() or step().
"""

from dataclasses import dataclass, field

from providers.base import SUBMIT_ACTION, TOOL_NAME, ToolSpec


@dataclass
class Grade:
    passed: bool  # the module's own overall verdict
    task_correct: bool | None  # did the end result come out right?
    workflow_correct: bool | None  # did the model follow a valid process? None = not graded separately
    details: dict = field(default_factory=dict)  # every raw grading field, for the record


class Module:
    #: Hard cap on actions per session (the runner stops after this many).
    max_actions: int = 10

    def start(self) -> dict:
        raise NotImplementedError

    def step(self, action_json: str) -> dict:
        raise NotImplementedError

    def tools(self) -> list[ToolSpec]:
        """The named tools this test offers the model. Default: the single generic submit_action tool."""
        return [SUBMIT_ACTION]

    def act(self, tool_name: str, tool_args: dict | None, action_json: str) -> dict:
        """Apply one tool call and return the observation. Raise ValueError for a call the test can't accept
        (unknown tool, malformed arguments); the runner ends the session as an invalid action.

        Default: submit_action goes to step() (v1 simulators). Multi-tool test types override this.
        """
        if tool_name != TOOL_NAME:
            raise ValueError(f"unknown tool '{tool_name}'")
        return self.step(action_json)

    @property
    def finished(self) -> bool:
        """True once the session is over (model finished or the simulator closed it)."""
        raise NotImplementedError

    @property
    def last_action_valid(self) -> bool:
        """False if the last action was malformed. The runner stops the session then (v1 rule)."""
        return True

    def grade(self) -> Grade:
        raise NotImplementedError

    def operator_record(self) -> dict:
        """Full private record for the operator file. Never shown to contestants."""
        return {}

    def fingerprint(self) -> str:
        """Hash identifying the exact module code + inputs. Goes into the run record."""
        return ""
