# Records the README demo GIF with vhs (https://github.com/charmbracelet/vhs).
#
#   brew install vhs          # or: go install github.com/charmbracelet/vhs@latest
#   vhs demo.tape             # → writes demo.gif
#
# This is dependency-free on purpose: it shows the input request and the
# resulting discovery brief straight from the committed example files — no
# Python, virtualenv, or API call — so it records identically on any machine.
# (To re-generate the brief itself: python src/engine.py --from examples/leave-approval/model.json)

Output demo.gif

Set Shell "bash"
Set FontSize 13
Set Width 1200
Set Height 1000
Set Padding 20
Set Theme "Dracula"

Type "cat examples/leave-approval/request.md"
Enter
Sleep 2s

Type "cat examples/leave-approval/discovery-brief.txt"
Enter
Sleep 6s
