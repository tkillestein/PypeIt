#!/usr/bin/env python

"""
Wrapper to the linetools XSpecGUI
"""
from pypit import pyputils
from pypit import armsgs
msgs = pyputils.get_dummy_logger()
from numpy import isnan

def parser(options=None):

    import argparse

    parser = argparse.ArgumentParser(description='Script to coadd a set of spec1D files and 1 or more slits and 1 or more objects. Current defaults use Optimal + Fluxed extraction. [v1.1]')
    parser.add_argument("infile", type=str, help="Input file (YAML)")

    if options is None:
        args = parser.parse_args()
    else:
        args = parser.parse_args(options)
    return args


def main(args, unit_test=False, path=''):
    """ Runs the XSpecGui on an input file
    path : str, optional
      Mainly for running the unit test
    """
    import yaml, glob
    from pypit import arcoadd
    from pypit import arspecobj
    from astropy.io import fits

    # Load the input file
    with open(args.infile, 'r') as infile:
        coadd_dict = yaml.load(infile)

    # Grab object names in the spectra
    filelist = coadd_dict.pop('filenames')
    # Allow for wildcards
    files = []
    for ifl in filelist:
        if '*' in ifl:
            files += glob.glob(path+ifl)
        else:
            files += [path+ifl]
    fdict = {}
    for ifile in files:
        # Open file
        hdulist = fits.open(ifile)
        # Grab objects
        objects = [hdu.name for hdu in hdulist][1:]
        fdict[ifile] = objects

    # Global parameters?
    if 'global' in coadd_dict.keys():
        gparam = coadd_dict.pop('global')
    else:
        gparam = {}
    # Extraction
    if 'extract' in coadd_dict.keys():
        ex_value = coadd_dict.pop('extract')
    else:
        ex_value = 'opt'
    # Fluxed data?
    if 'flux' in coadd_dict.keys():
        flux_value = coadd_dict.pop('flux')
    else:
        flux_value = True
    # Loop on sources
    for key in coadd_dict.keys():
        iobj = coadd_dict[key]['object']
        # Check iobj input
        #pdb.set_trace()
        if isinstance(iobj, list):
            if len(iobj) != len(files):
                raise IOError("Input list of object names must have same length as files")
        #
        outfile = coadd_dict[key]['outfile']
        if unit_test:
            return gparam, ex_value, flux_value, iobj, outfile, files

        # Loop on spec1d files
        gdfiles = []
        extensions = []
        gdobj = []

        for fkey in fdict:
            if len(iobj) == 1:
                mtch_obj, idx = arspecobj.mtch_obj_to_objects(iobj, fdict[fkey])
            else:
                ind = files.index(fkey)
                mtch_obj, idx = arspecobj.mtch_obj_to_objects(iobj[ind], fdict[fkey])
            if mtch_obj is None:
                print("No object {:s} in file {:s}".format(iobj, fkey))
            elif len(mtch_obj) == 1:
                #Check if optimal extraction is present in all  objects. If not, warn the user and set ex_value to 'box'.
                hdulist = fits.open(fkey)
                try:
                    #In case the optimal extraction array is a NaN array
                    obj_opt_flam = hdulist[mtch_obj[0]].data['OPT_FLAM']
                    if any(isnan(obj_opt_flam)):
                        msgs.warn("Object {:s} in file {:s} has a NaN array for optimal extraction. Boxcar will be used instead.".format(mtch_obj[0],fkey))
                        ex_value = 'box'
                except KeyError:
                    #In case the array is absent altogether.
                    msgs.warn("Object {:s} in file {:s} doesn't have an optimal extraction. Boxcar will be used instead.".format(mtch_obj[0],fkey))
                    try:
                        hdulist[mtch_obj[0]].data['BOX_FLAM']
                    except KeyError:
                        #In case the boxcar extract is also absent
                        msgs.error("Object {:s} in file {:s} doesn't have a boxcar extraction either. Co-addition cannot be performed".format(mtch_obj[0],fkey))
                    ex_value = 'box'
                
                    msgs.error()
                gdfiles.append(fkey)
                gdobj += mtch_obj
                extensions.append(idx[0]+1)
            else:
                raise ValueError("Multiple matches to object {:s} in file {:s}".format(iobj, fkey))

        # Load spectra
        spectra = arcoadd.load_spec(gdfiles, iextensions=extensions, extract=ex_value, flux=flux_value)
        exten = outfile.split('.')[-1]  # Allow for hdf or fits or whatever
        qafile = outfile.replace(exten, 'pdf')
        # Coadd!
        arcoadd.coadd_spectra(spectra, qafile=qafile, outfile=outfile, **gparam)
