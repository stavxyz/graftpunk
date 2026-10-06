# Handing the recording to the user

The recording needs a person at the keyboard: logging in, clicking through
pages, and ending the recorder. The skill prints the command, says what to do,
and waits. It never runs the recorder itself.

## What to print

In enhance mode, run `gp session list` first. Print the recorder line from the
"Run by the user" block of `references/commands.md`, with the site's URL in
place of `<url>`: in enhance mode, the `-s <session>` line when that list shows
a session for the plugin, so the recording starts already logged in; otherwise,
and always in create mode, the `--no-session` line.

Tell the user to run that line in a separate terminal window, not with the
`!` prefix in this session: ending the recorder takes a Ctrl+C that reaches it,
and a terminal of its own always delivers one. In that terminal they log in,
work through every item on the list below, and press Ctrl+C, which ends the
recorder and saves what it captured. Then they come back here and say it is
done.

## What to exercise

Turn each want the user named in the frame step into something to click, and
list those for them before they start. Useful prompts:

- Log in the ordinary way, even when you expect the session to be remembered.
- For each list you want as a command: open it, go to the next page, and apply
  one filter or sort you would use from the shell.
- For each detail you want: open two different items, so the digest can tell
  the fixed part of the path from the identifier.
- For each download: start it once and let it finish.
- Skip everything else. Unrelated pages add endpoints to read and nothing to
  use.

## After the recording

This is where the recording is chosen, once. When the user says they are done,
run `gp observe list` and take the newest run under the name the recording was
stored under: after the `--no-session` line, the name graftpunk inferred from
the host; after the `-s <session>` line, that session's stored name as
`gp observe list` shows it. Say the session name and the run ID back; the user
may name another run instead. Every later step reads that session and that run,
never whichever run is newest by then. A want that the digest later shows no
endpoint for gets a second, narrower recording aimed at that one flow, and that
recording is chosen the same way.
