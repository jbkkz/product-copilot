# Records the README demo GIF with vhs (https://github.com/charmbracelet/vhs).
#
#   brew install vhs          # or: go install github.com/charmbracelet/vhs@latest
#   vhs demo.tape             # → writes demo.gif
#
# This runs a deterministic pass: it regenerates the discovery brief from the
# saved example model, so the recording is reproducible and needs no typed input
# (only an ANTHROPIC_API_KEY in .env for the one advisory call).

Output demo.gif

Set FontSize 14
Set Width 1200
Set Height 820
Set Padding 20
Set Theme "Dracula"

Type "cat examples/leave-approval/request.md"
Enter
Sleep 2s

Type "python src/engine.py --from examples/leave-approval/model.json"
Enter
Sleep 12s          # covers the advisory API call; bump if your latency is higher

Sleep 4s           # hold on the finished brief
