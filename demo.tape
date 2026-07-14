# Records the README demo GIF with vhs (https://github.com/charmbracelet/vhs).
#
#   brew install vhs          # or: go install github.com/charmbracelet/vhs@latest
#   vhs demo.tape             # → writes demo.gif
#
# vhs runs in a fresh shell, so we activate the virtualenv first (otherwise
# `python` isn't found / the deps aren't on the path). Assumes you've already run
# the Quickstart: python -m venv .venv && pip install -r requirements.txt, and
# set ANTHROPIC_API_KEY in .env. Deterministic pass — regenerates the brief from
# the saved example model, no typed input needed.

Output demo.gif

Set FontSize 13
Set Width 1200
Set Height 1000
Set Padding 20
Set Theme "Dracula"

Hide
Type "source .venv/bin/activate" Enter
Sleep 1s
Show

Type "cat examples/leave-approval/request.md"
Enter
Sleep 2s

Type "python src/engine.py --from examples/leave-approval/model.json"
Enter
Sleep 15s          # covers the advisory API call; bump if your latency is higher

Sleep 4s           # hold on the finished brief
