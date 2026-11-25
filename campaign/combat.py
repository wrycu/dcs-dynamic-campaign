"""Abstract combat: fights resolved in the engine when no player is near (items 8 and 17, proof P7).

Every function here is pure apart from the ``random.Random`` it is given, so a fixed seed gives
the same war.

Ground fights are hourly attrition between formations, Lanchester style: each side's fire is its
combat power (type values x supply x morale x posture), spread over the enemy's vehicles, and each
vehicle survives it according to its toughness. A formation breaks when it has lost too much or
its morale is gone; an attacker that can't win in ``max_hours`` gives up.

Air fights are per mission:
- air to air: a BVR pass and a merge, each aircraft shooting once per pass with a chance that
  depends on the quality difference (radar, missiles, AWACS);
- SAMs: each aircraft has a chance to be shot down per site whose range it enters, lower with
  SEAD in the package, with ECM, and for a site only passed rather than attacked;
- SEAD: each aircraft with anti-radiation missiles has a chance to knock out a site's radar;
- CAS: each aircraft that reaches the target kills vehicles at a per-type rate.

The numbers are first guesses tuned to give believable results (see ``tools/war_sim.py
--battles``); they are meant to move into scenario settings once the model settles.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

# ---------- ground ----------


@dataclass(frozen=True)
class GroundType:
    power: float      # direct-fire combat power (a T-55 is about 2.5)
    toughness: float  # how many "hits" it takes on average; 1 for a light AFV
    shorad_pk: float = 0.0  # per-aircraft loss chance it adds to an attack on its formation


GROUND_TYPES: dict[str, GroundType] = {
    # blue
    "M-1 Abrams": GroundType(15, 8),
    "Merkava_Mk4": GroundType(14, 8),
    "M-2 Bradley": GroundType(4, 1.5),
    "M-109": GroundType(3, 1),
    "M1097 Avenger": GroundType(0.5, 0.6, shorad_pk=0.003),
    "M1043 HMMWV Armament": GroundType(1.0, 0.6),
    "Soldier M4": GroundType(0.5, 0.4),
    "Soldier M249": GroundType(0.7, 0.4),
    "Soldier stinger": GroundType(0.1, 0.4, shorad_pk=0.004),
    # red
    "T-72B": GroundType(5, 2),
    "T-55": GroundType(2.5, 1.2),
    "BMP-2": GroundType(2.5, 1),
    "BMP-1": GroundType(1.8, 0.9),
    "BTR-80": GroundType(1.2, 0.7),
    "SAU Gvozdika": GroundType(2.5, 0.8),
    "ZSU-23-4 Shilka": GroundType(0.8, 0.8, shorad_pk=0.002),
    "Strela-1 9P31": GroundType(0.2, 0.6, shorad_pk=0.003),
    "Osa 9A33 ln": GroundType(0.2, 0.8, shorad_pk=0.005),
    "Ural-375": GroundType(0, 0.4),
    "Grad-URAL": GroundType(2.0, 0.5),
    "Strela-10M3": GroundType(0.2, 0.7, shorad_pk=0.004),
    # garrisons: local infantry and guns holding a town or base
    "Infantry AK": GroundType(0.4, 0.4),
    "Paratrooper AKS-74": GroundType(0.4, 0.4),
    "Soldier RPG": GroundType(1.0, 0.4),     # anti-tank
    "SA-18 Igla manpad": GroundType(0.1, 0.4, shorad_pk=0.004),
    "ZU-23 Emplacement": GroundType(0.6, 0.8, shorad_pk=0.002),
}
DEFAULT_GROUND = GroundType(1, 1)

# Defender (fire, cover) multipliers by terrain: cover divides the hits it takes. An attacker on
# the move fights at (1, 1).
POSTURE = {"town": (1.5, 1.5), "airbase": (1.3, 1.3), "open": (1.25, 1.2)}


def gtype(name: str) -> GroundType:
    return GROUND_TYPES.get(name, DEFAULT_GROUND)


def raw_power(composition: dict[str, int]) -> float:
    return sum(gtype(t).power * n for t, n in composition.items())


def vehicles(composition: dict[str, int]) -> int:
    return sum(composition.values())


@dataclass
class Fighter:
    """One formation in a ground fight. ``full`` is its vehicle count at full strength."""
    id: str
    composition: dict[str, int]
    full: int
    supply: float = 1.0
    morale: float = 1.0
    support: bool = False  # artillery firing from nearby: adds fire, takes none

    def strength(self) -> float:
        return vehicles(self.composition) / self.full if self.full else 0.0

    def power(self) -> float:
        return raw_power(self.composition) * (0.5 + 0.5 * self.supply) * (0.4 + 0.6 * self.morale)


@dataclass
class GroundConfig:
    lethality: float = 0.022     # hits per hour per point of enemy power, spread over its targets
    break_strength: float = 0.6  # a formation below this fraction of its full strength breaks
    break_morale: float = 0.35
    morale_per_loss: float = 1.5  # morale lost per fraction of the formation lost in one hour
    support_factor: float = 0.7   # artillery fire counts at this fraction
    max_hours: int = 12           # an attack that hasn't won by then is called off
    supply_use: float = 0.03      # supply used per hour of fighting


@dataclass
class HourResult:
    losses: dict[str, dict[str, int]] = field(default_factory=dict)  # fighter id -> type -> killed
    broken: list[str] = field(default_factory=list)                  # fighter ids that broke

    def total(self, ids: list[str]) -> int:
        return sum(sum(self.losses.get(i, {}).values()) for i in ids)


def _fire(side: list[Fighter], cfg: GroundConfig, mult: float = 1.0) -> float:
    return mult * sum(f.power() * (cfg.support_factor if f.support else 1.0) for f in side)


def _apply_fire(fire: float, targets: list[Fighter], cfg: GroundConfig, rng: random.Random,
                res: HourResult, cover: float = 1.0) -> None:
    exposed = [f for f in targets if not f.support]
    n = sum(vehicles(f.composition) for f in exposed)
    if n == 0 or fire <= 0:
        return
    hits_per_vehicle = cfg.lethality * fire / n / cover
    for f in exposed:
        lost: dict[str, int] = {}
        for t in sorted(f.composition):
            count = f.composition[t]
            p = 1.0 - math.exp(-hits_per_vehicle / gtype(t).toughness)
            k = binomial(rng, count, p)
            if k:
                lost[t] = k
                f.composition[t] = count - k
        if lost:
            res.losses[f.id] = lost
        f.morale = max(0.0, f.morale - cfg.morale_per_loss * (sum(lost.values()) / f.full if f.full else 0))
        f.supply = max(0.0, f.supply - cfg.supply_use)


def ground_hour(attackers: list[Fighter], defenders: list[Fighter], terrain: str,
                rng: random.Random, cfg: GroundConfig | None = None) -> HourResult:
    """One hour of a ground fight. Mutates the fighters (losses, morale, supply)."""
    cfg = cfg or GroundConfig()
    res = HourResult()
    a_fire = _fire(attackers, cfg)
    fire_mult, cover = POSTURE.get(terrain, POSTURE["open"])
    d_fire = _fire(defenders, cfg, fire_mult)
    _apply_fire(a_fire, defenders, cfg, rng, res, cover)
    _apply_fire(d_fire, attackers, cfg, rng, res)
    for f in attackers + defenders:
        if not f.support and (f.strength() < cfg.break_strength or f.morale < cfg.break_morale):
            res.broken.append(f.id)
    return res


@dataclass
class BattleResult:
    hours: int
    winner: str  # "attacker" | "defender"
    losses: dict[str, int]  # fighter id -> vehicles lost


def ground_battle(attackers: list[Fighter], defenders: list[Fighter], terrain: str,
                  rng: random.Random, cfg: GroundConfig | None = None) -> BattleResult:
    """Fight to a result: the defenders all break (attacker wins) or the attack fails."""
    cfg = cfg or GroundConfig()
    a_live, d_live = list(attackers), list(defenders)
    lost: dict[str, int] = {}
    for hour in range(1, cfg.max_hours + 1):
        res = ground_hour(a_live, d_live, terrain, rng, cfg)
        for fid, by_type in res.losses.items():
            lost[fid] = lost.get(fid, 0) + sum(by_type.values())
        a_live = [f for f in a_live if f.id not in res.broken]
        d_live = [f for f in d_live if f.id not in res.broken]
        if not [f for f in d_live if not f.support]:
            return BattleResult(hour, "attacker", lost)
        if not [f for f in a_live if not f.support]:
            return BattleResult(hour, "defender", lost)
    return BattleResult(cfg.max_hours, "defender", lost)


def binomial(rng: random.Random, n: int, p: float) -> int:
    if p <= 0 or n <= 0:
        return 0
    if p >= 1:
        return n
    return sum(1 for _ in range(n) if rng.random() < p)


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    limit, k, prod = math.exp(-lam), 0, rng.random()
    while prod > limit:
        k += 1
        prod *= rng.random()
    return k


# ---------- air ----------


@dataclass(frozen=True)
class AirType:
    a2a: float         # air-to-air quality: radar, missiles, training (F-16C about 9, MiG-21 3)
    cas_kills: float   # vehicles killed per sortie that reaches a CAS target
    sead: float = 0.0  # chance per sortie to knock out a SAM site's radar (anti-radiation missiles)
    ecm: float = 1.0   # multiplies SAM shot-down chances


AIR_TYPES: dict[str, AirType] = {
    "F-16C_50": AirType(9, 0.6, sead=0.35, ecm=0.7),
    "FA-18C_hornet": AirType(9, 0.6, sead=0.3, ecm=0.7),
    "F-15C": AirType(10, 0),
    "A-10C_2": AirType(2, 1.0),
    "MiG-29A": AirType(6, 0.3),
    "MiG-23MLD": AirType(4, 0.4),
    "MiG-21Bis": AirType(3, 0.3),
    "Su-22M4": AirType(1, 0.5),
    "Su-24M": AirType(1, 0.6, ecm=0.9),
}
DEFAULT_AIR = AirType(3, 0.5)


def atype(name: str) -> AirType:
    return AIR_TYPES.get(name, DEFAULT_AIR)


@dataclass
class AirConfig:
    bvr_pk: float = 0.35          # kill chance per shot at equal quality
    wvr_pk: float = 0.25
    quality_scale: float = 2.0    # quality points for e (~2.7x) in the odds
    awacs_bonus: float = 1.0      # quality points for having AWACS or GCI
    min_bvr_quality: float = 6.0  # aircraft below this don't shoot in the BVR pass
    fighter_quality: float = 3.0  # aircraft below this never shoot (strikers)


def _shot(rng: random.Random, pk: float, q_own: float, q_target: float, cfg: AirConfig) -> bool:
    odds = 1.0 / (1.0 + math.exp(-(q_own - q_target) / cfg.quality_scale))
    return rng.random() < min(0.95, 2 * pk * odds)  # pk at equal quality, up to 2x pk when far better


@dataclass
class AirSide:
    aircraft: list[str]  # one entry per aircraft, its DCS type
    awacs: bool = False


def air_battle(a: AirSide, b: AirSide, rng: random.Random,
               cfg: AirConfig | None = None) -> tuple[list[int], list[int]]:
    """A BVR pass and a merge. Returns the indexes of the aircraft each side lost."""
    cfg = cfg or AirConfig()
    lost_a: set[int] = set()
    lost_b: set[int] = set()
    for pk, min_q in ((cfg.bvr_pk, cfg.min_bvr_quality), (cfg.wvr_pk, cfg.fighter_quality)):
        new_a: set[int] = set()
        new_b: set[int] = set()
        for shooters, s_lost, targets, t_lost, hits in ((a, lost_a, b, lost_b, new_b),
                                                        (b, lost_b, a, lost_a, new_a)):
            alive_t = [i for i in range(len(targets.aircraft)) if i not in t_lost]
            for i, t in enumerate(shooters.aircraft):
                q = atype(t).a2a
                if i in s_lost or q < min_q or not alive_t:
                    continue
                j = rng.choice(alive_t)
                q_own = q + (cfg.awacs_bonus if shooters.awacs else 0)
                q_tgt = atype(targets.aircraft[j]).a2a + (cfg.awacs_bonus if targets.awacs else 0)
                if _shot(rng, pk, q_own, q_tgt, cfg):
                    hits.add(j)
        lost_a |= new_a
        lost_b |= new_b
    return sorted(lost_a), sorted(lost_b)


# SAM site types: shot-down chance per aircraft per site it is exposed to, and range.
@dataclass(frozen=True)
class SamType:
    pk: float
    range_m: float


SAM_TYPES: dict[str, SamType] = {
    "SA-2": SamType(0.02, 40_000),
    "SA-3": SamType(0.025, 15_000),
    "SA-6": SamType(0.03, 22_000),
    "SA-8": SamType(0.03, 10_000),
    "SA-11": SamType(0.05, 35_000),
    "SA-10": SamType(0.07, 75_000),
}


@dataclass
class SamConfig:
    sead_factor: float = 0.4      # SEAD in the package multiplies SAM chances
    transit_factor: float = 0.5   # a site only passed, not attacked
    repair_h: float = 36.0        # a knocked-out radar is back after this long


def sam_losses(aircraft: list[str], exposures: list[tuple[str, bool]], sead: bool, rng: random.Random,
               cfg: SamConfig | None = None) -> list[int]:
    """Indexes of aircraft shot down. ``exposures``: (site type, attacked rather than passed)."""
    cfg = cfg or SamConfig()
    lost: list[int] = []
    for i, t in enumerate(aircraft):
        for site, at_target in exposures:
            p = SAM_TYPES[site].pk * atype(t).ecm
            p *= cfg.sead_factor if sead else 1.0
            p *= 1.0 if at_target else cfg.transit_factor
            if rng.random() < p:
                lost.append(i)
                break
    return lost


def shorad_losses(aircraft: list[str], target: dict[str, int], rng: random.Random) -> list[int]:
    """Aircraft lost to the target formation's own guns and short-range missiles."""
    p_site = [gtype(t).shorad_pk for t, n in target.items() for _ in range(n) if gtype(t).shorad_pk]
    return [i for i, _ in enumerate(aircraft) if any(rng.random() < p for p in p_site)]


def sead_kills(aircraft: list[str], rng: random.Random) -> bool:
    """Does the SEAD flight knock out the site it is sent against?"""
    return any(rng.random() < atype(t).sead for t in aircraft)


def cas_kills(aircraft: list[str], target: dict[str, int], rng: random.Random,
              cover: float = 1.0) -> dict[str, int]:
    """Vehicles killed by aircraft that reached the target. Tougher vehicles are harder to kill,
    and ``cover`` (a town: 2) divides the kills."""
    shots = sum(poisson(rng, atype(t).cas_kills / cover) for t in aircraft)
    killed: dict[str, int] = {}
    pool = {t: n for t, n in target.items() if n > 0}
    for _ in range(shots):
        if not pool:
            break
        types = sorted(pool)
        t = rng.choices(types, weights=[pool[x] for x in types])[0]
        if rng.random() < min(1.0, 2.0 / gtype(t).toughness):  # light vehicles always die, tanks often
            killed[t] = killed.get(t, 0) + 1
            pool[t] -= 1
            if pool[t] == 0:
                del pool[t]
    return killed
