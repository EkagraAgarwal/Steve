#!/usr/bin/env python3
# Copyright 2025-2026 Dimensional Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Text-command movement control with no LLM in the loop.

`JevMovementTeleop` classifies typed commands with TypeSafe's Jev (a fast System
One judgment, not a reasoning model) and drives the robot directly — no
McpServer/McpClient/tool-calling round trip. Use `humancli` as the text client,
same as with `unitree-go2-agentic-movement`; it just needs TYPESAFE_API_KEY set
instead of (or alongside) OPENAI_API_KEY.

For open-ended natural language beyond simple directional moves, use
`unitree-go2-agentic-movement` instead (or compose both — they don't conflict).
"""

from dimos.core.coordination.blueprints import autoconnect
from dimos.robot.unitree.go2.blueprints.smart.unitree_go2 import unitree_go2
from dimos.robot.unitree.jev_movement_teleop import JevMovementTeleop

unitree_go2_jev_movement = autoconnect(
    unitree_go2,
    JevMovementTeleop.blueprint(),
)
