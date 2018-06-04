""" Primary module for guiding the reduction of long/multi-slit data
"""
from __future__ import (print_function, absolute_import, division, unicode_literals)

import numpy as np
import os

from pypit import msgs
from pypit import arparse as settings
from pypit import arcalib
from pypit import arload
from pypit import arproc
from pypit.core import arprocimg
from pypit import arsave
from pypit import arsciexp
from pypit.core import arsetup
from pypit import arpixels
from pypit.core import arsort
from pypit import fluxspec

from pypit import ardebug as debugger


def ARMS(spectrograph, fitstbl, setup_dict):
    """
    Automatic Reduction of Multislit Data

    Parameters
    ----------
    fitsdict : dict
      Contains relevant information from fits header files
    reuseMaster : bool
      If True, a master frame that will be used for another science frame
      will not be regenerated after it is first made.
      This setting comes with a price, and if a large number of science frames are
      being generated, it may be more efficient to simply regenerate the master
      calibrations on the fly.

    Returns
    -------
    status : int
      Status of the reduction procedure
      0 = Successful full execution
      1 = Successful processing of setup or calcheck
    """
    status = 0

    # Generate sciexp list, if need be (it will be soon)
    sv_std_idx = []
    std_dict = {}
    basenames = []  # For fluxing at the very end
    sciexp = []
    all_sci_ID = fitstbl['sci_ID'].data[fitstbl['science']]  # Binary system: 1,2,4,8, etc.
    for sci_ID in all_sci_ID:
        sciexp.append(arsciexp.ScienceExposure(sci_ID, fitstbl, settings.argflag,
                                               settings.spect, do_qa=True))
        basenames.append(sciexp[-1]._basename)
        std_idx = arsort.ftype_indices(fitstbl, 'standard', sci_ID)
        if (len(std_idx) > 0):
            if len(std_idx) > 1:
                msgs.warn("Will only reduce the first, unique standard for each standard frame!")
            if std_idx[0] not in sv_std_idx:  # Only take the first one
                sv_std_idx.append(std_idx[0])
                # Standard stars
                std_dict[std_idx[0]] = arsciexp.ScienceExposure(sci_ID, fitstbl, settings.argflag,
                                                                settings.spect, do_qa=False, idx_sci=std_idx[0])
    numsci = len(sciexp)

    # Init calib dict
    calib_dict = {}

    # Loop on Detectors
    for kk in range(settings.spect['mosaic']['ndet']):
        det = kk + 1  # Detectors indexed from 1
        if settings.argflag['reduce']['detnum'] is not None:
            if det not in map(int, settings.argflag['reduce']['detnum']):
                msgs.warn("Skipping detector {:d}".format(det))
                continue
            else:
                msgs.warn("Restricting the reduction to detector {:d}".format(det))

        # Loop on science exposure (somewhat necessary as instruments can get paired with specific
        #  calib frames, e.g. arcs)
        for sc in range(numsci):

            #sci_ID = slf.sci_ID
            sci_ID = all_sci_ID[sc]
            scidx = np.where((fitstbl['sci_ID'] == sci_ID) & fitstbl['science'])[0][0]
            #scidx = slf._idx_sci[0]
            msgs.info("Reducing file {0:s}, target {1:s}".format(fitstbl['filename'][scidx],
                                                                 fitstbl['target'][scidx])) #slf._target_name))

            dnum = settings.get_dnum(det)
            msgs.info("Working on detector {:s}".format(dnum))

            # Setup
            namp = settings.spect[dnum]["numamplifiers"]
            setup = arsetup.instr_setup(sci_ID, det, fitstbl, setup_dict, namp, must_exist=True)
            settings.argflag['reduce']['masters']['setup'] = setup

            ###############
            # Get data sections (Could avoid doing this for every sciexp, but it is quick)
            # TODO -_ Clean this up!
            scifile = os.path.join(fitstbl['directory'][scidx],fitstbl['filename'][scidx])
            settings_det = settings.spect[dnum].copy()  # Should include naxis0, naxis1 in this
            datasec_img, naxis0, naxis1 = arprocimg.get_datasec_trimmed(
                settings.argflag['run']['spectrograph'], scifile, namp, det, settings_det,
                naxis0=fitstbl['naxis0'][scidx],
                naxis1=fitstbl['naxis1'][scidx])
            # Yes, this looks goofy.  Is needed for LRIS and DEIMOS for now
            settings.spect[dnum] = settings_det.copy()  # Used internally..
            fitstbl['naxis0'][scidx] = naxis0
            fitstbl['naxis1'][scidx] = naxis1

            # Calib dict
            if setup not in calib_dict.keys():
                calib_dict[setup] = {}

            # TODO -- Update/avoid the following with new settings
            tsettings = settings.argflag.copy()
            tsettings['detector'] = settings.spect[settings.get_dnum(det)]
            try:
                tsettings['detector']['dataext'] = tsettings['detector']['dataext01']  # Kludge; goofy named key
            except KeyError: # LRIS, DEIMOS
                tsettings['detector']['dataext'] = None
            tsettings['detector']['dispaxis'] = settings.argflag['trace']['dispersion']['direction']

            ###############################################################################
            # Prepare for Bias subtraction
            if 'bias' in calib_dict[setup].keys():
                msbias = calib_dict[setup]['bias']
            else:
                # Grab it
                #   Bias will either be an image (ndarray) or a command (str, e.g. 'overscan') or none
                msbias, _ = arcalib.get_msbias(det, setup, sci_ID, fitstbl, tsettings)
                # Save
                calib_dict[setup]['bias'] = msbias

            ###############################################################################
            # Generate a master arc frame
            if 'arc' in calib_dict[setup].keys():
                msarc = calib_dict[setup]['arc']
            else:
                msarc, _ = arcalib.get_msarc(det, setup, sci_ID, spectrograph, fitstbl, tsettings, msbias)
                # Save
                calib_dict[setup]['arc'] = msarc

            ###############################################################################
            # Generate a bad pixel mask (should not repeat)
            if 'bpm' in calib_dict[setup].keys():
                msbpm = calib_dict[setup]['bpm']
            else:
                # Grab it
                msbpm, _ = arcalib.get_mspbm(det, spectrograph, tsettings, msarc.shape,
                                      binning=fitstbl['binning'][scidx],
                                      reduce_badpix=settings.argflag['reduce']['badpix'],
                                      msbias=msbias)
                # Save
                calib_dict[setup]['bpm'] = msbpm

            ###############################################################################
            # Generate an array that provides the physical pixel locations on the detector
            pixlocn = arpixels.gen_pixloc(msarc.shape, det, settings.argflag)

            ###############################################################################
            # Slit Tracing
            if 'trace' in calib_dict[setup].keys():  # Internal
                tslits_dict = calib_dict[setup]['trace']
            else:
                # Setup up the settings (will be Refactored with settings)
                ts_settings = dict(trace=settings.argflag['trace'], masters=settings.argflag['reduce']['masters'])
                ts_settings['masters']['directory'] = settings.argflag['run']['directory']['master']+'_'+ settings.argflag['run']['spectrograph']
                # Get it
                tslits_dict, _ = arcalib.get_tslits_dict(
                    det, setup, spectrograph, sci_ID, ts_settings, tsettings, fitstbl, pixlocn,
                    msbias, msbpm, trim=settings.argflag['reduce']['trim'])
                # Save in calib
                calib_dict[setup]['trace'] = tslits_dict

            ###############################################################################
            # Initialize maskslits
            nslits = tslits_dict['lcen'].shape[1]
            maskslits = np.zeros(nslits, dtype=bool)

            ###############################################################################
            # Generate the 1D wavelength solution
            if 'wavecalib' in calib_dict[setup].keys():
                wv_calib = calib_dict[setup]['wavecalib']
                wv_maskslits = calib_dict[setup]['wvmask']
            elif settings.argflag["reduce"]["calibrate"]["wavelength"] == "pixel":
                msgs.info("A wavelength calibration will not be performed")
                wv_calib = None
                wv_maskslits = np.zeros_like(maskslits, dtype=bool)
            else:
                # Setup up the settings (will be Refactored with settings)
                wvc_settings = dict(calibrate=settings.argflag['arc']['calibrate'], masters=settings.argflag['reduce']['masters'])
                wvc_settings['masters']['directory'] = settings.argflag['run']['directory']['master']+'_'+ settings.argflag['run']['spectrograph']
                nonlinear = settings.spect[settings.get_dnum(det)]['saturation'] * settings.spect[settings.get_dnum(det)]['nonlinear']
                # Get it
                wv_calib, wv_maskslits, _ = arcalib.get_wv_calib(
                    det, setup, spectrograph, sci_ID, wvc_settings, fitstbl, tslits_dict, pixlocn,
                    msarc, nonlinear=nonlinear)
                # Save in calib
                calib_dict[setup]['wavecalib'] = wv_calib
                calib_dict[setup]['wvmask'] = wv_maskslits
            # Mask me
            maskslits += wv_maskslits

            ###############################################################################
            # Derive the spectral tilt
            if 'tilts' in calib_dict[setup].keys():
                mstilts = calib_dict[setup]['tilts']
                wt_maskslits = calib_dict[setup]['wtmask']
            else:
                # Settings kludges
                tilt_settings = dict(tilts=settings.argflag['trace']['slits']['tilts'].copy(),
                                     masters=settings.argflag['reduce']['masters'])
                tilt_settings['tilts']['function'] = settings.argflag['trace']['slits']['function']
                tilt_settings['masters']['directory'] = settings.argflag['run']['directory']['master']+'_'+ settings.argflag['run']['spectrograph']
                # Get it
                mstilts, wt_maskslits, _ = arcalib.get_wv_tilts(
                    det, setup, tilt_settings, settings_det, tslits_dict, pixlocn,
                    msarc, wv_calib, maskslits)
                # Save
                calib_dict[setup]['tilts'] = mstilts
                calib_dict[setup]['wtmask'] = wt_maskslits

            # Mask me
            maskslits += wt_maskslits

            ###############################################################################
            # Prepare the pixel flat field frame
            if settings.argflag['reduce']['flatfield']['perform']:  # Only do it if the user wants to flat field
                if 'normpixelflat' in calib_dict[setup].keys():
                    mspixflatnrm = calib_dict[setup]['normpixelflat']
                    slitprof = calib_dict[setup]['slitprof']
                else:
                    # Settings
                    flat_settings = dict(flatfield=settings.argflag['reduce']['flatfield'].copy(),
                                         slitprofile=settings.argflag['reduce']['slitprofile'].copy(),
                                         combine=settings.argflag['pixelflat']['combine'].copy(),
                                         masters=settings.argflag['reduce']['masters'].copy(),
                                         detector=settings.spect[dnum])
                    flat_settings['masters']['directory'] = settings.argflag['run']['directory']['master']+'_'+ settings.argflag['run']['spectrograph']
                    # Get it
                    mspixflatnrm, slitprof, _ = arcalib.get_msflat(
                        det, setup, sci_ID, fitstbl, tslits_dict, datasec_img,
                        flat_settings, msbias, mstilts)
                    # Save internallly
                    calib_dict[setup]['normpixelflat'] = mspixflatnrm
                    calib_dict[setup]['slitprof'] = slitprof
            else:
                mspixflatnrm = None
                slitprof = None


            ###############################################################################
            # Generate/load a master wave frame
            if 'wave' in calib_dict[setup].keys():
                mswave = calib_dict[setup]['wave']
            else:
                if settings.argflag["reduce"]["calibrate"]["wavelength"] == "pixel":
                    mswave = mstilts * (mstilts.shape[0]-1.0)
                else:
                    # Settings
                    wvimg_settings = dict(masters=settings.argflag['reduce']['masters'].copy())
                    wvimg_settings['masters']['directory'] = settings.argflag['run']['directory']['master']+'_'+ settings.argflag['run']['spectrograph']
                    # Get it
                    mswave, _ = arcalib.get_mswave(
                        setup, tslits_dict, wvimg_settings, mstilts, wv_calib, maskslits)
                # Save internally
                calib_dict[setup]['wave'] = mswave

            # CALIBS END HERE
            ###############################################################################


            ###############
            # Load the science frame and from this generate a Poisson error frame
            msgs.info("Loading science frame")
            sciframe = arload.load_frames(fitstbl, [scidx], det,
                                          frametype='science',
                                          msbias=msbias)
            sciframe = sciframe[:, :, 0]
            # Extract
            msgs.info("Processing science frame")

            slf = sciexp[sc]
            slf.det = det
            slf.setup = setup
            msgs.sciexp = slf  # For QA on crash
            # Save in slf
            # TODO -- Deprecate this means of holding the info (e.g. just pass around traceSlits)
            slf.SetFrame(slf._lordloc, tslits_dict['lcen'], det)
            slf.SetFrame(slf._rordloc, tslits_dict['rcen'], det)
            slf.SetFrame(slf._pixcen, tslits_dict['pixcen'], det)
            slf.SetFrame(slf._pixwid, tslits_dict['pixwid'], det)
            slf.SetFrame(slf._lordpix, tslits_dict['lordpix'], det)
            slf.SetFrame(slf._rordpix, tslits_dict['rordpix'], det)
            slf.SetFrame(slf._slitpix, tslits_dict['slitpix'], det)
            # TODO -- Deprecate using slf for this
            slf.SetFrame(slf._pixlocn, pixlocn, det)
            #
            slf._maskslits[det-1] = maskslits
            arproc.reduce_multislit(slf, mstilts, sciframe, msbpm, datasec_img, scidx, fitstbl, det,
                                    mswave, mspixelflatnrm=mspixflatnrm, slitprof=slitprof)


            ######################################################
            # Reduce standard here; only legit if the mask is the same
            std_idx = arsort.ftype_indices(fitstbl, 'standard', sci_ID)
            if len(std_idx) > 0:
                std_idx = std_idx[0]
            else:
                continue
            stdslf = std_dict[std_idx]
            if stdslf.extracted[det-1] is False:
                # Fill up the necessary pieces
                for iattr in ['pixlocn', 'lordloc', 'rordloc', 'pixcen', 'pixwid', 'lordpix', 'rordpix',
                              'slitpix', 'satmask', 'maskslits', 'mswave']:
                    setattr(stdslf, '_'+iattr, getattr(slf, '_'+iattr))  # Brings along all the detectors, but that is ok
                # Load
                stdframe = arload.load_frames(fitstbl, [std_idx], det, frametype='standard', msbias=msbias)
                stdframe = stdframe[:, :, 0]
                # Reduce
                msgs.info("Processing standard frame")
                arproc.reduce_multislit(stdslf, mstilts, stdframe, msbpm, datasec_img, std_idx, fitstbl, det,
                                        mswave, mspixelflatnrm=mspixflatnrm, standard=True, slitprof=slitprof)
                # Finish
                stdslf.extracted[det-1] = True

        ###########################
        # Write
        # Write 1D spectra
        save_format = 'fits'
        if save_format == 'fits':
            outfile = settings.argflag['run']['directory']['science']+'/spec1d_{:s}.fits'.format(slf._basename)
            helio_dict = dict(refframe=settings.argflag['reduce']['calibrate']['refframe'],
                              vel_correction=slf.vel_correction)
            arsave.save_1d_spectra_fits(slf._specobjs, fitstbl[slf._idx_sci[0]], outfile,
                                            helio_dict=helio_dict, obs_dict=settings.spect['mosaic'])
            #arsave.save_1d_spectra_fits(slf, fitstbl)
        elif save_format == 'hdf5':
            arsave.save_1d_spectra_hdf5(slf)
        else:
            msgs.error(save_format + ' is not a recognized output format!')
        arsave.save_obj_info(slf, fitstbl)
        # Write 2D images for the Science Frame
        arsave.save_2d_images(slf, fitstbl)
        # Free up some memory by replacing the reduced ScienceExposure class
        sciexp[sc] = None

    # Write standard stars
    for key in std_dict.keys():
        outfile = settings.argflag['run']['directory']['science']+'/spec1d_{:s}.fits'.format(std_dict[key]._basename)
        arsave.save_1d_spectra_fits(std_dict[key]._specobjs, fitstbl[std_idx], outfile,
                                        obs_dict=settings.spect['mosaic'])

    #########################
    # Flux towards the very end..
    #########################
    if settings.argflag['reduce']['calibrate']['flux'] and (len(std_dict) > 0):
        # Standard star (is this a calibration, e.g. goes above?)
        msgs.info("Processing standard star")
        msgs.info("Taking one star per detector mosaic")
        msgs.info("Waited until very end to work on it")
        msgs.warn("You should probably consider using the pypit_flux_spec script anyhow...")

        # Kludge settings
        fsettings = settings.spect.copy()
        fsettings['run'] = settings.argflag['run']
        fsettings['reduce'] = settings.argflag['reduce']
        # Generate?
        if (settings.argflag['reduce']['calibrate']['sensfunc']['archival'] == 'None'):
            std_keys = list(std_dict.keys())
            std_key = std_keys[0] # Take the first extraction
            FxSpec = fluxspec.FluxSpec(settings=fsettings, std_specobjs=std_dict[std_key]._specobjs,
                                       setup=setup)  # This takes the last setup run, which is as sensible as any..
            sensfunc = FxSpec.master(fitstbl[std_key])
        else:  # Input by user
            FxSpec = fluxspec.FluxSpec(settings=fsettings,
                                       sens_file=settings.argflag['reduce']['calibrate']['sensfunc']['archival'])
            sensfunc = FxSpec.sensfunc
        # Flux
        msgs.info("Fluxing with {:s}".format(sensfunc['std']['name']))
        for kk, sci_ID in enumerate(all_sci_ID):
            # Load from disk (we zero'd out the class to free memory)
            if save_format == 'fits':
                sci_spec1d_file = settings.argflag['run']['directory']['science']+'/spec1d_{:s}.fits'.format(
                    basenames[kk])
            # Load
            sci_specobjs, sci_header = arload.load_specobj(sci_spec1d_file)
            FxSpec.sci_specobjs = sci_specobjs
            FxSpec.sci_header = sci_header
            # Flux
            FxSpec.flux_science()
            # Over-write
            FxSpec.write_science(sci_spec1d_file)

    return status
