# coding=utf-8

import numpy


def simple_object_repr(o):
    """
    Calculate a possible repr() for the given object using the class name and all __dict__ properties.

    eg. MyClass(prop1='val1')

    It will call repr() on property values too, so beware of circular dependencies.
    """
    return "%s(%s)" % (
        o.__class__.__name__,
        ", ".join("%s=%r" % (k, v) for k, v in sorted(o.__dict__.items()))
    )


def wofs_fuser(dest, src):
    """
    Fuse two WOfS water measurements represented as `ndarray`s.
    
    This fuser is intended for de-duplication of WOfS observations
    (where a single observation pass is represented in overlapping scenes). 
    It is not advised for compositing of multiple independent observations.
    
    The first bit in the WOfS bitfield indicates a lack of data,
    i.e. pixels that are outside of the valid area for that layer.
    For example, in regions of non-overlap, at least one layer will have nodata set.
    All other bits are meaningless, and the layer is expected to be disregarded,
    where nodata is set.
    
    The following 6 (for some contexts arguably 7) bits indicate various positive reasons
    for imputing an observation. (Most downstream applications will discard imputed data,
    but applications can differ in which bits/reasons they consider/ignore when doing so.)
    The fuser conservatively flags the output pixel if either layer validly found a
    positive issue with it. For example a shadow may be cast on a near-boundary pixel by 
    something (e.g. cloud) that is outside of the extent of one scene but is able to be 
    detected in an overlapping scene.
    """
    empty = (dest & 1).astype(numpy.bool)
    both = ~empty & ~((src & 1).astype(numpy.bool))
    dest[empty] = src[empty]
    dest[both] |= src[both]
