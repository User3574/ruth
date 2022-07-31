import functools
import logging

from datetime import timedelta
from itertools import chain, groupby
from multiprocessing import Pool
from typing import Any, List, Dict, Tuple, Callable, NewType

from probduration import VehiclePlan, Route, SegmentPosition

from ..globalview import GlobalView
from ..utils import osm_route_to_segments, route_to_osm_route
from ..vehicle import Vehicle
from ..distsim import alternatives, advance_vehicle
from ..losdb import GlobalViewDb

from .simulation import Simulation, VehicleUpdate


logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


VehiclePlans = NewType("VehiclePlans", List[Tuple[Vehicle, VehiclePlan]])
Comparable = NewType("Comparable", Any)


class Simulator:

    def __init__(self, sim: Simulation, nproc=1):
        """Initialize the simulator.

        Parameters:
        -----------
            sim: Simulation
                State of the simulation.
            nproc: int [default=1]
                Number of concurrent processes.
        """
        self.sim = sim
        self.pool = None
        self.nproc = nproc

    def __enter__(self):
        self.pool = Pool(processes=self.nproc)
        return self

    def __exit__(self, exc_type_, exc_val_, exc_tb_):
        self.pool.close()

    @property
    def state(self):
        return self.sim

    def simulate(self,
                 route_ranking_fn: Callable[[GlobalView, VehiclePlans, List, Dict], Comparable],
                 rr_fn_args=(),
                 rr_fn_kwargs=None,
                 extend_plans_fn: Callable[[VehiclePlans, List, Dict], Any] = lambda plans: plans,
                 ep_fn_args=(),
                 ep_fn_kwargs=None,
                 end_step_fn: Callable[[Simulation, List, Dict], None] = lambda _: None,
                 es_fn_args=(),
                 es_fn_kwargs=None):
        """Perform the simulation.

        Parameters:
        -----------
            route_ranking_fn: Callable[[GlobalView, VehiclePlans, List, Dict], Comparable]
              Compute a (comparable) rank for a route
            rr_fn_args: Tuple
              Positional arguments for route ranking function
            rr_fn_kwargs: Dict
              Keyword arguments for route ranking function
            extend_plans_fn: Callable[[VehiclePlans, List, Dict, Any]
              Extends a list of vehicle plans about additional information. This information can be then accessed
              within route ranking function.
            ep_fn_args: Tuple
              Positional arguments for extend vehicle plans function
            ep_fn_kwargs: Dict
              Keyword arguments for extend vehicle plans function
            end_step_fn: Callable[[Simulation], None]
              An arbitrary function that is called at the end of each step with the current state of simulation.
              It can be used for storing the state, for example.
            es_fn_args: Tuple
              Positional arguments for end-step function.
            es_fn_kwargs: Dict
              Keyword arguments for end-step function
        """

        current_offset = timedelta(seconds=0)

        while current_offset is not None:
            offset = self.sim.round_time_offset(current_offset)
            allowed_vehicles = list(filter(lambda v: self.sim.is_vehicle_within_offset(v, offset), self.sim.vehicles))

            alts = self.alternatives(allowed_vehicles)

            if not alts:
                new_vehicles = []
                for v in allowed_vehicles:
                    v.active = False
                    leap_history, v.leap_history = v.leap_history, []
                    new_vehicles.append(VehicleUpdate(v, leap_history))
            else:
                vehicle_plans = chain.from_iterable(
                    filter(None, map(functools.partial(prepare_vehicle_plans,
                                                       departure_time=self.sim.setting.departure_time),
                                     alts)))

                selected_plans = select_plans(vehicle_plans,
                                              route_ranking_fn, rr_fn_args, rr_fn_kwargs,
                                              extend_plans_fn, ep_fn_args, ep_fn_kwargs)

                assert selected_plans, "Unexpected empty list of selected plans."

                def transform_plan(vehicle_plan):
                    vehicle, plan = vehicle_plan
                    return vehicle, route_to_osm_route(plan.route)
                bests = map(transform_plan, selected_plans)

                new_vehicles = [self.advance_vehicle(best, offset) for best in bests]

            self.sim.update(new_vehicles)
            current_offset_new = self.sim.compute_current_offset()
            if current_offset_new == current_offset:
                logger.error(f"The consecutive step with the same offset: {current_offset}.")
                logger.error(allowed_vehicles)
                break
            current_offset = current_offset_new
            self.sim.drop_old_records(current_offset)

            es_fn_kwargs_ = {} if es_fn_kwargs is None else es_fn_kwargs
            end_step_fn(self.sim, *es_fn_args, **es_fn_kwargs_)
        logger.info("Simulation done!")

        for v in self.sim.vehicles:
            print(v)
            print(v.osm_route)

    def alternatives(self, vehicles):
        if self.pool is None:
            logger.info("The alternative routes are computed without multiprocessing.")
            map_fn = map
        else:
            map_fn = self.pool.map

        alts = list(filter(None, map_fn(functools.partial(alternatives, k=self.sim.setting.k_alternatives),
                                        vehicles)))

        if not alts:
            offsets = sorted(v.time_offset for v in vehicles)
            logger.debug(f"No alternatives found at offset range: ({offsets[0]}, {offsets[-1]})")
        return alts

    def advance_vehicle(self, vehicle_route, current_offset):
        """Move with the vehicle on the route (update its state), and disentangle its leap history"""

        vehicle, osm_route = vehicle_route

        leap_history = []
        if vehicle.is_active(current_offset, self.sim.setting.round_freq):
            new_vehicle = advance_vehicle(vehicle, osm_route,
                                          self.sim.setting.departure_time, GlobalViewDb(self.sim.global_view))
            # swap the empty history with the filled one
            leap_history, new_vehicle.leap_history = new_vehicle.leap_history, leap_history
        else:
            new_vehicle = vehicle

        return VehicleUpdate(new_vehicle, leap_history)


def select_plans(vehicle_plans,
                 rank_fn, rank_fn_args=(), rank_fn_kwargs=None,
                 extend_plans_fn=lambda plans: plans, ep_fn_args=(), ep_fn_kwargs=None):
    """Select from alternatives based on rank function.

    Parameters:
    -----------
        vehicle_plans: List[Tuple[Vehicle, VehiclePlan]]
        rank_fn: Callable[[List, Dict], Comparable]
          A route ranking function
        rank_fn_args: Tuple
        rank_fn_kwargs: Dict
        extend_plans_fn: Callable[[List, Dict], Comparable]
          A function that can extend the vehicle plans by additional information which can be used in route ranking
          function.
        ep_fn_args: Tuple
        ep_fn_kwargs: Dict
    """

    vehicle_plans_ = list(vehicle_plans)  # materialize the iterator as it is used twice

    ep_fn_kwargs_ = {} if ep_fn_kwargs is None else ep_fn_kwargs
    vehicle_plans_extended = extend_plans_fn(vehicle_plans_, *ep_fn_args, **ep_fn_kwargs_)

    rank_fn_kwargs_ = {} if rank_fn_kwargs is None else rank_fn_kwargs
    ranks = map(functools.partial(rank_fn, *rank_fn_args, **rank_fn_kwargs_), vehicle_plans_extended)

    def by_plan_id(data):
        (_, plan), _ = data
        return plan.id

    def by_rank(data):
        _, rank = data
        return rank

    data = groupby(sorted(zip(vehicle_plans_, ranks), key=by_plan_id), by_plan_id)

    def select_best(data):
        plan_id, group = data
        # a single group contains tuple with vehicle plan and computed duration, respectively
        return sorted(group, key=by_rank)[0][0]

    return list(map(select_best, data))


def prepare_vehicle_plans(alt, departure_time):
    """Prepare a list of vehicle plans for particular alternative. Returns a list of pairs: (`Vehicle`, `VehiclePlan`)

    Parameters:
    -----------
        alt: (Vehicle, List[int])
          A pair of vehicle and list of alternative osm routes (one osm route is a list of node ids).
        departure_time: datetime
          A departure time of the simulation.

    """
    vehicle, osm_routes = alt
    if osm_routes is None:
        logger.debug(f"No alternative for vehicle: {vehicle.id}")
        return None

    return [(vehicle, VehiclePlan(vehicle.id,
                                  Route(osm_route_to_segments(osm_route, vehicle.routing_map), vehicle.frequency),
                                  SegmentPosition(0, 0.0),
                                  departure_time + vehicle.time_offset))
            for osm_route in osm_routes]