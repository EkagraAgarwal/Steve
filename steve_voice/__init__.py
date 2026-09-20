"""Push-to-talk voice input via Deepgram Flux (no actuation).

Deepgram turns finalized speech into text. Optional JEV routing of the
finalized transcript lives in the peer-owned ``steve_voice.commands``
module; this package never actuates hardware.
"""

from steve_voice.deepgram import (
    CHANNELS,
    ENC,
    EOT_THRESHOLD,
    FLUX_URL,
    FRAME_BYTES,
    FRAME_MS,
    MODEL,
    QUEUE_MAX_FRAMES,
    SAMPLE_RATE,
    FinalizedUtterance,
    FluxConfig,
    TurnFinalizer,
    build_flux_url,
    force_end_turn_message,
    run_push_to_talk,
    split_frames,
)

__all__ = [
    "CHANNELS",
    "ENC",
    "EOT_THRESHOLD",
    "FLUX_URL",
    "FRAME_BYTES",
    "FRAME_MS",
    "MODEL",
    "QUEUE_MAX_FRAMES",
    "SAMPLE_RATE",
    "FinalizedUtterance",
    "FluxConfig",
    "TurnFinalizer",
    "build_flux_url",
    "force_end_turn_message",
    "run_push_to_talk",
    "split_frames",
]
