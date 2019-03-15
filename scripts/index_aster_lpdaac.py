#!/usr/bin/env python
"""
This program allows indexing the Australia region ASTER (Advanced Spaceborne Thermal
Emission and Reflection Radiometer)  L1T data stored on the NCI into an ODC Database.

ASTER data consists of visible and near infrared (VNIR) frequencies
with three bands at 15-meter resolution, short-wave infrared (SWIR)
frequencies with six bands at 30-meter resolution, and  thermal infrared (TIR)
wavelength with five bands at 90-meter resolution.

Further details of AST_L1T data is available from
https://lpdaac.usgs.gov/dataset_discovery/aster/aster_products_table/ast_l1t_v003

The ASTER L1T data product is derived from ASTER Level 1A data that has been
geometrically corrected and reprojected to a north-up Universal Transverse Mercator (UTM)
projection.
(Please see: https://lpdaac.usgs.gov/sites/default/files/public/elearning/ASTER_L1T_Tutorial.html)

Further, depending on whether the following modes are enabled, dataset may present
different bands:
  ASTEROBSERVATIONMODE.1=VNIR1, ON/OFF
  ASTEROBSERVATIONMODE.2=VNIR2, ON/OFF
  ASTEROBSERVATIONMODE.3=SWIR, ON/OFF
  ASTEROBSERVATIONMODE.4=TIR, ON/OFF

It runs in two modes, one to create the product definition in the database,
 and the second to record
dataset details. Both modes need to be pointed at a directory of ASTER_L1T data
stored in hdf format.

The data is stored in sets of hdf files
in `/g/data/v10/ASTER_AU/`.

The script  can be run in either with either a `create_product`
or `index_data` parameter mode, and an output directory of hdf files.
 It reads the hdf files to create the Product/Dataset
definitions, and write them directly into an ODC database.

It doesn't write out intermediate YAML files, and attempts to create
stable UUIDs for the generated Datasets, based on the file path
and modification time of the underlying NetCDF?? Data.

::

    ./index_nci_aster_lpdaac.py create_product /g/data/v10/ASTER_AU/2018.01.01
    ./index_nci_aster_lpdaac.py index_data /g/data/v10/ASTER_AU/2018.01.01

::

    psql -h agdcdev-db.nci.org.au
    CREATE DATABASE aster_lpdaac WITH OWNER agdc_admin;
    GRANT TEMPORARY, CONNECT ON DATABASE aster_lpdaac to public;

aster_lpdaac.conf::

    [datacube]
    db_hostname: agdcdev-db.nci.org.au
    db_port: 6432
    db_database: aster_lpdaac

::

    datacube --config aster_lpdaac.conf system init

::

    for i in /g/data/v10/ASTER_AU/*; do
        ./index_nci_aster_lpdaac.py --config aster_lpdacc.conf index-data $i
    done

"""
import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import click
import numpy as np
from osgeo import gdal, osr
import rasterio

from datacube import Datacube
from datacube.index.hl import Doc2Dataset
from datacube.utils.geometry import CRS, box


LOG = logging.getLogger(__name__)

PRODUCTS = {'vnir': {'ImageData2', 'ImageData1', 'ImageData3N'},
            'tir': {'ImageData10', 'ImageData11', 'ImageData12', 'ImageData13', 'ImageData14'}}


@click.group(help=__doc__)
@click.option('--config', '-c', help="Pass the configuration file to access the database",
              type=click.Path(exists=True))
@click.pass_context
def cli(ctx, config):
    """ Used to pass the datacube index to functions via click."""
    ctx.obj = Datacube(config=config).index


@cli.command()
@click.argument('path')
@click.option('--product', help='Which ASTER product? vnir, swir, or tir')
def create_vrt(path, product):

    file_paths = find_lpdaac_file_paths(Path(path))
    print(file_paths)

    for file_path in file_paths:
        vrt = generate_vrt(file_path, PRODUCTS[product])
        with open(file_path.with_name(f'{file_path.stem}_{product}.vrt'), 'w') as fd:
            fd.write(vrt)


@cli.command()
@click.argument('path')
@click.option('--product', help='Which ASTER product? vnir, swir, or tir')
@click.pass_obj
def show(index, path, product):

    file_paths = find_lpdaac_file_paths(Path(path))
    print(file_paths)

    _ = Doc2Dataset(index)
    for file_path in file_paths:
        doc = generate_lpdaac_doc(file_path, PRODUCTS[product])
        print_dict(doc)


@cli.command()
@click.argument('path')
@click.option('--product', help='Which ASTER product? vnir, swir, or tir')
@click.pass_obj
def create_product(index, path, product):
    file_paths = find_lpdaac_file_paths(Path(path))
    print(file_paths)
    measurements = raster_to_measurements(file_paths[0], PRODUCTS[product])
    for measure in measurements:
        measure.pop('path')  # This is not needed here
    print_dict(measurements)
    product_def = generate_lpdaac_defn(measurements)
    print_dict(product_def)

    print(index)
    product = index.products.from_doc(product_def)
    print(product)
    # indexed_product = index.products.add(product)
    # print(indexed_product)


@cli.command()
@click.argument('path')
@click.option('--product', help='Which ASTER product? vnir, swir, or tir')
@click.pass_obj
def index_data(index, path, product):
    file_paths = find_lpdaac_file_paths(Path(path))
    print(file_paths)

    resolver = Doc2Dataset(index)
    for file_path in file_paths:
        doc = generate_lpdaac_doc(file_path, PRODUCTS[product])
        print_dict(doc)
        dataset, err = resolver(doc, file_path.as_uri())

        if err is not None:
            logging.error("%s", err)
        try:
            index.datasets.add(dataset)
        except Exception as e:
            logging.error("Couldn't index %s", file_path)
            logging.exception("Exception", e)


def print_dict(doc):
    print(json.dumps(doc, indent=4, sort_keys=True, cls=NumpySafeEncoder))


def find_lpdaac_file_paths(path: Path):
    """
    Return a list of hdf file path objects.

    :param path:
    :return: A list of path objects.
    """
    file_paths = []
    for afile in path.iterdir():
        if afile.suffix == '.hdf' and afile.stem[:7] == 'AST_L1T':
            file_paths.append(afile)
    return file_paths


def raster_to_measurements(file_path, band_suffixes):

    measurements = []
    for index, band in enumerate(selected_bands(file_path, band_suffixes)):
        measure = dict(name=band[0].split(':')[-1])
        measure['path'] = str(file_path.with_suffix('.vrt'))

        with rasterio.open(band[0]) as band_:
            measure['dtype'] = str(band_.dtypes[0])
            measure['nodata'] = float(band_.nodatavals[0] or 0)
            measure['units'] = str(band_.units[0])
        measurements.append(measure)
    return measurements


def selected_bands(file_path, band_suffixes):

    ds = gdal.Open(str(file_path), gdal.GA_ReadOnly)
    sub_datasets = ds.GetSubDatasets()
    # Check the last field of the band name: something like 'ImageDataXX'

    return tuple(band for band in sub_datasets if band[0].split(':')[-1] in band_suffixes)


def generate_lpdaac_defn(measurements):
    return {
        'name': 'ASTER_L1T',
        'metadata_type': 'eo',
        'metadata': {
            'product_type': 'aster_lpdaac_l1t',
            'platform': {'code': 'ASTER'},
            'version': 1,
            'coverage': 'aust'
        },
        'description': 'ASTER L1T - Precision Terrain Corrected Registered At-Sensor Radiance data',
        'measurements': measurements
    }


def generate_lpdaac_doc(file_path, band_suffixes):

    modification_time = file_path.stat().st_mtime

    unique_ds_uri = f'{file_path.as_uri()}#{modification_time}'

    left, bottom, right, top = compute_extents(file_path)
    spatial_ref = infer_aster_srs(file_path)
    geo_ref_points = {
        'ul': {'x': left, 'y': top},
        'ur': {'x': right, 'y': top},
        'll': {'x': left, 'y': bottom},
        'lr': {'x': right, 'y': bottom},
    }

    acquisition_time = get_acquisition_time(file_path)
    measurements = raster_to_measurements(file_path, band_suffixes)
    the_format = 'HDF4_EOS:EOS_GRID'

    doc = {
        'id': str(uuid.uuid5(uuid.NAMESPACE_URL, unique_ds_uri)),
        'product_type': 'modis_lpdaac_MYD13Q1',
        'creation_dt': str(datetime.fromtimestamp(modification_time)),
        'platform': {'code': 'MODIS'},
        'extent': {
            'from_dt': str(acquisition_time),
            'to_dt': str(acquisition_time),
            'coord': geo_ref_points
        },
        'format': {'name': the_format},
        'grid_spatial': {
            'projection': {
                'geo_ref_points': geo_ref_points,
                'spatial_reference': spatial_ref,
            }
        },
        'image': {
            'bands': {
                measure['name']: {
                    'path': measure['path'],
                    'layer': index + 1,
                } for index, measure in enumerate(measurements)
            }
        },
        'version': 1,
        'coverage': 'aust',
        'lineage': {'source_datasets': {}}
    }
    return doc


def infer_aster_srs(file_path: Path):
    """
    Compute SRS based on metadata (UTMZONENUMBER and NORTHBOUNDINGCOORDINATE) in the file and
    generic osr.SpatialReference data.
    Reference:
    https://git.earthdata.nasa.gov/projects/LPDUR/repos/aster-l1t/raw/ASTERL1T_hdf2tif.py?at=refs%2Fheads%2Fmaster
    """

    ds = gdal.Open(str(file_path), gdal.GA_ReadOnly)
    meta = ds.GetMetadata()

    # Define UL, LR, UTM zone
    utm = np.int(meta['UTMZONENUMBER'])
    n_s = np.float(meta['NORTHBOUNDINGCOORDINATE'])

    # Create UTM zone code numbers
    utm_n = [i + 32600 for i in range(60)]
    utm_s = [i + 32700 for i in range(60)]

    # Define UTM zone based on North or South
    if n_s < 0:
        utm_zone = utm_s[utm]
    else:
        utm_zone = utm_n[utm]

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(utm_zone)

    return srs.ExportToWkt()


def generate_vrt(file_path: Path, band_suffixes):
    """
    Generate a VRT file for a given file
    The following tags did not show visual impact on raster bands when rendering:
        1. Top level GeoTransform
    """

    bands = selected_bands(file_path, band_suffixes)
    x_size, y_size = get_raster_sizes(bands)

    return """\
    <VRTDataset rasterXSize="{x}" rasterYSize="{y}">
        <SRS>{srs}</SRS>
        <GeoTransform>{geo}</GeoTransform>
        {raster_bands}
    </VRTDataset>
    """.format(x=x_size, y=y_size, srs=infer_aster_srs(file_path), geo='0, 1, 0, 0, 0, 1',
               raster_bands=get_raster_bands_vrt(bands))


def get_raster_bands_vrt(bands):
    """
    Compute the <VRTRasterBand> tags for each band ang return them as a single string

    :param bands: GDAL SubDatasets
    """

    raster_band_template = """\
    <VRTRasterBand dataType="{dtype}" band="{number}">
        <NoDataValue>{nodata}</NoDataValue>
        <ComplexSource>
            <SourceFilename relativeToVRT="1">{band_name}</SourceFilename>
        </ComplexSource>
    </VRTRasterBand>
    """

    raster_bands = ''
    for index, band in enumerate(bands):
        sdt = gdal.Open(band[0], gdal.GA_ReadOnly)
        data_type = gdal.GetDataTypeName(sdt.GetRasterBand(1).DataType)
        raster_bands += raster_band_template.format(dtype=data_type, number=index + 1,
                                                    nodata=0, band_name=band[0])
    return raster_bands


def get_raster_sizes(bands):
    """
    Raster sizes of different bands are different. So compute the max of x axis
    and max of y axis

    :param bands: GDAL SubDatasets
    """

    x_size = []
    y_size = []
    for band in bands:
        sdt = gdal.Open(band[0], gdal.GA_ReadOnly)
        x_size.append(sdt.RasterXSize)
        y_size.append(sdt.RasterYSize)
    return max(x_size), max(y_size)


def get_acquisition_time(file_path):

    dt = gdal.Open(str(file_path), gdal.GA_ReadOnly)
    meta = dt.GetMetadata()
    date_string = meta['CALENDARDATE']
    # ToDo: Probably more to do here for time of day
    return datetime(year=int(date_string[:4]), month=int(date_string[4:6]), day=int(date_string[6:8]))


def compute_extents(file_path):
    """
    Compute the union of extents of individual raster bands.
    https://git.earthdata.nasa.gov/projects/LPDUR/repos/aster-l1t/raw/ASTERL1T_hdf2tif.py?at=refs%2Fheads%2Fmaster
    """
    dt = gdal.Open(str(file_path), gdal.GA_ReadOnly)
    meta = dt.GetMetadata()

    # Define LL, UR
    ll = [np.float(x) for x in meta['LOWERLEFTM'].split(', ')]
    ur = [np.float(x) for x in meta['UPPERRIGHTM'].split(', ')]
    n_s = np.float(meta['NORTHBOUNDINGCOORDINATE'])
    # Define extent and provide offset for UTM South zones
    if n_s < 0:
        ll_y = ll[0] + 10000000
        ll_x = ll[1]

        ur_y = ur[0] + 10000000
        ur_x = ur[1]

    # Define extent for UTM North zones
    else:
        ll_y = ll[0]
        ll_x = ll[1]

        ur_y = ur[0]
        ur_x = ur[1]

    # Do we need to offset pixel center by half of pixel resolution as in the above reference?
    # Note: pixel resolution vary per band

    return ll_x, ll_y, ur_x, ur_y


class NumpySafeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        else:
            return super(NumpySafeEncoder, self).default(obj)


if __name__ == '__main__':
    cli()