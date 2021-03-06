"""mirgecom driver for the Y0 demonstration.

Note: this example requires a *scaled* version of the Y0
grid. A working grid example is located here:
github.com:/illinois-ceesd/data@y0scaled
"""

__copyright__ = """
Copyright (C) 2020 University of Illinois Board of Trustees
"""

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""
import yaml
import logging
import numpy as np
import pyopencl as cl
import numpy.linalg as la  # noqa
import pyopencl.array as cla  # noqa
from functools import partial
import math

from pytools.obj_array import obj_array_vectorize
import pickle

from meshmode.array_context import PyOpenCLArrayContext
from meshmode.dof_array import thaw, flatten, unflatten
from meshmode.mesh import BTAG_ALL, BTAG_NONE  # noqa
from grudge.eager import EagerDGDiscretization
from grudge.shortcuts import make_visualizer

from mirgecom.profiling import PyOpenCLProfilingArrayContext

from mirgecom.euler import euler_operator
from mirgecom.navierstokes import ns_operator
from mirgecom.fluid import split_conserved
from mirgecom.simutil import (
    inviscid_sim_timestep,
    sim_checkpoint,
    check_step,
    generate_and_distribute_mesh
)
from mirgecom.io import make_init_message
from mirgecom.mpi import mpi_entry_point
import pyopencl.tools as cl_tools
# from mirgecom.checkstate import compare_states
from mirgecom.integrators import (
    rk4_step, 
    lsrk54_step, 
    lsrk144_step, 
    euler_step
)
from mirgecom.steppers import advance_state
from mirgecom.boundary import (
    PrescribedViscousBoundary
)
from mirgecom.initializers import (
    PlanarDiscontinuity,
    MixtureInitializer
)
from mirgecom.transport import SimpleTransport
from mirgecom.eos import PyrometheusMixture
import cantera
import pyrometheus as pyro

from logpyle import IntervalTimer

from mirgecom.euler import extract_vars_for_logging, units_for_logging
from mirgecom.logging_quantities import (initialize_logmgr,
    logmgr_add_many_discretization_quantities, logmgr_add_cl_device_info,
    logmgr_set_time, logmgr_add_device_memory_usage)
logger = logging.getLogger(__name__)


@mpi_entry_point
def run_flame(ctx_factory=cl.create_some_context, casename="flame1d", user_input_file="",
         snapshot_pattern="flame1d-{step:06d}-{rank:04d}.pkl",
         restart_step=None, restart_name=None, use_logmgr=False):
    """Drive the 1D Flame example."""

    from mpi4py import MPI
    comm = MPI.COMM_WORLD
    rank = 0
    rank = comm.Get_rank()
    nparts = comm.Get_size()

    if restart_step is None:
        if rank == 0:
            logging.info("Driver only supports restart, not initialization")
        exit()

    if restart_name is None:
      restart_name=casename

    """logging and profiling"""
    logmgr = initialize_logmgr(use_logmgr, filename="flame1d.sqlite",
        mode="wo", mpi_comm=comm)

    cl_ctx = ctx_factory()
    queue = cl.CommandQueue(cl_ctx)
    actx = PyOpenCLArrayContext(queue,
        allocator=cl_tools.MemoryPool(cl_tools.ImmediateAllocator(queue)))

    #nviz = 500
    #nrestart = 500
    nviz = 5
    nrestart = 5
    #current_dt = 5.0e-8 # stable with euler
    current_dt = 5.0e-8 # stable with rk4
    #current_dt = 4e-7 # stable with lrsrk144
    t_final = 2.5e-7

    #input_data = yaml.load_all(open('user_inputs.yaml', 'r'))
    if(user_input_file):
        #input_data = yaml.load_all(open('{user_input_file}'.format(user_input_file=user_input_file), 'r'))
        input_data = yaml.load_all(open(user_input_file), 'r')
        #input_data = yaml.load_all(open('run2_params.yaml'), 'r')
        with open('run2_params.yaml') as f:
            input_data = yaml.load(f, Loader=yaml.FullLoader)
            #print(input_data)
        nviz = int(input_data["nviz"])
        nrestart = int(input_data["nrestart"])
        current_dt = float(input_data["current_dt"])
        t_final = float(input_data["t_final"])

    if(rank == 0):
        print(f'Simluation control data:')
        print(f'\tnviz = {nviz}')
        print(f'\tnrestart = {nrestart}')
        print(f'\tcurrent_dt = {current_dt}')
        print(f'\tt_final = {t_final}')

    dim = 2
    order = 1
    exittol = 1000000000000
    #t_final = 0.001
    current_cfl = 1.0
    current_t = 0
    constant_cfl = False
    nstatus = 10000000000
    checkpoint_t = current_t
    current_step = 0
    vel_burned = np.zeros(shape=(dim,))
    vel_unburned = np.zeros(shape=(dim,))

    # {{{  Set up initial state using Cantera

    # Use Cantera for initialization
    # -- Pick up a CTI for the thermochemistry config
    # --- Note: Users may add their own CTI file by dropping it into
    # ---       mirgecom/mechanisms alongside the other CTI files.
    from mirgecom.mechanisms import get_mechanism_cti
    # uiuc C2H4
    #mech_cti = get_mechanism_cti("uiuc")
    # sanDiego H2
    mech_cti = get_mechanism_cti("sanDiego")

    cantera_soln = cantera.Solution(phase_id="gas", source=mech_cti)
    nspecies = cantera_soln.n_species

    # Initial temperature, pressure, and mixutre mole fractions are needed to
    # set up the initial state in Cantera.
    temp_unburned = 300.0
    temp_ignition = 1500.0
    # Parameters for calculating the amounts of fuel, oxidizer, and inert species
    equiv_ratio = 1.0
    ox_di_ratio = 0.21
    # H2
    stoich_ratio = 0.5
    #C2H4
    #stoich_ratio = 3.0
    # Grab the array indices for the specific species, ethylene, oxygen, and nitrogen
    # C2H4
    #i_fu = cantera_soln.species_index("C2H4")
    # H2
    i_fu = cantera_soln.species_index("H2")
    i_ox = cantera_soln.species_index("O2")
    i_di = cantera_soln.species_index("N2")
    x = np.zeros(nspecies)
    # Set the species mole fractions according to our desired fuel/air mixture
    x[i_fu] = (ox_di_ratio*equiv_ratio)/(stoich_ratio+ox_di_ratio*equiv_ratio)
    x[i_ox] = stoich_ratio*x[i_fu]/equiv_ratio
    x[i_di] = (1.0-ox_di_ratio)*x[i_ox]/ox_di_ratio
    # Uncomment next line to make pylint fail when it can't find cantera.one_atm
    one_atm = cantera.one_atm  # pylint: disable=no-member
    # one_atm = 101325.0
    pres_unburned = one_atm

    # Let the user know about how Cantera is being initilized
    print(f"Input state (T,P,X) = ({temp_unburned}, {pres_unburned}, {x}")
    # Set Cantera internal gas temperature, pressure, and mole fractios
    cantera_soln.TPX = temp_unburned, pres_unburned, x
    # Pull temperature, total density, mass fractions, and pressure from Cantera
    # We need total density, and mass fractions to initialize the fluid/gas state.
    y_unburned = np.zeros(nspecies)
    can_t, rho_unburned, y_unburned = cantera_soln.TDY
    can_p = cantera_soln.P
    # *can_t*, *can_p* should not differ (significantly) from user's initial data,
    # but we want to ensure that we use exactly the same starting point as Cantera,
    # so we use Cantera's version of these data.


    # now find the conditions for the burned gas
    cantera_soln.equilibrate('TP')
    temp_burned, rho_burned, y_burned = cantera_soln.TDY
    pres_burned = cantera_soln.P

    pyrometheus_mechanism = pyro.get_thermochem_class(cantera_soln)(actx.np)

    # C2H4
    mu = 1.e-5
    kappa = 1.6e-5  # Pr = mu*rho/alpha = 0.75
    # H2
    mu = 1.e-5
    kappa = mu*0.08988/0.75  # Pr = mu*rho/alpha = 0.75

    species_diffusivity = 1.e-5 * np.ones(nspecies)
    transport_model = SimpleTransport(viscosity=mu, thermal_conductivity=kappa, species_diffusivity=species_diffusivity)

    eos = PyrometheusMixture(pyrometheus_mechanism, temperature_guess=temp_unburned, transport_model=transport_model)
    species_names = pyrometheus_mechanism.species_names

    print(f"Pyrometheus mechanism species names {species_names}")
    print(f"Unburned state (T,P,Y) = ({temp_unburned}, {pres_unburned}, {y_unburned}")
    print(f"Burned state (T,P,Y) = ({temp_burned}, {pres_burned}, {y_burned}")

    flame_start_loc = 0.05
    flame_speed = 1000

    # use the burned conditions with a lower temperature
    #bulk_init = PlanarDiscontinuity(dim=dim, disc_location=flame_start_loc, sigma=0.01, nspecies=nspecies,
                              #temperature_left=temp_ignition, temperature_right=temp_unburned,
                              #pressure_left=pres_burned, pressure_right=pres_unburned,
                              #velocity_left=vel_burned, velocity_right=vel_unburned,
                              #species_mass_left=y_burned, species_mass_right=y_unburned)

    inflow_init = MixtureInitializer(dim=dim, nspecies=nspecies, pressure=pres_burned, 
                                     temperature=temp_ignition, massfractions= y_burned,
                                     velocity=vel_burned)
    outflow_init = MixtureInitializer(dim=dim, nspecies=nspecies, pressure=pres_unburned, 
                                     temperature=temp_unburned, massfractions= y_unburned,
                                     velocity=vel_unburned)

    inflow = PrescribedViscousBoundary(q_func=inflow_init)
    outflow = PrescribedViscousBoundary(q_func=outflow_init)
    wall = PrescribedViscousBoundary()  # essentially a "dummy" use the interior solution for the exterior

    from grudge import sym
    boundaries = {sym.DTAG_BOUNDARY("Inflow"): inflow,
                  sym.DTAG_BOUNDARY("Outflow"): outflow,
                  sym.DTAG_BOUNDARY("Wall"): wall}

    # process restart
    with open(snapshot_pattern.format(casename=restart_name, step=restart_step, rank=rank), "rb") as f:
        restart_data = pickle.load(f)
    local_mesh = restart_data["local_mesh"]
    local_nelements = local_mesh.nelements
    global_nelements = restart_data["global_nelements"]

    assert comm.Get_size() == restart_data["num_parts"]

    if rank == 0:
        logging.info("Making discretization")
    discr = EagerDGDiscretization(
        actx, local_mesh, order=order, mpi_communicator=comm
    )
    nodes = thaw(actx, discr.nodes())

    current_t = restart_data["t"]
    current_step = restart_step

    current_state = unflatten(
        actx, discr.discr_from_dd("vol"),
        obj_array_vectorize(actx.from_numpy, restart_data["state"]))

    vis_timer = None

    if logmgr:
        logmgr_add_cl_device_info(logmgr, queue)
        logmgr_add_many_discretization_quantities(logmgr, discr, dim,
            extract_vars_for_logging, units_for_logging)
        logmgr_set_time(logmgr, current_step, current_t)
        logmgr.add_watches(["step.max", "t_sim.max", "t_step.max", "t_log.max",
                            "min_pressure", "max_pressure",
                            "min_temperature", "max_temperature"])

        try:
            logmgr.add_watches(["memory_usage_python.max",
                                "memory_usage_gpu.max"])
        except KeyError:
            pass

        vis_timer = IntervalTimer("t_vis", "Time spent visualizing")
        logmgr.add_quantity(vis_timer)

    visualizer = make_visualizer(discr, order)
    timestepper = euler_step

    get_timestep = partial(inviscid_sim_timestep, discr=discr, t=current_t,
                           dt=current_dt, cfl=current_cfl, eos=eos,
                           t_final=t_final, constant_cfl=constant_cfl)

    
    def my_rhs(t, state):
        # check for some troublesome output types
        inf_exists = not np.isfinite(discr.norm(state, np.inf))
        if inf_exists:
            if rank == 0:
                logging.info("Non-finite values detected in simulation, exiting...")
            # dump right now
            sim_checkpoint(discr=discr, visualizer=visualizer, eos=eos,
                              q=state, vizname=casename,
                              step=999999999, t=t, dt=current_dt,
                              nviz=1, exittol=exittol,
                              constant_cfl=constant_cfl, comm=comm, vis_timer=vis_timer,
                              overwrite=True,s0=s0_sc,kappa=kappa_sc)
            exit()

        cv = split_conserved(dim=dim, q=state)
        return ( ns_operator(discr, q=state, t=t, boundaries=boundaries, eos=eos)
                 + eos.get_species_source_terms(cv))

    def my_checkpoint(step, t, dt, state):

        write_restart = (check_step(step, nrestart)
                         if step != restart_step else False)
        if write_restart is True:
            with open(snapshot_pattern.format(casename=casename, step=step, rank=rank), "wb") as f:
                pickle.dump({
                    "local_mesh": local_mesh,
                    "state": obj_array_vectorize(actx.to_numpy, flatten(state)),
                    "t": t,
                    "step": step,
                    "global_nelements": global_nelements,
                    "num_parts": nparts,
                    }, f)

        cv = split_conserved(dim, state)
        reaction_rates = eos.get_production_rates(cv)
        viz_fields = [("reaction_rates", reaction_rates)]
        
        return sim_checkpoint(discr=discr, visualizer=visualizer, eos=eos,
                              q=state, vizname=casename,
                              step=step, t=t, dt=dt, nstatus=nstatus,
                              nviz=nviz, exittol=exittol,
                              constant_cfl=constant_cfl, comm=comm, vis_timer=vis_timer,
                              overwrite=True, viz_fields=viz_fields)

    if rank == 0:
        logging.info("Stepping.")

    (current_step, current_t, current_state) = \
        advance_state(rhs=my_rhs, timestepper=timestepper,
                      checkpoint=my_checkpoint,
                      get_timestep=get_timestep, state=current_state,
                      t_final=t_final, t=current_t, istep=current_step,
                      logmgr=logmgr,eos=eos,dim=dim)

    if rank == 0:
        logger.info("Checkpointing final state ...")

    my_checkpoint(current_step, t=current_t,
                  dt=(current_t - checkpoint_t),
                  state=current_state)

    if current_t - t_final < 0:
        raise ValueError("Simulation exited abnormally")

    if logmgr:
        logmgr.close()

    exit()


if __name__ == "__main__":
    import sys
    import os.path
    #from os import path, split
    
    logging.basicConfig(format="%(message)s", level=logging.INFO)

    import argparse
    parser = argparse.ArgumentParser(description="MIRGE-Com Flame 1D Flow Driver")

    parser.add_argument('-r', '--restart_file',  type=ascii,
                        dest='restart_file', nargs='?', action='store',
                        help='simulation restart file')
    parser.add_argument('-i', '--input_file',  type=ascii,
                        dest='input_file', nargs='?', action='store',
                        help='simulation config file')
    parser.add_argument('-c', '--casename',  type=ascii,
                        dest='casename', nargs='?', action='store',
                        help='simulation case name')
    parser.add_argument("--log", action="store_true", default=True,
        help="enable logging profiling [ON]")

    args = parser.parse_args()

    # for writing output
    casename = "flame1d"
    if(args.casename):
        print(f"Custom casename {args.casename}")
        casename = (args.casename).replace("'","")
    else:
        print(f"Default casename {casename}")

    snapshot_pattern="{casename}-{step:06d}-{rank:04d}.pkl"
    restart_step=None
    restart_name=None
    if(args.restart_file):
        print(f"Restarting from file {args.restart_file}")
        file_path, file_name = os.path.split(args.restart_file)
        restart_step = int(file_name.split('-')[1])
        restart_name = (file_name.split('-')[0]).replace("'","")
        print(f"step {restart_step}")
        print(f"name {restart_name}")
    #print(f"step {restart_step}")


    if(args.input_file):
        input_file = (args.input_file).replace("'","")
        print(f"Reading user input from {args.input_file}")
    else:
        print("No user input file, using default values")


    print(f"Running {sys.argv[0]}\n")
    run_flame(restart_step=restart_step, restart_name=restart_name, user_input_file=input_file,
         snapshot_pattern=snapshot_pattern, use_logmgr=args.log, casename=casename)


# vim: foldmethod=marker
