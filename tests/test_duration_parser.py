"""Unit tests for the duration-string parser and model integration."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledge.graph_models.duration import (
    parse_duration,
    reset_ambiguous_sink,
    get_ambiguous_records,
    validate_duration,
)
from knowledge.graph_models.factory_graph_model import (
    FactoryPlanningGraph,
    Resource,
    OrderLogic,
    TransportVehicle,
    ShiftModel,
)


# ---------------------------------------------------------------------------
# parse_duration
# ---------------------------------------------------------------------------
def test_parse_constant_integer():
    r = parse_duration("d=40s")
    assert r.kind == "constant"
    assert r.is_ambiguous is False
    assert r.canonical == "d=40s"
    assert r.constant_seconds == 40.0


def test_parse_constant_decimal():
    r = parse_duration("d=40.5s")
    assert r.kind == "constant"
    assert r.canonical == "d=40.5s"
    assert r.constant_seconds == 40.5


def test_parse_constant_tolerates_whitespace():
    r = parse_duration("d = 40 s")
    assert r.kind == "constant"
    assert r.canonical == "d=40s"


def test_parse_distribution_normal():
    r = parse_duration("normal(mean=300, std=45)")
    assert r.kind == "distribution"
    assert r.distribution_name == "normal"
    assert r.distribution_params == {"mean": 300.0, "std": 45.0}
    assert r.canonical == "normal(mean=300, std=45)"


def test_parse_distribution_rewrites_mu_sigma():
    r = parse_duration("normal(mu=300, sigma=45)")
    assert r.kind == "distribution"
    assert r.distribution_params == {"mean": 300.0, "std": 45.0}
    assert r.canonical == "normal(mean=300, std=45)"


def test_parse_distribution_uniform_no_space():
    r = parse_duration("uniform(min=10,max=20)")
    assert r.kind == "distribution"
    assert r.distribution_params == {"min": 10.0, "max": 20.0}
    assert r.canonical == "uniform(min=10, max=20)"


def test_parse_distribution_exponential():
    r = parse_duration("exponential(lambda=0.5)")
    assert r.kind == "distribution"
    assert r.distribution_name == "exponential"
    assert r.canonical == "exponential(lambda=0.5)"


def test_parse_distribution_unknown_name_is_ambiguous():
    r = parse_duration("gamma(alpha=2, beta=3)")
    assert r.kind == "ambiguous"
    assert r.is_ambiguous is True


def test_parse_positional_args_are_ambiguous():
    # exponential(120) lacks key=value -> ambiguous per schema
    r = parse_duration("exponential(120)")
    assert r.kind == "ambiguous"
    assert r.is_ambiguous is True


def test_parse_garbage_is_ambiguous():
    r = parse_duration("about half an hour")
    assert r.kind == "ambiguous"
    assert r.canonical == "about half an hour"


def test_parse_empty_is_ambiguous():
    r = parse_duration("")
    assert r.kind == "ambiguous"


# ---------------------------------------------------------------------------
# validate_duration + sink
# ---------------------------------------------------------------------------
def test_validate_duration_records_ambiguous():
    reset_ambiguous_sink()
    out = validate_duration("roughly two shifts", "Resource", "M-1", "processing_time")
    assert out == "roughly two shifts"
    recs = get_ambiguous_records()
    assert len(recs) == 1
    assert recs[0].entity_type == "Resource"
    assert recs[0].entity_name == "M-1"
    assert recs[0].field_name == "processing_time"


def test_validate_duration_canonicalizes_constant():
    reset_ambiguous_sink()
    out = validate_duration("d=40s", "Resource", "M-1", "processing_time")
    assert out == "d=40s"
    assert get_ambiguous_records() == []


# ---------------------------------------------------------------------------
# Model integration
# ---------------------------------------------------------------------------
def test_resource_valid_duration_round_trips():
    g = FactoryPlanningGraph.model_validate_json(
        '{"resources": [{"name": "M-1", "name_has_index": true, "description": "m", '
        '"resource_type": "machine", "processing_time": "d=40s", "mtbf": "normal(mean=300, std=45)"}]}'
    )
    r = g.resources[0]
    assert r.processing_time == "d=40s"
    assert r.mtbf == "normal(mean=300, std=45)"
    assert g.ambiguous_durations == []


def test_resource_mu_sigma_is_canonicalized():
    g = FactoryPlanningGraph.model_validate_json(
        '{"resources": [{"name": "M-1", "name_has_index": true, "description": "m", '
        '"resource_type": "machine", "mtbf": "normal(mu=300, sigma=45)"}]}'
    )
    assert g.resources[0].mtbf == "normal(mean=300, std=45)"
    assert g.ambiguous_durations == []


def test_resource_ambiguous_lands_in_top_list():
    g = FactoryPlanningGraph.model_validate_json(
        '{"resources": [{"name": "M-2", "name_has_index": true, "description": "m", '
        '"resource_type": "machine", "processing_time": "roughly two shifts"}]}'
    )
    assert g.resources[0].processing_time == "roughly two shifts"
    assert len(g.ambiguous_durations) == 1
    a = g.ambiguous_durations[0]
    assert a.entity_type == "Resource"
    assert a.entity_name == "M-2"
    assert a.field_name == "processing_time"
    assert a.raw_value == "roughly two shifts"
    assert a.name == "Resource:M-2:processing_time"


def test_orderlogic_interval_distribution():
    g = FactoryPlanningGraph.model_validate_json(
        '{"order_logic": [{"name": "O-1", "order_category": "production_order", '
        '"interval": "exponential(lambda=0.5)"}]}'
    )
    assert g.order_logic[0].interval == "exponential(lambda=0.5)"
    assert g.ambiguous_durations == []


def test_orderlogic_interval_ambiguous_flagged():
    g = FactoryPlanningGraph.model_validate_json(
        '{"order_logic": [{"name": "O-1", "order_category": "production_order", '
        '"interval": "every few hours"}]}'
    )
    assert g.order_logic[0].interval == "every few hours"
    assert len(g.ambiguous_durations) == 1
    assert g.ambiguous_durations[0].entity_type == "OrderLogic"


def test_transportvehicle_duration_fields():
    g = FactoryPlanningGraph.model_validate_json(
        '{"transport_vehicles": [{"name": "AGV-01", "vehicle_type": "AGV", '
        '"charge_duration": "d=1800s", "mttr": "uniform(min=300, max=900)"}]}'
    )
    v = g.transport_vehicles[0]
    assert v.charge_duration == "d=1800s"
    assert v.mttr == "uniform(min=300, max=900)"
    assert g.ambiguous_durations == []


def test_shiftmodel_shift_duration():
    g = FactoryPlanningGraph.model_validate_json(
        '{"shift_models": [{"name": "3-shift", "shift_duration": "d=28800s"}]}'
    )
    assert g.shift_models[0].shift_duration == "d=28800s"
    assert g.ambiguous_durations == []


def test_ambiguous_durations_deduplicated():
    # two identical ambiguous entries should only appear once
    g = FactoryPlanningGraph.model_validate_json(
        '{"resources": ['
        '{"name": "M-2", "name_has_index": true, "description": "m", "resource_type": "machine", "processing_time": "roughly two shifts"}'
        ']}'
    )
    # re-validate the same data via model_validate to trigger a second pass
    g2 = FactoryPlanningGraph.model_validate(g.model_dump())
    assert len(g2.ambiguous_durations) == len(g.ambiguous_durations)