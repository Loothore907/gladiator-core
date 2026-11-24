# Building a test with an AI assistant

`gladiator mcp` lets an AI assistant build a test for your own flow. It is the third way to walk the same path
(docs/PRODUCT.md, section 4a): the build page and `gladiator chat` ask you one question at a time; here your
assistant leads. It reads what a library test needs, collects your values in whatever way suits you (asking you,
or reading documents and systems you point it to), checks the design with the engine, and has the engine create
the sealed, validated test.

It speaks the Model Context Protocol (MCP), runs on your own machine, calls no model and no network, and needs
no API key. Your assistant's own model does the reading. Nothing you tell it leaves your machine through this
server, and the test it creates is written into your copy of this repository.

Do this once first, in a terminal, from the repository folder:

```bash
uv sync
```

## Claude Code
**Simplest: open the repository folder.** The file `.mcp.json` at the root registers the server. The first time,
Claude Code asks you to approve it; say yes. Then check it:

```bash
claude mcp list
```
It should show `gladiator` as connected. Inside a session, `/mcp` shows the same, with the number of tools.

**From any other folder,** register it yourself (use the full path to your copy of the repository):

```bash
claude mcp add --transport stdio gladiator -- uv run --directory /full/path/to/gladiator-core gladiator mcp
```

## Claude desktop app
Add this to the app's `claude_desktop_config.json`, using the full path to your copy of the repository, then quit
the app completely and open it again.

```json
{
  "mcpServers": {
    "gladiator": {
      "command": "uv",
      "args": ["run", "--directory", "/full/path/to/gladiator-core", "gladiator", "mcp"]
    }
  }
}
```
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`. Write the path with forward slashes or doubled
  backslashes, for example `C:/Users/you/gladiator-core`.
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`.

If the app cannot find `uv`, put the full path to `uv` in `"command"` (find it with `where uv` on Windows or
`which uv` on macOS and Linux).

## Any other MCP client
Start the server as a subprocess on stdio with: `uv run --directory /full/path/to/gladiator-core gladiator mcp`.

## Try it
Ask in plain words, for example:

> I want to test whether a model can run our refund desk. We refund within 30 days, VIP customers get 45, and a
> refund never pays out more than 250. Build me a Gladiator test for that.

A good session finds the refund test, tells you what it covers and what it has no choice for, asks about any
choice your words did not settle, shows you the rules a model will be told with your values in them, and creates
the test only after you say yes. It then tells you what the engine's checks found, where the files are, and the
command that runs a match on your test.

## What the assistant can do

| Tool | What it answers |
|---|---|
| `list_tests` | Every current test in the library, what each tests, its trust status and whether it can be adapted. With your description, the tests that share words with it come first. |
| `read_requirements` | Everything needed to design a test from one library test: the job and rules a model is told, the tools the agent gets, every open choice (the question to ask, its type, unit, allowed range and the library's value), the rules between choices, a JSON Schema for the values, and what is fixed. |
| `check_design` | Checks values without creating anything: what is wrong with each, which do not fit together, which choices stay as the library has them, and the rules with these values. |
| `create_test` | Validates, seals and writes the test into your checkout as `owner/name@version`: the package under `challenges/`, its validation record under `validations/` and a one-test suite under `suites/`. Nothing is written unless validation passes. |
| `get_result` | The checks take a minute or two. While they run the answer is "checking"; this asks again. |

## The rules it is given

The server sends the assistant its instructions with the connection. In short:

- **Declared choices only.** Everything that shapes a test is a choice the library test declares, checked by the
  engine against its allowed range. A value for anything else is refused. If your flow needs something no choice
  covers (a different decision, another record, an approval step), the assistant is told to say that this test
  cannot express it, and never to claim the created test covers it.
- **No guessed values.** A choice you have not settled stays as the library has it, and the assistant says so.
- **You see the rules first.** Nothing is created until you have seen what a model will be told and said yes.
- **A created test is unreviewed.** It passed automatic validation and is sealed; no person has reviewed it.
  Reports name that status wherever the test is used.

These are instructions to a model, so treat them as strong guidance, not as a lock. The lock is the engine:
whatever an assistant sends, only declared choices within their ranges can reach a test, and only a test that
passes validation is written.

## For programs

`engine.assistant.Designs` is the same five calls as plain data in and out, with no transport in it:

```python
from engine.assistant import Designs

designs = Designs()
needs = designs.requirements("gladiator/refund-queue@2.3.0")
look = designs.check("gladiator/refund-queue@2.3.0", {"refund_window_days": 30})
made = designs.create("gladiator/refund-queue@2.3.0", {"refund_window_days": 30}, owner="acme", name="Returns desk")
```

A hosted connection (the same calls behind an address, with a key for each organisation and a private place for
its tests) is not built yet.
