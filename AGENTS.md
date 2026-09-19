# AGENTS.md - HackMIT voice-to-Go2 (Phase 1 router implemented, not yet actuating)

- Project: HackMIT build targeting Dimensional OS (dimOS) on Unitree Go2; speech via ElevenLabs, low-latency typed routing via TypeSafe AI JEV.
- Repo state: Phase 1 package `steve_router/` (`commands.py` schema/motion, `router.py` JEV routing, `cli.py` JSON CLI) plus `tests/test_router.py` (stdlib unittest, fake client). Manifest: `pyproject.toml` (setuptools, `steve-router`, `typesafe-sdk`, script `steve-route`). Install: `pip install -e .`. Test: `python -m unittest discover -s tests -v`.
- Priority: Phase 1 is JEV text-to-typed-command routing first; ElevenLabs/microphone voice integration comes later.
- Keep repo text ASCII unless an existing file clearly requires Unicode.

## Target pipeline (Phase 1 built, actuation pending)

- `mic -> ElevenLabs Scribe v2 Realtime -> committed transcript ONLY -> JEV route/score -> allowlisted typed command -> dimOS skill/MCP -> Go2`. Built: text -> JEV -> typed command + pure tests. Not built: dimOS actuation, voice/mic.
- Never actuate on partial STT transcripts; committed events only. Physical/local e-stop must not depend on cloud APIs or JEV.
- Router reject (`action=None`) must never actuate; explicit stop is an accepted `stop`. Do not edit `dimos/robot/unitree/keyboard_teleop.py` teleop semantics (lx +0.5 fwd, ly +0.5 strafe-left, az +0.8 turn-left; boost x2, slow x0.5; stop exact zero).

## dimOS / Go2 (verify flags against docs before use)

- dimOS is Python, agent-native, no ROS. Order: replay, then simulation, then real hardware.
- Quickstart ships `install.sh`; runs: `dimos --replay run unitree-go2`, `dimos --simulation run unitree-go2`, `dimos run unitree-go2` (+ `ROBOT_IP`, verified env name).
- Go2: stock Pro/Air firmware 1.1.7+, WebRTC, no jailbreak. AP mode was tested at `192.168.12.1`; WiFi/STA used DHCP `192.168.1.101` in this setup and may change. Ping <10ms before hardware; keep obstacle avoidance enabled. Never commit AP/WiFi passwords or AES keys.
- Incoming `dimos/` file in this repo is a patch/reference for teleop semantics, not the installed `/home/ekagr/dimos` checkout unless explicitly copied/upstreamed.
- WSLg pygame must use the patch's subprocess path (`_is_wsl`); the old installed worker-thread implementation opens a blank `[WARN:COPY MODE]` window. Copy only `dimos/robot/unitree/keyboard_teleop.py` into `/home/ekagr/dimos` when explicitly updating the local install, then compile it. Run hardware teleop in one foreground terminal only; detached/duplicate coordinators can hide the window. Current `GO2Connection` auto-stands on start and calls `liedown()` on stop. Full commands and recovery: `instructions.md`.

## Speech / routing contracts (verified 2026-09-19; recheck current docs before use)

- ElevenLabs Scribe v2 Realtime: websocket `wss://api.elevenlabs.io/v1/speech-to-text/realtime`, PCM 16k chunks, partial + committed events. API keys server-side only.
- JEV (TypeSafe AI Jev): `POST https://api.typesafe.ai/v1/systemone`, IDs `jev-latest` / `jev-1.13.0`. Consumes text, emits typed decisions - not audio, not prose. SDKs: `typesafe-sdk` (Python), `@typesafe-ai/sdk` (JS); never the generic npm `jev`.
- Python SDK surface used by `steve_router/`: package `typesafe-sdk`, `from typesafe_sdk import Choice, TypeSafeClient`, `Choice(criteria={...}, instructions=...)`, one `client.system_one(state, questions)` call with `command` + `speed_mode` questions, response `response.choices[id].choice` / `.confidence`. Endpoint/model handled by SDK, default `jev-latest`. Python >=3.10.

## Secrets / testing

- `ROBOT_IP` (verified) + `ELEVENLABS_API_KEY` expected via env; TypeSafe key is documented `TYPESAFE_API_KEY` (read from env by the SDK; `.env.example` template only).
- Test command: `python -m unittest discover -s tests -v` (stdlib only, fake JEV client; no network, key, or hardware). For hardware-affecting work validate replay -> simulation -> hardware; keep focused pure tests on transcript-to-command mapping only.

## Docs

- https://docs.dimensional.org/quickstart/ | https://docs.dimensional.org/platforms/quadruped/go2/ | https://typesafe.ai/ (blog/docs) | https://elevenlabs.io/docs (STT Realtime)
