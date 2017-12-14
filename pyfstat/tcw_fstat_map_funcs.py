""" Additional helper functions dealing with transient-CW F(t0,tau) maps """

import numpy as np
import os
import logging

# optional imports
import importlib as imp


def optional_import ( modulename, shorthand=None ):
    '''
    Import a module/submodule only if it's available.

    using importlib instead of __import__
    because the latter doesn't handle sub.modules
    '''
    if shorthand is None:
        shorthand    = modulename
        shorthandbit = ''
    else:
        shorthandbit = ' as '+shorthand
    try:
        globals()[shorthand] = imp.import_module(modulename)
        #logging.debug('Successfully imported module %s%s.' % (modulename, shorthandbit))
        success = True
    except ImportError, e:
        if e.message == 'No module named '+modulename:
            logging.warning('No module {:s} found.'.format(modulename))
            success = False
        else:
            raise
    return success


class pyTransientFstatMap(object):
    '''
    simplified object class for a F(t0,tau) F-stat map (not 2F!)
    based on LALSuite's transientFstatMap_t type
    replacing the gsl matrix with a numpy array

    F_mn:   2D array of 2F values
    maxF:   maximum of F (not 2F!)
    t0_ML:  maximum likelihood transient start time t0 estimate
    tau_ML: maximum likelihood transient duration tau estimate
    '''

    def __init__(self, N_t0Range, N_tauRange):
        self.F_mn   = np.zeros((N_t0Range, N_tauRange), dtype=np.float32)
        self.maxF   = float(-1.0) # Initializing to a negative value ensures that we always update at least once and hence return sane t0_d_ML, tau_d_ML even if there is only a single bin where F=0 happens.
        self.t0_ML  = float(0.0)
        self.tau_ML = float(0.0)


# dictionary of the actual callable F-stat map functions we support,
# if the corresponding modules are available.
fstatmap_versions = {
                     'lal':    lambda multiFstatAtoms, windowRange:
                               getattr(lalpulsar,'ComputeTransientFstatMap')
                                ( multiFstatAtoms, windowRange, False ),
                     'pycuda': lambda multiFstatAtoms, windowRange:
                               pycuda_compute_transient_fstat_map
                                ( multiFstatAtoms, windowRange )
                    }


def init_transient_fstat_map_features ( ):
    '''
    Initialization of available modules (or "features") for F-stat maps.

    Returns a dictionary of method names, to match fstatmap_versions
    each key's value set to True only if
    all required modules are importable on this system.
    '''

    features = {}

    have_lal           = optional_import('lal')
    have_lalpulsar     = optional_import('lalpulsar')
    features['lal']    = have_lal and have_lalpulsar

    # import GPU features
    have_pycuda_drv      = optional_import('pycuda.driver', 'drv')
    have_pycuda_init     = optional_import('pycuda.autoinit', 'autoinit')
    have_pycuda_gpuarray = optional_import('pycuda.gpuarray', 'gpuarray')
    have_pycuda_tools    = optional_import('pycuda.tools', 'cudatools')
    have_pycuda_compiler = optional_import('pycuda.compiler', 'cudacomp')
    features['pycuda']   = have_pycuda_drv and have_pycuda_init and have_pycuda_gpuarray and have_pycuda_tools and have_pycuda_compiler

    logging.debug('Got the following features for transient F-stat maps:')
    logging.debug(features)

    return features


def call_compute_transient_fstat_map ( version, features, multiFstatAtoms=None, windowRange=None ):
    '''Choose which version of the ComputeTransientFstatMap function to call.'''

    if version in fstatmap_versions:
        if features[version]:
            FstatMap = fstatmap_versions[version](multiFstatAtoms, windowRange)
        else:
            raise Exception('Required module(s) for transient F-stat map method "{}" not available!'.format(version))
    else:
        raise Exception('Transient F-stat map method "{}" not implemented!'.format(version))
    return FstatMap


def reshape_FstatAtomsVector ( atomsVector ):
    '''
    Make a dictionary of ndarrays out of a atoms "vector" structure.

    The input is a "vector"-like structure with times as the higher hierarchical
    level and a set of "atoms" quantities defined at each timestamp.
    The output is a dictionary with an entry for each quantity,
    which is a 1D ndarray over timestamps for that one quantity.
    '''

    numAtoms = atomsVector.length
    atomsDict = {}
    atom_fieldnames = ['timestamp', 'Fa_alpha', 'Fb_alpha',
                       'a2_alpha', 'ab_alpha', 'b2_alpha']
    atom_dtypes     = [np.uint32, complex, complex,
                       np.float32, np.float32, np.float32]
    for f, field in enumerate(atom_fieldnames):
        atomsDict[field] = np.ndarray(numAtoms,dtype=atom_dtypes[f])

    for n,atom in enumerate(atomsVector.data):
        for field in atom_fieldnames:
            atomsDict[field][n] = atom.__getattribute__(field)

    atomsDict['Fa_alpha_re'] = np.float32(atomsDict['Fa_alpha'].real)
    atomsDict['Fa_alpha_im'] = np.float32(atomsDict['Fa_alpha'].imag)
    atomsDict['Fb_alpha_re'] = np.float32(atomsDict['Fb_alpha'].real)
    atomsDict['Fb_alpha_im'] = np.float32(atomsDict['Fb_alpha'].imag)

    return atomsDict


def get_absolute_kernel_path ( kernel ):
    pyfstatdir = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
    kernelfile = kernel + '.cu'
    return os.path.join(pyfstatdir,'pyCUDAkernels',kernelfile)


def pycuda_compute_transient_fstat_map ( multiFstatAtoms, windowRange ):
    '''
    GPU version of the function to compute transient-window "F-statistic map"
    over start-time and timescale {t0, tau}.
    Based on XLALComputeTransientFstatMap from LALSuite,
    (C) 2009 Reinhard Prix, licensed under GPL

    Returns a 2D matrix F_mn,
    with m = index over start-times t0,
    and  n = index over timescales tau,
    in steps of dt0  in [t0,  t0+t0Band],
    and         dtau in [tau, tau+tauBand]
    as defined in windowRange input.
    '''

    if ( windowRange.type >= lalpulsar.TRANSIENT_LAST ):
        raise ValueError ('Unknown window-type ({}) passed as input. Allowed are [0,{}].'.format(windowRange.type, lalpulsar.TRANSIENT_LAST-1))

    # internal dict for search/setup parameters
    tCWparams = {}

    # first combine all multi-atoms
    # into a single atoms-vector with *unique* timestamps
    tCWparams['TAtom'] = multiFstatAtoms.data[0].TAtom
    TAtomHalf          = int(tCWparams['TAtom']/2) # integer division
    atoms = lalpulsar.mergeMultiFstatAtomsBinned ( multiFstatAtoms,
                                                   tCWparams['TAtom'] )

    # make a combined input matrix of all atoms vectors, for transfer to GPU
    tCWparams['numAtoms'] = atoms.length
    atomsDict = reshape_FstatAtomsVector(atoms)
    atomsInputMatrix = np.column_stack ( (atomsDict['a2_alpha'],
                                          atomsDict['b2_alpha'],
                                          atomsDict['ab_alpha'],
                                          atomsDict['Fa_alpha_re'],
                                          atomsDict['Fa_alpha_im'],
                                          atomsDict['Fb_alpha_re'],
                                          atomsDict['Fb_alpha_im'])
                                       )

    # actual data spans [t0_data, t0_data + tCWparams['numAtoms'] * TAtom]
    # in steps of TAtom
    tCWparams['t0_data'] = int(atoms.data[0].timestamp)
    tCWparams['t1_data'] = int(atoms.data[tCWparams['numAtoms']-1].timestamp + tCWparams['TAtom'])

    logging.debug('t0_data={:d}, t1_data={:d}'.format(tCWparams['t0_data'],
                                                      tCWparams['t1_data']))
    logging.debug('numAtoms={:d}, TAtom={:d}, TAtomHalf={:d}'.format(tCWparams['numAtoms'],
                                                                     tCWparams['TAtom'],
                                                                     TAtomHalf))

    # special treatment of window_type = none
    # ==> replace by rectangular window spanning all the data
    if ( windowRange.type == lalpulsar.TRANSIENT_NONE ):
        windowRange.type    = lalpulsar.TRANSIENT_RECTANGULAR
        windowRange.t0      = tCWparams['t0_data']
        windowRange.t0Band  = 0
        windowRange.dt0     = tCWparams['TAtom'] # irrelevant
        windowRange.tau     = tCWparams['numAtoms'] * tCWparams['TAtom']
        windowRange.tauBand = 0;
        windowRange.dtau    = tCWparams['TAtom'] # irrelevant

    """ NOTE: indices {i,j} enumerate *actual* atoms and their timestamps t_i, while the
    * indices {m,n} enumerate the full grid of values in [t0_min, t0_max]x[Tcoh_min, Tcoh_max] in
    * steps of deltaT. This allows us to deal with gaps in the data in a transparent way.
    *
    * NOTE2: we operate on the 'binned' atoms returned from XLALmergeMultiFstatAtomsBinned(),
    * which means we can safely assume all atoms to be lined up perfectly on a 'deltaT' binned grid.
    *
    * The mapping used will therefore be {i,j} -> {m,n}:
    *   m = offs_i  / deltaT		= start-time offset from t0_min measured in deltaT
    *   n = Tcoh_ij / deltaT		= duration Tcoh_ij measured in deltaT,
    *
    * where
    *   offs_i  = t_i - t0_min
    *   Tcoh_ij = t_j - t_i + deltaT
    *
    """

    # We allocate a matrix  {m x n} = t0Range * TcohRange elements
    # covering the full timerange the transient window-range [t0,t0+t0Band]x[tau,tau+tauBand]
    tCWparams['N_t0Range']  = int(np.floor( 1.0*windowRange.t0Band / windowRange.dt0 ) + 1)
    tCWparams['N_tauRange'] = int(np.floor( 1.0*windowRange.tauBand / windowRange.dtau ) + 1)
    FstatMap = pyTransientFstatMap ( tCWparams['N_t0Range'], tCWparams['N_tauRange'] )

    logging.debug('N_t0Range={:d}, N_tauRange={:d}'.format(tCWparams['N_t0Range'],
                                                           tCWparams['N_tauRange']))

    if ( windowRange.type == lalpulsar.TRANSIENT_RECTANGULAR ):
        FstatMap.F_mn = pycuda_compute_transient_fstat_map_rect ( atomsInputMatrix,
                                                                  windowRange,
                                                                  tCWparams )
    elif ( windowRange.type == lalpulsar.TRANSIENT_EXPONENTIAL ):
        FstatMap.F_mn = pycuda_compute_transient_fstat_map_exp ( atomsInputMatrix,
                                                                 windowRange,
                                                                 tCWparams )
    else:
        raise ValueError('Invalid transient window type {} not in [{}, {}].'.format(windowRange.type, lalpulsar.TRANSIENT_NONE, lalpulsar.TRANSIENT_LAST-1))

    # out of loop: get max2F and ML estimates over the m x n matrix
    FstatMap.maxF = FstatMap.F_mn.max()
    maxidx = np.unravel_index ( FstatMap.F_mn.argmax(),
                               (tCWparams['N_t0Range'],
                                tCWparams['N_tauRange']))
    FstatMap.t0_ML  = windowRange.t0  + maxidx[0] * windowRange.dt0
    FstatMap.tau_ML = windowRange.tau + maxidx[1] * windowRange.dtau

    logging.debug('Done computing transient F-stat map. maxF={}, t0_ML={}, tau_ML={}'.format(FstatMap.maxF , FstatMap.t0_ML, FstatMap.tau_ML))

    return FstatMap


def pycuda_compute_transient_fstat_map_rect ( atomsInputMatrix, windowRange, tCWparams ):
    '''only GPU-parallizing outer loop, keeping partial sums with memory in kernel'''

    # gpu data setup and transfer
    logging.debug('Initial GPU memory: {}/{} MB free'.format(drv.mem_get_info()[0]/(2.**20),drv.mem_get_info()[1]/(2.**20)))
    input_gpu = gpuarray.to_gpu ( atomsInputMatrix )
    Fmn_gpu   = gpuarray.GPUArray ( (tCWparams['N_t0Range'],tCWparams['N_tauRange']), dtype=np.float32 )
    logging.debug('GPU memory with input+output allocated: {}/{} MB free'.format(drv.mem_get_info()[0]/(2.**20), drv.mem_get_info()[1]/(2.**20)))

    # GPU kernel
    kernel = 'cudaTransientFstatRectWindow'
    kernelfile = get_absolute_kernel_path ( kernel )
    partial_Fstat_cuda_code = cudacomp.SourceModule(open(kernelfile,'r').read())
    partial_Fstat_cuda = partial_Fstat_cuda_code.get_function(kernel)
    partial_Fstat_cuda.prepare('PIIIIIIIIP')

    # GPU grid setup
    blockRows = min(1024,tCWparams['N_t0Range'])
    blockCols = 1
    gridRows  = int(np.ceil(1.0*tCWparams['N_t0Range']/blockRows))
    gridCols  = 1
    logging.debug('Looking to compute transient F-stat map over a N={}*{}={} (t0,tau) grid...'.format(tCWparams['N_t0Range'],tCWparams['N_tauRange'],tCWparams['N_t0Range']*tCWparams['N_tauRange']))

    # running the kernel
    logging.debug('Calling kernel with a grid of {}*{}={} blocks of {}*{}={} threads each: {} total threads...'.format(gridRows, gridCols, gridRows*gridCols, blockRows, blockCols, blockRows*blockCols, gridRows*gridCols*blockRows*blockCols))
    partial_Fstat_cuda.prepared_call ( (gridRows,gridCols), (blockRows,blockCols,1), input_gpu.gpudata, tCWparams['numAtoms'], tCWparams['TAtom'], tCWparams['t0_data'], windowRange.t0, windowRange.dt0, windowRange.tau, windowRange.dtau, tCWparams['N_tauRange'], Fmn_gpu.gpudata )

    # return results to host
    F_mn = Fmn_gpu.get()

    logging.debug('Final GPU memory: {}/{} MB free'.format(drv.mem_get_info()[0]/(2.**20),drv.mem_get_info()[1]/(2.**20)))

    return F_mn


def pycuda_compute_transient_fstat_map_exp ( atomsInputMatrix, windowRange, tCWparams ):
    '''exponential window, inner and outer loop GPU-parallelized'''

    # gpu data setup and transfer
    logging.debug('Initial GPU memory: {}/{} MB free'.format(drv.mem_get_info()[0]/(2.**20),drv.mem_get_info()[1]/(2.**20)))
    input_gpu = gpuarray.to_gpu ( atomsInputMatrix )
    Fmn_gpu   = gpuarray.GPUArray ( (tCWparams['N_t0Range'],tCWparams['N_tauRange']), dtype=np.float32 )
    logging.debug('GPU memory with input+output allocated: {}/{} MB free'.format(drv.mem_get_info()[0]/(2.**20), drv.mem_get_info()[1]/(2.**20)))

    # GPU kernel
    kernel = 'cudaTransientFstatExpWindow'
    kernelfile = get_absolute_kernel_path ( kernel )
    partial_Fstat_cuda_code = cudacomp.SourceModule(open(kernelfile,'r').read())
    partial_Fstat_cuda = partial_Fstat_cuda_code.get_function(kernel)
    partial_Fstat_cuda.prepare('PIIIIIIIIIP')

    # GPU grid setup
    blockRows = min(32,tCWparams['N_t0Range'])
    blockCols = min(32,tCWparams['N_tauRange'])
    gridRows  = int(np.ceil(1.0*tCWparams['N_t0Range']/blockRows))
    gridCols  = int(np.ceil(1.0*tCWparams['N_tauRange']/blockCols))
    logging.debug('Looking to compute transient F-stat map over a N={}*{}={} (t0,tau) grid...'.format(tCWparams['N_t0Range'], tCWparams['N_tauRange'], tCWparams['N_t0Range']*tCWparams['N_tauRange']))

    # running the kernel
    logging.debug('Calling kernel with a grid of {}*{}={} blocks of {}*{}={} threads each: {} total threads...'.format(gridRows, gridCols, gridRows*gridCols, blockRows, blockCols, blockRows*blockCols, gridRows*gridCols*blockRows*blockCols))
    partial_Fstat_cuda.prepared_call ( (gridRows,gridCols), (blockRows,blockCols,1), input_gpu.gpudata, tCWparams['numAtoms'], tCWparams['TAtom'], tCWparams['t0_data'], windowRange.t0, windowRange.dt0, windowRange.tau, windowRange.dtau, tCWparams['N_t0Range'], tCWparams['N_tauRange'], Fmn_gpu.gpudata )

    # return results to host
    F_mn = Fmn_gpu.get()

    logging.debug('Final GPU memory: {}/{} MB free'.format(drv.mem_get_info()[0]/(2.**20),drv.mem_get_info()[1]/(2.**20)))

    return F_mn
