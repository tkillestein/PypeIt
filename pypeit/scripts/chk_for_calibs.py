#!/usr/bin/env python
#
# See top-level LICENSE file for Copyright information
#
# -*- coding: utf-8 -*-
"""
This script examines a set of files and indicates which do and
which do not have sufficient calibs
"""

from pypeit import defs

def parse_args(options=None, return_parser=False):
    import argparse

    parser = argparse.ArgumentParser(description="Script to check for calibrations [v1]",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('root', type=str, default=None, help='File path+root, e.g. /data/Kast/b ')
    parser.add_argument('-s', '--spectrograph', default=None, type=str,
                        help='A valid spectrograph identifier: {0}'.format(
                                ', '.join(defs.pypeit_spectrographs)))
    parser.add_argument('-e', '--extension', default='.fits',
                        help='File extension; compression indicators (e.g. .gz) not required.')

    if return_parser:
        return parser

    return parser.parse_args() if options is None else parser.parse_args(options)


def main(args):
    """

    Args:
        args:

    Returns:
        astropy.table.Table:

    """

    import os

    from IPython import embed

    import numpy as np

    from astropy import table

    from pypeit.pypeitsetup import PypeItSetup
    from pypeit import calibrations
    from pypeit import msgs
    from pypeit.par import PypeItPar

    # Check that the spectrograph is provided if using a file root
    if args.root is not None:
        if args.spectrograph is None:
            raise ValueError('Must provide spectrograph identifier with file root.')
        # Check that input spectrograph is supported
        instruments_served = defs.pypeit_spectrographs
        if args.spectrograph not in instruments_served:
            raise ValueError('Instrument \'{0}\' unknown to PypeIt.\n'.format(args.spectrograph)
                             + '\tOptions are: {0}\n'.format(', '.join(instruments_served))
                             + '\tSelect an available instrument or consult the documentation '
                             + 'on how to add a new instrument.')

    # Initialize PypeItSetup based on the arguments
    ps = PypeItSetup.from_file_root(args.root, args.spectrograph, extension=args.extension)

    # Run the setup
    ps.run(setup_only=True)#, write_bkg_pairs=args.background)
    is_science = ps.fitstbl.find_frames('science')

    msgs.info('Loaded spectrograph {0}'.format(ps.spectrograph.spectrograph))

    # Unique configurations
    uniq_cfg = ps.fitstbl.unique_configurations(copy=True)

    # Setup the table. Need to use object type for strings so that
    # they're not truncated.
    answers = table.Table()
    answers['setups'] = list(uniq_cfg.keys())
    # Add the configuration columns
    for setup, setup_dict in uniq_cfg.items():
        for key, value in setup_dict.items():
            answers[key] = np.empty(len(answers), dtype=object) if isinstance(value, str) \
                                else type(value)(0)
        break
    answers['pass'] = False
    answers['scifiles'] = np.empty(len(answers), dtype=object)

    for i, setup in enumerate(uniq_cfg.keys()):
        for setup_key, setup_value in uniq_cfg[setup].items():
            answers[setup_key] = setup_value
        if setup == 'None':
            print("There is a setup without science frames.  Skipping...")
            answers['pass'][i] = False
            answers['scifiles'][i] = None
            continue

        msgs.info('=======================================================================')
        msgs.info('Working on setup: {}'.format(setup))
        msgs.info(str(uniq_cfg[setup]))
        msgs.info('=======================================================================')

        # TODO: Make the snippet below, which is also in the init of
        # PypeIt a method somewhere
        in_cfg = ps.fitstbl['setup'] == setup
        config_specific_file = None

        # Grab a science/standard frame
        data_files = [os.path.join(row['directory'], row['filename']) for row in ps.fitstbl[in_cfg]]
        for idx, row in enumerate(ps.fitstbl[in_cfg]):
            if 'science' in row['frametype'] or 'standard' in row['frametype']:
                config_specific_file = data_files[idx]
        if config_specific_file is not None:
            msgs.info('Setting configuration-specific parameters using {0}'.format(
                        os.path.split(config_specific_file)[1]))
        else:
            msgs.warn('No science or standard frame.  Punting..')
            answers['pass'][i] = False
            answers['scifiles'][i] = None
            continue
        #
        spectrograph_cfg_lines = ps.spectrograph.config_specific_par(config_specific_file).to_config()

        #   - Build the full set, merging with any user-provided
        #     parameters
        par = PypeItPar.from_cfg_lines(cfg_lines=spectrograph_cfg_lines)
        # Print science frames
        if np.any(in_cfg & is_science):
            msgs.info('Your science frames are: {0}'.format(
                        ps.fitstbl['filename'][in_cfg & is_science].tolist()))
            answers['scifiles'][i] = ', '.join(ps.fitstbl['filename'][in_cfg & is_science].tolist())
        else:
            msgs.warn("This setup has no science frames!")
            answers['scifiles'][i] = ''

        # Check!
        answers['pass'][i] = calibrations.check_for_calibs(par, ps.fitstbl, raise_error=False,
                                                           cut_cfg=in_cfg)
        if not answers['pass'][i]:
            msgs.warn("Setup {} did not pass the calibration check!".format(setup))

    print('= RESULTS ============================================')
    # Print
    answers.pprint_all()
    print('======================================================')
    # Return objects used by unit tests
    return answers, ps


