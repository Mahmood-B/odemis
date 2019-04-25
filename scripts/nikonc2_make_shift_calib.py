#!/usr/bin/env python
# -*- coding: utf-8 -*-
'''
Created on 15 October 2018

@author: Anders Muskens

Take a TSV file generated by analyze_shifts.py from a combinatorial acquisition.
From the shifts in this file, generate a calibration file for the Nikon C2 to
compensate for horizontal shift by determining a scan delay.

'''
from __future__ import division, print_function

import sys
# import matplotlib.pyplot as plt
import numpy as np
from scipy.optimize import curve_fit
import logging
import argparse
import csv
import collections

LOW_DWELL_TIME = 0.00000192
LEASTSQ_KWARGS = {
        "maxfev": 10000,
    }

MAX_DWELL_TIME = 0.00009472  # s
MAX_SHIFT = 50
PIXEL_SHIFT_THRESHOLD = 10  # pixels

logging.getLogger().setLevel(logging.DEBUG)


def arctan_func(x, a, b, c, d):
    return a * np.arctan(b * x) + c * x + d

def quad_func(x, a, b, c):
    return a * (x - b) ** 2 + c

def lin_func(x, a, b):
    return a * x + b

def load_data(filenames):
    """
    Load the TSV data generated by analyse_shifts.py into a collection
    """
    data = collections.defaultdict(dict)  # res -> zoom -> td > s

    for filename in filenames:
        with open(filename) as csvfile:
            reader = csv.DictReader(csvfile, delimiter='\t',)
            for row in reader:

                zoom = float(row['zoom'])
                res = int(row['res X'])
                td = float(row['dwell time (s)'])
                s = float(row['shift X (base px)'])

                try:
                    data[res][zoom][td] = s
                except KeyError:
                    if not res in data:
                        data[res] = {}
                    if not zoom in data[res]:
                        data[res][zoom] = {}
                    data[res][zoom][td] = s

    return data


def get_shift_calibration(data):
    """
    Given data loaded by "load_data" from analyse_shifts, calculate a calibration
    data structure from curve fitting.
    """
    calib = {}

    for res in data.keys():
        for zoom in data[res].keys():
            td = sorted(np.array(data[res][zoom].keys()))
            s = np.array([data[res][zoom][x] for x in td])
            try:
                popt, pcov = curve_fit(arctan_func, td, s)
            except (RuntimeError, TypeError, AttributeError) as e:
                popt = None
                logging.warning("Could not find calibration for res. %d, zoom %f", res, zoom)
                logging.warning(e)
                continue

            try:
                calib[res][zoom] = popt
            except KeyError:
                if not res in calib:
                    calib[res] = {}
                if not zoom in calib[res]:
                    calib[res][zoom] = {}
                calib[res][zoom] = popt

    return calib


def get_shift(res, zoom, td, calib):
    """
    Model function for getting a shift from a calibration, based on:
    res (int): Resolution
    zoom (float): Zoom
    td (float): dwell time
    calib (dict): multilevel dict of int -> float -> float -> [], which corresponds to
        resolution -> zoom -> dwell time -> [list of calibration constants]
    """

    if res not in calib.keys():
        raise ValueError("Unsupported resolution for shift compensation %d" % (res,))

    try:
        popt = calib[res][zoom]
        return arctan_func(td, *popt)
    except KeyError:
        # No zoom for this position. Interpolate.
        zooms = sorted(calib[res].keys())

        try:

            if zoom <= 2:
                z1 = [z for z in zooms if z <= 2]
                s_of_td1 = [arctan_func(td, *calib[res][z]) for z in z1]
                if len(z1) > 1:
                    popt, pcov = curve_fit(lin_func, z1, s_of_td1)
                    return lin_func(zoom, *popt)
                else:
                    raise RuntimeError("Not enough zoom data points under z=2.")

            if td > LOW_DWELL_TIME:
                if 2 < zoom < 20:
                    z2 = [z for z in zooms if 2 <= z <= 20]
                    s_of_td2 = [arctan_func(td, *calib[res][z]) for z in z2]
                    if len(z2) > 3:
                        popt, pcov = curve_fit(arctan_func, z2, s_of_td2)
                        return arctan_func(zoom, *popt)
                    else:
                        popt, pcov = curve_fit(lin_func, z2, s_of_td2)
                        return lin_func(zoom, *popt)
                elif zoom >= 20:
                    z3 = [z for z in zooms if z >= 20]
                    s_of_td3 = [arctan_func(td, *calib[res][z]) for z in z3]
                    if len(z3) > 3:
                        popt, pcov = curve_fit(quad_func, z3, s_of_td3)
                        return quad_func(zoom, *popt)
                    elif len(z3) >= 2:
                        popt, pcov = curve_fit(lin_func, z3, s_of_td3)
                        return lin_func(zoom, *popt)
                    else:
                        raise RuntimeError("Not enough zoom data points.")
            else:
                if 2 < zoom:
                    z4 = [z for z in zooms if 2 <= z]
                    s_of_td4 = [arctan_func(td, *calib[res][z]) for z in z4]
                    if len(z4) > 3:
                        popt, pcov = curve_fit(arctan_func, z4, s_of_td4)
                        return arctan_func(zoom, *popt)
                    else:
                        popt, pcov = curve_fit(lin_func, z4, s_of_td4)
                        return lin_func(zoom, *popt)
        except RuntimeError as e:
            logging.warning("Could not determine shift for res %d, zoom %f, dwelltime %f", res, zoom, td)
            logging.warning(e)
            return 0

        
def test_calibration(data, calib):
    """
    Determine if any reasonable combination of arguments fed into the
    calibration return outlandish shifts. If there are any, display warnings to the user.
    """

    invalid_arguments = []
    counter = 0
    
    for res in data.keys():
        for zoom in data[res].keys():
            for dt in data[res][zoom].keys():
                counter += 1
                shift = get_shift(res, zoom, dt, calib)
                actual_shift = data[res][zoom][dt]
                if abs((actual_shift - shift)) > PIXEL_SHIFT_THRESHOLD:
                    invalid_arguments.append((res, zoom, dt, shift, actual_shift))

    if len(invalid_arguments) > 0:
        for (res, zoom, dt, shift, actual_shift) in invalid_arguments:
            logging.warning("Bad value @ res %d, z %f, dt %f s. Actual shift was %f, calculated was %f",
                            res, zoom, dt, actual_shift, shift)

        pass_percent = len(invalid_arguments) / counter * 100.0

        logging.warning("%f %% (%d / %d) of tested points were outside of the pixel shift threshold.",
                        pass_percent, len(invalid_arguments), counter)
    else:
        logging.info("Calibration passed validation.")


def main(args):

    # arguments handling
    parser = argparse.ArgumentParser(description="Generate configuration dictionary for Nikon C2 shift correction. ")

    parser.add_argument(dest="filenames", nargs="+",
                        help="filenames of the TSV tables generated by analyze_shifts.py")
    options = parser.parse_args(args[1:])
    
    filenames = options.filenames

    data = load_data(filenames)
    calib = get_shift_calibration(data)
    logging.info("Testing calibration validity...")
    test_calibration(data, calib)
    
    # output the calibration in a YAML friendly format
    output = "{"

    for res in sorted(calib.keys()):
        output += (str(res) + ": {")
        for zoom in sorted(calib[res].keys()):
            output += (str(zoom) + ": [")
            for constants in calib[res][zoom]:
                output += (str(constants) + ", ")
            output += "], "
        output += "},"

    output += "}"
    print("Generated configuration:")
    print(output, "\n")

    return 0


if __name__ == '__main__':
    ret = main(sys.argv)
