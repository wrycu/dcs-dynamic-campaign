"""The first scenario: a coalition against Syria, in the south (data/scenarios/southern_syria.toml).

The starting state comes from the scenario file on the Syria map layer (campaign/scenario_file.py):
who holds which objective, the squadrons, and the ground order of battle. Nothing moves or plans
yet: the planner (item 2) and the ground war (item 8) come next. On the campaign server, the
formations spawn around players like any others, and Status lists the sides.
"""
from __future__ import annotations

import logging

from campaign.model import CampaignState, Side
from campaign.scenario_file import build_state, load_scenario_def, scenario_path, summary
from campaign.server import Engine, Scenario

NAME = "southern_syria"
log = logging.getLogger(__name__)


class SouthernSyria(Scenario):
    name = NAME
    theater_map = "Syria"
    home = "Khalkhalah"

    def __init__(self) -> None:
        self.sd = load_scenario_def(scenario_path(NAME))
        self.built = build_state(self.sd)
        self.theater = self.built.theater
        self.magvar = self.sd.magvar

    def new_state(self) -> CampaignState:
        return self.built.state

    async def setup(self, engine: Engine) -> None:
        for line in summary(self.sd, self.built).splitlines():
            log.info(line)

    async def on_mission(self, engine: Engine, new_run: bool) -> None:
        if not new_run:
            return
        # Ramp starts take their aircraft from the airbase warehouse (P2): stock each squadron's base.
        stock: dict[str, dict[str, int]] = {}
        for sq in engine.state.squadrons.values():
            ab = engine.dcs_airbase(sq.base_id)
            if ab in engine.airbases:
                stock.setdefault(ab, {})[sq.aircraft_type] = sq.aircraft_ready
        for ab, aircraft in stock.items():
            await engine.mission_call("warehouse_set", {"airbase": ab, "aircraft": aircraft})
        st = engine.state
        held = {s: sum(1 for o in st.objectives.values() if o.owner is s) for s in (Side.BLUE, Side.RED)}
        engine.say(f"Campaign: {self.sd.title}. Coalition holds {held[Side.BLUE]} objectives, Syria "
                   f"{held[Side.RED]}. F10 > Other > Campaign > Status for the order of battle.", duration=20)


def make() -> Scenario:
    return SouthernSyria()
