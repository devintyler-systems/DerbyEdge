# Working rules for sessions (read with CLAUDE.md and session_state.md)

## Local-update alert (the user runs DerbyEdge on their own Windows machine)
Whenever work in a session changes anything the user's machine needs (code, docs the app reads, schema, scripts, `.gitignore`),
the final message MUST start a clearly labelled block **UPDATE YOUR LOCAL FILES** with copy-paste steps. If nothing local is
needed, say "No local update needed." Never leave a pull, restart, review or merge implied.

The block contains, in order:
1. `cd C:\Projects\derbyedge-engine`, then `git status`: if tracked files are modified, stop and report them (untracked files are fine).
2. `git fetch origin`, `git checkout <branch>` (if not already on it), `git pull origin <branch>`, then `git log --oneline -1` and the
   commit it should start with.
3. Restart the app (close it, reopen the desktop **DerbyEdge** shortcut): Streamlit keeps old code loaded until restarted.
4. A verification command, and what its output should look like (and which wrong outputs mean what).
5. What to send back.

Command rules (PowerShell): no escaped inner double quotes; use `python -c "..."` with single quotes inside, or write a small
script file. State the expected output. Commands that read the database must be read-only (`?mode=ro`).

Manual merges, reviews and pushes get the same treatment: exact clicks or commands, in order, with the expected result.
