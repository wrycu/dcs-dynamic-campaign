"""World-state data model for the dynamic campaign.

Principles:
- Aggregated state is the source of truth; DCS spawns are a temporary projection of it.
- Stable internal IDs everywhere; DCS names are only a lookup while something is spawned.
- The theater is a graph of objectives joined by links; movement, supply and the front line derive from it.
- Static theater definition is kept apart from the dynamic campaign state.
- DCS only reports events; the engine applies them to the state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


# ---------- primitives ----------

@dataclass(frozen=True)
class Pos:
    x: float  # DCS map coords (metres); convert to lat/lon only for the UI
    y: float


class Side(Enum):
    BLUE = auto()
    RED = auto()
    NEUTRAL = auto()


class SpawnState(Enum):
    AGGREGATED = auto()  # exists only as data
    SPAWNED = auto()     # currently has a live DCS group


# ---------- static theater (authored once, read-only at runtime) ----------

class ObjectiveType(Enum):
    AIRBASE = auto()
    FARP = auto()
    PORT = auto()
    BRIDGE = auto()
    FACTORY = auto()
    POWER_PLANT = auto()
    SUBSTATION = auto()
    FUEL_DEPOT = auto()
    DAM = auto()
    REFINERY = auto()
    DEPOT = auto()
    RADAR = auto()
    CITY = auto()
    ROAD_JUNCTION = auto()
    BORDER = auto()


@dataclass
class Feature:
    """A destroyable piece of an objective: runway, bridge span, hangar, tank farm."""
    id: str
    dcs_ref: str  # static object name or scenery object id
    value: float  # contribution to the objective's function; weights per objective sum to 1


@dataclass
class ObjectiveDef:
    id: str
    name: str
    type: ObjectiveType
    pos: Pos
    features: list[Feature]
    priority: int  # base strategic value
    dcs_airbase: str | None = None
    # For an objective off the road network (infrastructure): the town, airbase or military site
    # whose area it's in, so it changes hands with it.
    area_of: str | None = None
    country: str | None = None   # from the map's borders (Natural Earth), e.g. "Syria", "Turkey"


@dataclass
class Link:
    a: str  # objective id
    b: str  # objective id
    kind: str  # "road" | "rail" | "sea"
    via: list[str] = field(default_factory=list)  # bridge objective ids this link depends on
    route: list[Pos] = field(default_factory=list)  # DCS road route from a to b (simplified), if any
    length_m: float = 0.0                           # along the route (straight line if none)


@dataclass
class Theater:
    map: str  # "Caucasus", "Syria", ...
    objectives: dict[str, ObjectiveDef]
    links: list[Link]


# ---------- dynamic campaign state ----------

@dataclass
class ObjectiveState:
    id: str
    owner: Side
    feature_damage: dict[str, float] = field(default_factory=dict)  # feature id -> 0.0 ok .. 1.0 destroyed
    repair_rate: float = 0.0  # per hour, scaled by supply

    def function(self, d: ObjectiveDef) -> float:
        """0..1 operational capacity, e.g. runway availability or factory output."""
        return 1.0 - sum(f.value * self.feature_damage.get(f.id, 0.0) for f in d.features)


@dataclass
class Warehouse:
    """Mirrors the DCS warehouse at an airbase; drives dynamic spawn availability."""
    aircraft: dict[str, int] = field(default_factory=dict)  # DCS type name -> count
    weapons: dict[str, int] = field(default_factory=dict)   # DCS weapon type -> count
    fuel_t: float = 0.0


@dataclass
class Squadron:
    id: str
    side: Side
    name: str
    aircraft_type: str
    base_id: str  # objective id
    roles: set[str]  # {"CAP", "SEAD", "STRIKE", "CAS", ...}
    aircraft_ready: int
    aircraft_damaged: int  # repaired over time if the base is supplied
    pilots: int
    player_flyable: bool


class FlightState(Enum):
    PLANNED = auto()
    TAXI = auto()
    ENROUTE = auto()
    ON_STATION = auto()
    EGRESS = auto()
    LANDED = auto()
    LOST = auto()


class StartMode(Enum):
    AIR = auto()        # spawned airborne at its first waypoint (or where it was aggregated)
    RUNWAY = auto()     # on the runway, engines running
    RAMP_HOT = auto()   # parked, engines running
    RAMP_COLD = auto()  # parked, cold; may be held uncontrolled until told to start


@dataclass
class Waypoint:
    pos: Pos
    alt_m: float
    tot: float | None = None   # campaign seconds
    # What the flight does here; see campaign/flight_spawn.py for the supported actions:
    # None (fly through), "ORBIT", "CAS", "SEAD", "BOMB", "EXIT" (leave the map), "LAND".
    action: str | None = None
    params: dict = field(default_factory=dict)  # per action, e.g. {"duration_s": 1200, "radius_m": 9000}
    # True airspeed on the leg to this point (m/s), e.g. slow for a CAS stack or fast for an ingress.
    # None: the flight's cruise speed (see campaign.catalog.flight_cruise_ms).
    speed_ms: float | None = None


@dataclass
class Flight:
    id: str
    package_id: str
    squadron_id: str
    callsign: str
    mission: str  # "BARCAP", "SEAD", "STRIKE", ...
    aircraft: int
    loadout: str
    waypoints: list[Waypoint]
    takeoff_time: float
    state: FlightState = FlightState.PLANNED
    start: StartMode = StartMode.AIR
    # Offered to players first; if unclaimed at the claim deadline, AI flies it or it is cancelled.
    player_preferred: bool = False
    pos: Pos | None = None  # abstract position while aggregated
    # Last known airborne state, used to respawn the flight where it was aggregated.
    alt_m: float | None = None
    hdg: float | None = None   # degrees true
    speed_ms: float | None = None
    fuel: float = 1.0          # internal fuel fraction of the emptiest aircraft
    next_wp: int = 0           # index into waypoints of the next one to fly to
    # Campaign clock time an ORBIT at waypoints[next_wp] ends, once the flight is there.
    hold_until: float | None = None
    spawn: SpawnState = SpawnState.AGGREGATED
    dcs_group: str | None = None
    player_seats: dict[int, str | None] = field(default_factory=dict)  # seat -> pilot id


@dataclass
class Package:
    id: str
    side: Side
    target_id: str  # objective id or ground unit id
    tot: float
    flight_ids: list[str]


@dataclass
class ATO:
    side: Side
    cycle_start: float
    package_ids: list[str]


class GroundRole(Enum):
    ARMOR = auto()
    MECH = auto()
    INFANTRY = auto()
    ARTILLERY = auto()
    SAM = auto()
    AAA = auto()
    SUPPLY = auto()
    HQ = auto()


class GroundOrder(Enum):
    HOLD = auto()
    DEFEND = auto()
    ATTACK = auto()
    MOVE = auto()
    RESERVE = auto()
    RETREAT = auto()


@dataclass
class GroundUnit:
    """Battalion-level formation. Composition is the source of truth."""
    id: str
    side: Side
    name: str
    role: GroundRole
    composition: dict[str, int]  # DCS unit type -> count
    pos: Pos
    location_id: str | None  # objective it occupies, if any
    order: GroundOrder
    order_target: str | None = None
    route: list[str] = field(default_factory=list)  # objective ids along links
    supply: float = 1.0  # 0..1: fuel, ammo, food
    morale: float = 1.0  # 0..1
    spawn: SpawnState = SpawnState.AGGREGATED
    dcs_group: str | None = None
    sited: bool = False  # spots found in DCS (see spawner.py: siting)
    spots: list[Pos] = field(default_factory=list)  # each vehicle's spot: clear of buildings, water, runways


@dataclass
class SideState:
    side: Side
    target_weights: dict[str, float]  # commander priorities feeding the ATO planner
    supply_pool: float = 0.0  # produced by factories, consumed by units
    reinforcements: list[GroundUnit] = field(default_factory=list)  # arrive over time


@dataclass
class Pilot:
    id: str  # player UCID, for MP persistence
    name: str
    squadron_id: str
    status: str = "ACTIVE"  # "ACTIVE" | "MIA" | "POW" | "KIA"
    sorties: int = 0
    kills: dict[str, int] = field(default_factory=dict)


# ---------- events (the only input from DCS) ----------

@dataclass
class Event:
    t: float
    kind: str  # "unit_killed", "feature_hit", "takeoff", "landed", "ejected", ...
    data: dict


# ---------- root ----------

@dataclass
class CampaignState:
    theater_map: str
    clock: float  # campaign seconds since start
    sides: dict[Side, SideState]
    objectives: dict[str, ObjectiveState] = field(default_factory=dict)
    warehouses: dict[str, Warehouse] = field(default_factory=dict)  # keyed by airbase objective id
    squadrons: dict[str, Squadron] = field(default_factory=dict)
    ground_units: dict[str, GroundUnit] = field(default_factory=dict)
    packages: dict[str, Package] = field(default_factory=dict)
    flights: dict[str, Flight] = field(default_factory=dict)
    atos: dict[Side, ATO] = field(default_factory=dict)
    pilots: dict[str, Pilot] = field(default_factory=dict)
    event_log: list[Event] = field(default_factory=list)
