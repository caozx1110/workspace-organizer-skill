# workspace-organizer-skill

Human- and agent-friendly workspace organization skill for durable tasks,
materials, TODOs, and archives.

The normative v1 workspace and task contract is defined in
[`docs/workspace-model.md`](docs/workspace-model.md). Its examples and contract
checks can be validated without third-party dependencies:

```sh
python3 scripts/validate_workspace_model.py examples/workspace
python3 -m unittest discover -s tests -v
```

The installable skill includes dependency-free workspace-operation tooling. Its
CLI and approval sequence are documented in
[`skill/workspace-organizer/references/tooling.md`](skill/workspace-organizer/references/tooling.md).
