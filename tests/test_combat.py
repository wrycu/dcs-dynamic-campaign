import random
import statistics
import unittest

from campaign import combat
from campaign.combat import AirSide, Fighter

T72 = {"T-72B": 31}
M1 = {"M-1 Abrams": 30}
BMP = {"BMP-1": 30, "T-55": 10}


def f(fid, comp, **kw):
    return Fighter(fid, dict(comp), combat.vehicles(comp), **kw)


def battle(att, dfd, terrain, seed):
    a = [f(f"a{i}", c) for i, c in enumerate(att)]
    d = [f(f"d{i}", c) for i, c in enumerate(dfd)]
    r = combat.ground_battle(a, d, terrain, random.Random(seed))
    return r, a, d


class GroundTests(unittest.TestCase):
    def test_same_seed_same_battle(self):
        r1, a1, _ = battle([M1], [T72], "town", 7)
        r2, a2, _ = battle([M1], [T72], "town", 7)
        self.assertEqual((r1, a1[0].composition), (r2, a2[0].composition))

    def test_quality_and_numbers_win(self):
        wins = lambda att, dfd, terrain: sum(  # noqa: E731
            battle(att, dfd, terrain, s)[0].winner == "attacker" for s in range(200))
        self.assertGreater(wins([M1], [T72], "town"), 180)       # Abrams beat T-72s even in a town
        self.assertLess(wins([T72], [M1], "open"), 10)           # and T-72s don't beat Abrams
        self.assertLess(wins([T72], [T72], "town"), 20)          # 1:1 against a town fails
        self.assertGreater(wins([T72, T72, T72], [T72], "town"), 180)  # 3:1 succeeds

    def test_a_town_costs_the_attacker_more(self):
        def attacker_losses(terrain):
            return statistics.fmean(sum(v for k, v in battle([T72, T72], [T72], terrain, s)[0].losses.items()
                                        if k.startswith("a")) for s in range(200))
        self.assertGreater(attacker_losses("town"), 1.3 * attacker_losses("open"))

    def test_a_formation_breaks_before_it_is_wiped_out(self):
        r, _, d = battle([M1, M1], [BMP], "open", 3)
        self.assertEqual(r.winner, "attacker")
        self.assertGreater(combat.vehicles(d[0].composition), 0)

    def test_artillery_support_fires_but_is_not_hit(self):
        a = [f("a", T72)]
        d = [f("d", BMP), f("guns", {"SAU Gvozdika": 18}, support=True)]
        res = combat.ground_hour(a, d, "town", random.Random(1))
        self.assertNotIn("guns", res.losses)
        self.assertEqual(d[1].composition["SAU Gvozdika"], 18)


class AirTests(unittest.TestCase):
    def test_better_fighters_and_awacs_win(self):
        rng = random.Random(1)
        blue = red = 0
        for _ in range(300):
            lb, lr = combat.air_battle(AirSide(["F-16C_50"] * 4, True), AirSide(["MiG-23MLD"] * 4), rng)
            blue, red = blue + len(lb), red + len(lr)
        self.assertGreater(red, 10 * blue)

    def test_strikers_do_not_shoot(self):
        rng = random.Random(2)
        for _ in range(100):
            _, lost = combat.air_battle(AirSide(["Su-22M4"] * 4), AirSide(["A-10C_2"] * 4), rng)
            self.assertEqual(lost, [])

    def test_sead_and_ecm_cut_sam_losses(self):
        def losses(aircraft, sead):
            rng = random.Random(3)
            return sum(len(combat.sam_losses(aircraft, [("SA-6", True)], sead, rng)) for _ in range(2000))
        self.assertLess(losses(["F-16C_50"] * 4, True), 0.6 * losses(["F-16C_50"] * 4, False))
        self.assertLess(losses(["F-16C_50"] * 4, False), losses(["A-10C_2"] * 4, False))  # ECM
        self.assertEqual(combat.sam_losses(["F-16C_50"] * 4, [], False, random.Random(1)), [])

    def test_cas_kills_fewer_in_cover_and_never_more_than_there(self):
        rng = random.Random(4)
        open_ = sum(sum(combat.cas_kills(["A-10C_2"] * 4, BMP, rng).values()) for _ in range(500))
        town = sum(sum(combat.cas_kills(["A-10C_2"] * 4, BMP, rng, cover=2).values()) for _ in range(500))
        self.assertLess(town, 0.7 * open_)
        kills = combat.cas_kills(["A-10C_2"] * 40, {"BTR-80": 3}, random.Random(5))
        self.assertLessEqual(kills.get("BTR-80", 0), 3)


class HelperTests(unittest.TestCase):
    def test_binomial_and_poisson_edges(self):
        rng = random.Random(1)
        self.assertEqual((combat.binomial(rng, 5, 0), combat.binomial(rng, 5, 1), combat.binomial(rng, 0, 0.5)),
                         (0, 5, 0))
        self.assertEqual(combat.poisson(rng, 0), 0)
        self.assertAlmostEqual(statistics.fmean(combat.poisson(rng, 2.0) for _ in range(5000)), 2.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
