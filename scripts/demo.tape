# Render the Loop Breaker demo to demo.gif (headless — no live terminal needed):
#   brew install vhs        # one-time (pulls ffmpeg + ttyd)
#   vhs scripts/demo.tape   # run from the repo root -> writes demo.gif
#
# vhs docs: https://github.com/charmbracelet/vhs
Output demo.gif

Set Shell bash
Set FontSize 18
Set Width 1200
Set Height 860
Set Padding 20
Set Theme "Dracula"

Type "DEMO_SLEEP=0.25 bash scripts/demo.sh"
Sleep 400ms
Enter
Sleep 12s
