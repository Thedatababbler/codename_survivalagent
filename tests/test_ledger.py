import pytest

from adamas_unit.ledger import Budget, BudgetExhausted, Ledger


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(tmp_path, **kw):
    clock = Clock()
    lg = Ledger(Budget(tokens=1000, seconds=100, compressions=4, repairs=8), 4, path=tmp_path / "ledger.json",
                clock=clock, **kw)
    return lg, clock


def test_even_allocation_and_carry_over(tmp_path):
    lg, _ = make(tmp_path)
    a0 = lg.open_unit(0)
    assert a0.tokens == 250 and a0.seconds == 25 and a0.repairs == 3
    ls = lg.lease(0, "builder")
    lg.charge(ls.lease_id, 100)
    lg.close_unit(0)
    a1 = lg.open_unit(1)                                   # 900 left over 3 units: the unused 150 carries over
    assert a1.tokens == 300


def test_overspend_in_one_unit_leaves_less_for_the_next(tmp_path):
    lg, _ = make(tmp_path)
    lg.open_unit(0)
    ls = lg.lease(0, "builder")
    lg.grant(ls.lease_id, "tokens", 300, requested=300, reason="r", decided_by="test")
    lg.charge(ls.lease_id, 550)
    lg.close_unit(0)
    assert lg.open_unit(1).tokens == 150                   # 450 left / 3


def test_grant_is_cut_to_the_mechanical_cap(tmp_path):
    lg, _ = make(tmp_path)                                 # floor 5% of 1000 = 50 per later unit
    lg.open_unit(0)
    ls = lg.lease(0, "builder")
    lg.charge(ls.lease_id, 250)                            # allocation spent
    got = lg.grant(ls.lease_id, "tokens", 10_000, requested=10_000, reason="more", decided_by="planner")
    assert got == 750 - 3 * 50                             # left 750 minus three later floors
    assert lg.events[-1]["cut"] is True


def test_shares_from_a_budget_table_respect_the_floors(tmp_path):
    lg, _ = make(tmp_path)
    a = lg.open_unit(0, shares={"tokens": 0.99, "seconds": 0.5})
    assert a.tokens == 1000 - 3 * 50 and a.seconds == 50


def test_repairs_per_unit_and_per_round(tmp_path):
    lg, _ = make(tmp_path)
    for u in range(3):
        lg.open_unit(u)
        for _ in range(3):
            if lg.can_repair(u):
                lg.use_repair(u)
    assert lg.used.repairs == 8                            # round cap 8 hit inside unit 2
    assert not lg.can_repair(2)
    with pytest.raises(BudgetExhausted):
        lg.use_repair(2)


def test_tighten_never_raises(tmp_path):
    lg, _ = make(tmp_path)
    lg.open_unit(0)
    lg.tighten(0, "repairs", 1, reason="useless", decided_by="planner")
    assert lg.alloc[0].repairs == 1
    lg.tighten(0, "repairs", 5, reason="", decided_by="planner")
    assert lg.alloc[0].repairs == 1


def test_compressions_pool(tmp_path):
    lg, _ = make(tmp_path)
    lg.open_unit(0)                                        # 4 / 4 units = 1 for unit 0
    assert lg.use_compression(0)
    assert not lg.use_compression(0)


def test_harness_time_is_not_the_agents(tmp_path):
    lg, clock = make(tmp_path)
    lg.open_unit(0)
    clock.t = 30
    lg.side_charge("harness", seconds=20, what="progress probe")
    assert lg.left("seconds") == 90                        # 30 elapsed, 20 of them harness


def test_round_exhaustion_refuses_new_leases(tmp_path):
    lg, clock = make(tmp_path)
    lg.open_unit(0)
    clock.t = 101
    assert lg.exhausted() == "seconds"
    with pytest.raises(BudgetExhausted):
        lg.lease(0, "builder")


def test_management_cap(tmp_path):
    lg, _ = make(tmp_path)                                 # 2% of 1000 = 20
    lg.side_charge("management", tokens=15)
    with pytest.raises(BudgetExhausted):
        lg.side_charge("management", tokens=10)
    assert lg.used.tokens == 0                             # side accounts never touch B


def test_ledger_file_is_written(tmp_path):
    lg, _ = make(tmp_path)
    lg.open_unit(0)
    import json

    d = json.loads((tmp_path / "ledger.json").read_text())
    assert [e["event"] for e in d["events"]] == ["open", "open_unit"]
