"""Custom graph model for factory planning knowledge graph extraction.

Domain: Factory planning & material flow simulation.

Notes:
    - Field descriptions guide the LLM extractor — be precise.
    - All time-valued fields are STRINGS following the duration schema:
      constant ``d=40s`` or distribution ``normal(mean=300, std=45)``.
      All values are seconds. See ``graph_models.duration`` for the grammar.
    - Lengths in meters, weights in grams, speeds in m/s.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from knowledge.graph_models.duration import (
    AmbiguousRecord,
    get_ambiguous_records,
    reset_ambiguous_sink,
    validate_duration,
)


_DURATION_DESC = (
    "Duration as a string. Constant: 'd=40s'. "
    "Distribution: 'normal(mean=300, std=45)' or 'uniform(min=10, max=20)'. "
    "All values are seconds. Only use 'mean'/'std' (not mu/sigma), "
    "'min'/'max', 'lambda', 'k'. If the source text is ambiguous, put the "
    "raw text here — it will be flagged for review."
)


class Resource(BaseModel):
    """A discrete physical resource in the manufacturing system."""

    name: str = Field(
        description="Unique identifier or label of the resource (e.g. 'AKL-01', 'Workstation-3A')."
    )
    name_has_index: bool = Field(
        description=(
            "True when the name includes a clear index, ID, or code that distinguishes "
            "this resource from others of the same type (e.g. 'AKL-01', 'Workstation-3A', "
            "'AGV-02'). False when the name is a plain/generic word with no distinguishing "
            "index (e.g. 'Machine', 'Buffer', 'Conveyor')."
        )
    )
    description: str = Field(
        description=(
            "Semantically rich description of this resource: its function, location, "
            "role in the production flow, and any distinguishing characteristics. Always "
            "provide a description, even for sparse mentions. When re-encountering a "
            "known resource, extend the description with newly discovered context while "
            "preserving prior information."
        )
    )
    resource_type: str = Field(
        description=(
            "Type category. One of: machine, workstation, buffer, source, sink, "
            "conveyor, AS/RS, supermarket, warehouse, gate, charging_station, "
            "inspection_station, or other. NOTE: assembly_line and pick_zone are "
            "NOT resources — model them as Zone entities instead."
        )
    )
    processing_time: Optional[str] = Field(None, description="Processing / cycle time. " + _DURATION_DESC)
    setup_time: Optional[str] = Field(None, description="Setup / changeover time. " + _DURATION_DESC)
    capacity: Optional[int] = Field(None, description="Storage or processing capacity in units.")
    availability_pct: Optional[float] = Field(None, description="Technical availability in percent (0-100).")
    mtbf: Optional[str] = Field(None, description="Mean Time Between Failures. " + _DURATION_DESC)
    mttr: Optional[str] = Field(None, description="Mean Time To Repair. " + _DURATION_DESC)
    maintenance_duration: Optional[str] = Field(None, description="Planned maintenance duration. " + _DURATION_DESC)
    maintenance_interval: Optional[str] = Field(None, description="Planned maintenance interval. " + _DURATION_DESC)
    scrap_rate_pct: Optional[float] = Field(None, description="Scrap rate in percent.")
    rework_rate_pct: Optional[float] = Field(None, description="Rework rate in percent.")
    workers_required: Optional[int] = Field(None, description="Number of workers needed to operate this resource.")
    worker_qualification: Optional[str] = Field(None, description="Required worker skill or qualification level.")
    length_m: Optional[float] = Field(None, description="Physical length in meters.")
    width_m: Optional[float] = Field(None, description="Physical width in meters.")
    height_m: Optional[float] = Field(None, description="Physical height in meters.")
    position_x_m: Optional[float] = Field(None, description="X coordinate in layout in meters.")
    position_y_m: Optional[float] = Field(None, description="Y coordinate in layout in meters.")
    zone: Optional[str] = Field(None, description="Named area or zone this resource belongs to.")
    storage_policy: Optional[str] = Field(None, description="Buffer policy: FIFO, LIFO, priority, or other.")
    access_time: Optional[str] = Field(None, description="Time to access/retrieve an item (AS/RS, supermarket). " + _DURATION_DESC)
    loading_time: Optional[str] = Field(None, description="Loading time. " + _DURATION_DESC)
    unloading_time: Optional[str] = Field(None, description="Unloading time. " + _DURATION_DESC)
    opening_time: Optional[str] = Field(None, description="Gate/airlock opening time. " + _DURATION_DESC)
    closing_time: Optional[str] = Field(None, description="Gate/airlock closing time. " + _DURATION_DESC)
    cycle_time: Optional[str] = Field(None, description="Full passage or cycle time. " + _DURATION_DESC)
    reorder_point: Optional[int] = Field(None, description="Inventory level triggering replenishment.")
    assigned_products: List[str] = Field(default_factory=list, description="Product names or variant IDs assigned to this resource.")
    shift_model: Optional[str] = Field(None, description="Name of the shift model governing this resource.")
    additional_attributes: Optional[str] = Field(None, description="Any other stated attributes not covered above, as key:value pairs.")

    @field_validator(
        "processing_time", "setup_time", "mtbf", "mttr",
        "maintenance_duration", "maintenance_interval",
        "access_time", "loading_time", "unloading_time",
        "opening_time", "closing_time", "cycle_time",
        mode="before",
    )
    @classmethod
    def _validate_durations(cls, v, info):
        if v is None or v == "":
            return None
        name = info.data.get("name", "?")
        return validate_duration(v, "Resource", name, info.field_name)


class TransportVehicle(BaseModel):
    """A mobile transport unit: AGV, tugger train, forklift, AMR, etc."""

    name: str = Field(description="Unique identifier or label of the vehicle (e.g. 'AGV-01').")
    vehicle_type: str = Field(description="Type: AGV, tugger_train, forklift, AMR, or other.")
    speed_straight_ms: Optional[float] = Field(None, description="Average speed on straight segments in m/s.")
    speed_curve_ms: Optional[float] = Field(None, description="Average speed in curves / intersections in m/s.")
    speed_bay_ms: Optional[float] = Field(None, description="Average speed in bays in m/s.")
    transport_capacity: Optional[int] = Field(None, description="Number of containers/racks the vehicle can carry.")
    length_m: Optional[float] = Field(None, description="Vehicle length in meters.")
    width_m: Optional[float] = Field(None, description="Vehicle width in meters.")
    battery_capacity_kwh: Optional[float] = Field(None, description="Battery capacity in kWh.")
    charge_current_a: Optional[float] = Field(None, description="Charging current in Ampere.")
    charge_duration: Optional[str] = Field(None, description="Full charge duration. " + _DURATION_DESC)
    standby_consumption_a: Optional[float] = Field(None, description="Standby power consumption in Ampere.")
    driving_consumption_a: Optional[float] = Field(None, description="Driving power consumption in Ampere.")
    battery_swap_time: Optional[str] = Field(None, description="Battery swap time. " + _DURATION_DESC)
    availability_pct: Optional[float] = Field(None, description="Technical availability in percent.")
    mttr: Optional[str] = Field(None, description="Mean Time To Repair. " + _DURATION_DESC)
    maintenance_duration: Optional[str] = Field(None, description="Planned maintenance duration. " + _DURATION_DESC)
    maintenance_interval: Optional[str] = Field(None, description="Planned maintenance interval. " + _DURATION_DESC)
    transport_category: Optional[str] = Field(None, description="Product types this vehicle may carry.")

    @field_validator(
        "charge_duration", "battery_swap_time", "mttr",
        "maintenance_duration", "maintenance_interval",
        mode="before",
    )
    @classmethod
    def _validate_durations(cls, v, info):
        if v is None or v == "":
            return None
        name = info.data.get("name", "?")
        return validate_duration(v, "TransportVehicle", name, info.field_name)


class Trailer(BaseModel):
    """A passive transport attachment (trailer, rack frame)."""

    name: str = Field(description="Unique identifier of the trailer or rack.")
    length_m: Optional[float] = Field(None, description="Length in meters.")
    width_m: Optional[float] = Field(None, description="Width in meters.")
    height_m: Optional[float] = Field(None, description="Height in meters.")
    drawbar_length_m: Optional[float] = Field(None, description="Drawbar length in meters.")
    container_capacity: Optional[int] = Field(None, description="Number of handling units the trailer can carry.")


class TransportSegment(BaseModel):
    """A single track/path segment in the transport network."""

    name: str = Field(description="Segment identifier (e.g. 'Segment-A3', 'Aisle-7').")
    from_node: Optional[str] = Field(None, description="Name of the resource or intersection at the start.")
    to_node: Optional[str] = Field(None, description="Name of the resource or intersection at the end.")
    length_m: Optional[float] = Field(None, description="Segment length in meters.")
    speed_limit_ms: Optional[float] = Field(None, description="Maximum speed on this segment in m/s.")
    lane_capacity: Optional[int] = Field(None, description="Max vehicles simultaneously on segment.")
    width_m: Optional[float] = Field(None, description="Lane width in meters.")
    directionality: Optional[str] = Field(None, description="one_way, two_way, or reversible.")
    overtaking_possible: Optional[bool] = Field(None, description="Whether vehicles can overtake on this segment.")
    vehicle_type_restriction: Optional[str] = Field(None, description="Vehicle types allowed on this segment.")


class TransportRoute(BaseModel):
    """An ordered route through the transport network."""

    name: str = Field(description="Route name or identifier.")
    stop_sequence: List[str] = Field(default_factory=list, description="Ordered list of resource/stop names along the route.")
    waiting_positions: List[str] = Field(default_factory=list, description="Names of designated waiting/parking positions.")
    served_demand_points: List[str] = Field(default_factory=list, description="Demand points served by this route.")


class TrafficRule(BaseModel):
    """Right-of-way, priority, or interference traffic rule."""

    name: str = Field(description="Rule identifier or short label.")
    rule_type: str = Field(description="right_of_way, deadlock_avoidance, cross_traffic, speed_zone, or other.")
    description: str = Field(description="Full textual description of the rule and where it applies.")
    affected_segments: List[str] = Field(default_factory=list, description="Names of segments or intersections affected.")


class Product(BaseModel):
    """A product, part, variant, or container type."""

    name: str = Field(description="Product or part name/ID.")
    product_type: str = Field(description="product, part, variant, container, handling_unit, or raw_material.")
    length_m: Optional[float] = Field(None, description="Length in meters.")
    width_m: Optional[float] = Field(None, description="Width in meters.")
    height_m: Optional[float] = Field(None, description="Height in meters.")
    weight_g: Optional[float] = Field(None, description="Weight in grams.")
    parts_per_container: Optional[int] = Field(None, description="Number of parts per handling unit / container.")
    production_category: Optional[str] = Field(None, description="Production-specific category.")
    transport_category: Optional[str] = Field(None, description="Transport priority or category.")
    scrap_rate_pct: Optional[float] = Field(None, description="Scrap rate in percent.")
    rework_rate_pct: Optional[float] = Field(None, description="Rework rate in percent.")
    additional_process_step_pct: Optional[float] = Field(None, description="Percent requiring additional/modified steps.")
    bom_children: List[str] = Field(default_factory=list, description="Names of child parts in the bill of materials.")
    tracking_points: List[str] = Field(default_factory=list, description="Process points where this product is tracked.")


class ProductionProgram(BaseModel):
    """The production program defining variant mix, volumes, and sequencing."""

    name: str = Field(description="Program name or identifier (e.g. 'PP-2026-Q1').")
    variant_mix: Optional[str] = Field(None, description="Description of variant distribution or ratios.")
    volume_per_period: Optional[str] = Field(None, description="Target volume per time period.")
    sequence_rule: Optional[str] = Field(None, description="Sequencing or scheduling rule.")
    production_days_per_year: Optional[int] = Field(None, description="Number of production days per year.")


class OrderLogic(BaseModel):
    """Order trigger, dispatching, or replenishment logic."""

    name: str = Field(description="Name of the order type or trigger (e.g. 'Kanban-Refill-AKL').")
    order_category: str = Field(description="production_order, transport_order, replenishment, or inbound_delivery.")
    trigger_description: Optional[str] = Field(None, description="What event or condition triggers this order.")
    priority: Optional[str] = Field(None, description="Priority level or rule.")
    interval: Optional[str] = Field(
        None,
        description=(
            "Order interval or arrival distribution. Constant: 'd=120s'. "
            "Distribution: 'exponential(lambda=0.5)' or 'normal(mean=120, std=15)'. "
            "All values are seconds. If the source is ambiguous, put the raw text here — "
            "it will be flagged for review."
        ),
    )
    quantity: Optional[str] = Field(None, description="Order quantity or quantity rule.")
    associated_product: Optional[str] = Field(None, description="Product name this order relates to.")
    associated_resource: Optional[str] = Field(None, description="Resource name this order targets.")

    @field_validator("interval", mode="before")
    @classmethod
    def _validate_interval(cls, v, info):
        if v is None or v == "":
            return None
        name = info.data.get("name", "?")
        return validate_duration(v, "OrderLogic", name, info.field_name)


class ShiftModel(BaseModel):
    """A shift and break schedule."""

    name: str = Field(description="Shift model name (e.g. '3-shift-production').")
    num_shifts: Optional[int] = Field(None, description="Number of shifts per day.")
    shift_duration: Optional[str] = Field(None, description="Duration of one shift. " + _DURATION_DESC)
    break_times: Optional[str] = Field(None, description="Break schedule description (timing and duration).")
    applicable_zones: List[str] = Field(default_factory=list, description="Zones or resources this model applies to.")
    holidays: Optional[str] = Field(None, description="Company holiday or shutdown periods.")

    @field_validator("shift_duration", mode="before")
    @classmethod
    def _validate_shift_duration(cls, v, info):
        if v is None or v == "":
            return None
        name = info.data.get("name", "?")
        return validate_duration(v, "ShiftModel", name, info.field_name)


class WorkerPool(BaseModel):
    """A pool of workers with shared qualifications."""

    name: str = Field(description="Pool name (e.g. 'Logistics-Team-A').")
    headcount: Optional[int] = Field(None, description="Number of workers in the pool.")
    qualifications: List[str] = Field(default_factory=list, description="Skills or qualifications held by this pool.")
    assigned_resources: List[str] = Field(default_factory=list, description="Resource names this pool can operate.")
    assignment_rule: Optional[str] = Field(None, description="How workers from this pool are allocated to tasks.")


class ControlStrategy(BaseModel):
    """A control rule, dispatching strategy, or process constraint."""

    name: str = Field(description="Strategy name (e.g. 'FIFO-Dispatch', 'Opportunity-Charging').")
    strategy_type: str = Field(
        description=(
            "Type: dispatching, sequencing, batching, charging, empty_container_return, "
            "departure_trigger, process_dependency, incident_response, pick_strategy, or other."
        )
    )
    description: str = Field(description="Full description of the rule, when it applies, and its logic.")
    affected_resources: List[str] = Field(default_factory=list, description="Resource names governed by this strategy.")
    affected_products: List[str] = Field(default_factory=list, description="Product names affected.")


class Zone(BaseModel):
    """A spatial or logical area that groups resources: a functional area, hall,
    segment, assembly line, or pick zone.

    Zones are containers — they do NOT carry operational state themselves
    (no processing time, capacity, MTBF, or storage policy). Those attributes
    belong to the member Resources. A building-scale warehouse is a Zone; the
    single storage unit inside it is a Resource of type 'warehouse' or
    'supermarket'.
    """

    name: str = Field(description="Zone name (e.g. 'Hall-A', 'Assembly-Line-1', 'Pick-Zone-North').")
    zone_type: str = Field(
        description=(
            "Type category. One of: hall, area, segment, assembly_line, "
            "pick_zone, building, floor, or other."
        )
    )
    description: Optional[str] = Field(
        None,
        description=(
            "Semantically rich description of the zone: its function, layout, "
            "the resources it contains, and its role in the production flow."
        ),
    )
    parent_zone: Optional[str] = Field(
        None,
        description="Name of the enclosing zone (e.g. a hall containing an area). Supports nesting halls -> areas -> segments.",
    )
    floor_area_m2: Optional[float] = Field(None, description="Floor area in square meters.")
    ceiling_height_m: Optional[float] = Field(None, description="Ceiling height in meters.")
    coordinate_system: Optional[str] = Field(None, description="Coordinate system or scale description.")
    file_reference: Optional[str] = Field(None, description="File path or name of layout/CAD/3D file (DWG, DGN, JT, etc.) depicting this zone.")
    member_resources: List[str] = Field(
        default_factory=list,
        description="Names of Resources located inside this zone.",
    )


class KPI(BaseModel):
    """A performance target from requirements engineering."""

    name: str = Field(description="KPI name (e.g. 'Throughput-Line-A', 'Max-LeadTime').")
    kpi_type: str = Field(
        description="throughput, utilization, lead_time, delivery_performance, wip_limit, transport_time, buffer_fill, or other."
    )
    target_value: Optional[str] = Field(None, description="Numeric target value with unit (e.g. '120 units/hour').")
    scope: Optional[str] = Field(None, description="Which resource, area, or product this KPI applies to.")
    description: Optional[str] = Field(None, description="Additional context about the target.")


class AmbiguousDuration(BaseModel):
    """A duration field whose value could not be parsed into the canonical schema.

    Surfaced for human review. The raw text is preserved verbatim.
    """

    name: str = Field(description="Composite key: '<entity_type>:<entity_name>:<field_name>'.")
    entity_name: str = Field(description="Name of the entity holding the ambiguous field.")
    entity_type: str = Field(description="Entity class label (e.g. 'Resource', 'OrderLogic').")
    field_name: str = Field(description="Name of the duration field on the entity.")
    raw_value: str = Field(description="The unparseable raw string from the extraction.")
    note: Optional[str] = Field(None, description="Optional context about why it was flagged.")


class FactoryPlanningGraph(BaseModel):
    """Root extraction schema for factory planning documents.

    Extract all entities found in the text into the appropriate typed lists.
    Use consistent, exact entity names across extractions to enable deduplication.

    Duration policy:
        Every time-valued field is a STRING following a fixed grammar:
        - Constant: ``d=40s``
        - Distribution: ``normal(mean=300, std=45)`` / ``uniform(min=10, max=20)``
          / ``exponential(lambda=0.5)`` / ``weibull(k=1.5, lambda=200)``
        - All values are SECONDS. Use ``mean``/``std`` (not mu/sigma).
        - If the source text is ambiguous, put the raw text in the field; it
          will be flagged and copied into ``ambiguous_durations`` for review.

    Resource / Zone boundary:
        A Resource is an atomic, addressable asset that performs an operation or
        stores material — it has processing time, MTBF, capacity, access time,
        or storage policy. A Zone is a spatial/logical container that groups
        resources; it has no operational state of its own.
        - Resource types: machine, workstation, buffer, source, sink, conveyor,
          AS/RS, supermarket, warehouse, gate, charging_station,
          inspection_station, other.
        - Zone types: hall, area, segment, assembly_line, pick_zone, building,
          floor, other.
        assembly_line and pick_zone are Zones, NOT Resources. A building-scale
        warehouse is a Zone; the single storage unit inside it is a Resource
        of type 'warehouse' or 'supermarket'.

    Lengths in meters, weights in grams, speeds in m/s.
    Never extract personal names, contact information, or employee identifiers.

    Resource-specific rules:
    - Every resource must carry a semantically rich ``description`` capturing its
      function, location, role in the production flow, and distinguishing
      characteristics. Extend the description when new context is discovered.
    - Set ``name_has_index`` to True when the name includes a clear index, ID, or
      code (e.g. 'AKL-01', 'Workstation-3A'). Set it to False for plain/generic
      names with no distinguishing index (e.g. 'Machine', 'Buffer').
    """

    resources: List[Resource] = Field(default_factory=list, description="All physical resources: machines, buffers, stations, AS/RS, gates, supermarkets, warehouses, etc.")
    transport_vehicles: List[TransportVehicle] = Field(default_factory=list, description="AGVs, tugger trains, forklifts, AMRs.")
    trailers: List[Trailer] = Field(default_factory=list, description="Passive transport attachments.")
    transport_segments: List[TransportSegment] = Field(default_factory=list, description="Track/path segments with topology.")
    transport_routes: List[TransportRoute] = Field(default_factory=list, description="Named routes through the network.")
    traffic_rules: List[TrafficRule] = Field(default_factory=list, description="Right-of-way, interference, deadlock rules.")
    products: List[Product] = Field(default_factory=list, description="Products, parts, variants, containers.")
    production_programs: List[ProductionProgram] = Field(default_factory=list, description="Production program definitions.")
    order_logic: List[OrderLogic] = Field(default_factory=list, description="Order triggers, dispatching, replenishment.")
    shift_models: List[ShiftModel] = Field(default_factory=list, description="Shift and break schedules.")
    worker_pools: List[WorkerPool] = Field(default_factory=list, description="Worker groups with skills and assignments.")
    control_strategies: List[ControlStrategy] = Field(default_factory=list, description="Dispatching, sequencing, charging, and other control rules.")
    zones: List[Zone] = Field(default_factory=list, description="Spatial/logical areas that group resources: halls, areas, segments, assembly lines, pick zones.")
    kpis: List[KPI] = Field(default_factory=list, description="Performance targets from requirements.")
    ambiguous_durations: List[AmbiguousDuration] = Field(default_factory=list, description="Duration fields whose value could not be parsed into the canonical schema; surfaced for human review.")

    @model_validator(mode="before")
    @classmethod
    def _reset_ambiguous_sink(cls, data):
        """Install a fresh sink before child validators run."""
        reset_ambiguous_sink()
        return data

    @model_validator(mode="after")
    def _harvest_ambiguous(self):
        """Copy any ambiguous-duration records collected by child field
        validators into the top-level ``ambiguous_durations`` list."""
        records: list[AmbiguousRecord] = get_ambiguous_records()
        if records:
            existing = {(r.entity_type, r.entity_name, r.field_name, r.raw_value) for r in (
                self.ambiguous_durations or []
            )}
            for r in records:
                key = (r.entity_type, r.entity_name, r.field_name, r.raw_value)
                if key in existing:
                    continue
                self.ambiguous_durations.append(
                    AmbiguousDuration(
                        name=f"{r.entity_type}:{r.entity_name}:{r.field_name}",
                        entity_name=r.entity_name,
                        entity_type=r.entity_type,
                        field_name=r.field_name,
                        raw_value=r.raw_value,
                    )
                )
                existing.add(key)
        return self