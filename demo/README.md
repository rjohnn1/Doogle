# Terminal demo

`demo.tape` is a [VHS](https://github.com/charmbracelet/vhs) script that records
`doogle ask` -> `doogle explain` -> `doogle plan` for the README. It runs only offline,
read-only commands: no cloud calls, no `--apply`, no credentials needed, and it points
`DOOGLE_STATE_DIR` at a temporary folder.

## Render the GIF

1. Install VHS and its dependencies (`ttyd` and `ffmpeg`), for example:
   * macOS: `brew install vhs`
   * Linux: see the [VHS install instructions](https://github.com/charmbracelet/vhs#installation)
     (packages, `go install github.com/charmbracelet/vhs@latest`, or the Docker image)
2. Install Doogle so `doogle` is on your PATH (`pip install -e .` from the repo root).
3. From the repository root:

   ```bash
   vhs demo/demo.tape          # writes demo/doogle-demo.gif
   ```

4. Check the GIF, keep it under about 5 MB, and reference it from README.md.

Re-render whenever command output changes noticeably (new blueprints, renamed commands). Keep it
honest: never edit frames to show output the CLI does not produce.
