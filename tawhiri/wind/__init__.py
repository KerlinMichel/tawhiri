import logging
from collections import namedtuple
import os
import os.path
from datetime import datetime
import numpy as np
import pygrib

logger = logging.getLogger("tawhiri.wind")


class Dataset(object):
    shape = (65, 47, 3, 361, 720)

    # TODO: use the other levels too?
    # {10, 80, 100}m heightAboveGround (u, v)
    #       -- note ground, not mean sea level - would need elevation
    # 0 unknown "planetary boundry layer" (u, v) (first two records)
    # 0 surface "Planetary boundary layer height"
    # {1829, 2743, 3658} heightAboveSea (u, v)
    pressures_pgrb2f = [10, 20, 30, 50, 70, 100, 150, 200, 250, 300, 350, 400,
                        450, 500, 550, 600, 650, 700, 750, 800, 850, 900, 925,
                        950, 975, 1000]
    pressures_pgrb2bf = [1, 2, 3, 5, 7, 125, 175, 225, 275, 325, 375, 425,
                         475, 525, 575, 625, 675, 725, 775, 825, 875]

    _axes_type = namedtuple("axes",
                ("hour", "pressure", "variable", "latitude", "longitude"))

    axes = _axes_type(
        range(0, 192 + 3, 3),
        sorted(pressures_pgrb2f + pressures_pgrb2bf, reverse=True),
        ["height", "wind_u", "wind_v"],
        np.arange(-90, 90 + 0.5, 0.5),
        np.arange(0, 360, 0.5)
    )

    _listdir_type = namedtuple("dataset_in_row",
                ("ds_time", "suffix", "filename", "path"))

    assert shape == tuple(len(x) for x in axes)

    SUFFIX_GRIBMIRROR = '.gribmirror'

    @classmethod
    def filename(cls, directory, ds_time, suffix=''):
        ds_time_str = ds_time.strftime("%Y%m%d%H")
        return os.path.join(directory, ds_time_str + suffix)

    @classmethod
    def listdir(cls, directory, only_suffices=None):
        for filename in os.listdir(directory):
            if len(filename) < 10:
                continue

            ds_time_str = filename[:10]
            try:
                ds_time = datetime.strptime(ds_time_str, "%Y%m%d%H")
            except ValueError:
                pass
            else:
                suffix = filename[10:]
                if only_suffices and suffix not in only_suffices:
                    continue

                yield cls._listdir_type(ds_time, suffix, filename,
                                        os.path.join(directory, filename))

    @classmethod
    def checklist(cls):
        return np.zeros(cls.shape[0:3], dtype=np.bool_)

    def __init__(self, directory, ds_time, suffix='', new=False):
        self.directory = directory
        self.ds_time = ds_time
        self.new = new

        self.fn = self.filename(self.directory, self.ds_time, suffix)

        logger.info("Opening dataset %s %s %s", self.ds_time, self.fn,
                        '(truncate and write)' if new else '(read)')

        self.array = np.memmap(self.fn, mode=('w+' if self.new else 'r'),
                               dtype=np.float64, shape=self.shape, order='C')

    def __del__(self):
        self.close()

    def close(self):
        if hasattr(self, 'array'):
            logger.info("Closing dataset %s %s", self.ds_time, self.fn)
            del self.array


_grib_name_to_variable = {"Geopotential Height": "height",
                          "U component of wind": "wind_u",
                          "V component of wind": "wind_v"}

def unpack_grib(filename, dataset=None, checklist=None, gribmirror=None,
                assert_hour=None, file_checklist=None, callback=None):
    # docstring needs warning about modifying dataset and then returning
    # ValueError, see DatasetDownloader's comments on self._checklist
    # in __init__

    assert Dataset.axes._fields[0:3] == ("hour", "pressure", "variable")
    if dataset is not None:
        assert dataset.axes == Dataset.axes
        assert dataset.shape == Dataset.shape

    if file_checklist is not None:
        file_checklist = file_checklist.copy()

    checklist_temp = Dataset.checklist()
    checked_axes = False

    for record, location, location_name in _grib_records(filename):
        _check_record(record, location, location_name, checklist_temp,
                      checklist, assert_hour, file_checklist)

        # Checking axes (for some reason) is really slow, so do it once as
        # a small sanity check, and hope that if it's OK for one record,
        # the file is good.
        if not checked_axes:
            _check_axes(record)
            checked_axes = True

        if dataset is not None:
            dataset.array[location] = record.values
        if gribmirror is not None:
            gribmirror.write(record.tostring())

        logger.debug("unpacked %s %s %s", filename, location_name, location)

        # don't update the main checklist until we finish the file and
        # therefore know that we won't be raising a ValueError
        checklist_temp[location] = True

        if file_checklist is not None:
            file_checklist.remove(location_name)
        if callback is not None:
            callback(location, location_name)

    if file_checklist != set():
        raise ValueError("records missing from file")

    # callback may yield to another greenlet, so could race:
    if (checklist & checklist_temp).any():
        raise ValueError("records already unpacked (checklist race)")
    # numpy overloads in-place-or which will update the referenced array
    # rather than only modifying the local variable
    checklist |= checklist_temp

    logger.info("unpacked %s", filename)

def _grib_records(filename):
    grib = pygrib.open(filename)
    try:
        for record in grib:
            if record.typeOfLevel != "isobaricInhPa":
                continue
            if record.name not in _grib_name_to_variable:
                continue

            location_name = (record.forecastTime, record.level,
                             _grib_name_to_variable[record.name])

            location = tuple(Dataset.axes[i].index(n)
                             for i, n in enumerate(location_name))

            yield record, location, location_name
    finally:
        grib.close()

def _check_record(record, location, location_name, checklist_temp,
                  checklist, assert_hour, file_checklist):
    if checklist_temp[location]:
        raise ValueError("repeated in file: {0}".format(location_name))
    if checklist is not None and checklist[location]:
        raise ValueError("record already unpacked (from other file): {0}"
                            .format(location_name))
    if assert_hour is not None and record.forecastTime != assert_hour:
        raise ValueError("Incorrect forecastTime (assert_hour)")
    if file_checklist is not None and location_name not in file_checklist:
        raise ValueError("unexpected record: {0}".format(location_name))

def _check_axes(record):
    # I'm unsure whether this is the correct thing to do.
    # Some GRIB functions (.latitudes, .latLonValues) have the
    # latitudes scanning negatively (90 to -90); but .values and
    # .distinctLatitudes seem to return a grid scanning positively
    # If it works...
    if not np.array_equal(record.distinctLatitudes,
                          Dataset.axes.latitude):
        raise ValueError("unexpected axes on record (latitudes)")
    if not np.array_equal(record.distinctLongitudes,
                          Dataset.axes.longitude):
        raise ValueError("unexpected axes on record (longitudes)")
