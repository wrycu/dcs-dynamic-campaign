"""The first scenario: a coalition against Syria, in the south (data/scenarios/southern_syria.toml).

The starting state comes from the scenario file on the Syria map layer (campaign/scenario_file.py):
who holds which objective, the squadrons, and the ground order of battle. The ground war
(campaign/ground_war.py) runs from the start: the coalition attacks towards Damascus, Syria holds
and counterattacks. The planner (item 2) comes next. On the campaign server, the formations spawn
around players like any others, and Status shows the front and the battles.
"""
from __future__ import annotations

import logging

from campaign.ground_war import GroundWar, sides_from
from campaign.model import CampaignState, Side
from campaign.scenario_file import build_state, load_scenario_def, scenario_path, summary
from campaign.server import Engine, Scenario, System

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

    def systems(self, engine: Engine) -> list[System]:
        def news(text: str) -> None:
            log.info("WAR %s %s", engine.clock_text(), text)
            engine.say(text, duration=10, log_it=False)
        return [GroundWar(engine.state, self.theater, sides_from(self.sd), notify=news, call=engine.mission_call)]

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
