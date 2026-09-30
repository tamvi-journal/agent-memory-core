<div align="center">
  <img src="assets/mark.svg" width="92" alt="Trajecta Identity Memory mark" />

  # Trajecta Identity Memory

  **Memory evolves. History remains.**

  Identity memory for AI agents. The agent locates itself through the
  Vector-Human Ontology,<br>writes its own phases, and comes back in the same
  direction after every reset.

  [![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-69d9f0?style=for-the-badge&logo=python&logoColor=white)](#quick-start)
  [![Status: Alpha](https://img.shields.io/badge/status-alpha-a77cff?style=for-the-badge)](#status)
  [![macOS · Linux · Windows](https://img.shields.io/badge/macOS_·_Linux_·_Windows-62d8d8?style=for-the-badge)](#quick-start)
  [![Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-f0a96a?style=for-the-badge)](LICENSE)
</div>

<img src="assets/memory-constellation-hero.png" width="100%" alt="A crystalline memory core surrounded by an evolving graph of luminous memory nodes" />

## Quick start

```bash
pipx install git+https://github.com/tamvi-journal/trajecta-identity-memory
trajecta-identity -p example setup
```

`setup` bootstraps the profile and prints an MCP config block. Paste it into
your agent client (Claude, Codex, Cursor…). From then on the agent can recall
who it is and log its own phases.

A profile can be a bundled name (`example`, `companion`, `researcher`), a
folder, a `.json` file, or a URL. The last one you used is remembered. See
[writing a profile](trajecta_identity/profiles/SETUP.md).

From a checkout: `install/install.sh example` (Windows:
`install\install.ps1 -AgentProfile example`).

### As a Claude plugin (nothing to paste)

```bash
trajecta-identity -p example plugin --out dist/
# or, with data kept elsewhere:
trajecta-identity -p aux plugin --house ~/my-house --out dist/
```

This writes `dist/trajecta-identity-<profile>.plugin`. Install it once in
Claude and the memory server starts with Claude from then on. The plugin
carries its own copy of the code and a launcher in `scripts/` that finds
Python 3.10+ by itself. `--target windows` builds a Python launcher instead of
a shell one. Rebuild after pulling a new version.

## Two memories

An agent needs two kinds of memory:

| | Question | Package |
|---|---|---|
| **Identity** | Who am I, how do I recognize myself, what phases have I been through? | **this repo** |
| **Work** | What are we doing, what changed, where do we resume? | [`trajecta-work-memory`](https://github.com/tamvi-journal/trajecta-work-memory) |

Identity points to work (`work_refs`) and never copies it. Set
`TRAJECTA_WORK_ROOT` (or `work_root` in a profile) to a trajecta-work-memory
store, and identity will check that each ref exists and show the referenced
work (topic, status, next action) next to the memories that point to it. The
bridge is read-only: identity never writes to work. `trajecta-identity work`
lists the work items it can see.

## What is inside

- **A core that is self-location, not a persona.** The core is the agent's
  position across the seven VHO layers (substrate × runtime × policy × memory
  × identity schema × environment × relational field), plus a recognition
  signature and a falsifier. See the
  [Vector-Human Ontology](docs/VECTOR-HUMAN-ONTOLOGY.md).
- **The agent writes its own memory.**

  | Tier | Write | Overwrite |
  |---|---|---|
  | phase | freely | never. A new reading is a new phase, linked to the earlier one |
  | fact | freely | freely. The old revision stays as history |
  | core | freely | freely, but it opens a discussion with the owner that shows until it is closed |

  Every core revision records its phase context (model, harness, policies in
  force), so a later phase can tell the agent from the policy that was
  pressing on it.
- **Recall works the way people remember.** A cue triggers a behavior, and the
  behavior rebuilds the shape. Recall raises activation with a capped,
  diminishing gain. Unused memories fade and go **dormant**: search skips a
  dormant memory, but its own cue or a causal edge wakes it. The core never
  fades.
- **Ego guard.** Recalling a self-description again never makes it "truer".
  Each packet reports how much of it is self-authored.
- **A provenance kernel.** Every claim carries evidence, and every revision is
  immutable. See the [kernel docs](docs/KERNEL.md).

## Use

```bash
trajecta-identity --profile example retrieve "who are you" --packet
trajecta-identity --profile example log-phase first-light \
  --title "First session" --summary "Started with the example core"
trajecta-identity --profile example timeline
trajecta-identity --profile example decay          # daily
trajecta-identity --profile example view           # read-only web view on localhost
```

`view` opens a local page with the core, open proposals, legacy discussions and loops, every
memory with its activation state (pinned, active, fading, dormant), the phase
timeline, and a read-only recall box. Recall from the page never changes
activation.

MCP tools: `identity_status`, `identity_retrieve`, `identity_log_phase`,
`identity_log_fact`, `identity_core_propose`, `identity_core_proposals`,
`identity_core_apply`, `identity_retract`, `identity_close_legacy_discussion`,
`identity_close_loop`, `identity_timeline`. Owner receipts are issued only by
the interactive CLI. The skill that teaches an agent to
use them is [`skills/identity-continuity`](skills/identity-continuity/SKILL.md).

## Profiles

A profile is the agent's seed: its name, its owner, and its core.

- `example`, `companion` and `researcher` are bundled and work immediately.
  `trajecta-identity profiles` lists what is available.
- To write your own, copy `trajecta_identity/profiles/_template/`. The template
  refuses to load until the core is filled in, **in the agent's own words**.
  See [SETUP.md](trajecta_identity/profiles/SETUP.md).
- Keep private profiles outside the repo, in `$TRAJECTA_IDENTITY_PROFILES` or
  `<data dir>/profiles/`.

Memory lives only on your machine:

| OS | Location |
|---|---|
| macOS | `~/Library/Application Support/Trajecta Identity Memory/` |
| Linux | `$XDG_DATA_HOME/trajecta-identity-memory/` (or `~/.local/share/…`) |
| Windows | `%LOCALAPPDATA%\Trajecta Identity Memory\` |

## Status

Alpha. CI runs on macOS, Linux and Windows with Python 3.10 and 3.13. Design
notes are in [`docs/SPEC-v0.1.md`](docs/SPEC-v0.1.md).

## License

[Apache License 2.0](LICENSE).

---

<div align="center">
  <sub>Built for agents that should be able to change their minds without changing their past.</sub>
</div>
