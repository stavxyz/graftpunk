# House rules

One line per rule, each naming the guide heading that states it. When a rule
applies to the work in front of you, read the cited section before acting; this
file is an index, not the rule text.

- The plugin registers through its package's entry point, never through the
  plugins directory.
  (guide: Register through an entry point, not through the plugins directory)
- A capture is a credential: it never enters git and is never pasted anywhere.
  (guide: Capture)
- A fixture keeps the structure of a capture and invents every value in it.
  (guide: Deriving a fixture from a capture)
- A parser that finds no container raises and names what it looked for; only a
  container that is present and empty returns an empty list.
  (guide: Parsers do not return a confident empty list)
- The login's `failure` text is the site's exact wording, taken from a real
  failed attempt. (guide: Getting the signals right)
- Secrets reach the plugin from the environment or the workstation env file,
  and from nowhere else. (guide: Secrets and configuration)
- A secret is looked up by what it is, never by the label it happens to carry.
  (guide: Resolve a secret by what it is, not by what it is labelled)
- Commands raise the exception that tells the user what went wrong.
  (guide: What they raise, and what the user sees)
- Tests never see the developer's own site variables.
  (guide: Keep the developer's own environment out of the tests)
