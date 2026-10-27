"""Geocoder tests: gazetteer matching, district inference and SL signals."""

from __future__ import annotations

from geocoder import (
    district_centroid,
    find_district,
    find_town,
    geocode_local,
    resolve_district,
    sri_lanka_signal_score,
    town_district,
    text_has_sri_lanka_signal,
)


def test_explicit_district_wins_over_road_name():
    assert find_district("A van crashed on Kandy Road near Kadawatha") == "Kandy"
    assert find_district("Flooding in Gampaha District") == "Gampaha"


def test_find_town_returns_most_specific_place():
    town = find_town("Two injured near Kadawatha on the Kandy Road")
    assert town is not None
    assert town["name"] == "Kadawatha"
    assert town["district"] == "Gampaha"


def test_town_district_lookup():
    assert town_district("Negombo") == "Gampaha"
    assert town_district("Jaffna") == "Jaffna"
    assert town_district("Not A Real Town") == ""


def test_resolve_district_from_town_and_text():
    assert resolve_district("Gampaha", "Kadawatha") == "Gampaha"
    assert resolve_district("", "Negombo") == "Gampaha"
    assert resolve_district("", "", "Heavy rain in Matara") == "Matara"
    assert resolve_district("not-a-district") == ""


def test_local_geocoding_precisions():
    exact = geocode_local("Kadawatha", "Gampaha")
    assert exact is not None
    assert exact.precision == "exact"
    assert round(exact.latitude, 2) == 6.98

    district = geocode_local("", "Kandy")
    assert district is not None
    assert district.precision == "district"

    assert geocode_local("Nowhereville", "") is None


def test_district_centroid():
    centroid = district_centroid("Colombo")
    assert centroid is not None
    assert centroid.precision == "district"
    assert 6.7 < centroid.latitude < 7.1
    assert 79.7 < centroid.longitude < 80.1
    assert district_centroid("Atlantis") is None


def test_sri_lanka_signal_scoring():
    assert sri_lanka_signal_score("Flooding hits Ratnapura town") == 2
    assert sri_lanka_signal_score("Sri Lanka's parliament meets today") == 1
    assert sri_lanka_signal_score("Manchester United win the derby") == 0
    assert text_has_sri_lanka_signal("Sri Lanka") is True
    assert text_has_sri_lanka_signal("World news roundup") is False
