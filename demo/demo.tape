# Doogle terminal demo, recorded with charmbracelet VHS (https://github.com/charmbracelet/vhs).
# It only runs offline, read-only commands (ask -> explain -> plan). No cloud calls, no --apply.
# Render from the repository root:  vhs demo/demo.tape

Output demo/doogle-demo.gif

Require doogle

Set Shell "bash"
Set FontSize 15
Set Width 1280
Set Height 760
Set Padding 20
Set TypingSpeed 35ms
Set WindowBar Colorful

Hide
Type "export PS1='$ ' DOOGLE_STATE_DIR=$(mktemp -d)" Enter
Type "clear" Enter
Show

Type `doogle ask "add login and a database to my react web app"`
Sleep 400ms
Enter
Sleep 4s

Type "clear" Enter
Type "doogle explain firebase-auth-mvp | head -32"
Sleep 400ms
Enter
Sleep 7s

Type "clear" Enter
Type "doogle plan firebase-auth-mvp --var project_id=demo-mvp-123 | head -42"
Sleep 400ms
Enter
Sleep 9s
