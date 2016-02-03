#!/usr/bin/env python

import matplotlib as mpl
mpl.use('AGG')
import matplotlib.pyplot as plt
import simplekml as kml
from mpl_toolkits.basemap import Basemap
import numpy as np
import netCDF4 as nc4
import sys, os, StringIO, utils, json, logging
import os.path as osp

from var_wisdom import convert_value, get_wisdom
from wrf_raster import make_colorbar, basemap_raster_mercator, basemap_barbs_mercator


class PostprocError(Exception):
    pass


class Postprocessor(object):
    """
    Postprocessing of WRF data.
    """

    def __init__(self, output_path, prod_name):
        """
        Initialize postprocessor with output parameters.

        :param output_path: path where postprocessing files are stored
        :param prod_name: name of manifest json file and prefix of all output files
        """
        self.output_path = output_path
        self.product_name = prod_name
        self.manifest = {}

    def _scalar2raster(self, d, var, tndx):
        """
        Convert a single variable into a raster and colorbar.

        :param d: the netcdf file
        :param var: the variable name
        :param tndx: the time index
        :return: two StringIO objects, first is the PNG raster, second is PNG colorbar
        """
        # gather wisdom about the variable
        wisdom = get_wisdom(var)
        native_unit = wisdom['native_unit']
        cmap_name = wisdom['colormap']
        cmap = mpl.cm.get_cmap(cmap_name)
        cb_unit = wisdom['colorbar_units'][0]

        # extract variable
        fa = wisdom['retrieve_as'](d,tndx) # this calls a lambda defined to read the required 2d field
        lat, lon = wisdom['grid'](d)

        if lat.shape != fa.shape:
            raise PostprocError("Variable %s size does not correspond to grid size." % var)

        # look at mins and maxes
        fa_min,fa_max = np.nanmin(fa),np.nanmax(fa)

        # determine if we will use the range in the variable or a fixed range
        scale = wisdom['scale']
        if scale != 'original':
            fa_min, fa_max = scale[0], scale[1]
            fa[fa < fa_min] = fa_min
            fa[fa > fa_max] = fa_max

        cbu_min,cbu_max = convert_value(native_unit, cb_unit, fa_min), convert_value(native_unit, cb_unit, fa_max)

        #  colorbar + add it to the KMZ as a screen overlay
        cb_png_data = make_colorbar([cbu_min, cbu_max],'vertical',2,cmap,wisdom['name'] + ' ' + cb_unit,var)

        # create the raster & get coordinate bounds
        raster_png_data,corner_coords = basemap_raster_mercator(lon,lat,fa,fa_min,fa_max,cmap)

        return raster_png_data, corner_coords, cb_png_data


    def _vector2raster(self, d, var, tndx):
        """
        Postprocess a vector field into barbs (used for wind) and contains hacks for WRF staggered grids.

        :param d: the open netCDF file
        :param var: the name of the variable in var_wisdom
        :param tndx: the time index
        :return: the raster png as a StringIO, and the coordinates
        """
        # gather wisdom about the variable
        wisdom = get_wisdom(var)
        native_unit = wisdom['native_unit']
        u_name, v_name = wisdom['components']
        lat, lon = wisdom['grid'](d)

        # extract variable
        uw, vw = get_wisdom(u_name), get_wisdom(v_name)
        u = uw['retrieve_as'](d, tndx)
        v = vw['retrieve_as'](d, tndx)

        if u.shape != lat.shape:
            raise PostprocError("Variable %s size does not correspond to grid size: var %s grid %s." % (u_name, u.shape, u_lat.shape))
        
        if v.shape != lat.shape:
            raise PostprocError("Variable %s size does not correspond to grid size." % v_name)

        # look at mins and maxes
        fa_min,fa_max = min(np.nanmin(u), np.nanmin(v)),max(np.nanmax(u), np.nanmax(v))

        # determine if we will use the range in the variable or a fixed range
        scale = wisdom['scale']
        if scale != 'original':
            fa_min, fa_max = scale[0], scale[1]
            u[u < fa_min] = fa_min
            u[u > fa_max] = fa_max
            v[v < fa_min] = fa_min
            v[v > fa_max] = fa_max

        # create the raster & get coordinate bounds, HACK to get better quiver resolution
        s = 3
        raster_png_data,corner_coords = basemap_barbs_mercator(u[::s,::s],v[::s,::s],lat[::s,::s],lon[::s,::s])

        return raster_png_data, corner_coords


    def _scalar2png(self, d, var, tndx, out_path):
        """
        Postprocess a scalar field into a raster file and a colorbar file, both PNG.

        :param d: the open netCDF file
        :param var: the variable name
        :param tndx: the temporal index of the grid to store
        :param out_path: the path to the KMZ output
        :return: the path to the raster, to the colorbar and the bounding coordinates
        """
        # render the raster & colorbar
        raster_png_data, corner_coords, cb_png_data = self._scalar2raster(d, var, tndx)

        # write raster file
        raster_path = out_path + "-raster.png"
        with open(raster_path, 'w') as f:
            f.write(raster_png_data)

        # write colorbar file
        colorbar_path = out_path + "-cb.png"
        with open(colorbar_path, "w") as f:
            f.write(cb_png_data)

        return raster_path, colorbar_path, corner_coords


    def _vector2png(self, d, var, tndx, out_path):
        """
        Postprocess a single vector variable ``var`` and stores result in a raster file.

        :param d: the open netCDF file
        :param var: the variable name
        :param tndx: the temporal index of the grid to store
        :param out_path: the path to the KMZ output
        :return: the path to the raster and the bounding coordinates
        """
        # render the raster & colorbar
        raster_png_data, corner_coords = self._vector2raster(d, var, tndx)

        raster_path = out_path + '-raster.png'
        with open(raster_path, 'w') as f:
            f.write(raster_png_data)

        return raster_path, corner_coords


    def _scalar2kmz(self, d, var, tndx, out_path, cleanup = True):
        """
        Postprocess a single raster variable ``fa`` and store result in out_path.

        :param d: the open netCDF file
        :param var: the variable name
        :param tndx: the temporal index of the grid to store
        :param out_path: the path to the KMZ output
        :param cleanup: if true, delete png files
        :return: the path to the generated KMZ
        """
        # construct kml file
        doc = kml.Kml(name = var)

        # generate the png files
        raster_path, cb_path, corner_coords = self._scalar2png(d, var, tndx, out_path)

        # add colorbar to KMZ
        cbo = doc.newscreenoverlay(name='colorbar')
        cbo.overlayxy = kml.OverlayXY(x=0,y=1,xunits=kml.Units.fraction,yunits=kml.Units.fraction)
        cbo.screenxy = kml.ScreenXY(x=0.02,y=0.95,xunits=kml.Units.fraction,yunits=kml.Units.fraction)
        cbo.size = kml.Size(x=150,y=300,xunits=kml.Units.pixel,yunits=kml.Units.pixel)
        cbo.color = kml.Color.rgb(255,255,255,a=150)
        cbo.visibility = 1
        doc.addfile(cb_path)
        cbo.icon.href=cb_path

        # add ground overlay
        ground = doc.newgroundoverlay(name=var,color='80ffffff')
        ground.gxlatlonquad.coords = corner_coords
        doc.addfile(raster_path)
        ground.icon.href = raster_path

        # build output file
        kmz_path = out_path + ".kmz"
        doc.savekmz(kmz_path)

        # cleanup
        if cleanup:
            os.remove(cb_name)
            os.remove(raster_name)
        
        return kmz_path, raster_path, cb_path, corner_coords


    def _vector2kmz(self, d, var, tndx, out_path, cleanup = True):
        """
        Postprocess a single vector variable ``var`` and store result in out_path.

        :param d: the open netCDF file
        :param var: the variable name
        :param tndx: the temporal index of the grid to store
        :param out_path: the path to the KMZ output
        :return: the path to the generated KMZ
        """
        # construct kml file
        doc = kml.Kml(name = var)

        # generate the png files
        raster_path, corner_coords = self._vector2png(d, var, tndx, out_path)

        # add ground overlay
        ground = doc.newgroundoverlay(name=var,color='80ffffff')
        ground.gxlatlonquad.coords = corner_coords
        doc.addfile(raster_path)
        ground.icon.href = raster_path

        # build output file
        kmz_path = out_path + ".kmz"
        doc.savekmz(kmz_path)

        # cleanup
        if cleanup:
            os.remove(raster_name)
        
        return kmz_path, raster_path, corner_coords


    def vars2kmz(self, wrfout_path, dom_id, ts_esmf, vars):
        """
        Postprocess a list of scalar fields at a given simulation time into KMZ files.

        :param wrfout_path: WRF file to process
        :param dom_id: the domain identifier
        :param ts_esmf: time stamp in ESMF format
        :param vars: list of variables to process
        """
        # open the netCDF dataset
        d = nc4.Dataset(wrfout_path)

        # extract ESMF string times and identify timestamp of interest
        times = [''.join(x) for x in d.variables['Times'][:]]
        if ts_esmf not in times:
            raise PostprocError("Invalid timestamp %s" % ts_esmf)
        tndx = times.index(ts_esmf)

        # build one KMZ per variable
        for var in vars:
            try:
                outpath_base = os.path.join(self.output_path, self.product_name + ("-%02d-" % dom_id) + ts_esmf + "-" + var) 
                kmz_path = None
                if var in ['WINDVEC']:
                    kmz_path,_,_,_= self._vector2kmz(d, var, tndx, outpath_base)
                else:
                    kmz_path,_,_,_ = self._scalar2kmz(d, var, tndx, outpath_base)
                kmz_name = osp.basename(kmz_path)
                self._update_manifest(ts_esmf, var, { 'kml' : kmz_name })
            except Exception as e:
                logging.warning("Exception %s while postprocessing %s for time %s" % (e.message, var, ts_esmf))

    
    def vars2png(self, wrfout_path, dom_id, ts_esmf, vars):
        """
        Postprocess a list of scalar fields into KMZ files.

        :param wrfout_path: WRF file to process
        :param dom_id: the domain identifier
        :param ts_esmf: time stamp in ESMF format
        :param vars: list of variables to process
        """
        # open the netCDF dataset
        d = nc4.Dataset(wrfout_path)

        # extract ESMF string times and identify timestamp of interest
        times = [''.join(x) for x in d.variables['Times'][:]]
        if ts_esmf not in times:
            raise PostprocError("Invalid timestamp %s" % ts_esmf)
        tndx = times.index(ts_esmf)

        # build one KMZ per variable
        for var in vars:
            try:
                outpath_base = os.path.join(self.output_path, self.product_name + ("-%02d-" % dom_id) + ts_esmf + "-" + var) 
                if var in ['WINDVEC']:
                    raster_path, coords = self._vector2png(d, var, tndx, outpath_base)
                    raster_name = osp.basename(raster_path)
                    self._update_manifest(ts_esmf, var, { 'raster' : raster_name, 'coords' : coords})
                else:
                    raster_path, cb_path, coords = self._scalar2png(d, var, tndx, outpath_base)
                    raster_name, cb_name = osp.basename(raster_path), osp.basename(cb_path)
                    self._update_manifest(ts_esmf, var, { 'raster' : raster_name, 'colorbar' : cb_name, 'coords' : coords})
            except Exception as e:
                logging.warning("Exception %s while postprocessing %s for time %s" % (e.message, var, ts_esmf))


    def process_file(self, wrfout_path, var_list):
        """
        Process an entire file, all timestamps.

        :param wrfout_path: the wrfout to process
        :param var_list: list of variables to process
        """
        # open the netCDF dataset
        d = nc4.Dataset(wrfout_path)

        # extract ESMF string times and identify timestamp of interest
        times = [''.join(x) for x in d.variables['Times'][:]]

        # build one KMZ per variable
        for tndx, ts_esmf in enumerate(times):
            print('Processing time %s ...' % ts_esmf)
            for var in var_list:
                try:
                    outpath_base = os.path.join(self.output_path, self.product_name + '-' + ts_esmf + '-' + var) 
                    if var in ['WINDVEC']:
                        kmz_path,raster_path,coords = self._vector2kmz(d, var, tndx, outpath_base, cleanup=False)
                        raster_name = osp.basename(raster_path)
                        kmz_name = osp.basename(kmz_path)
                        self._update_manifest(ts_esmf, var, { 'raster' : raster_name, 'coords' : coords, 'kml' : kmz_name})
                    else:
                        kmz_path,raster_path,cb_path,coords = self._scalar2kmz(d, var, tndx, outpath_base, cleanup=False)
                        raster_name, cb_name = osp.basename(raster_path), osp.basename(cb_path)
                        kmz_name = osp.basename(kmz_path)
                        self._update_manifest(ts_esmf, var, { 'raster' : raster_name, 'colorbar' : cb_name, 'coords' : coords, 'kml' : kmz_name })
                except Exception as e:
                    logging.warning("Exception %s while postprocessing %s for time %s" % (e.message, var, ts_esmf))




    def _update_manifest(self,ts_esmf, var, kv):
        """
        Adds a key-value set to the dictionary storing metadata for time ts_esmf and variable var.

        :param ts_esmf: ESMF time string 
        :param var: variable name
        :param kv: key-value dictionary to merge
        """
        # update the manifest with the ts_esmf/var info
        td = self.manifest.get(ts_esmf, {})
        self.manifest[ts_esmf] = td
        vd = td.get(var, {})
        td[var] = vd
        vd.update(kv)
        mf_path = os.path.join(self.output_path, self.product_name + '.json')
        json.dump(self.manifest, open(mf_path, 'w'))




if __name__ == '__main__':
    
    if len(sys.argv) != 4:
        print('usage: %s <wrfout_path> <var_list> <prefix>' % sys.argv[0])
        sys.exit(1)

    wrf_path = sys.argv[1]
    var_list = sys.argv[2].split(',')
    prefix = sys.argv[3]

    p = Postprocessor(os.path.dirname(prefix), os.path.basename(prefix))
    p.process_file(wrf_path, var_list)




