import pytest
from sqlalchemy import text

from cpm.budget import seed
from cpm.budget.pricing import PriceError, PriceTable, Tier
from cpm.db.engine import make_engine
from cpm.db.migrate import upgrade


@pytest.fixture
def table(tmp_path):
    db = tmp_path / "d" / "cpm.db"
    db.parent.mkdir()
    upgrade(f"sqlite:///{db.as_posix()}")
    engine = make_engine(db)
    yield PriceTable(engine)
    engine.dispose()


def test_migration_seed_matches_the_seed_module(table):
    got = {
        (r.model, r.version): (r.base, r.long_threshold, r.long) for r in table.current().values()
    }
    assert {m for m, _ in got} == set(seed.BASE)
    for m, (i, o, cr, cw) in seed.BASE.items():
        base, thr, long = got[(m, 1)]
        assert (base.input, base.output, base.cache_read, base.cache_write) == (i, o, cr, cw)
        lg = seed.LONG.get(m)
        assert (thr, long and (long.input, long.output, long.cache_read, long.cache_write)) == (
            (lg[0], lg[1:]) if lg else (None, None)
        )


def test_costs_are_integer_micro_usd_rounded_up(table):
    # 1000 in + 500 out on Sonnet 5.5: 1000*2 + 500*10 = 7000 micro-USD
    assert table.estimate("claude-sonnet-5-5", 1000, 500) == (7000, 1)
    assert table.estimate("claude-sonnet-5-5", 1, 0)[0] == 2  # never rounds down to 0
    assert table.estimate("claude-haiku-5-5", 10, 0)[0] == 1
    cost, ver = table.estimate("claude-opus-5-5", 3, 3, 3, 3)
    assert isinstance(cost, int) and ver == 1
    # cache read/write use their own rates
    assert table.estimate("claude-sonnet-5-5", 0, 0, 1_000_000, 1_000_000)[0] == 200_000 + 2_500_000


def test_haiku_long_prompt_tier(table):
    below = table.estimate("claude-haiku-5-5", 100_000, 1_000_000)[0]
    above = table.estimate("claude-haiku-5-5", 100_001, 1_000_000)[0]
    assert below == 100_000 * 100_000 // 1_000_000 + 500_000
    assert above == -(-(100_001 * 500_000) // 1_000_000) + 2_500_000
    # cache tokens count towards the prompt size
    assert table.estimate("claude-haiku-5-5", 1, 0, 99_999, 0)[0] == 1001  # short tier
    assert table.estimate("claude-haiku-5-5", 1, 0, 100_000, 0)[0] == 5001  # long tier


def test_unknown_model_is_charged_the_dearest_price_and_has_no_version(table):
    cost, ver = table.estimate("future-model", 1_000_000, 1_000_000)
    assert ver is None
    assert cost >= table.estimate("claude-fable-5-1", 1_000_000, 1_000_000)[0]


def test_negative_tokens_rejected(table):
    with pytest.raises(PriceError):
        table.estimate("claude-sonnet-5-5", -1, 0)


def test_new_version_is_appended_and_used_old_rows_stay(table):
    new = table.add_version(
        "claude-sonnet-5-5", Tier(3_000_000, 15_000_000, 300_000, 3_750_000),
        source="test", verified_on="2026-11-01",
    )  # fmt: skip
    assert new.version == 2
    assert table.estimate("claude-sonnet-5-5", 1_000_000, 0) == (3_000_000, 2)
    with table.engine.connect() as c:
        n = c.execute(text("select count(*) from prices where model='claude-sonnet-5-5'")).scalar()
    assert n == 2


@pytest.mark.parametrize(
    "kw",
    [
        {"model": "Bad Model!"},
        {"model": "../x"},
        {"tier": Tier(-1, 0, 0, 0)},
        {"tier": Tier(10**10, 0, 0, 0)},
        {"verified_on": "yesterday"},
        {"long_threshold": 5},  # threshold without prices
    ],
)
def test_price_validation(table, kw):
    args = {"model": "claude-x", "tier": Tier(1, 1, 1, 1), "verified_on": "2026-10-26"}
    args |= kw
    with pytest.raises(PriceError):
        table.add_version(
            args["model"], args["tier"], long_threshold=args.get("long_threshold"),
            source="t", verified_on=args["verified_on"],
        )  # fmt: skip
