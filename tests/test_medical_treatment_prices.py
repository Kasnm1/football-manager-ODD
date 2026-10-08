from tools.club_economy import MEDICAL_TREATMENTS


def test_medical_treatment_prices_are_stored_at_the_reduced_amounts() -> None:
    assert MEDICAL_TREATMENTS["conservative"]["daily_cost"] == 10_000.0
    assert MEDICAL_TREATMENTS["aggressive"]["daily_cost"] == 30_000.0
