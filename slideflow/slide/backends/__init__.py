"""Abstraction to support both Libvips and cuCIM backends."""

import slideflow as sf


def tile_worker(*args, **kwargs):
    if sf.slide_backend() == 'libvips':
        from .vips import tile_worker
    elif sf.slide_backend() == 'cucim':
        from .cucim import tile_worker
    return tile_worker(*args, **kwargs)


def wsi_reader(path: str, *args, **kwargs):
    """Get a slide image reader from the current backend."""
    if sf.slide_backend() == 'libvips':
        from .vips import get_libvips_reader
        return get_libvips_reader(path, *args, **kwargs)

    elif sf.slide_backend() == 'cucim':
        from .cucim import get_cucim_reader
        return get_cucim_reader(path, *args, **kwargs)
