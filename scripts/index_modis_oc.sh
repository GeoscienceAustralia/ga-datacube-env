#!/usr/bin/env bash

# The argument is the config file location
# e.g.
# index_modis_oc.sh ../../../modis_oc.conf
#
# Before running this script do a create-product;
# ./index_nci_modis_oc.py create-product --config ../../../modis_oc.conf [PATH]
# e.g. [PATH]= /g/data2/u39/public/data/modis/oc-1d-aust.v201508.recent/2016/12

for j in /g/data2/u39/public/data/modis/oc-1d-aust.v201508.recent/*; do
    for i in $j/*; do
        ./index_nci_modis_oc.py --config "$1" index-data "$i"
    done
done


for j in /g/data2/u39/public/data/modis/oc-1d-aust.v201508.past/*; do
    for i in $j/*; do
        ./index_nci_modis_oc.py --config "$1" index-data "$i"
    done
done
