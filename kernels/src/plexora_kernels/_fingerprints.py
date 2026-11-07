"""Which Plexora kernel sources these ports follow (written by
kernels/tools/fingerprints.py; do not edit by hand)."""

#: The Plexora commit the fingerprints were read from.
PORTED_FROM = '35ec13ecc2e9'

#: {module: {kernel: fingerprint}} (plexora.server.utils.jit.kernel_fingerprints).
FINGERPRINTS = {
    'plexora.plugins.gating.server.autogate.kernels': {
        '_count': '1f19b93bb10edce74e38',
    },
    'plexora.plugins.qc.server.segqc.kernels': {
        'count_pairs': '303eb17932b67d7ebe64',
        'fill_pairs': '334701ce5c0eda6f34a8',
        'label_stats': '26efdbd1e55eaea65cc5',
    },
    'plexora.server.surface.mesh': {
        '_adjacency': '76c1756a912dd3725b35',
        '_face_normals_into': '75c989565be60ebcc4a7',
        '_object_moments': 'd92a85c875b66030083e',
        '_object_sums': 'e615db7caf9e3b7aaa44',
        '_signed_volumes': 'dc71a5a8669b9a3cfbcf',
        '_umbrella_step': 'd8c56fc7334370b97f2e',
    },
    'plexora.server.surface.nets': {
        '_corners': '7a37ea8644b1049244cc',
        '_count': 'b58932c9be2785c7a394',
        '_distinct': '896612aca73e6c313d6d',
        '_fill': '0618bfaeede8c748177e',
        '_pool_runs': '093627f5553c650efb60',
        '_triangulate': 'a83f2ea7fcca18e457af',
    },
    'plexora.server.utils.cell_features': {
        '_background_blocks': 'd35e83366ec533d7a465',
        '_run_labels': '6b0736c1650bbfa92b69',
        '_tile_kernel': '81a38c9955f2af74ddc3',
    },
    'plexora.server.utils.label_kernels': {
        '_bboxes': 'ed6cdca36a194c82f4be',
        '_boundary': '5b436f9249ab30c69528',
    },
    'plexora.server.volume.measure': {
        '_bbox_kernel': 'cddaeb4b66a0de79cf03',
    },
}
