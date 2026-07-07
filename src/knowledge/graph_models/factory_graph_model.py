"""Custom graph model for factory planning knowledge graph extraction.

Domain: Factory planning & material flow simulation.

Notes:
    - Field descriptions guide the LLM extractor — be precise.
    - All time values in seconds, lengths in meters, weights in grams, speeds in m/s.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class Resource(BaseModel):
    """A discrete physical resource in the manufacturing system."""

    name: str = Field(
        description="Unique identifier or label of the resource (e.g. 'AKL-01', 'Workstation-3A')."
    )
    resource_type: str = Field(
        description=(
            "Type category. One of: machine, workstation, buffer, source, sink, "
            "conveyor, AS/RS, supermarket, warehouse, gate, charging_station, "
            "pick_zone, assembly_line, inspection_station, or other."
        )
    )
    processing_time_s: Optional[float] = Field(None, description="Processing / cycle time in seconds.")
    setup_time_s: Optional[float] = Field(None, description="Setup / changeover time in seconds.")
    capacity: Optional[int] = Field(None, description="Storage or processing capacity in units.")
    availability_pct: Optional[float] = Field(None, description="Technical availability in percent (0-100).")
    mtbf_s: Optional[float] = Field(None, description="Mean Time Between Failures in seconds.")
    mttr_s: Optional[float] = Field(None, description="Mean Time To Repair in seconds.")
    maintenance_duration_s: Optional[float] = Field(None, description="Planned maintenance duration in seconds.")
    maintenance_interval_s: Optional[float] = Field(None, description="Planned maintenance interval in seconds.")
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
    access_time_s: Optional[float] = Field(None, description="Time to access/retrieve an item in seconds (AS/RS, supermarket).")
    loading_time_s: Optional[float] = Field(None, description="Loading time in seconds.")
    unloading_time_s: Optional[float] = Field(None, description="Unloading time in seconds.")
    opening_time_s: Optional[float] = Field(None, description="Gate/airlock opening time in seconds.")
    closing_time_s: Optional[float] = Field(None, description="Gate/airlock closing time in seconds.")
    cycle_time_s: Optional[float] = Field(None, description="Full passage or cycle time in seconds.")
    reorder_point: Optional[int] = Field(None, description="Inventory level triggering replenishment.")
    assigned_products: List[str] = Field(default_factory=list, description="Product names or variant IDs assigned to this resource.")
    shift_model: Optional[str] = Field(None, description="Name of the shift model governing this resource.")
    additional_attributes: Optional[str] = Field(None, description="Any other stated attributes not covered above, as key:value pairs.")


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
    charge_duration_s: Optional[float] = Field(None, description="Full charge duration in seconds.")
    standby_consumption_a: Optional[float] = Field(None, description="Standby power consumption in Ampere.")
    driving_consumption_a: Optional[float] = Field(None, description="Driving power consumption in Ampere.")
    battery_swap_time_s: Optional[float] = Field(None, description="Battery swap time in seconds.")
    availability_pct: Optional[float] = Field(None, description="Technical availability in percent.")
    mttr_s: Optional[float] = Field(None, description="Mean Time To Repair in seconds.")
    maintenance_duration_s: Optional[float] = Field(None, description="Planned maintenance duration in seconds.")
    maintenance_interval_s: Optional[float] = Field(None, description="Planned maintenance interval in seconds.")
    transport_category: Optional[str] = Field(None, description="Product types this vehicle may carry.")


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
    distribution: Optional[str] = Field(None, description="Statistical distribution of order arrival (e.g. 'exponential(120)').")
    interval_s: Optional[float] = Field(None, description="Order interval in seconds.")
    quantity: Optional[str] = Field(None, description="Order quantity or quantity rule.")
    associated_product: Optional[str] = Field(None, description="Product name this order relates to.")
    associated_resource: Optional[str] = Field(None, description="Resource name this order targets.")


class ShiftModel(BaseModel):
    """A shift and break schedule."""

    name: str = Field(description="Shift model name (e.g. '3-shift-production').")
    num_shifts: Optional[int] = Field(None, description="Number of shifts per day.")
    shift_duration_s: Optional[float] = Field(None, description="Duration of one shift in seconds.")
    break_times: Optional[str] = Field(None, description="Break schedule description (timing and duration).")
    applicable_zones: List[str] = Field(default_factory=list, description="Zones or resources this model applies to.")
    holidays: Optional[str] = Field(None, description="Company holiday or shutdown periods.")


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


class LayoutElement(BaseModel):
    """A spatial or structural element of the factory layout."""

    name: str = Field(description="Element name (e.g. 'Hall-A', 'Layout-DWG-Rev3').")
    element_type: str = Field(description="layout_file, zone, area, building, floor, 3d_model_reference, or other.")
    file_reference: Optional[str] = Field(None, description="File path or name of layout/CAD/3D file (DWG, DGN, JT, etc.).")
    floor_area_m2: Optional[float] = Field(None, description="Floor area in square meters.")
    ceiling_height_m: Optional[float] = Field(None, description="Ceiling height in meters.")
    coordinate_system: Optional[str] = Field(None, description="Coordinate system or scale description.")
    description: Optional[str] = Field(None, description="Additional spatial description.")


class KPI(BaseModel):
    """A performance target from requirements engineering."""

    name: str = Field(description="KPI name (e.g. 'Throughput-Line-A', 'Max-LeadTime').")
    kpi_type: str = Field(
        description="throughput, utilization, lead_time, delivery_performance, wip_limit, transport_time, buffer_fill, or other."
    )
    target_value: Optional[str] = Field(None, description="Numeric target value with unit (e.g. '120 units/hour').")
    scope: Optional[str] = Field(None, description="Which resource, area, or product this KPI applies to.")
    description: Optional[str] = Field(None, description="Additional context about the target.")


class StochasticParameter(BaseModel):
    """A parameter defined by a probability distribution rather than a fixed value."""

    name: str = Field(description="What this distribution describes (e.g. 'MTBF-Machine-3', 'Delivery-Delay').")
    distribution_type: str = Field(
        description="Distribution name: normal, exponential, uniform, triangular, weibull, lognormal, erlang, empirical, or other."
    )
    parameters: str = Field(
        description="Distribution parameters as key-value string (e.g. 'mean=300,std=45' or 'min=10,max=20')."
    )
    unit: Optional[str] = Field(None, description="Unit of the distributed value (e.g. 'seconds', 'meters').")
    associated_entity: Optional[str] = Field(None, description="Name of the resource or process this distribution belongs to.")


class FactoryPlanningGraph(BaseModel):
    """Root extraction schema for factory planning documents.

    Extract all entities found in the text into the appropriate typed lists.
    Use consistent, exact entity names across extractions to enable deduplication.
    All time values in seconds, lengths in meters, weights in grams, speeds in m/s.
    Never extract personal names, contact information, or employee identifiers.
    """

    resources: List[Resource] = Field(default_factory=list, description="All physical resources: machines, buffers, stations, AS/RS, gates, etc.")
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
    layout_elements: List[LayoutElement] = Field(default_factory=list, description="Spatial and structural layout data.")
    kpis: List[KPI] = Field(default_factory=list, description="Performance targets from requirements.")
    stochastic_parameters: List[StochasticParameter] = Field(default_factory=list, description="Parameters defined by probability distributions.")
