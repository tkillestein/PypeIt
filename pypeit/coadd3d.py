"""
Module containing routines used by 3D datacubes.

.. include:: ../include/links.rst
"""

import os
import copy
import inspect

from astropy import wcs, units
from astropy.coordinates import SkyCoord
from astropy.io import fits
from scipy.interpolate import interp1d
import numpy as np

from pypeit import msgs
from pypeit import alignframe, datamodel, flatfield, io, spec2dobj, utils
from pypeit.core.flexure import calculate_image_phase
from pypeit.core import datacube, flux_calib, parse
from pypeit.spectrographs.util import load_spectrograph

# Use a fast histogram for speed!
try:
    from fast_histogram import histogramdd
except ImportError:
    histogramdd = None

from IPython import embed


class DataCube(datamodel.DataContainer):
    """
    DataContainer to hold the products of a datacube

    The datamodel attributes are:

    .. include:: ../include/class_datamodel_datacube.rst

    Args:
        flux (`numpy.ndarray`_):
            The science datacube (nwave, nspaxel_y, nspaxel_x)
        sig (`numpy.ndarray`_):
            The error datacube (nwave, nspaxel_y, nspaxel_x)
        bpm (`numpy.ndarray`_):
            The bad pixel mask of the datacube (nwave, nspaxel_y, nspaxel_x).
            True values indicate a bad pixel
        blaze_wave (`numpy.ndarray`_):
            Wavelength array of the spectral blaze function
        blaze_spec (`numpy.ndarray`_):
            The spectral blaze function
        sensfunc (`numpy.ndarray`_, None):
            Sensitivity function (nwave,). Only saved if the data are fluxed.
        PYP_SPEC (str):
            Name of the PypeIt Spectrograph
        fluxed (bool):
            If the cube has been flux calibrated, this will be set to "True"

    Attributes:
        head0 (`astropy.io.fits.Header`_):
            Primary header
        filename (str):
            Filename to use when loading from file
        spect_meta (:obj:`dict`):
            Parsed meta from the header
        spectrograph (:class:`~pypeit.spectrographs.spectrograph.Spectrograph`):
            Build from PYP_SPEC

    """
    version = '1.1.0'

    datamodel = {'flux': dict(otype=np.ndarray, atype=np.floating,
                              descr='Flux datacube in units of counts/s/Ang/arcsec^2 or '
                                    '10^-17 erg/s/cm^2/Ang/arcsec^2'),
                 'sig': dict(otype=np.ndarray, atype=np.floating,
                             descr='Error datacube (matches units of flux)'),
                 'bpm': dict(otype=np.ndarray, atype=np.uint8,
                             descr='Bad pixel mask of the datacube (0=good, 1=bad)'),
                 'blaze_wave': dict(otype=np.ndarray, atype=np.floating,
                                    descr='Wavelength array of the spectral blaze function'),
                 'blaze_spec': dict(otype=np.ndarray, atype=np.floating,
                                    descr='The spectral blaze function'),
                 'sensfunc': dict(otype=np.ndarray, atype=np.floating,
                                  descr='Sensitivity function 10^-17 erg/(counts/cm^2)'),
                 'PYP_SPEC': dict(otype=str, descr='PypeIt: Spectrograph name'),
                 'fluxed': dict(otype=bool, descr='Boolean indicating if the datacube is fluxed.')}

    internals = ['head0',
                 'filename',
                 'spectrograph',
                 'spect_meta'
                ]

    def __init__(self, flux, sig, bpm, PYP_SPEC, blaze_wave, blaze_spec, sensfunc=None,
                 fluxed=None):

        args, _, _, values = inspect.getargvalues(inspect.currentframe())
        _d = dict([(k, values[k]) for k in args[1:]])
        # Setup the DataContainer
        datamodel.DataContainer.__init__(self, d=_d)

    def _bundle(self):
        """
        Over-write default _bundle() method to separate the DetectorContainer
        into its own HDU

        Returns:
            :obj:`list`: A list of dictionaries, each list element is
            written to its own fits extension. See the description
            above.
        """
        d = []
        # Rest of the datamodel
        for key in self.keys():
            # Skip Nones
            if self[key] is None:
                continue
            # Array?
            if self.datamodel[key]['otype'] == np.ndarray:
                tmp = {}
                if self.datamodel[key]['atype'] == np.floating:
                    tmp[key] = self[key].astype(np.float32)
                else:
                    tmp[key] = self[key]
                d.append(tmp)
            else:
                # Add to header of the primary image
                d[0][key] = self[key]
        # Return
        return d

    def to_file(self, ofile, primary_hdr=None, hdr=None, **kwargs):
        """
        Over-load :func:`~pypeit.datamodel.DataContainer.to_file`
        to deal with the header

        Args:
            ofile (:obj:`str`):
                Filename
            primary_hdr (`astropy.io.fits.Header`_, optional):
                Base primary header.  Updated with new subheader keywords.  If
                None, initialized using :func:`~pypeit.io.initialize_header`.
            wcs (`astropy.io.fits.Header`_, optional):
                The World Coordinate System, represented by a fits header
            kwargs (dict):
                Keywords passed directly to parent ``to_file`` function.

        """
        if primary_hdr is None:
            primary_hdr = io.initialize_header()
        # Build the header
        if self.head0 is not None and self.PYP_SPEC is not None:
            spectrograph = load_spectrograph(self.PYP_SPEC)
            subheader = spectrograph.subheader_for_spec(self.head0, self.head0)
        else:
            subheader = {}
        # Add em in
        for key in subheader:
            primary_hdr[key] = subheader[key]
        # Do it
        super(DataCube, self).to_file(ofile, primary_hdr=primary_hdr, hdr=hdr, **kwargs)

    @classmethod
    def from_file(cls, ifile):
        """
        Over-load :func:`~pypeit.datamodel.DataContainer.from_file`
        to deal with the header

        Args:
            ifile (str):  Filename holding the object
        """
        with io.fits_open(ifile) as hdu:
            # Read using the base class
            self = super().from_hdu(hdu)
            # Internals
            self.filename = ifile
            self.head0 = hdu[1].header  # Actually use the first extension here, since it contains the WCS
            # Meta
            self.spectrograph = load_spectrograph(self.PYP_SPEC)
            self.spect_meta = self.spectrograph.parse_spec_header(hdu[0].header)
        return self

    @property
    def ivar(self):
        """
        Utility function to compute the inverse variance cube
        """
        return utils.inverse(self.sig**2)

    @property
    def wcs(self):
        """
        Utility function to provide the world coordinate system of the datacube
        """
        return wcs.WCS(self.head0)


class CoAdd3D:
    """
    Main routine to convert processed PypeIt spec2d frames into
    DataCube (spec3d) files. This routine is only used for IFU
    data reduction.

    Algorithm steps are as follows:
        - TODO :: Fill this in.

    """
    # Superclass factory method generates the subclass instance
    @classmethod
    def get_instance(cls, spec2dfiles, opts, spectrograph=None, par=None, det=1, overwrite=False,
                     show=False, debug=False):
        """
        Instantiate the subclass appropriate for the provided spectrograph.

        The class to instantiate must match the ``pypeline``
        attribute of the provided ``spectrograph``, and must be a
        subclass of :class:`CoAdd3D`; see the parent class
        instantiation for parameter descriptions.

        Returns:
            :class:`CoAdd3D`: One of the subclasses with
            :class:`CoAdd3D` as its base.
        """

        return next(c for c in cls.__subclasses__()
                    if c.__name__ == (spectrograph.pypeline + 'CoAdd3D'))(
                        spec2dfiles, spectrograph=spectrograph, par=par, det=det, overwrite=overwrite,
                        show=show, debug=debug)

    def __init__(self, files, opts, spectrograph=None, par=None, det=None, overwrite=False,
                 show=False, debug=False):
        """

        Args:
            files (:obj:`list`):
                List of all spec2D files
            opts (:obj:`dict`):
                Options associated with each spec2d file
            spectrograph (:obj:`str`, :class:`~pypeit.spectrographs.spectrograph.Spectrograph`, optional):
                The name or instance of the spectrograph used to obtain the data.
                If None, this is pulled from the file header.
            par (:class:`~pypeit.par.pypeitpar.PypeItPar`, optional):
                An instance of the parameter set.  If None, assumes that detector 1
                is the one reduced and uses the default reduction parameters for the
                spectrograph (see
                :func:`~pypeit.spectrographs.spectrograph.Spectrograph.default_pypeit_par`
                for the relevant spectrograph class).
            det (:obj:`int`_, optional):
                Detector index
            overwrite (:obj:`bool`, optional):
                Overwrite the output file, if it exists?
            show (:obj:`bool`, optional):
                Show results in ginga
            debug (:obj:`bool`, optional):
                Show QA for debugging.

        """
        self.spec2d = files
        self.numfiles = len(files)
        self.opts = opts
        self.overwrite = overwrite

        # Check on Spectrograph input
        if spectrograph is None:
            with fits.open(files[0]) as hdu:
                spectrograph = hdu[0].header['PYP_SPEC']

        if isinstance(spectrograph, str):
            self.spec = load_spectrograph(spectrograph)
            self.specname = spectrograph
        else:
            # Assume it's a Spectrograph instance
            self.spec = spectrograph
            self.specname = spectrograph.name

        # Grab the parset, if not provided
        if par is None:
            # TODO :: Use config_specific_par instead?
            par = self.spec.default_pypeit_par()
        self.par = par
        # Extract some parsets for simplicity
        self.cubepar = self.par['reduce']['cube']
        self.flatpar = self.par['calibrations']['flatfield']
        self.senspar = self.par['sensfunc']

        # Initialise arrays for storage
        self.ifu_ra, self.ifu_dec = np.array([]), np.array([])  # The RA and Dec at the centre of the IFU, as stored in the header
        self.all_ra, self.all_dec, self.all_wave = np.array([]), np.array([]), np.array([])
        self.all_sci, self.all_ivar, self.all_idx, self.all_wghts = np.array([]), np.array([]), np.array([]), np.array([])
        self.all_spatpos, self.all_specpos, self.all_spatid = np.array([], dtype=int), np.array([], dtype=int), np.array([], dtype=int)
        self.all_tilts, self.all_slits, self.all_align = [], [], []
        self.all_wcs = []
        self.weights = np.ones(self.numfiles)  # Weights to use when combining cubes

        self._dspat = None if self.cubepar['spatial_delta'] is None else self.cubepar['spatial_delta'] / 3600.0  # binning size on the sky (/3600 to convert to degrees)
        self._dwv = self.cubepar['wave_delta']  # linear binning size in wavelength direction (in Angstroms)

        # Extract some commonly used variables
        self.method = self.cubepar['method'].lower()
        self.combine = self.cubepar['combine']
        self.align = self.cubepar['align']
        # If there is only one frame being "combined" AND there's no reference image, then don't compute the translation.
        if self.numfiles == 1 and self.cubepar["reference_image"] is None:
            if not self.align:
                msgs.warn("Parameter 'align' should be False when there is only one frame and no reference image")
                msgs.info("Setting 'align' to False")
            self.align = False
        if self.opts['ra_offset'] is not None:
            if not self.align:
                msgs.warn("When 'ra_offset' and 'dec_offset' are set, 'align' must be True.")
                msgs.info("Setting 'align' to True")
            self.align = True
        # TODO :: The default behaviour (combine=False, align=False) produces a datacube that uses the instrument WCS
        #  It should be possible (and perhaps desirable) to do a spatial alignment (i.e. align=True), apply this to the
        #  RA,Dec values of each pixel, and then use the instrument WCS to save the output (or, just adjust the crval).
        #  At the moment, if the user wishes to spatially align the frames, a different WCS is generated.
        # Check if fast-histogram exists
        if histogramdd is None:
            msgs.warn("Generating a datacube is faster if you install fast-histogram:"+msgs.newline()+
                      "https://pypi.org/project/fast-histogram/")
            if self.method != 'ngp':
                msgs.warn("Forcing NGP algorithm, because fast-histogram is not installed")
                self.method = 'ngp'

        # Determine what method is requested
        self.spec_subpixel, self.spat_subpixel = 1, 1
        if self.method == "subpixel":
            msgs.info("Adopting the subpixel algorithm to generate the datacube.")
            spec_subpixel, spat_subpixel = self.cubepar['spec_subpixel'], self.cubepar['spat_subpixel']
        elif self.method == "ngp":
            msgs.info("Adopting the nearest grid point (NGP) algorithm to generate the datacube.")
        else:
            msgs.error(f"The following datacube method is not allowed: {self.method}")

        # Get the detector number and string representation
        if det is None:
            det = 1 if self.par['rdx']['detnum'] is None else self.par['rdx']['detnum']
        self.detname = self.spec.get_det_name(det)

        # Check if the output file exists
        self.check_outputs()

        # Check the reference cube and image exist, if requested
        self.fluxcal = False
        self.blaze_wave, self.blaze_spec = None, None
        self.blaze_spline, self.flux_spline = None, None
        if self.cubepar['standard_cube'] is not None:
            self.make_sensfunc()

        # If a reference image has been set, check that it exists
        if self.cubepar['reference_image'] is not None:
            if not os.path.exists(self.cubepar['reference_image']):
                msgs.error("Reference image does not exist:" + msgs.newline() + self.cubepar['reference_image'])

    def check_outputs(self):
        """
        Check if any of the intended output files already exist. This check should be done near the
        beginning of the coaddition, to avoid any computation that won't be saved in the event that
        files won't be overwritten.
        """
        if self.combine:
            outfile = datacube.get_output_filename("", self.cubepar['output_filename'], self.combine)
            out_whitelight = datacube.get_output_whitelight_filename(outfile)
            if os.path.exists(outfile) and not self.overwrite:
                msgs.error("Output filename already exists:"+msgs.newline()+outfile)
            if os.path.exists(out_whitelight) and self.cubepar['save_whitelight'] and not self.overwrite:
                msgs.error("Output filename already exists:"+msgs.newline()+out_whitelight)
        else:
            # Finally, if there's just one file, check if the output filename is given
            if self.numfiles == 1 and self.cubepar['output_filename'] != "":
                outfile = datacube.get_output_filename("", self.cubepar['output_filename'], True, -1)
                out_whitelight = datacube.get_output_whitelight_filename(outfile)
                if os.path.exists(outfile) and not self.overwrite:
                    msgs.error("Output filename already exists:" + msgs.newline() + outfile)
                if os.path.exists(out_whitelight) and self.cubepar['save_whitelight'] and not self.overwrite:
                    msgs.error("Output filename already exists:" + msgs.newline() + out_whitelight)
            else:
                for ff in range(self.numfiles):
                    outfile = datacube.get_output_filename(self.spec2d[ff], self.cubepar['output_filename'], self.combine, ff+1)
                    out_whitelight = datacube.get_output_whitelight_filename(outfile)
                    if os.path.exists(outfile) and not self.overwrite:
                        msgs.error("Output filename already exists:" + msgs.newline() + outfile)
                    if os.path.exists(out_whitelight) and self.cubepar['save_whitelight'] and not self.overwrite:
                        msgs.error("Output filename already exists:" + msgs.newline() + out_whitelight)

    def create_wcs(self, all_ra, all_dec, all_wave, dspat, dwv, collapse=False, equinox=2000.0,
                   specname="PYP_SPEC"):
        """
        Create a WCS and the expected edges of the voxels, based on user-specified
        parameters or the extremities of the data.

        Parameters
        ----------
        all_ra : `numpy.ndarray`_
            1D flattened array containing the RA values of each pixel from all
            spec2d files
        all_dec : `numpy.ndarray`_
            1D flattened array containing the DEC values of each pixel from all
            spec2d files
        all_wave : `numpy.ndarray`_
            1D flattened array containing the wavelength values of each pixel from
            all spec2d files
        dspat : float
            Spatial size of each square voxel (in arcsec). The default is to use the
            values in cubepar.
        dwv : float
            Linear wavelength step of each voxel (in Angstroms)
        collapse : bool, optional
            If True, the spectral dimension will be collapsed to a single channel
            (primarily for white light images)
        equinox : float, optional
            Equinox of the WCS
        specname : str, optional
            Name of the spectrograph

        Returns
        -------
        cubewcs : `astropy.wcs.WCS`_
            astropy WCS to be used for the combined cube
        voxedges : tuple
            A three element tuple containing the bin edges in the x, y (spatial) and
            z (wavelength) dimensions
        reference_image : `numpy.ndarray`_
            The reference image to be used for the cross-correlation. Can be None.
        """
        # Grab cos(dec) for convenience
        cosdec = np.cos(np.mean(all_dec) * np.pi / 180.0)

        # Setup the cube ranges
        reference_image = None  # The default behaviour is that the reference image is not used
        ra_min = self.cubepar['ra_min'] if self.cubepar['ra_min'] is not None else np.min(all_ra)
        ra_max = self.cubepar['ra_max'] if self.cubepar['ra_max'] is not None else np.max(all_ra)
        dec_min = self.cubepar['dec_min'] if self.cubepar['dec_min'] is not None else np.min(all_dec)
        dec_max = self.cubepar['dec_max'] if self.cubepar['dec_max'] is not None else np.max(all_dec)
        wav_min = self.cubepar['wave_min'] if self.cubepar['wave_min'] is not None else np.min(all_wave)
        wav_max = self.cubepar['wave_max'] if self.cubepar['wave_max'] is not None else np.max(all_wave)
        dwave = self.cubepar['wave_delta'] if self.cubepar['wave_delta'] is not None else dwv

        # Number of voxels in each dimension
        numra = int((ra_max - ra_min) * cosdec / dspat)
        numdec = int((dec_max - dec_min) / dspat)
        numwav = int(np.round((wav_max - wav_min) / dwave))

        # If a white light WCS is being generated, make sure there's only 1 wavelength bin
        if collapse:
            wav_min = np.min(all_wave)
            wav_max = np.max(all_wave)
            dwave = wav_max - wav_min
            numwav = 1

        # Generate a master WCS to register all frames
        coord_min = [ra_min, dec_min, wav_min]
        coord_dlt = [dspat, dspat, dwave]

        # If a reference image is being used and a white light image is requested (collapse=True) update the celestial parts
        if self.cubepar["reference_image"] is not None:
            # Load the requested reference image
            reference_image, imgwcs = datacube.load_imageWCS(self.cubepar["reference_image"])
            # Update the celestial WCS
            coord_min[:2] = imgwcs.wcs.crval
            coord_dlt[:2] = imgwcs.wcs.cdelt
            numra, numdec = reference_image.shape

        cubewcs = datacube.generate_WCS(coord_min, coord_dlt, equinox=equinox, name=specname)
        msgs.info(msgs.newline() + "-" * 40 +
                  msgs.newline() + "Parameters of the WCS:" +
                  msgs.newline() + "RA   min = {0:f}".format(coord_min[0]) +
                  msgs.newline() + "DEC  min = {0:f}".format(coord_min[1]) +
                  msgs.newline() + "WAVE min, max = {0:f}, {1:f}".format(wav_min, wav_max) +
                  msgs.newline() + "Spaxel size = {0:f} arcsec".format(3600.0 * dspat) +
                  msgs.newline() + "Wavelength step = {0:f} A".format(dwave) +
                  msgs.newline() + "-" * 40)

        # Generate the output binning
        xbins = np.arange(1 + numra) - 0.5
        ybins = np.arange(1 + numdec) - 0.5
        spec_bins = np.arange(1 + numwav) - 0.5
        voxedges = (xbins, ybins, spec_bins)
        return cubewcs, voxedges, reference_image

    def make_sensfunc(self):
        """
        Generate the sensitivity function to be used for the flux calibration.
        """
        self.fluxcal = True
        ss_file = self.cubepar['standard_cube']
        if not os.path.exists(ss_file):
            msgs.error("Standard cube does not exist:" + msgs.newline() + ss_file)
        msgs.info(f"Loading standard star cube: {ss_file:s}")
        # Load the standard star cube and retrieve its RA + DEC
        stdcube = fits.open(ss_file)
        star_ra, star_dec = stdcube[1].header['CRVAL1'], stdcube[1].header['CRVAL2']

        # Extract a spectrum of the standard star
        wave, Nlam_star, Nlam_ivar_star, gpm_star = datacube.extract_standard_spec(stdcube)

        # Extract the information about the blaze
        if self.cubepar['grating_corr']:
            blaze_wave_curr, blaze_spec_curr = stdcube['BLAZE_WAVE'].data, stdcube['BLAZE_SPEC'].data
            blaze_spline_curr = interp1d(blaze_wave_curr, blaze_spec_curr,
                                         kind='linear', bounds_error=False, fill_value="extrapolate")
            # The first standard star cube is used as the reference blaze spline
            if self.blaze_spline is None:
                self.blaze_wave, self.blaze_spec = stdcube['BLAZE_WAVE'].data, stdcube['BLAZE_SPEC'].data
                self.blaze_spline = interp1d(self.blaze_wave, self.blaze_spec,
                                             kind='linear', bounds_error=False, fill_value="extrapolate")
            # Perform a grating correction
            grat_corr = datacube.correct_grating_shift(wave.value, blaze_wave_curr, blaze_spline_curr, self.blaze_wave,
                                              self.blaze_spline)
            # Apply the grating correction to the standard star spectrum
            Nlam_star /= grat_corr
            Nlam_ivar_star *= grat_corr ** 2

        # Read in some information above the standard star
        std_dict = flux_calib.get_standard_spectrum(star_type=self.senspar['star_type'],
                                                    star_mag=self.senspar['star_mag'],
                                                    ra=star_ra, dec=star_dec)
        # Calculate the sensitivity curve
        # TODO :: This needs to be addressed... unify flux calibration into the main PypeIt routines.
        msgs.warn("Datacubes are currently flux-calibrated using the UVIS algorithm... this will be deprecated soon")
        zeropoint_data, zeropoint_data_gpm, zeropoint_fit, zeropoint_fit_gpm = \
            flux_calib.fit_zeropoint(wave.value, Nlam_star, Nlam_ivar_star, gpm_star, std_dict,
                                     mask_hydrogen_lines=self.senspar['mask_hydrogen_lines'],
                                     mask_helium_lines=self.senspar['mask_helium_lines'],
                                     hydrogen_mask_wid=self.senspar['hydrogen_mask_wid'],
                                     nresln=self.senspar['UVIS']['nresln'],
                                     resolution=self.senspar['UVIS']['resolution'],
                                     trans_thresh=self.senspar['UVIS']['trans_thresh'],
                                     polyorder=self.senspar['polyorder'],
                                     polycorrect=self.senspar['UVIS']['polycorrect'],
                                     polyfunc=self.senspar['UVIS']['polyfunc'])
        wgd = np.where(zeropoint_fit_gpm)
        sens = np.power(10.0, -0.4 * (zeropoint_fit[wgd] - flux_calib.ZP_UNIT_CONST)) / np.square(wave[wgd])
        self.flux_spline = interp1d(wave[wgd], sens, kind='linear', bounds_error=False, fill_value="extrapolate")

        # Load the default scaleimg frame for the scale correction
        self.scalecorr_default = "none"
        self.relScaleImgDef = np.array([1])
        self.set_default_scalecorr()

        # Load the default sky frame to be used for sky subtraction
        self.skysub_default = "image"
        self.skyImgDef, self.skySclDef = None, None  # This is the default behaviour (i.e. to use the "image" for the sky subtraction)
        self.set_default_skysub()

    def set_default_scalecorr(self):
        """
        Set the default mode to use for relative spectral scale correction.
        """
        if self.cubepar['scale_corr'] is not None:
            if self.cubepar['scale_corr'] == "image":
                msgs.info("The default relative spectral illumination correction will use the science image")
                self.scalecorr_default = "image"
            else:
                msgs.info("Loading default scale image for relative spectral illumination correction:" +
                          msgs.newline() + self.cubepar['scale_corr'])
                try:
                    spec2DObj = spec2dobj.Spec2DObj.from_file(self.cubepar['scale_corr'], self.detname)
                    self.relScaleImgDef = spec2DObj.scaleimg
                    self.scalecorr_default = self.cubepar['scale_corr']
                except:
                    msgs.warn("Could not load scaleimg from spec2d file:" + msgs.newline() +
                              self.cubepar['scale_corr'] + msgs.newline() +
                              "scale correction will not be performed unless you have specified the correct" + msgs.newline() +
                              "scale_corr file in the spec2d block")
                    self.cubepar['scale_corr'] = None
                    self.scalecorr_default = "none"

    def get_current_scalecorr(self, spec2DObj, opts_scalecorr=None):
        """
        Determine the scale correction that should be used to correct
        for the relative spectral scaling of the science frame

        Args:
            spec2DObj (:class:`~pypeit.spec2dobj.Spec2DObj`_):
                2D PypeIt spectra object.
            opts_scalecorr (:obj:`str`, optional):
                A string that describes what mode should be used for the sky subtraction. The
                allowed values are:
                default - Use the default value, as defined in self.set_default_scalecorr()
                image - Use the relative scale that was derived from the science frame
                none - Do not perform relative scale correction

        Returns:
            :obj:`str`_: A string that describes the scale correction mode to be used (see opts_scalecorr description)
            `numpy.ndarray`_: 2D image (same shape as science frame) containing the relative spectral scaling to apply to the science frame
        """
        this_scalecorr = self.scalecorr_default
        relScaleImg = self.relScaleImgDef.copy()
        if opts_scalecorr is not None:
            if opts_scalecorr.lower() == 'default':
                if self.scalecorr_default == "image":
                    relScaleImg = spec2DObj.scaleimg
                    this_scalecorr = "image"  # Use the current spec2d for the relative spectral illumination scaling
                else:
                    this_scalecorr = self.scalecorr_default  # Use the default value for the scale correction
            elif opts_scalecorr.lower() == 'image':
                relScaleImg = spec2DObj.scaleimg
                this_scalecorr = "image"  # Use the current spec2d for the relative spectral illumination scaling
            elif opts_scalecorr.lower() == 'none':
                relScaleImg = np.array([1])
                this_scalecorr = "none"  # Don't do relative spectral illumination scaling
            else:
                # Load a user specified frame for sky subtraction
                msgs.info("Loading the following frame for the relative spectral illumination correction:" +
                          msgs.newline() + opts_scalecorr)
                try:
                    spec2DObj_scl = spec2dobj.Spec2DObj.from_file(opts_scalecorr, self.detname)
                except:
                    msgs.error(
                        "Could not load skysub image from spec2d file:" + msgs.newline() + opts_scalecorr)
                relScaleImg = spec2DObj_scl.scaleimg
                this_scalecorr = opts_scalecorr
        if this_scalecorr == "none":
            msgs.info("Relative spectral illumination correction will not be performed.")
        else:
            msgs.info("Using the following frame for the relative spectral illumination correction:" +
                      msgs.newline() + this_scalecorr)
        # Return the scaling correction for this frame
        return this_scalecorr, relScaleImg

    def set_default_skysub(self):
        """
        Set the default mode to use for sky subtraction.
        """
        if self.cubepar['skysub_frame'] in [None, 'none', '', 'None']:
            self.skysub_default = "none"
            self.skyImgDef = np.array([0.0])  # Do not perform sky subtraction
            self.skySclDef = np.array([0.0])  # Do not perform sky subtraction
        elif self.cubepar['skysub_frame'].lower() == "image":
            msgs.info("The sky model in the spec2d science frames will be used for sky subtraction" + msgs.newline() +
                      "(unless specific skysub frames have been specified)")
            self.skysub_default = "image"
        else:
            msgs.info("Loading default image for sky subtraction:" +
                      msgs.newline() + self.cubepar['skysub_frame'])
            try:
                spec2DObj = spec2dobj.Spec2DObj.from_file(self.cubepar['skysub_frame'], self.detname)
                skysub_exptime = fits.open(self.cubepar['skysub_frame'])[0].header['EXPTIME']
                self.skysub_default = self.cubepar['skysub_frame']
                self.skyImgDef = spec2DObj.sciimg / skysub_exptime  # Sky counts/second
                # self.skyImgDef = spec2DObj.skymodel/skysub_exptime  # Sky counts/second
                self.skySclDef = spec2DObj.scaleimg
            except:
                msgs.error("Could not load skysub image from spec2d file:" + msgs.newline() + self.cubepar['skysub_frame'])

    def get_current_skysub(self, spec2DObj, exptime, opts_skysub=None):
        """
        Determine the sky frame that should be used to subtract from the science frame

        Args:
            spec2DObj (:class:`~pypeit.spec2dobj.Spec2DObj`_):
                2D PypeIt spectra object.
            exptime (:obj:`float`_):
                The exposure time of the science frame (in seconds)
            opts_skysub (:obj:`str`, optional):
                A string that describes what mode should be used for the sky subtraction. The
                allowed values are:
                default - Use the default value, as defined in self.set_default_skysub()
                image - Use the sky model derived from the science frame
                none - Do not perform sky subtraction

        Returns:
            :obj:`str`_: A string that describes the sky subtration mode to be used (see opts_skysub description)
            `numpy.ndarray`_: 2D image (same shape as science frame) containing the sky frame to be subtracted from the science frame
            `numpy.ndarray`_: 2D image (same shape as science frame) containing the relative spectral scaling that has been applied to the returned sky frame
        """
        this_skysub = self.skysub_default
        if self.skysub_default == "image":
            skyImg = spec2DObj.skymodel
            skyScl = spec2DObj.scaleimg
        else:
            skyImg = self.skyImgDef.copy() * exptime
            skyScl = self.skySclDef.copy()
        # See if there's any changes from the default behaviour
        if opts_skysub is not None:
            if opts_skysub.lower() == 'default':
                if self.skysub_default == "image":
                    skyImg = spec2DObj.skymodel
                    skyScl = spec2DObj.scaleimg
                    this_skysub = "image"  # Use the current spec2d for sky subtraction
                else:
                    skyImg = self.skyImgDef.copy() * exptime
                    skyScl = self.skySclDef.copy() * exptime
                    this_skysub = self.skysub_default  # Use the global value for sky subtraction
            elif opts_skysub.lower() == 'image':
                skyImg = spec2DObj.skymodel
                skyScl = spec2DObj.scaleimg
                this_skysub = "image"  # Use the current spec2d for sky subtraction
            elif opts_skysub.lower() == 'none':
                skyImg = np.array([0.0])
                skyScl = np.array([1.0])
                this_skysub = "none"  # Don't do sky subtraction
            else:
                # Load a user specified frame for sky subtraction
                msgs.info("Loading skysub frame:" + msgs.newline() + opts_skysub)
                try:
                    spec2DObj_sky = spec2dobj.Spec2DObj.from_file(opts_skysub, self.detname)
                    skysub_exptime = fits.open(opts_skysub)[0].header['EXPTIME']
                except:
                    msgs.error("Could not load skysub image from spec2d file:" + msgs.newline() + opts_skysub)
                skyImg = spec2DObj_sky.sciimg * exptime / skysub_exptime  # Sky counts
                skyScl = spec2DObj_sky.scaleimg
                this_skysub = opts_skysub  # User specified spec2d for sky subtraction
        if this_skysub == "none":
            msgs.info("Sky subtraction will not be performed.")
        else:
            msgs.info("Using the following frame for sky subtraction:" + msgs.newline() + this_skysub)
        # Return the skysub params for this frame
        return this_skysub, skyImg, skyScl

    def compute_DAR(self, hdr0, waves, cosdec, wave_ref=None):
        """
        Compute the differential atmospheric refraction correction for a given frame.

        Args:
            hdr0 (`astropy.io.fits.Header`_):
                Header of the spec2d file. This input should be retrieved from spec2DObj.head0
            waves (`numpy.ndarray`_):
                1D flattened array containing the wavelength of each pixel (units = Angstroms)
            cosdec (:obj:`float`):
                Cosine of the target declination.
            wave_ref (:obj:`float`, optional):
                Reference wavelength (The DAR correction will be performed relative to this wavelength)

        Returns:
            `numpy.ndarray`_: 1D differential RA for each wavelength of the input waves array
            `numpy.ndarray`_: 1D differential Dec for each wavelength of the input waves array
        """
        if wave_ref is None:
            wave_ref = 0.5 * (np.min(waves) + np.max(waves))
        # Get DAR parameters
        raval = self.spec.get_meta_value([hdr0], 'ra')
        decval = self.spec.get_meta_value([hdr0], 'dec')
        obstime = self.spec.get_meta_value([hdr0], 'obstime')
        pressure = self.spec.get_meta_value([hdr0], 'pressure')
        temperature = self.spec.get_meta_value([hdr0], 'temperature')
        rel_humidity = self.spec.get_meta_value([hdr0], 'humidity')
        coord = SkyCoord(raval, decval, unit=(units.deg, units.deg))
        location = self.spec.location  # TODO :: spec.location should probably end up in the TelescopePar (spec.telescope.location)
        # Set a default value
        ra_corr, dec_corr = 0.0, 0.0
        if pressure == 0.0:
            msgs.warn("Pressure is set to zero - DAR correction will not be performed")
        else:
            msgs.info("DAR correction parameters:" + msgs.newline() +
                      "   Pressure = {0:f} bar".format(pressure) + msgs.newline() +
                      "   Temperature = {0:f} deg C".format(temperature) + msgs.newline() +
                      "   Humidity = {0:f}".format(rel_humidity))
            ra_corr, dec_corr = datacube.correct_dar(waves, coord, obstime, location,
                                                     pressure * units.bar, temperature * units.deg_C, rel_humidity,
                                                     wave_ref=wave_ref)
        return ra_corr*cosdec, dec_corr

    def align_user_offsets(self):
        """
        Align the RA and DEC of all input frames, and then
        manually shift the cubes based on user-provided offsets.
        The offsets should be specified in arcseconds, and the
        ra_offset should include the cos(dec) factor.
        """
        # First, translate all coordinates to the coordinates of the first frame
        # Note: You do not need cos(dec) here, this just overrides the IFU coordinate centre of each frame
        #       The cos(dec) factor should be input by the user, and should be included in the self.opts['ra_offset']
        ref_shift_ra = self.ifu_ra[0] - self.ifu_ra
        ref_shift_dec = self.ifu_dec[0] - self.ifu_dec
        for ff in range(self.numfiles):
            # Apply the shift
            self.all_ra[self.all_idx == ff] += ref_shift_ra[ff] + self.opts['ra_offset'][ff] / 3600.0
            self.all_dec[self.all_idx == ff] += ref_shift_dec[ff] + self.opts['dec_offset'][ff] / 3600.0
            msgs.info("Spatial shift of cube #{0:d}:" + msgs.newline() +
                      "RA, DEC (arcsec) = {1:+0.3f} E, {2:+0.3f} N".format(ff + 1,
                                                                           self.opts['ra_offset'][ff],
                                                                           self.opts['dec_offset'][ff]))

    def coadd(self):
        """
        Main entry routine to set the order of operations to coadd the data. For specific
        details of this procedure, see the child routines.
        """
        msgs.bug("This routine should be overridden by child classes.")
        msgs.error("Cannot proceed without coding the coadd routine.")
        return


class SlicerIFUCoAdd3D(CoAdd3D):
    """
    Child of CoAdd3D for SlicerIFU data reduction. For documentation, see CoAdd3d parent class above.
    spec2dfiles, opts, spectrograph=None, par=None, det=1, overwrite=False,
                     show=False, debug=False

    """
    def __init__(self, spec2dfiles, opts, spectrograph=None, par=None, det=1, overwrite=False,
                 show=False, debug=False):
        super().__init__(spec2dfiles, opts, spectrograph=spectrograph, par=par, det=det, overwrite=overwrite,
                         show=show, debug=debug)
        self.flat_splines = dict()  # A dictionary containing the splines of the flatfield
        self.mnmx_wv = None  # Will be used to store the minimum and maximum wavelengths of every slit and frame.
        self._spatscale = np.zeros((self.numfiles, 2))  # index 0, 1 = pixel scale, slicer scale

    def get_alignments(self, spec2DObj, slits, spat_flexure=None):
        """
        Generate and return the spline interpolation fitting functions to be used for
        the alignment frames, as part of the astrometric correction.

        Parameters
        ----------
        spec2DObj : :class:`~pypeit.spec2dobj.Spec2DObj`_):
            2D PypeIt spectra object.
        slits : :class:`pypeit.slittrace.SlitTraceSet`_):
            Class containing information about the slits
        spat_flexure: :obj:`float`, optional:
            Spatial flexure in pixels

        Returns
        -------
        alignSplines : :class:`~pypeit.alignframe.AlignmentSplines`_)
            Alignment splines used for the astrometric correction
        """
        # Loading the alignments frame for these data
        alignments = None
        if self.cubepar['astrometric']:
            key = alignframe.Alignments.calib_type.upper()
            if key in spec2DObj.calibs:
                alignfile = os.path.join(spec2DObj.calibs['DIR'], spec2DObj.calibs[key])
                if os.path.exists(alignfile) and self.cubepar['astrometric']:
                    msgs.info("Loading alignments")
                    alignments = alignframe.Alignments.from_file(alignfile)
            else:
                msgs.warn(f'Processed alignment frame not recorded or not found!')
                msgs.info("Using slit edges for astrometric transform")
        else:
            msgs.info("Using slit edges for astrometric transform")
        # If nothing better was provided, use the slit edges
        if alignments is None:
            left, right, _ = slits.select_edges(initial=True, flexure=spat_flexure)
            locations = [0.0, 1.0]
            traces = np.append(left[:, None, :], right[:, None, :], axis=1)
        else:
            locations = self.par['calibrations']['alignment']['locations']
            traces = alignments.traces
        # Generate an RA/DEC image
        msgs.info("Generating RA/DEC image")
        alignSplines = alignframe.AlignmentSplines(traces, locations, spec2DObj.tilts)
        # Return the alignment splines
        return alignSplines

    def get_grating_shift(self, flatfile, waveimg, slits, spat_flexure=None):
        """
        TODO :: docstring
        """
        if flatfile not in self.flat_splines.keys():
            msgs.info("Calculating relative sensitivity for grating correction")
            # Check if the Flat file exists
            if not os.path.exists(flatfile):
                msgs.error("Grating correction requested, but the following file does not exist:" +
                           msgs.newline() + flatfile)
            # Load the Flat file
            flatimages = flatfield.FlatImages.from_file(flatfile)
            total_illum = flatimages.fit2illumflat(slits, finecorr=False, frametype='illum', initial=True,
                                                   spat_flexure=spat_flexure) * \
                          flatimages.fit2illumflat(slits, finecorr=True, frametype='illum', initial=True,
                                                   spat_flexure=spat_flexure)
            flatframe = flatimages.pixelflat_raw / total_illum
            if flatimages.pixelflat_spec_illum is None:
                # Calculate the relative scale
                scale_model = flatfield.illum_profile_spectral(flatframe, waveimg, slits,
                                                               slit_illum_ref_idx=self.flatpar['slit_illum_ref_idx'],
                                                               model=None,
                                                               skymask=None, trim=self.flatpar['slit_trim'],
                                                               flexure=spat_flexure,
                                                               smooth_npix=self.flatpar['slit_illum_smooth_npix'])
            else:
                msgs.info("Using relative spectral illumination from FlatImages")
                scale_model = flatimages.pixelflat_spec_illum
            # Apply the relative scale and generate a 1D "spectrum"
            onslit = waveimg != 0
            wavebins = np.linspace(np.min(waveimg[onslit]), np.max(waveimg[onslit]), slits.nspec)
            hist, edge = np.histogram(waveimg[onslit], bins=wavebins,
                                      weights=flatframe[onslit] / scale_model[onslit])
            cntr, edge = np.histogram(waveimg[onslit], bins=wavebins)
            cntr = cntr.astype(float)
            norm = (cntr != 0) / (cntr + (cntr == 0))
            spec_spl = hist * norm
            wave_spl = 0.5 * (wavebins[1:] + wavebins[:-1])
            self.flat_splines[flatfile] = interp1d(wave_spl, spec_spl, kind='linear',
                                                   bounds_error=False, fill_value="extrapolate")
            self.flat_splines[flatfile + "_wave"] = wave_spl.copy()
            # Check if a reference blaze spline exists (either from a standard star if fluxing or from a previous
            # exposure in this for loop)
            if self.blaze_spline is None:
                self.blaze_wave, self.blaze_spec = wave_spl, spec_spl
                self.blaze_spline = interp1d(wave_spl, spec_spl, kind='linear',
                                             bounds_error=False, fill_value="extrapolate")

    def set_spatial_scale(self):
        """
        This function checks if the spatial scales of all frames are consistent.
        If the user has not specified the spatial scale, it will be set here.
        """
        # Make sure all frames have consistent scales
        if not np.all(self._spatscale[:,0] != self._spatscale[0,0]):
            msgs.warn("The pixel scales of all input frames are not the same!")
            msgs.info("Pixel scales of all input frames:" + msgs.newline() + self._spatscale[:,0])
        if not np.all(self._spatscale[:,1] != self._spatscale[0,1]):
            msgs.warn("The slicer scales of all input frames are not the same!")
            msgs.info("Slicer scales of all input frames:" + msgs.newline() + self._spatscale[:,1])
        # If the user has not specified the spatial scale, then set it appropriately now to the largest spatial scale
        if self._dspat is None:
            self._dspat = np.max(self._spatscale)
            msgs.info("Adopting a square pixel spatial scale of {0:f} arcsec".format(3600.0 * self._dspat))

    def load(self):
        """
        TODO :: docstring
        """
        # Initialise variables
        wave_ref = None
        # Load all spec2d files and prepare the data for making a datacube
        for ff, fil in enumerate(self.spec2d):
            # Load it up
            msgs.info("Loading PypeIt spec2d frame:" + msgs.newline() + fil)
            spec2DObj = spec2dobj.Spec2DObj.from_file(fil, self.detname)
            detector = spec2DObj.detector
            spat_flexure = None  # spec2DObj.sci_spat_flexure

            # Load the header
            hdr0 = spec2DObj.head0
            self.ifu_ra = np.append(self.ifu_ra, self.spec.compound_meta([hdr0], 'ra'))
            self.ifu_dec = np.append(self.ifu_dec, self.spec.compound_meta([hdr0], 'dec'))

            # Get the exposure time
            # TODO :: Surely this should be retrieved from metadata...
            exptime = hdr0['EXPTIME']

            # Setup for PypeIt imports
            msgs.reset(verbosity=2)

            # TODO :: Consider loading all calibrations into a single variable within the main CoAdd3D parent class.

            # Initialise the slit edges
            msgs.info("Constructing slit image")
            slits = spec2DObj.slits
            slitid_img_init = slits.slit_img(pad=0, initial=True, flexure=spat_flexure)
            slits_left, slits_right, _ = slits.select_edges(initial=True, flexure=spat_flexure)

            # The order of operations below proceeds as follows:
            #  (1) Get science image
            #  (2) Subtract sky (note, if a joint fit has been performed, the relative scale correction is applied in the reduction!)
            #  (3) Apply relative scale correction to both science and ivar

            # Set the default behaviour if a global skysub frame has been specified
            this_skysub, skyImg, skyScl = self.get_current_skysub(spec2DObj, exptime,
                                                                  opts_skysub=self.opts['skysub_frame'][ff])

            # Load the relative scale image, if something other than the default has been provided
            this_scalecorr, relScaleImg = self.get_current_scalecorr(spec2DObj,
                                                                     opts_scalecorr=self.opts['scale_corr'][ff])

            # Prepare the relative scaling factors
            relSclSky = skyScl / spec2DObj.scaleimg  # This factor ensures the sky has the same relative scaling as the science frame
            relScale = spec2DObj.scaleimg / relScaleImg  # This factor is applied to the sky subtracted science frame

            # Extract the relevant information from the spec2d file
            sciImg = (spec2DObj.sciimg - skyImg * relSclSky) * relScale  # Subtract sky and apply relative illumination
            ivar = spec2DObj.ivarraw / relScale ** 2
            waveimg = spec2DObj.waveimg
            bpmmask = spec2DObj.bpmmask

            # TODO :: Really need to write some detailed information in the docs about all of the various corrections that can optionally be applied

            # TODO :: Include a flexure correction from the sky frame? Note, you cannot use the waveimg from a sky frame,
            #  since the heliocentric correction may have been applied to the sky frame. Need to recalculate waveimg using
            #  the slitshifts from a skyimage, and then apply the vel_corr from the science image.

            wnonzero = (waveimg != 0.0)
            if not np.any(wnonzero):
                msgs.error("The wavelength image contains only zeros - You need to check the data reduction.")
            wave0 = waveimg[wnonzero].min()
            # Calculate the delta wave in every pixel on the slit
            waveimp = np.roll(waveimg, 1, axis=0)
            waveimn = np.roll(waveimg, -1, axis=0)
            dwaveimg = np.zeros_like(waveimg)
            # All good pixels
            wnz = np.where((waveimg != 0) & (waveimp != 0))
            dwaveimg[wnz] = np.abs(waveimg[wnz] - waveimp[wnz])
            # All bad pixels
            wnz = np.where((waveimg != 0) & (waveimp == 0))
            dwaveimg[wnz] = np.abs(waveimg[wnz] - waveimn[wnz])
            # All endpoint pixels
            dwaveimg[0, :] = np.abs(waveimg[0, :] - waveimn[0, :])
            dwaveimg[-1, :] = np.abs(waveimg[-1, :] - waveimp[-1, :])
            dwv = np.median(dwaveimg[dwaveimg != 0.0]) if self.cubepar['wave_delta'] is None else self.cubepar['wave_delta']

            msgs.info("Using wavelength solution: wave0={0:.3f}, dispersion={1:.3f} Angstrom/pixel".format(wave0, dwv))

            # Obtain the minimum and maximum wavelength of all slits
            if self.mnmx_wv is None:
                self.mnmx_wv = np.zeros((len(self.spec2d), slits.nslits, 2))
            for slit_idx, slit_spat in enumerate(slits.spat_id):
                onslit_init = (slitid_img_init == slit_spat)
                self.mnmx_wv[ff, slit_idx, 0] = np.min(waveimg[onslit_init])
                self.mnmx_wv[ff, slit_idx, 1] = np.max(waveimg[onslit_init])

            # Remove edges of the spectrum where the sky model is bad
            sky_is_good = datacube.make_good_skymask(slitid_img_init, spec2DObj.tilts)

            # Construct a good pixel mask
            # TODO: This should use the mask function to figure out which elements are masked.
            onslit_gpm = (slitid_img_init > 0) & (bpmmask.mask == 0) & sky_is_good

            # Grab the WCS of this frame
            frame_wcs = self.spec.get_wcs(spec2DObj.head0, slits, detector.platescale, wave0, dwv)
            self.all_wcs.append(copy.deepcopy(frame_wcs))

            # Find the largest spatial scale of all images being combined
            # TODO :: probably need to put this in the DetectorContainer
            pxscl = detector.platescale * parse.parse_binning(detector.binning)[1] / 3600.0  # This should be degrees/pixel
            slscl = self.spec.get_meta_value([spec2DObj.head0], 'slitwid')
            self._spatscale[ff, 0] = pxscl
            self._spatscale[ff, 1] = slscl
            # If the spatial scale has been set by the user, check that it doesn't exceed the pixel or slicer scales
            if self._dspat is not None:
                if pxscl > self._dspat:
                    msgs.warn("Spatial scale requested ({0:f} arcsec) is less than the pixel scale ({1:f} arcsec)".format(
                        3600.0 * self._dspat, 3600.0 * pxscl))
                if slscl > self._dspat:
                    msgs.warn("Spatial scale requested ({0:f} arcsec) is less than the slicer scale ({1:f} arcsec)".format(
                        3600.0 * self._dspat, 3600.0 * slscl))

            # Generate the alignment splines, and then
            # retrieve images of the RA and Dec of every pixel,
            # and the number of spatial pixels in each slit
            alignSplines = self.get_alignments(spec2DObj, slits, spat_flexure=spat_flexure)
            raimg, decimg, minmax = slits.get_radec_image(frame_wcs, alignSplines, spec2DObj.tilts,
                                                          initial=True, flexure=spat_flexure)

            # Get copies of arrays to be saved
            ra_ext = raimg[onslit_gpm]
            dec_ext = decimg[onslit_gpm]
            wave_ext = waveimg[onslit_gpm]
            flux_ext = sciImg[onslit_gpm]
            ivar_ext = ivar[onslit_gpm]
            dwav_ext = dwaveimg[onslit_gpm]

            # From here on out, work in sorted wavelengths
            wvsrt = np.argsort(wave_ext)
            wave_sort = wave_ext[wvsrt]
            dwav_sort = dwav_ext[wvsrt]
            ra_sort = ra_ext[wvsrt]
            dec_sort = dec_ext[wvsrt]
            # Here's an array to get back to the original ordering
            resrt = np.argsort(wvsrt)

            # Perform the DAR correction
            cosdec = np.cos(np.mean(dec_sort) * np.pi / 180.0)
            ra_corr, dec_corr = self.compute_DAR(spec2DObj.head0, wave_sort, cosdec, wave_ref=wave_ref)
            ra_sort += ra_corr
            dec_sort += dec_corr

            # Perform extinction correction
            msgs.info("Applying extinction correction")
            longitude = self.spec.telescope['longitude']
            latitude = self.spec.telescope['latitude']
            airmass = spec2DObj.head0[self.spec.meta['airmass']['card']]
            extinct = flux_calib.load_extinction_data(longitude, latitude, self.senspar['UVIS']['extinct_file'])
            # extinction_correction requires the wavelength is sorted
            extcorr_sort = flux_calib.extinction_correction(wave_sort * units.AA, airmass, extinct)

            # Correct for sensitivity as a function of grating angle
            # (this assumes the spectrum of the flatfield lamp has the same shape for all setups)
            gratcorr_sort = 1.0
            if self.cubepar['grating_corr']:
                # Load the flatfield file
                key = flatfield.FlatImages.calib_type.upper()
                if key not in spec2DObj.calibs:
                    msgs.error('Processed flat calibration file not recorded by spec2d file!')
                flatfile = os.path.join(spec2DObj.calibs['DIR'], spec2DObj.calibs[key])
                # Setup the grating correction
                self.get_grating_shift(flatfile, waveimg, slits, spat_flexure=spat_flexure)
                # Calculate the grating correction
                gratcorr_sort = datacube.correct_grating_shift(wave_sort, self.flat_splines[flatfile + "_wave"],
                                                               self.flat_splines[flatfile],
                                                               self.blaze_wave, self.blaze_spline)
            # Sensitivity function
            sensfunc_sort = 1.0
            if self.fluxcal:
                msgs.info("Calculating the sensitivity function")
                sensfunc_sort = self.flux_spline(wave_sort)
            # Convert the flux_sav to counts/s,  correct for the relative sensitivity of different setups
            extcorr_sort *= sensfunc_sort / (exptime * gratcorr_sort)
            # Correct for extinction
            flux_sort = flux_ext[wvsrt] * extcorr_sort
            ivar_sort = ivar_ext[wvsrt] / extcorr_sort ** 2

            # Convert units to Counts/s/Ang/arcsec2
            # Slicer sampling * spatial pixel sampling
            sl_deg = np.sqrt(frame_wcs.wcs.cd[0, 0] ** 2 + frame_wcs.wcs.cd[1, 0] ** 2)
            px_deg = np.sqrt(frame_wcs.wcs.cd[1, 1] ** 2 + frame_wcs.wcs.cd[0, 1] ** 2)
            scl_units = dwav_sort * (3600.0 * sl_deg) * (3600.0 * px_deg)
            flux_sort /= scl_units
            ivar_sort *= scl_units ** 2

            # Calculate the weights relative to the zeroth cube
            self.weights[ff] = 1.0  # exptime  #np.median(flux_sav[resrt]*np.sqrt(ivar_sav[resrt]))**2

            # Get the slit image and then unset pixels in the slit image that are bad
            this_specpos, this_spatpos = np.where(onslit_gpm)
            this_spatid = slitid_img_init[onslit_gpm]

            # If individual frames are to be output without aligning them,
            # there's no need to store information, just make the cubes now
            numpix = ra_sort.size
            if not self.combine and not self.align:
                # Get the output filename
                if self.numfiles == 1 and self.cubepar['output_filename'] != "":
                    outfile = datacube.get_output_filename("", self.cubepar['output_filename'], True, -1)
                else:
                    outfile = datacube.get_output_filename(fil, self.cubepar['output_filename'], self.combine, ff + 1)
                # Get the coordinate bounds
                slitlength = int(np.round(np.median(slits.get_slitlengths(initial=True, median=True))))
                numwav = int((np.max(waveimg) - wave0) / dwv)
                bins = self.spec.get_datacube_bins(slitlength, minmax, numwav)
                # Generate the output WCS for the datacube
                crval_wv = self.cubepar['wave_min'] if self.cubepar['wave_min'] is not None else 1.0E10 * frame_wcs.wcs.crval[2]
                cd_wv = self.cubepar['wave_delta'] if self.cubepar['wave_delta'] is not None else 1.0E10 * frame_wcs.wcs.cd[2, 2]
                output_wcs = self.spec.get_wcs(spec2DObj.head0, slits, detector.platescale, crval_wv, cd_wv)
                # Set the wavelength range of the white light image.
                wl_wvrng = None
                if self.cubepar['save_whitelight']:
                    wl_wvrng = datacube.get_whitelight_range(np.max(self.mnmx_wv[ff, :, 0]),
                                                    np.min(self.mnmx_wv[ff, :, 1]),
                                                    self.cubepar['whitelight_range'])
                # Make the datacube
                if self.method in ['subpixel', 'ngp']:
                    # Generate the datacube
                    generate_cube_subpixel(outfile, output_wcs, ra_sort[resrt], dec_sort[resrt], wave_sort[resrt],
                                           flux_sort[resrt], ivar_sort[resrt], np.ones(numpix),
                                           this_spatpos, this_specpos, this_spatid,
                                           spec2DObj.tilts, slits, alignSplines, bins,
                                           all_idx=None, overwrite=self.overwrite,
                                           blaze_wave=self.blaze_wave, blaze_spec=self.blaze_spec,
                                           fluxcal=self.fluxcal, specname=self.specname, whitelight_range=wl_wvrng,
                                           spec_subpixel=self.spec_subpixel, spat_subpixel=self.spat_subpixel)
                continue

            # Store the information if we are combining multiple frames
            self.all_ra = np.append(self.all_ra, ra_sort[resrt])
            self.all_dec = np.append(self.all_dec, dec_sort[resrt])
            self.all_wave = np.append(self.all_wave, wave_sort[resrt])
            self.all_sci = np.append(self.all_sci, flux_sort[resrt])
            self.all_ivar = np.append(self.all_ivar, ivar_sort[resrt].copy())
            self.all_idx = np.append(self.all_idx, ff * np.ones(numpix))
            self.all_wghts = np.append(self.all_wghts, self.weights[ff] * np.ones(numpix) / self.weights[0])
            self.all_spatpos = np.append(self.all_spatpos, this_spatpos)
            self.all_specpos = np.append(self.all_specpos, this_specpos)
            self.all_spatid = np.append(self.all_spatid, this_spatid)
            self.all_tilts.append(spec2DObj.tilts)
            self.all_slits.append(slits)
            self.all_align.append(alignSplines)

    def run_align(self):
        """
        This routine aligns multiple cubes by using manual input offsets or by cross-correlating white light images.
        """
        # Grab cos(dec) for convenience
        cosdec = np.cos(np.mean(self.all_dec) * np.pi / 180.0)

        # Register spatial offsets between all frames
        if self.opts['ra_offset'] is not None:
            self.align_user_offsets()
        else:
            # Find the wavelength range where all frames overlap
            min_wl, max_wl = datacube.get_whitelight_range(np.max(self.mnmx_wv[:, :, 0]),  # The max blue wavelength
                                                           np.min(self.mnmx_wv[:, :, 1]),  # The min red wavelength
                                                           self.cubepar['whitelight_range'])  # The user-specified values (if any)
            # Get the good whitelight pixels
            ww, wavediff = datacube.get_whitelight_pixels(self.all_wave, min_wl, max_wl)
            # Iterate over white light image generation and spatial shifting
            numiter = 2
            for dd in range(numiter):
                msgs.info(f"Iterating on spatial translation - ITERATION #{dd+1}/{numiter}")
                # Setup the WCS to use for all white light images
                ref_idx = None  # Don't use an index - This is the default behaviour when a reference image is supplied
                image_wcs, voxedge, reference_image = self.create_wcs(self.all_ra[ww], self.all_dec[ww], self.all_wave[ww],
                                                                      self._dspat, wavediff, collapse=True)
                if voxedge[2].size != 2:
                    msgs.error("Spectral range for WCS is incorrect for white light image")

                wl_imgs = generate_image_subpixel(image_wcs, self.all_ra[ww], self.all_dec[ww], self.all_wave[ww],
                                                  self.all_sci[ww], self.all_ivar[ww], self.all_wghts[ww],
                                                  self.all_spatpos[ww], self.all_specpos[ww], self.all_spatid[ww],
                                                  self.all_tilts, self.all_slits, self.all_align, voxedge,
                                                  all_idx=self.all_idx[ww],
                                                  spec_subpixel=self.spec_subpixel, spat_subpixel=self.spat_subpixel)
                if reference_image is None:
                    # ref_idx will be the index of the cube with the highest S/N
                    ref_idx = np.argmax(self.weights)
                    reference_image = wl_imgs[:, :, ref_idx].copy()
                    msgs.info("Calculating spatial translation of each cube relative to cube #{0:d})".format(ref_idx+1))
                else:
                    msgs.info("Calculating the spatial translation of each cube relative to user-defined 'reference_image'")

                # Calculate the image offsets relative to the reference image
                for ff in range(self.numfiles):
                    # Calculate the shift
                    ra_shift, dec_shift = calculate_image_phase(reference_image.copy(), wl_imgs[:, :, ff], maskval=0.0)
                    # Convert pixel shift to degrees shift
                    ra_shift *= self._dspat/cosdec
                    dec_shift *= self._dspat
                    msgs.info("Spatial shift of cube #{0:d}: RA, DEC (arcsec) = {1:+0.3f} E, {2:+0.3f} N".format(ff+1, ra_shift*3600.0, dec_shift*3600.0))
                    # Apply the shift
                    self.all_ra[self.all_idx == ff] += ra_shift
                    self.all_dec[self.all_idx == ff] += dec_shift

    def compute_weights(self):
        # Calculate the relative spectral weights of all pixels
        if self.numfiles == 1:
            # No need to calculate weights if there's just one frame
            self.all_wghts = np.ones_like(self.all_sci)
        else:
            # Find the wavelength range where all frames overlap
            min_wl, max_wl = datacube.get_whitelight_range(np.max(self.mnmx_wv[:, :, 0]),  # The max blue wavelength
                                                  np.min(self.mnmx_wv[:, :, 1]),  # The min red wavelength
                                                  self.cubepar['whitelight_range'])  # The user-specified values (if any)
            # Get the good white light pixels
            ww, wavediff = datacube.get_whitelight_pixels(self.all_wave, min_wl, max_wl)
            # Get a suitable WCS
            image_wcs, voxedge, reference_image = self.create_wcs(self.all_ra, self.all_dec, self.all_wave,
                                                                  self._dspat, wavediff, collapse=True)
            # Generate the white light image (note: hard-coding subpixel=1 in both directions, and combining into a single image)
            wl_full = generate_image_subpixel(image_wcs, self.all_ra, self.all_dec, self.all_wave,
                                              self.all_sci, self.all_ivar, self.all_wghts,
                                              self.all_spatpos, self.all_specpos, self.all_spatid,
                                              self.all_tilts, self.all_slits, self.all_align, voxedge, all_idx=self.all_idx,
                                              spec_subpixel=1, spat_subpixel=1, combine=True)
            # Compute the weights
            self.all_wghts = datacube.compute_weights(self.all_ra, self.all_dec, self.all_wave, self.all_sci, self.all_ivar, self.all_idx, wl_full[:, :, 0],
                                                      self._dspat, self._dwv, relative_weights=self.cubepar['relative_weights'])

    def coadd(self):
        """
        TODO :: Add docstring
        """
        # First loop through all of the frames, load the data, and save datacubes if no combining is required
        self.load()

        # No need to continue if we are not combining nor aligning frames
        if not self.combine and not self.align:
            return

        # If the user is aligning or combining, the spatial scale of the output cubes needs to be consistent.
        # Set the spatial scale of the output datacube
        self.set_spatial_scale()

        # Align the frames
        if self.align:
            self.run_align()

        # Compute the relative weights on the spectra
        self.compute_weights()

        # Generate the WCS, and the voxel edges
        cube_wcs, vox_edges, _ = self.create_wcs(self.all_ra, self.all_dec, self.all_wave, self._dspat, self._dwv)

        sensfunc = None
        if self.flux_spline is not None:
            # Get wavelength of each pixel, and note that the WCS gives this in m, so convert to Angstroms (x 1E10)
            numwav = vox_edges[2].size - 1
            senswave = cube_wcs.spectral.wcs_pix2world(np.arange(numwav), 0)[0] * 1.0E10
            sensfunc = self.flux_spline(senswave)

        # Generate a datacube
        outfile = datacube.get_output_filename("", self.cubepar['output_filename'], True, -1)
        if self.method in ['subpixel', 'ngp']:
            # Generate the datacube
            wl_wvrng = None
            if self.cubepar['save_whitelight']:
                wl_wvrng = datacube.get_whitelight_range(np.max(self.mnmx_wv[:, :, 0]),
                                                np.min(self.mnmx_wv[:, :, 1]),
                                                self.cubepar['whitelight_range'])
            if self.combine:
                generate_cube_subpixel(outfile, cube_wcs, self.all_ra, self.all_dec, self.all_wave, self.all_sci, self.all_ivar,
                                       np.ones(self.all_wghts.size),  # all_wghts,
                                       self.all_spatpos, self.all_specpos, self.all_spatid, self.all_tilts, self.all_slits, self.all_align, vox_edges,
                                       all_idx=self.all_idx, overwrite=self.overwrite, blaze_wave=self.blaze_wave,
                                       blaze_spec=self.blaze_spec,
                                       fluxcal=self.fluxcal, sensfunc=sensfunc, specname=self.specname, whitelight_range=wl_wvrng,
                                       spec_subpixel=self.spec_subpixel, spat_subpixel=self.spat_subpixel)
            else:
                for ff in range(self.numfiles):
                    outfile = datacube.get_output_filename("", self.cubepar['output_filename'], False, ff)
                    ww = np.where(self.all_idx == ff)
                    generate_cube_subpixel(outfile, cube_wcs, self.all_ra[ww], self.all_dec[ww], self.all_wave[ww], self.all_sci[ww],
                                           self.all_ivar[ww], np.ones(self.all_wghts[ww].size),
                                           self.all_spatpos[ww], self.all_specpos[ww], self.all_spatid[ww], self.all_tilts[ff],
                                           self.all_slits[ff], self.all_align[ff], vox_edges,
                                           all_idx=self.all_idx[ww], overwrite=self.overwrite, blaze_wave=self.blaze_wave,
                                           blaze_spec=self.blaze_spec,
                                           fluxcal=self.fluxcal, sensfunc=sensfunc, specname=self.specname,
                                           whitelight_range=wl_wvrng,
                                           spec_subpixel=self.spec_subpixel, spat_subpixel=self.spat_subpixel)


def generate_image_subpixel(image_wcs, all_ra, all_dec, all_wave, all_sci, all_ivar, all_wghts,
                            all_spatpos, all_specpos, all_spatid, tilts, slits, astrom_trans, bins,
                            all_idx=None, spec_subpixel=10, spat_subpixel=10, combine=False):
    """
    Generate a white light image from the input pixels

    Args:
        image_wcs (`astropy.wcs.WCS`_):
            World coordinate system to use for the white light images.
        all_ra (`numpy.ndarray`_):
            1D flattened array containing the right ascension of each pixel
            (units = degrees)
        all_dec (`numpy.ndarray`_):
            1D flattened array containing the declination of each pixel (units =
            degrees)
        all_wave (`numpy.ndarray`_):
            1D flattened array containing the wavelength of each pixel (units =
            Angstroms)
        all_sci (`numpy.ndarray`_):
            1D flattened array containing the counts of each pixel from all
            spec2d files
        all_ivar (`numpy.ndarray`_):
            1D flattened array containing the inverse variance of each pixel
            from all spec2d files
        all_wghts (`numpy.ndarray`_):
            1D flattened array containing the weights of each pixel to be used
            in the combination
        all_spatpos (`numpy.ndarray`_):
            1D flattened array containing the detector pixel location in the
            spatial direction
        all_specpos (`numpy.ndarray`_):
            1D flattened array containing the detector pixel location in the
            spectral direction
        all_spatid (`numpy.ndarray`_):
            1D flattened array containing the spatid of each pixel
        tilts (`numpy.ndarray`_, list):
            2D wavelength tilts frame, or a list of tilt frames (see all_idx)
        slits (:class:`~pypeit.slittrace.SlitTraceSet`, list):
            Information stored about the slits, or a list of SlitTraceSet (see
            all_idx)
        astrom_trans (:class:`~pypeit.alignframe.AlignmentSplines`, list):
            A Class containing the transformation between detector pixel
            coordinates and WCS pixel coordinates, or a list of Alignment
            Splines (see all_idx)
        bins (tuple):
            A 3-tuple (x,y,z) containing the histogram bin edges in x,y spatial
            and z wavelength coordinates
        all_idx (`numpy.ndarray`_, optional):
            If tilts, slits, and astrom_trans are lists, this should contain a
            1D flattened array, of the same length as all_sci, containing the
            index the tilts, slits, and astrom_trans lists that corresponds to
            each pixel. Note that, in this case all of these lists need to be
            the same length.
        spec_subpixel (:obj:`int`, optional):
            What is the subpixellation factor in the spectral direction. Higher
            values give more reliable results, but note that the time required
            goes as (``spec_subpixel * spat_subpixel``). The default value is 5,
            which divides each detector pixel into 5 subpixels in the spectral
            direction.
        spat_subpixel (:obj:`int`, optional):
            What is the subpixellation factor in the spatial direction. Higher
            values give more reliable results, but note that the time required
            goes as (``spec_subpixel * spat_subpixel``). The default value is 5,
            which divides each detector pixel into 5 subpixels in the spatial
            direction.
        combine (:obj:`bool`, optional):
            If True, all of the input frames will be combined into a single
            output. Otherwise, individual images will be generated.

    Returns:
        `numpy.ndarray`_: The white light images for all frames
    """
    # Perform some checks on the input -- note, more complete checks are performed in subpixellate()
    _all_idx = np.zeros(all_sci.size) if all_idx is None else all_idx
    if combine:
        numfr = 1
    else:
        numfr = np.unique(_all_idx).size
        if len(tilts) != numfr or len(slits) != numfr or len(astrom_trans) != numfr:
            msgs.error("The following arguments must be the same length as the expected number of frames to be combined:"
                       + msgs.newline() + "tilts, slits, astrom_trans")
    # Prepare the array of white light images to be stored
    numra = bins[0].size-1
    numdec = bins[1].size-1
    all_wl_imgs = np.zeros((numra, numdec, numfr))

    # Loop through all frames and generate white light images
    for fr in range(numfr):
        msgs.info(f"Creating image {fr+1}/{numfr}")
        if combine:
            # Subpixellate
            img, _, _ = subpixellate(image_wcs, all_ra, all_dec, all_wave,
                                     all_sci, all_ivar, all_wghts, all_spatpos,
                                     all_specpos, all_spatid, tilts, slits, astrom_trans, bins,
                                     spec_subpixel=spec_subpixel, spat_subpixel=spat_subpixel, all_idx=_all_idx)
        else:
            ww = np.where(_all_idx == fr)
            # Subpixellate
            img, _, _ = subpixellate(image_wcs, all_ra[ww], all_dec[ww], all_wave[ww],
                                     all_sci[ww], all_ivar[ww], all_wghts[ww], all_spatpos[ww],
                                     all_specpos[ww], all_spatid[ww], tilts[fr], slits[fr], astrom_trans[fr], bins,
                                     spec_subpixel=spec_subpixel, spat_subpixel=spat_subpixel)
        all_wl_imgs[:, :, fr] = img[:, :, 0]
    # Return the constructed white light images
    return all_wl_imgs


def generate_cube_subpixel(outfile, output_wcs, all_ra, all_dec, all_wave, all_sci, all_ivar, all_wghts,
                           all_spatpos, all_specpos, all_spatid, tilts, slits, astrom_trans, bins,
                           all_idx=None, spec_subpixel=10, spat_subpixel=10, overwrite=False, blaze_wave=None,
                           blaze_spec=None, fluxcal=False, sensfunc=None, whitelight_range=None,
                           specname="PYP_SPEC", debug=False):
    r"""
    Save a datacube using the subpixel algorithm. Refer to the subpixellate()
    docstring for further details about this algorithm

    Args:
        outfile (str):
            Filename to be used to save the datacube
        output_wcs (`astropy.wcs.WCS`_):
            Output world coordinate system.
        all_ra (`numpy.ndarray`_):
            1D flattened array containing the right ascension of each pixel
            (units = degrees)
        all_dec (`numpy.ndarray`_):
            1D flattened array containing the declination of each pixel (units =
            degrees)
        all_wave (`numpy.ndarray`_):
            1D flattened array containing the wavelength of each pixel (units =
            Angstroms)
        all_sci (`numpy.ndarray`_):
            1D flattened array containing the counts of each pixel from all
            spec2d files
        all_ivar (`numpy.ndarray`_):
            1D flattened array containing the inverse variance of each pixel
            from all spec2d files
        all_wghts (`numpy.ndarray`_):
            1D flattened array containing the weights of each pixel to be used
            in the combination
        all_spatpos (`numpy.ndarray`_):
            1D flattened array containing the detector pixel location in the
            spatial direction
        all_specpos (`numpy.ndarray`_):
            1D flattened array containing the detector pixel location in the
            spectral direction
        all_spatid (`numpy.ndarray`_):
            1D flattened array containing the spatid of each pixel
        tilts (`numpy.ndarray`_, list):
            2D wavelength tilts frame, or a list of tilt frames (see all_idx)
        slits (:class:`~pypeit.slittrace.SlitTraceSet`, list):
            Information stored about the slits, or a list of SlitTraceSet (see
            all_idx)
        astrom_trans (:class:`~pypeit.alignframe.AlignmentSplines`, list):
            A Class containing the transformation between detector pixel
            coordinates and WCS pixel coordinates, or a list of Alignment
            Splines (see all_idx)
        bins (tuple):
            A 3-tuple (x,y,z) containing the histogram bin edges in x,y spatial
            and z wavelength coordinates
        all_idx (`numpy.ndarray`_, optional):
            If tilts, slits, and astrom_trans are lists, this should contain a
            1D flattened array, of the same length as all_sci, containing the
            index the tilts, slits, and astrom_trans lists that corresponds to
            each pixel. Note that, in this case all of these lists need to be
            the same length.
        spec_subpixel (int, optional):
            What is the subpixellation factor in the spectral direction. Higher
            values give more reliable results, but note that the time required
            goes as (``spec_subpixel * spat_subpixel``). The default value is 5,
            which divides each detector pixel into 5 subpixels in the spectral
            direction.
        spat_subpixel (int, optional):
            What is the subpixellation factor in the spatial direction. Higher
            values give more reliable results, but note that the time required
            goes as (``spec_subpixel * spat_subpixel``). The default value is 5,
            which divides each detector pixel into 5 subpixels in the spatial
            direction.
        overwrite (bool, optional):
            If True, the output cube will be overwritten.
        blaze_wave (`numpy.ndarray`_, optional):
            Wavelength array of the spectral blaze function
        blaze_spec (`numpy.ndarray`_, optional):
            Spectral blaze function
        fluxcal (bool, optional):
            Are the data flux calibrated? If True, the units are: :math:`{\rm
            erg/s/cm}^2{\rm /Angstrom/arcsec}^2` multiplied by the
            PYPEIT_FLUX_SCALE.  Otherwise, the units are: :math:`{\rm
            counts/s/Angstrom/arcsec}^2`.
        sensfunc (`numpy.ndarray`_, None, optional):
            Sensitivity function that has been applied to the datacube
        whitelight_range (None, list, optional):
            A two element list that specifies the minimum and maximum
            wavelengths (in Angstroms) to use when constructing the white light
            image (format is: [min_wave, max_wave]). If None, the cube will be
            collapsed over the full wavelength range. If a list is provided an
            either element of the list is None, then the minimum/maximum
            wavelength range of that element will be set by the minimum/maximum
            wavelength of all_wave.
        specname (str, optional):
            Name of the spectrograph
        debug (bool, optional):
            If True, a residuals cube will be output. If the datacube generation
            is correct, the distribution of pixels in the residual cube with no
            flux should have mean=0 and std=1.
    """
    # Prepare the header, and add the unit of flux to the header
    hdr = output_wcs.to_header()
    if fluxcal:
        hdr['FLUXUNIT'] = (flux_calib.PYPEIT_FLUX_SCALE, "Flux units -- erg/s/cm^2/Angstrom/arcsec^2")
    else:
        hdr['FLUXUNIT'] = (1, "Flux units -- counts/s/Angstrom/arcsec^2")

    # Subpixellate
    subpix = subpixellate(output_wcs, all_ra, all_dec, all_wave, all_sci, all_ivar, all_wghts, all_spatpos, all_specpos,
                          all_spatid, tilts, slits, astrom_trans, bins, all_idx=all_idx,
                          spec_subpixel=spec_subpixel, spat_subpixel=spat_subpixel, debug=debug)
    # Extract the variables that we need
    if debug:
        datacube, varcube, bpmcube, residcube = subpix
        # Save a residuals cube
        outfile_resid = outfile.replace(".fits", "_resid.fits")
        msgs.info("Saving residuals datacube as: {0:s}".format(outfile_resid))
        hdu = fits.PrimaryHDU(residcube.T, header=hdr)
        hdu.writeto(outfile_resid, overwrite=overwrite)
    else:
        datacube, varcube, bpmcube = subpix

    # Check if the user requested a white light image
    if whitelight_range is not None:
        # Grab the WCS of the white light image
        whitelight_wcs = output_wcs.celestial
        # Determine the wavelength range of the whitelight image
        if whitelight_range[0] is None:
            whitelight_range[0] = np.min(all_wave)
        if whitelight_range[1] is None:
            whitelight_range[1] = np.max(all_wave)
        msgs.info("White light image covers the wavelength range {0:.2f} A - {1:.2f} A".format(
            whitelight_range[0], whitelight_range[1]))
        # Get the output filename for the white light image
        out_whitelight = datacube.get_output_whitelight_filename(outfile)
        nspec = datacube.shape[2]
        # Get wavelength of each pixel, and note that the WCS gives this in m, so convert to Angstroms (x 1E10)
        wave = 1.0E10 * output_wcs.spectral.wcs_pix2world(np.arange(nspec), 0)[0]
        whitelight_img = datacube.make_whitelight_fromcube(datacube, wave=wave, wavemin=whitelight_range[0], wavemax=whitelight_range[1])
        msgs.info("Saving white light image as: {0:s}".format(out_whitelight))
        img_hdu = fits.PrimaryHDU(whitelight_img.T, header=whitelight_wcs.to_header())
        img_hdu.writeto(out_whitelight, overwrite=overwrite)

    # Write out the datacube
    msgs.info("Saving datacube as: {0:s}".format(outfile))
    final_cube = DataCube(datacube.T, np.sqrt(varcube.T), bpmcube.T, specname, blaze_wave, blaze_spec,
                          sensfunc=sensfunc, fluxed=fluxcal)
    final_cube.to_file(outfile, hdr=hdr, overwrite=overwrite)


def subpixellate(output_wcs, all_ra, all_dec, all_wave, all_sci, all_ivar, all_wghts, all_spatpos, all_specpos,
                 all_spatid, tilts, slits, astrom_trans, bins, all_idx=None,
                 spec_subpixel=10, spat_subpixel=10, debug=False):
    r"""
    Subpixellate the input data into a datacube. This algorithm splits each
    detector pixel into multiple subpixels, and then assigns each subpixel to a
    voxel. For example, if ``spec_subpixel = spat_subpixel = 10``, then each
    detector pixel is divided into :math:`10^2=100` subpixels. Alternatively,
    when spec_subpixel = spat_subpixel = 1, this corresponds to the nearest grid
    point (NGP) algorithm.

    Important Note: If spec_subpixel > 1 or spat_subpixel > 1, the errors will
    be correlated, and the covariance is not being tracked, so the errors will
    not be (quite) right. There is a tradeoff one has to make between sampling
    and better looking cubes, versus no sampling and better behaved errors.

    Args:
        output_wcs (`astropy.wcs.WCS`_):
            Output world coordinate system.
        all_ra (`numpy.ndarray`_):
            1D flattened array containing the right ascension of each pixel
            (units = degrees)
        all_dec (`numpy.ndarray`_):
            1D flattened array containing the declination of each pixel (units =
            degrees)
        all_wave (`numpy.ndarray`_):
            1D flattened array containing the wavelength of each pixel (units =
            Angstroms)
        all_sci (`numpy.ndarray`_):
            1D flattened array containing the counts of each pixel from all
            spec2d files
        all_ivar (`numpy.ndarray`_):
            1D flattened array containing the inverse variance of each pixel
            from all spec2d files
        all_wghts (`numpy.ndarray`_):
            1D flattened array containing the weights of each pixel to be used
            in the combination
        all_spatpos (`numpy.ndarray`_):
            1D flattened array containing the detector pixel location in the
            spatial direction
        all_specpos (`numpy.ndarray`_):
            1D flattened array containing the detector pixel location in the
            spectral direction
        all_spatid (`numpy.ndarray`_):
            1D flattened array containing the spatid of each pixel
        tilts (`numpy.ndarray`_, list):
            2D wavelength tilts frame, or a list of tilt frames (see all_idx)
        slits (:class:`~pypeit.slittrace.SlitTraceSet`, list):
            Information stored about the slits, or a list of SlitTraceSet (see
            all_idx)
        astrom_trans (:class:`~pypeit.alignframe.AlignmentSplines`, list):
            A Class containing the transformation between detector pixel
            coordinates and WCS pixel coordinates, or a list of Alignment
            Splines (see all_idx)
        bins (tuple):
            A 3-tuple (x,y,z) containing the histogram bin edges in x,y spatial
            and z wavelength coordinates
        all_idx (`numpy.ndarray`_, optional):
            If tilts, slits, and astrom_trans are lists, this should contain a
            1D flattened array, of the same length as all_sci, containing the
            index the tilts, slits, and astrom_trans lists that corresponds to
            each pixel. Note that, in this case all of these lists need to be
            the same length.
        spec_subpixel (:obj:`int`, optional):
            What is the subpixellation factor in the spectral direction. Higher
            values give more reliable results, but note that the time required
            goes as (``spec_subpixel * spat_subpixel``). The default value is 5,
            which divides each detector pixel into 5 subpixels in the spectral
            direction.
        spat_subpixel (:obj:`int`, optional):
            What is the subpixellation factor in the spatial direction. Higher
            values give more reliable results, but note that the time required
            goes as (``spec_subpixel * spat_subpixel``). The default value is 5,
            which divides each detector pixel into 5 subpixels in the spatial
            direction.
        debug (bool):
            If True, a residuals cube will be output. If the datacube generation
            is correct, the distribution of pixels in the residual cube with no
            flux should have mean=0 and std=1.

    Returns:
        :obj:`tuple`: Three or four `numpy.ndarray`_ objects containing (1) the
        datacube generated from the subpixellated inputs, (2) the corresponding
        variance cube, (3) the corresponding bad pixel mask cube, and (4) the
        residual cube.  The latter is only returned if debug is True.
    """
    # Check for combinations of lists or not
    if type(tilts) is list and type(slits) is list and type(astrom_trans) is list:
        # Several frames are being combined. Check the lists have the same length
        numframes = len(tilts)
        if len(slits) != numframes or len(astrom_trans) != numframes:
            msgs.error("The following lists must have the same length:" + msgs.newline() +
                       "tilts, slits, astrom_trans")
        # Check all_idx has been set
        if all_idx is None:
            if numframes != 1:
                msgs.error("Missing required argument for combining frames: all_idx")
            else:
                all_idx = np.zeros(all_sci.size)
        else:
            tmp = np.unique(all_idx).size
            if tmp != numframes:
                msgs.warn("Indices in argument 'all_idx' does not match the number of frames expected.")
        # Store in the following variables
        _tilts, _slits, _astrom_trans = tilts, slits, astrom_trans
    elif type(tilts) is not list and type(slits) is not list and \
            type(astrom_trans) is not list:
        # Just a single frame - store as lists for this code
        _tilts, _slits, _astrom_trans = [tilts], [slits], [astrom_trans],
        all_idx = np.zeros(all_sci.size)
        numframes = 1
    else:
        msgs.error("The following input arguments should all be of type 'list', or all not be type 'list':" +
                   msgs.newline() + "tilts, slits, astrom_trans")
    # Prepare the output arrays
    outshape = (bins[0].size-1, bins[1].size-1, bins[2].size-1)
    binrng = [[bins[0][0], bins[0][-1]], [bins[1][0], bins[1][-1]], [bins[2][0], bins[2][-1]]]
    datacube, varcube, normcube = np.zeros(outshape), np.zeros(outshape), np.zeros(outshape)
    if debug:
        residcube = np.zeros(outshape)
    # Divide each pixel into subpixels
    spec_offs = np.arange(0.5/spec_subpixel, 1, 1/spec_subpixel) - 0.5  # -0.5 is to offset from the centre of each pixel.
    spat_offs = np.arange(0.5/spat_subpixel, 1, 1/spat_subpixel) - 0.5  # -0.5 is to offset from the centre of each pixel.
    spat_x, spec_y = np.meshgrid(spat_offs, spec_offs)
    num_subpixels = spec_subpixel * spat_subpixel
    area = 1 / num_subpixels
    all_wght_subpix = all_wghts * area
    all_var = utils.inverse(all_ivar)
    # Loop through all exposures
    for fr in range(numframes):
        # Extract tilts and slits for convenience
        this_tilts = _tilts[fr]
        this_slits = _slits[fr]
        # Loop through all slits
        for sl, spatid in enumerate(this_slits.spat_id):
            if numframes == 1:
                msgs.info(f"Resampling slit {sl+1}/{this_slits.nslits}")
            else:
                msgs.info(f"Resampling slit {sl+1}/{this_slits.nslits} of frame {fr+1}/{numframes}")
            this_sl = np.where((all_spatid == spatid) & (all_idx == fr))
            wpix = (all_specpos[this_sl], all_spatpos[this_sl])
            # Generate a spline between spectral pixel position and wavelength
            yspl = this_tilts[wpix]*(this_slits.nspec - 1)
            tiltpos = np.add.outer(yspl, spec_y).flatten()
            wspl = all_wave[this_sl]
            asrt = np.argsort(yspl)
            wave_spl = interp1d(yspl[asrt], wspl[asrt], kind='linear', bounds_error=False, fill_value='extrapolate')
            # Calculate spatial and spectral positions of the subpixels
            spat_xx = np.add.outer(wpix[1], spat_x.flatten()).flatten()
            spec_yy = np.add.outer(wpix[0], spec_y.flatten()).flatten()
            # Transform this to spatial location
            spatpos_subpix = _astrom_trans[fr].transform(sl, spat_xx, spec_yy)
            spatpos = _astrom_trans[fr].transform(sl, all_spatpos[this_sl], all_specpos[this_sl])
            ra_coeff = np.polyfit(spatpos, all_ra[this_sl], 1)
            dec_coeff = np.polyfit(spatpos, all_dec[this_sl], 1)
            this_ra = np.polyval(ra_coeff, spatpos_subpix)#ra_spl(spatpos_subpix)
            this_dec = np.polyval(dec_coeff, spatpos_subpix)#dec_spl(spatpos_subpix)
            # ssrt = np.argsort(spatpos)
            # ra_spl = interp1d(spatpos[ssrt], all_ra[this_sl][ssrt], kind='linear', bounds_error=False, fill_value='extrapolate')
            # dec_spl = interp1d(spatpos[ssrt], all_dec[this_sl][ssrt], kind='linear', bounds_error=False, fill_value='extrapolate')
            # this_ra = ra_spl(spatpos_subpix)
            # this_dec = dec_spl(spatpos_subpix)
            this_wave = wave_spl(tiltpos)
            # Convert world coordinates to voxel coordinates, then histogram
            vox_coord = output_wcs.wcs_world2pix(np.vstack((this_ra, this_dec, this_wave * 1.0E-10)).T, 0)
            if histogramdd is not None:
                # use the "fast histogram" algorithm, that assumes regular bin spacing
                datacube += histogramdd(vox_coord, bins=outshape, range=binrng, weights=np.repeat(all_sci[this_sl] * all_wght_subpix[this_sl], num_subpixels))
                varcube += histogramdd(vox_coord, bins=outshape, range=binrng, weights=np.repeat(all_var[this_sl] * all_wght_subpix[this_sl]**2, num_subpixels))
                normcube += histogramdd(vox_coord, bins=outshape, range=binrng, weights=np.repeat(all_wght_subpix[this_sl], num_subpixels))
                if debug:
                    residcube += histogramdd(vox_coord, bins=outshape, range=binrng, weights=np.repeat(all_sci[this_sl] * np.sqrt(all_ivar[this_sl]), num_subpixels))
            else:
                datacube += np.histogramdd(vox_coord, bins=outshape, weights=np.repeat(all_sci[this_sl] * all_wght_subpix[this_sl], num_subpixels))[0]
                varcube += np.histogramdd(vox_coord, bins=outshape, weights=np.repeat(all_var[this_sl] * all_wght_subpix[this_sl]**2, num_subpixels))[0]
                normcube += np.histogramdd(vox_coord, bins=outshape, weights=np.repeat(all_wght_subpix[this_sl], num_subpixels))[0]
                if debug:
                    residcube += np.histogramdd(vox_coord, bins=outshape, weights=np.repeat(all_sci[this_sl] * np.sqrt(all_ivar[this_sl]), num_subpixels))[0]
    # Normalise the datacube and variance cube
    nc_inverse = utils.inverse(normcube)
    datacube *= nc_inverse
    varcube *= nc_inverse**2
    bpmcube = (normcube == 0).astype(np.uint8)
    if debug:
        residcube *= nc_inverse
        return datacube, varcube, bpmcube, residcube
    return datacube, varcube, bpmcube
