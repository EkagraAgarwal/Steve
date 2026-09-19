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

"""Text/voice-driven movement control, without the extra deps `unitree_go2_agentic` pulls in.

Same LLM + skill wiring as `unitree_go2_agentic`, minus:
- `SpatialMemory`/`PerceiveLoopSkill` (via `unitree_go2_spatial`), which eagerly downloads
  a CLIP checkpoint from Git LFS at startup and needs LFS credentials for
  lfs.dimensionalos.com.
- `NavigationSkillContainer`, whose semantic skills (`navigate_with_text`, `tag_location`, ...)
  hard-require that same `SpatialMemory` module.
- `PersonFollowSkillContainer`, which eagerly constructs a Qwen VL model and needs
  `ALIBABA_API_KEY`.

None of those are needed to drive the robot by typed/spoken directional command: that's
`UnitreeSkillContainer.move_to()`, which only needs `NavigationInterfaceSpec` (satisfied by
`ReplanningAStarPlanner`, already in the base `unitree_go2` blueprint) and `GO2ConnectionSpec`
(satisfied by `GO2Connection`, likewise already there) — so this composes `unitree_go2` directly
instead of `unitree_go2_spatial`.
"""

from dimos.agents.mcp.mcp_client import McpClient
from dimos.agents.mcp.mcp_server import McpServer
from dimos.agents.skills.observe_skill import ObserveSkill
from dimos.agents.skills.speak_skill import SpeakSkill
from dimos.agents.web_human_input import WebInput
from dimos.core.coordination.blueprints import autoconnect
from dimos.robot.unitree.go2.blueprints.smart.unitree_go2 import unitree_go2
from dimos.robot.unitree.unitree_skill_container import UnitreeSkillContainer

unitree_go2_agentic_movement = autoconnect(
    unitree_go2,
    McpServer.blueprint(),
    McpClient.blueprint(),
    ObserveSkill.blueprint(),
    UnitreeSkillContainer.blueprint(),
    WebInput.blueprint(),
    SpeakSkill.blueprint(),
)
