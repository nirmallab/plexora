# Every supported format, every version of it: what opens, and what was fixed (2026-11-11)

Asked: after the `1_750` case (an OME-TIFF whose pyramid was extra pages of the main chain, not
SubIFDs, which took minutes to open on the website), check **every** format Plexora reads, local and
website, against the versions of it written over the last twenty years, and make each open promptly.

## How it was checked

- **What Plexora reads** was taken from the code (three read-only traces: TIFF family; OME-Zarr,
  Imaris, DICOM, OpenSlide, PNG/JPEG; run folders and tables), not from the docs page.
- **What older writers produced** was researched per format (schema and release histories, OpenSlide's
  format notes, vendor release notes) and, wherever a public sample exists, checked on the file itself.
- **A corpus of 100 samples**, one per layout:
  - 37 synthetic TIFFs with known pixels (`make_tiff_corpus.py`): SubIFD, main-chain and
    series-per-resolution pyramids; flat strips under five OME schema versions (2003-FC to 2012-06);
    one strip a plane; ImageJ hyperstack; multi-file OME; float, signed, 32-bit, 12-bit, planar and
    interleaved samples; big-endian; palette; 16-bit colour; pyramids that step by 3, by 4 and
    unevenly.
  - Real files: OME's own per-schema samples (2003-FC, 2008-09, 2010-06, 2016-06, BinaryOnly);
    OpenSlide's test slides (Aperio JPEG and both JPEG 2000 variants, Hamamatsu 2009 and S20, Leica
    SCN brightfield and fluorescence, Ventana BIF, Philips sparse and dense, Huron, generic pyramidal
    TIFF, Trestle, a TILED_SPARSE DICOM); Akoya QPTIFF and inForm component data; Imaris classic,
    HDF5 and LZ4; IDR's OME-Zarr 0.1, 0.2, 0.3, 0.4 and 0.5 (sharded) read over https; Xenium
    2.0, 3.0 and 4.0 bundles; Space Ranger 1.0, 1.3, 2.0 and 4.0.1 (Visium HD); AnnData 0.5, 0.7.0,
    0.7.8, 0.11 and 0.13; a SpatialData 0.7.1 store.
- **Each sample through the app's own routes**, in a fresh process and data folder: `/import/inspect`,
  `/import/sample`, `/init_database`, the channel names, one tile at the coarsest level and one at
  full resolution (`probe_e2e.py`), recording the time to the first tile, the channel count and names,
  the pixel size, the geometry, whether anything was built or written, and, for the synthetic files,
  whether the full-resolution tile equals the pixels that were written.
- The harness, corpus recipe and results are in the session scratchpad, not in the repository (the
  corpus is 9 GB).

The local server's code path is what was measured. The website runs the same Python (Pyodide) with a
codec stand-in and reads through the page; what that changes is in "The website" below, and was not
re-measured in a browser.

## What was already right

Opened in place, in under half a second to the first tile, with exact pixels:

- OME-TIFF with SubIFD pyramids (Bio-Formats 6+, 2019 on), **main-chain pyramids** with or without
  the reduced-image flag (Faas `pyramid_assemble`, Ashlar before 1.14 -- the `1_750` layout), and
  **one series per resolution** (Bio-Formats 5).
- BigTIFF and classic TIFF, either byte order; tiles and strips; no compression, LZW with and without
  predictor, Deflate, zstd, PackBits, JPEG (YCbCr), JPEG 2000; planar-separate samples; 12-bit data.
- Aperio SVS (JPEG, JPEG 2000 33003 and 33005), Hamamatsu NDPI (2009 and S20), Philips TIFF (sparse
  and dense), Huron, generic pyramidal TIFF, QPTIFF brightfield.
- Imaris HDF5 (5.5 to 10, gzip and LZ4), all with their channel names.
- DICOM whole-slide: TILED_FULL JPEG (3DHISTECH, 57344 x 60416) in 2 s, JPEG 2000 in 0.4 s; a
  TILED_SPARSE slide opens too, more slowly (below).
- OME-Zarr 0.1, 0.2, 0.3, 0.4 and 0.5 with sharding (2-5 s over https from EBI, all network).
- Xenium 2.0, 3.0 and 4.0 single-stain bundles; Space Ranger 1.0 (headerless positions), 1.3, 2.0 and
  Visium HD 4.0.1.

## What was wrong, and is fixed on `feature/legacy-formats`

| Files | Before | Now |
|---|---|---|
| OME-TIFF under any schema before 2016-06 (every Bio-Formats, MetaMorph, Leica, Olympus export to about 2016) | Channel names lost ("Channel 1, 2, 3") and the viewer's pixel size absent: `ome_types` reads older schemas only with lxml, which is not shipped | Names and pixel size read from any schema, 2003-FC on (`ome_xml.py`); the import no longer pays `ome_types`' import (0.2 s) |
| OME-TIFF with several `Pixels` (one Image per resolution, label and macro Images) | Pixel size of the LAST image recorded: 2.6 um for a 0.325 um image | The first image's, converted to microns whatever unit is stated |
| TIFF with a placeholder resolution (1 per inch or cm, libvips' 1 px/mm, 72 or 96 dpi) | Reported as a pixel size of 25.4 mm, 10 mm, 1 mm | Uncalibrated |
| Akoya / PerkinElmer QPTIFF and inForm `component_data.tif` | Generic channel names | The marker on each page ("PDL1 (Opal 520)") |
| **float32/float64** images (inForm component data, deconvolution and ratio output, ImageJ 32-bit) | Opened black: cast to integers, a 0-1 image became zeros and ones | A per-channel linear map onto 16 bits chosen from the image (`value_range.py`); signed and 32-bit integers no longer wrap |
| **Multi-file OME-TIFF**, one file per channel (Bio-Formats, Micro-Manager) | The first channel alone | All channels, named, when the file of channel 0 is opened |
| OME-TIFF holding **several scenes of one size** (bfconvert of a multi-scene .czi/.lif) | One image with every scene's channels end to end | The first scene |
| **Leica SCN** | The slide's MACRO opened (1616 x 4668 at 16 um), not the scan | The scan (36832 x 38432 at 0.5 um) |
| Leica SCN **fluorescence**, and any whole-slide-suffixed file of grey channel pages | Drawn as one colour picture | Its channels, as a multiplex image |
| Pyramids that **step by 4 or 3** (Leica SCN fluorescence, `bfconvert -pyramid-scale 4`) | Levels served as if they halved: zoomed-out views drawn at the wrong scale | A halving chain laid over the file's own levels |
| Pyramids whose steps are **not whole numbers** (libvips or GDAL overviews of odd sizes) | Seen as a flat image: the coarse levels rebuilt by reading the whole base | The file's own reduced pages used; nothing built |
| **Imaris 3/4 "classic" `.ims`** (a TIFF container; Imaris wrote it until 2007 and still exports it) | "Unable to synchronously open file (file signature not found)" | Opens, with its channel names and pixel size |
| Channels stored as **interleaved samples** of one page | An image as many channels deep as it is tall (5,000 channels, 81 s to import) | Its channels |
| **16-bit colour** TIFF (48-bit RGB) | Did not open ("Cannot handle this data type") | Opens |
| **Ventana `.bif`** | "Nothing Plexora can read here." though documented as supported | Opens (0.03 s) |
| Philips TIFF | No pixel size | Read from its DICOM pixel spacing |
| OME-Zarr from **old bioformats2raw** (8-bit, dtype `">u1"`) and other Zarr v2 metadata zarr-python 3 refuses | Failed: "No Zarr data type found that matches..." | Opens; metadata normalised as it is read, files untouched |
| Zarr v2 with **nested chunks and no `dimension_separator`** (ome-zarr-py on zarr < 2.8) | A black image, no error, in Python and in the page's reader | Read; decided once per array |
| SpatialData written by spatialdata 0.6+ (NGFF 0.5, metadata under `ome`) | Element transforms not found | Found |
| **AnnData 0.5/0.6** `.h5ad` | "'Dataset' object has no attribute 'keys'" | Reads |
| **AnnData 0.7.x** categoricals | Integer codes shown in place of labels | Labels |
| Tab- or semicolon-separated `.tsv`/`.txt`/`.csv` | One column; or refused | Separator read from the header line |
| **Xenium 4.0** multi-stain focus images (`ch0000_dapi.ome.tif`, ...) | DAPI alone, silently | Every stain, named |

Every row has regression tests under `tests/test_legacy_*.py`, `tests/test_delimited_tables.py` and
`tests/test_xenium_focus.py`.

## What is still slow or unread, and why

- **A flat image (no pyramid) larger than 4096 px**: a pyramid is built at import, as before -- 0.6 to
  2 s per 180 MB here, and in proportion to the file. Row-per-strip files (bfconvert's default for a
  decade) build about three times slower than tiled ones. Nothing shows until it is built unless the
  first view applies (at most 64 strips or 48 MB at the smallest level).
- **TILED_SPARSE DICOM**: 7 s to open an 85 MB slide (every instance's header read, then opened again
  by wsidicom).
- **Multi-file OME-TIFF** is assembled only for one plane per channel; z-stacks and time series spread
  over files open one file. Only the file of channel 0 stands for the set, so a folder import still
  lists the other members as images of their own.
- **A SpatialData store with no `global` coordinate system** (one system per field of view) is still
  "aligned by assumption".
- **Zarr label stores** with the legacy v2 metadata are opened by other code (`segmentation_pyramid`,
  `volume/labels`) and were not changed.
- **Leica SCN with several regions**: the largest opens; the others are not offered.
- **Ventana BIF** tiles overlap and are not stitched (tifffile's reading): seams at tile joins.
- **Float images**: the intensity the viewer reports is the mapped value; the map is kept on the
  pyramid (`value_transform`) but not yet shown or inverted anywhere.
- **Trestle** (not a supported format): opens, after a 30 s build, with its overlaps unresolved.
- **Not reachable for testing**: `.mrxs`/`.svslide` (OpenSlide is not installed in the test
  environment), Xenium 1.x bundles (10 GB; only the manifest and tables were read), a real multi-stain
  Xenium 4 run, a fluorescence QPTIFF (2 GB), ImageJ files over 4 GB.
- **Formats Plexora does not read** and that were common in the period: Zeiss CZI and ZVI, Nikon ND2,
  Leica LIF, Olympus VSI/OIB, Hamamatsu VMS, 3DHISTECH beyond OpenSlide's MRXS, Imaris 2 raw. These
  need Bio-Formats or a vendor library; the path is a conversion to OME-TIFF or OME-Zarr.

## The website

The website runs the same Python, so every fix above applies there. What differs:

- The page reads a TIFF's tiles itself only for a level group of 8- or 16-bit pages in a `.tif`/
  `.tiff`; every layout changed here (the value map, the halving chain, multi-file sets, SCN, Imaris
  classic) is refused by that test and served by Python, as before for anything unusual.
- The page's OME-Zarr reader (`node/zarrReader.js`) has the nested-chunk fix too.
- The first view (`services/firstView.js`) reads `.tif`/`.tiff` only, 8/16-bit integers, and the
  first IFD chain: it shows nothing for the layouts above, and the import's progress instead.
- Codecs the website's Python lacks: LERC, 12-bit JPEG, pre-TIFF-6 LZW, JPEG-LS (DICOM). JPEG
  2000 needs the page's OpenJPEG; JPEG XR is decoded by jxrlib compiled to WebAssembly
  (`client/external/jpegxr-0.3.0`, BSD-2, loaded on the first JPEG XR tile by
  `workers/jpegxr.js`), and CZI's zstd and "ZSTD1" by Python 3.14's `compression.zstd`.

None of this was re-measured in a browser.

## Formats added: CZI, ND2, LIF/LOF/XLEF, VSI, OIR, OIB/OIF, NDPIS, DeltaVision, MCD, ZVI, LSM, KFB, MRXS fluorescence

Asked next: support the microscope formats that needed a conversion before (CZI, ND2, LIF, VSI) and
any other common ones. Every one is read in place by a reader that knows its versions, and served
as the same pyramid as every other image (`plexora/server/utils/formats/`: one module per format,
one branch in each place an image is opened -- import, tiles, geometry, names, scale bar,
thumbnails, figure export, z-stacks, mask refusal, file dialogs).

| Format | Reader | Versions checked on real files | Pyramid |
|---|---|---|---|
| Zeiss CZI | czifile 2026.11.8 (BSD-3) | ZEN 2012 z-stack; Axioscan scans in JPEG XR, zstd, zstd hi/lo, 16-bit colour | the scan's own; an acquisition's derived at import |
| Nikon ND2 | nd2 0.12 (BSD-3) | 1.0 JPEG 2000 (2006, 2007 multi-point), 2.1 (2009-10), 3.0 (2017, 2025 float SIM) | derived at import |
| Leica LIF / LOF / XLEF | liffile 2026.11.8 (BSD-3) | LIF 2017 tile scan, 2019, LAS X project; LOF; XLEF with LOF and with TIFF frames; Thunder tile scan placed by stage position | derived at import |
| Olympus VSI | Plexora's own (property tree + ETS) | VS120-era brightfield (48502 x 77578, 9 levels); VS200 brightfield, fluorescence 2-D and 3-D; cellSens 4.1 acquisition (z 18, t 5) | the scan's own |
| Olympus OIR | oirfile 2026.10.22 (BSD-3) | FV3000 2017 (checked against Bio-Formats' OME-TIFF of the same file) | derived at import |
| Olympus OIB / OIF | oiffile 2026.10.15 (BSD-3) | FV1000/FV1200 2022 | derived at import |
| Hamamatsu NDPIS | Plexora's own over the NDPI reader | NanoZoomer 2011 fluorescence set | each channel's own |
| DeltaVision | Plexora's own | 2003 time-lapse, 2004 deconvolved stack | derived at import |
| Hyperion MCD | readimc 0.9.2 (MIT) | Hyperion 2021 (47 channels) | small; none needed |
| Zeiss ZVI | Plexora's own (an OLE2 parser, `cfb.py`, from MS-CFB) | AxioVision CFB v3 and v4, 16-bit, a 13-plane stack | derived at import |
| Zeiss LSM | Plexora's own over tifffile | Pascal, LSM 5/7: z-stacks, a 43-point time series, a 30-channel lambda stack, transmitted light | derived at import |
| KFBio KFB | Plexora's own | KFB 1.7, 20x IHC (24857 x 58040) | the scan's own |
| 3DHISTECH MRXS, fluorescence | Plexora's own (`claims()` takes `SLIDE_TYPE_FLUORESCENCE`; brightfield stays OpenSlide's) | Mirax2 fluorescence, 3 filters as the 3 JPEG components, photos placed by the position buffer | the scan's own |

All 27 samples of the first part, and the 11 of the second (3 ZVI, 5 LSM, 1 KFB, 2 MRXS
fluorescence), open through the app's own routes in about a second (the one flat 11k CZI derives
4.3 MB of coarse levels in 4.4 s); channel names are the file's (dyes, markers, wavelengths),
pixel size and z step are the file's own, z-stacks and time series are served as volumes, and
float, signed and 32-bit data are drawn through the value map.

**A file holding several images** (a LIF project, a VSI's scan areas, CZI scenes, ND2 positions,
MCD acquisitions) is offered as one sample per image, named `<file> - <image>`; a project records
which (`ImageSpec.image_index`, config `imageIndex`, written only for such a file), and every
opener, cache key, the brick and surface caches and data nodes (`resource_id_for(path, index)`)
carry it. Image k > 0 of every multi-image sample (9 files) was imported over the routes and served
image k's own pixels.

**A large z-stack written without a pyramid** (a stitched mosaic in z) is served plane by plane:
the default plane's coarse levels are derived at import, and every other plane resamples them
from its own level above as they are read (`base.build(derive=True)`), so its first zoomed-out
view reads that plane once at full resolution.

The six packages are pure Python and core dependencies (`pyproject.toml`; `uv.lock` updated,
which also moves tifffile to 2026.11.8). The website gets them as a stage loaded on first use
(`plexora/browser/stages.py` "microscope"), with imagecodecs being the website's stand-in (JPEG XR
and zstd above). On the website a `.vsi` brings its `_name_` folder and an `.ndpis` its `.ndpi`
files, picked, dropped or from a folder given to the site (`services/pickedFiles.js`).

**Checked on the website in Chrome 157** (headless, the bundle and static site built locally from
this branch, served with the site's own CSP and COOP/COEP, files picked, dropped and given as a
folder through the real UI): CZI (JPEG XR, zstd, ZSTD1, a 21-plane plate), ND2, LIF, OIB, OIR,
MCD, VSI (2-D, 3-D, brightfield, OS-2), NDPIS, ZVI, LSM, DeltaVision, KFB and fluorescence MRXS all
import and draw with the desktop's names and pixel sizes; pixels are bit-identical to the
desktop's except where the decoder differs (OpenJPEG on VSI: at most 1 grey level on 1.3 % of
pixels; JPEG on KFB: at most 4 levels on 2 %). Image k > 0 of LIF, ND2 and MCD imports there too.
What the browser found and is fixed: nd2 and readimc map the file, which the website's file
system refuses (`[Errno 43]`; both fall back to seek and read); readimc imports imageio and pandas
without declaring them (its own stage, "imc"); the shim ignored `outcolorspace="YCBCR"` (MRXS
channels mixed); an 8-bit slide's full-resolution window scan held the website's Python for up to
150 s (the window of 8-bit samples there is now (0, 255), read off nothing; OS-2's first tile 155.6
-> 6.9 s); "Open <name>" after a multi-sample import threw a page error (pre-existing, reached by
every multi-image file now).

Not done, or not verified:
- **On the website a 16-bit image's window** is still read off its full resolution on the one
  Python thread (a 3-D VSI: 13 s before its first tile), as for DICOM and OME-Zarr there. Not
  checked: Firefox, Safari, the `<input type=file>` fallback, XLEF, LIF time points, reopening
  after a reload, real network timings. Faint 1-px lines at some 1024-px tile edges were seen in
  VSI and ZVI screenshots (not in the data; drawing, not investigated).
- **Philips iSyntax** needs libisyntax (C, BSD-2; `pyisyntax`, MIT): not installed, desktop only.
  A sample is at Zenodo 5037046. **SDPC** has no reader without the vendor's binaries (and GPL x265).
- **Tile scans in ZVI (MosaiX) and LSM** open one tile at a time; no sample of either.
- **Fluorescence MRXS with more than 3 filters, or PNG/BMP-compressed,** stays on OpenSlide;
  seams show where the scanner's stored positions are off (Mirax2-Fluorescence-2, up to 17 px).
- **A vendor file as a layer**, New Dataset from a folder, `plexora.create_project` and
  `ImageSource` read image 0 only.
- **Not tested on files:** an XTi `.mcd`, CZI from ZEN 2009, multi-file CZI, Airyscan raw CZI, OIR
  companion files, an OIF folder, KFB other than 1.7, a compressed or > 4 GB LSM.
