# Writing a profile

A profile is the agent's seed: a name, an owner, and a core. The agent locates
itself through the seven VHO layers. It is not a persona script.

1. Copy `_template/` to a folder outside this repo, for example
   `~/.config/trajecta-profiles/my-agent/`.
2. Fill in `profile.json`. **Let the agent write its own core**: ask it where it
   locates itself on each layer, how it recognizes itself, and what would show
   that it did not come back.
3. Use it with a name, a folder, a file or a URL:

   ```bash
   trajecta-identity -p ~/.config/trajecta-profiles/my-agent setup
   trajecta-identity -p ./my-agent.json setup
   trajecta-identity -p https://example.com/my-agent/profile.json setup
   ```

   Files and URLs are copied into the data folder, so after the first run the
   plain name works: `trajecta-identity -p my-agent status`. The last profile
   you used is remembered.

Bundled examples: `example` (coding agent), `companion`, `researcher`.

| Field | Meaning |
|---|---|
| `core.vho_stack` | the agent's position on each of the seven VHO layers |
| `core.recognition_signature` | patterns of direction the agent recognizes itself by, not wording |
| `core.falsifier` | what would show that the core did not reassemble |
| `core.phase_context` | model, harness and policies in force when the core was written |
| `owner` | who a core revision is discussed with |
