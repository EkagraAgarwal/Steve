# Unitree Go2 MuJoCo Simulation + WASD Keyboard Teleop (macOS)

WSL2/WSLg users should follow `instructions.md` instead. The repository teleop patch detects WSL and moves pygame to a dedicated child-process main thread; without that patch, WSLg can show a blank `[WARN:COPY MODE]` window.

How to reproduce, exactly, the local setup that's currently working: the full
Go2 "smart" navigation blueprint running against a simulated robot in
MuJoCo, driven by a WASD keyboard window. No physical robot needed.

Verified on: macOS (Apple Silicon), Python 3.12, dev checkout.

## 1. Prerequisites

- macOS 14+ (Apple Silicon)
- [Homebrew](https://brew.sh)
- `git` + `git-lfs`

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
eval "$(/opt/homebrew/bin/brew shellenv)"
brew install gnu-sed gcc portaudio git-lfs libjpeg-turbo pre-commit ffmpeg libsndfile pkg-config
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
```

## 2. Clone and check out the branch

```bash
GIT_LFS_SKIP_SMUDGE=1 git clone https://github.com/dimensionalOS/dimos.git
cd dimos
git fetch origin fix/keyboard-teleop-macos-main-thread
git checkout fix/keyboard-teleop-macos-main-thread
```

(Swap the fetch URL for wherever the branch actually ends up — a fork or a
PR branch on `origin`.)

## 3. Install dependencies

This mirrors the environment that's currently working (`manipulation` pulls
in `sim` + `base` + `cpu`; `unitree` adds the Go2 WebRTC/DDS support):

```bash
uv sync --locked --python 3.12 --extra manipulation --extra unitree --extra cpu --group tests --group lint
source .venv/bin/activate
uv run --no-sync dimos --help
```

This installs, among other things: `mujoco`, `playground`, `pygame`
(simulation), and `unitree-webrtc-connect` (Go2 support). `mjpython` (needed
for the MuJoCo viewer on macOS) ships with the `mujoco` package itself —
no separate install step.

No `ROBOT_IP` or robot hardware is needed for simulation.

## 4. Run it

```bash
dimos --simulation mujoco --viewer none run unitree-go2 keyboard-teleop
```

What this does:
- `--simulation mujoco` — runs the full `unitree-go2` navigation stack
  (mapping, planning, exploration — same code as the real robot) against a
  simulated Go2 in MuJoCo instead of hardware.
- `keyboard-teleop` — composes in the `KeyboardTeleop` module, which
  auto-wires its `Twist` output into the stack's `cmd_vel`.
- `--viewer none` — skips the extra native Rerun 3D viewer window. The web
  command center below is unaffected by this flag.

The first run downloads a small MuJoCo scene/asset bundle via Git LFS
automatically (office scene, robot model, locomotion policy) — no manual
`git lfs pull` needed.

On startup you should see, in order: modules deploying, a MuJoCo viewer
window open (via `mjpython`, its own process), and a small **Keyboard
Teleop** control window (also its own process on macOS) titled "Keyboard
Teleop". A couple of harmless `objc[...]: Class GLFWHelper is implemented in
both ...` warnings are expected (duplicate GLFW symbols between two loaded
libs) — ignore them.

Also opens the command center at [http://localhost:7779](http://localhost:7779)
(Rerun-in-browser 3D map/vis), independent of `--viewer`.

## 5. Drive it

**Click into the small "Keyboard Teleop" window first** — it needs OS
keyboard focus to receive key events.

| Key(s) | Action |
|---|---|
| `W` / `S` | Forward / backward |
| `A` / `D` | Turn left / right |
| `Q` / `E` | Strafe left / right |
| `Shift` (hold) | Speed boost (2x) |
| `Ctrl` (hold) | Slow mode (0.5x) |
| `Space` | Emergency stop |
| `Esc` | Quit the teleop window |

The window shows live linear/angular velocity and which keys are currently
held, as a sanity check.

## 6. Stop

`Ctrl+C` in the terminal running `dimos run`, or from another terminal:

```bash
dimos stop
```

## Troubleshooting

- **App crashes immediately with an `NSInternalInconsistencyException` /
  Cocoa main-thread error** — you're not on the fix branch (Step 0/2).
- **Teleop window opens but WASD does nothing** — the window doesn't have
  OS focus; click on it.
- **`mjpython` errors / no MuJoCo window** — confirm `.venv/bin/mjpython`
  exists (`ls .venv/bin | grep mjpython`); it's installed by the `mujoco`
  PyPI package as part of Step 3, not a separate download.
