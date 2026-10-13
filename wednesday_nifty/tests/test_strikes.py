import pytest

from strategy.strikes import calculate_atm_strike, calculate_hedge_strike, snap_to_strike


def test_snap_rounds_to_nearest_interval():
    assert snap_to_strike(25123, 50) == 25100
    assert snap_to_strike(25126, 50) == 25150


def test_snap_ties_round_up():
    assert snap_to_strike(25125, 50) == 25150


def test_snap_rejects_non_positive_interval():
    with pytest.raises(ValueError):
        snap_to_strike(25000, 0)


def test_atm_strike_is_spot_snapped():
    assert calculate_atm_strike(25123.45, 50) == 25100


def test_hedge_strike_call_spread_is_atm_plus_offset():
    # Call Credit Spread -> short ATM CE, hedge CE further OTM (higher strike)
    assert calculate_hedge_strike(25100, 200, 50, direction_sign=1) == 25300


def test_hedge_strike_put_spread_is_atm_minus_offset():
    # Put Credit Spread -> short ATM PE, hedge PE further OTM (lower strike)
    assert calculate_hedge_strike(25100, 200, 50, direction_sign=-1) == 24900


def test_hedge_strike_rejects_invalid_sign():
    with pytest.raises(ValueError):
        calculate_hedge_strike(25100, 200, 50, direction_sign=0)
