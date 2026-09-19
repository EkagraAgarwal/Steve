# Steve Operations Runbook

This runbook covers the verified local setup for dimOS, MuJoCo, and the Unitree Go2 Air. Never commit WiFi passwords, TypeSafe keys, Unitree AES keys, or other secrets.

## Current layout

- HackMIT repository: `D:\Steve` on Windows, `/mnt/d/Steve` in WSL.
- dimOS checkout: `/home/ekagr/dimos` in Ubuntu 24.04 WSL2.
- Keep dimOS in the Linux filesystem, not `/mnt/d`, because Git and Unix permission operations can fail on the Windows mount.
- The repository's `dimos/robot/unitree/keyboard_teleop.py` is a reference/upstream patch. It is not automatically copied into `/home/ekagr/dimos`.

## Open the environment

From PowerShell:

```powershell
wsl -d Ubuntu-24.04
```

Inside WSL:

```bash
cd ~/dimos
source .venv/bin/activate
dimos --help
```

## WSL GPU rendering

Armoury Crate must expose the RX 7700S by using Standard, Optimized, or Ultimate GPU mode. Eco mode disables the discrete GPU.

The following settings are already in `~/.bashrc`:

```bash
export GALLIUM_DRIVER=d3d12
export MESA_D3D12_DEFAULT_ADAPTER_NAME="AMD Radeon RX 7700S"
```

Verify after changing GPU mode or restarting WSL:

```bash
source ~/.bashrc
glxinfo -B
```

Expected output includes `D3D12 (AMD Radeon RX 7700S)` and `Accelerated: yes`. MuJoCo physics is primarily CPU-based; this accelerates rendering.

## Go2 network setup

- The Go2 must use WiFi/STA mode, not AP-only mode, so it joins the same LAN as the laptop.
- A mainland-China Go2 may require the mainland-China Unitree Go2 app for WiFi provisioning.
- The currently observed DHCP address is `192.168.1.101`; it can change. Check the router DHCP table when it stops responding.
- WSL multicast discovery can fail through WSL NAT even when direct WebRTC works.
- The working Unitree AES key must be supplied through `UNITREE_AES_128_KEY`. Keep it in secure local storage and never add it to this repository.

### AP mode direct connection

AP mode was tested successfully in this setup at the robot's direct address. Use it for direct control, setup, or recovery when the Go2 is not joined to the router:

- Robot SSID pattern: `dimair05_xxxxxxxx`
- Robot AP address: `192.168.12.1`
- Obtain the AP password from secure local storage; never add it to this repository or a command committed to shell history.

Connect Windows to the robot SSID using the normal WiFi UI. The laptop may temporarily lose internet access while attached directly to the Go2. Verify the direct link from Windows and WSL:

```powershell
ping 192.168.12.1
```

```bash
ping -c 10 192.168.12.1
```

For direct AP operation, set:

```bash
export ROBOT_IP=192.168.12.1
```

Despite the installed wrapper naming its connection method `LocalSTA`, the AP path at `192.168.12.1` was observed working in this environment. Revalidate after dimOS, Unitree firmware, or network changes.

To return to WiFi/STA mode, use the correct-region Unitree Go2 app while connected to the AP to provision the robot onto the desired WiFi network. Reconnect the laptop to that same network, find the robot's DHCP address, and use that address as `ROBOT_IP`.

Check the link before hardware use:

```bash
ping -c 20 192.168.1.101
```

Require 0% loss and consistently less than 10 ms latency. Observed tests had large spikes (up to 405 ms from Windows), so hardware movement is blocked until the network is stable.

## WebRTC status

- Direct signaling to `192.168.1.101:9991` succeeded.
- ICE connected and the WebRTC data channel verified successfully with the working AES key.
- The other candidate AES key was rejected by the robot.
- A connection test is not permission to move; complete the network and physical safety checks first.

## Simulation-only WASD

This must not contact the physical robot:

First install the repository's WSLg-compatible teleop patch into the separate dimOS checkout:

```bash
dimos stop
cp /mnt/d/Steve/dimos/robot/unitree/keyboard_teleop.py \
  ~/dimos/dimos/robot/unitree/keyboard_teleop.py
~/dimos/.venv/bin/python -m py_compile \
  ~/dimos/dimos/robot/unitree/keyboard_teleop.py
```

The patch detects WSL and runs pygame in a dedicated child process whose main thread owns the window. This avoids the blank `[WARN:COPY MODE]` placeholder produced when WSLg creates pygame from a dimOS worker thread. The same subprocess path remains active on macOS.

```bash
cd ~/dimos
source .venv/bin/activate
dimos --simulation run unitree-go2-webrtc-keyboard-teleop
```

Click the Keyboard Teleop window before pressing keys. The window has to own keyboard focus.

The patched isolated WSLg window worker rendered, stayed alive, emitted inactive zero events, and stopped cleanly. The full simulation and teleop windows opened under WSLg, but simulated motion was not yet verified; logs showed `JointVelocityTask` update timeouts. Do not treat the simulation path as validated until the simulated Go2 visibly moves.

## Physical Go2 WASD

Only continue when all of the following are true:

- Robot is on a clear, level floor with no people or pets nearby.
- Operator is beside the robot with the Unitree app or local stop ready.
- Obstacle avoidance is enabled.
- Ping is stable below 10 ms with 0% loss.
- No other dimOS coordinator is running.

Clean up stale sessions first:

```bash
dimos stop
pgrep -af dimos
```

`pgrep` should print nothing. Then set secrets in the current shell without writing them to the repository:

```bash
# AP mode (verified direct address):
export ROBOT_IP=192.168.12.1

# Or WiFi/STA mode (observed DHCP address; may change):
# export ROBOT_IP=192.168.1.101

export UNITREE_AES_128_KEY='<working key from secure storage>'
```

Run the hardware teleop in the foreground of an interactive WSL terminal:

```bash
dimos --transport lcm --viewer none --obstacle-avoidance run unitree-go2-webrtc-keyboard-teleop
```

Do not launch hardware teleop through a detached automation process. A detached run can hide the pygame window, evade `dimos status`, collide with a second coordinator, and be killed by an execution timeout.

If the window is blank or titled `[WARN:COPY MODE]`, stop dimOS, repeat the patch copy/compile step above, and start one foreground session again. Confirm the installed file contains `_is_wsl`:

```bash
grep -n "_is_wsl" ~/dimos/dimos/robot/unitree/keyboard_teleop.py
```

## Teleop controls

| Key | Action |
|---|---|
| `W` / `S` | Forward / backward |
| `Q` / `E` | Strafe left / right |
| `A` / `D` | Turn left / right |
| `Shift` | Boost, 2x |
| `Ctrl` | Slow mode, 0.5x |
| `Space` | Publish zero velocity |
| `Esc` | Close the teleop window |

Start with `Ctrl` held for slow mode. Releasing movement keys publishes one zero command and then the teleop becomes silent. `Space` is a software zero-velocity command, not a replacement for the Unitree app or a physical/local emergency stop.

## Automatic stand and lie-down behavior

The current dimOS `GO2Connection` has non-obvious lifecycle behavior:

- Startup calls `standup()`, waits three seconds, and calls `balance_stand()`.
- Shutdown calls `liedown()` before disconnecting WebRTC.

The robot lying down after `Ctrl+C`, `dimos stop`, a coordinator conflict, or process teardown is therefore expected with this implementation. It was not caused by a WASD command.

## Avoid duplicate coordinators

Only run one dimOS session at a time. Symptoms of a duplicate include:

- `another Coordinator service is already running`
- `Coordinator already running`
- Missing teleop window while a hidden process still controls the connection

Recovery:

```bash
dimos stop
pgrep -af dimos
```

If `dimos stop` escalates to `SIGKILL`, confirm `pgrep` is empty before reconnecting. Keep clear of the robot during teardown because shutdown invokes `liedown()`.

## Phase 1 JEV router

The repository currently routes text through TypeSafe JEV into an allowlisted typed command. It does not actuate dimOS or the Go2.

```bash
cd /mnt/d/Steve
python -m venv .venv
source .venv/bin/activate
pip install -e .
export TYPESAFE_API_KEY='<key from secure storage>'
steve-route "move forward slowly"
python -m unittest discover -s tests -v
```

Router rejection produces `action=None` and must never actuate. Explicit `stop` is an accepted command. Voice input remains a later phase, and partial speech transcripts must never reach actuation.

## Safe development order

1. Pure JEV router tests.
2. dimOS replay.
3. MuJoCo simulation with visible movement.
4. Supervised hardware teleop after the network gate passes.
5. ElevenLabs committed-transcript integration later.
