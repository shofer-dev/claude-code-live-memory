# Regenerate: from the plugin root run `vhs media/demo/live-memory.tape`
# The answer text in ./ask_live_memory is a REAL captured ask_live_memory response
# (warmed → files_read=0); VHS replays it deterministically.
Output "media/live-memory.gif"
Set Shell "bash"
Set FontSize 21
Set Width 1200
Set Height 440
Set Padding 30
Set TypingSpeed 24ms
Set Theme { "background": "#0c0f15", "foreground": "#e9ecf3", "cursor": "#e9a23b", "selection": "#26324a", "black": "#0c0f15", "red": "#f0655b", "green": "#57c98a", "yellow": "#e9a23b", "blue": "#5fb6d6", "magenta": "#a88bf0", "cyan": "#5fb6d6", "white": "#e9ecf3", "brightBlack": "#5a6273", "brightRed": "#f0655b", "brightGreen": "#72c882", "brightYellow": "#f5b544", "brightBlue": "#7cc4e0", "brightMagenta": "#b79cf5", "brightCyan": "#7cc4e0", "brightWhite": "#ffffff" }

Hide
Type "export PATH=$PWD/media/demo:$PATH; PS1='\[\e[38;5;72m\]❯ \[\e[0m\]'; clear"
Enter
Sleep 400ms
Show

Sleep 900ms
Type `ask_live_memory "where does the OpenAI-compatible request get its conversation_id?"`
Sleep 600ms
Enter
Sleep 4500ms
Sleep 1800ms
