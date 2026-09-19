# AGENTS.md - HackMIT voice-to-Go2 (TARGET architecture, not yet implemented)

- Project: HackMIT build targeting Dimensional OS (dimOS) on Unitree Go2; speech via ElevenLabs, low-latency typed routing via TypeSafe AI JEV.
- Repo state: no source, manifests, scripts, or CI exist yet. Do not invent repo commands; update this file once manifests land.
- Keep repo text ASCII unless an existing file clearly requires Unicode.

## Target pipeline (planned)

- `mic -> ElevenLabs Scribe v2 Realtime -> committed transcript ONLY -> JEV route/score -> allowlisted typed command -> dimOS skill/MCP -> Go2`. Nothing here is built; treat as proposal.
- Never actuate on partial STT transcripts; committed events only. Physical/local e-stop must not depend on cloud APIs or JEV.

## dimOS / Go2 (verify flags against docs before use)

- dimOS is Python, agent-native, no ROS. Order: replay, then simulation, then real hardware.
- Quickstart ships `install.sh`; runs: `dimos --replay run unitree-go2`, `dimos --simulation run unitree-go2`, `dimos run unitree-go2` (+ `ROBOT_IP`, verified env name).
- Go2: stock Pro/Air firmware 1.1.7+, WebRTC, no jailbreak. Ping <10ms before hardware; keep obstacle avoidance enabled.

## Speech / routing contracts (verified 2026-09-19; recheck current docs before use)

- ElevenLabs Scribe v2 Realtime: websocket `wss://api.elevenlabs.io/v1/speech-to-text/realtime`, PCM 16k chunks, partial + committed events. API keys server-side only.
- JEV (TypeSafe AI Jev): `POST https://api.typesafe.ai/v1/systemone`, IDs `jev-latest` / `jev-1.13.0`. Consumes text, emits typed decisions - not audio, not prose. SDKs: `typesafe-sdk` (Python), `@typesafe-ai/sdk` (JS); never the generic npm `jev`.

## Secrets / testing

- `ROBOT_IP` (verified) + `ELEVENLABS_API_KEY` expected via env; TypeSafe key env name unverified - do not hardcode a `TYPESAFE_*` name as required, confirm from official docs/SDK first.
- No project test command exists. For hardware-affecting work validate replay -> simulation -> hardware; once code lands keep focused pure tests on transcript-to-command mapping only.

## Docs

- https://docs.dimensional.org/quickstart/ | https://docs.dimensional.org/platforms/quadruped/go2/ | https://typesafe.ai/ (blog/docs) | https://elevenlabs.io/docs (STT Realtime)
