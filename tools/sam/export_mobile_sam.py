"""Export MobileSAM to the two ONNX files magic select runs on.

Developer tool, run once per model version; never imported by Plexora. It
needs torch, timm, onnx and a MobileSAM checkout -- none of which Plexora
depends on at runtime:

    git clone https://github.com/ChaoningZhang/MobileSAM.git
    pip install torch timm onnx onnxruntime
    python tools/sam/export_mobile_sam.py --repo MobileSAM --out build/segment

It writes ``mobile_sam_encoder.onnx`` and ``mobile_sam_decoder.onnx`` and
prints the ``MODEL_FILES`` entries (sha256 and size) to paste into
``plexora/vision/sam_weights.py``. Upload both files to the release named by
``sam_weights.RELEASE_BASE`` in the public ``nirmallab/plexora-models``
repository (``gh release create <tag> -R nirmallab/plexora-models FILES``);
changed files get a new tag and a bumped ``MODEL_VERSION``, never a re-upload.

The layout (``sam_lowres_v1``) differs from the upstream export on purpose:

* the encoder takes the already normalised, zero-padded 1x3x1024x1024 tensor,
  so preprocessing is one vectorised numpy/OpenCV pass on the server and the
  graph has a fixed shape an accelerator can compile once;
* the decoder returns only the IoU predictions and the 256x256 low-resolution
  logits of all four masks. Plexora picks one and upsamples just that one
  with OpenCV instead of letting the graph upsample four masks it discards;
* the prompt batch axis is dynamic: B prompt sets against one embedding
  decode in a single call (the image embedding is repeated inside the graph).
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import warnings
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, type=Path, help="MobileSAM checkout")
    parser.add_argument("--checkpoint", type=Path, default=None,
                        help="default: <repo>/weights/mobile_sam.pt")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args(argv)

    sys.path.insert(0, str(args.repo.resolve()))
    import torch
    from mobile_sam import sam_model_registry
    from mobile_sam.utils.onnx import SamOnnxModel

    checkpoint = args.checkpoint or args.repo / "weights" / "mobile_sam.pt"
    sam = sam_model_registry["vit_t"](checkpoint=str(checkpoint)).eval()
    args.out.mkdir(parents=True, exist_ok=True)

    class Encoder(torch.nn.Module):
        def __init__(self, model):
            super().__init__()
            self.image_encoder = model.image_encoder

        def forward(self, image):
            return self.image_encoder(image)

    class Decoder(SamOnnxModel):
        """Upstream decoder minus the upsampling: IoU + low-res logits."""

        @torch.no_grad()
        def forward(self, image_embeddings, point_coords, point_labels,
                    mask_input, has_mask_input):
            sparse = self._embed_points(point_coords, point_labels)
            dense = self._embed_masks(mask_input, has_mask_input)
            masks, scores = self.model.mask_decoder.predict_masks(
                image_embeddings=image_embeddings,
                image_pe=self.model.prompt_encoder.get_dense_pe(),
                sparse_prompt_embeddings=sparse,
                dense_prompt_embeddings=dense,
            )
            return scores, masks

    encoder_path = args.out / "mobile_sam_encoder.onnx"
    decoder_path = args.out / "mobile_sam_decoder.onnx"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        torch.onnx.export(
            Encoder(sam), torch.randn(1, 3, 1024, 1024), str(encoder_path),
            input_names=["image"], output_names=["image_embeddings"],
            opset_version=args.opset, do_constant_folding=True, dynamo=False,
        )
        embed_dim = sam.prompt_encoder.embed_dim
        side = sam.prompt_encoder.image_embedding_size
        mask_side = [4 * side[0], 4 * side[1]]
        dummy = (
            torch.randn(1, embed_dim, *side),
            torch.randint(0, 1024, (1, 5, 2), dtype=torch.float),
            torch.randint(0, 4, (1, 5), dtype=torch.float),
            torch.randn(1, 1, *mask_side),
            torch.tensor([1], dtype=torch.float),
        )
        torch.onnx.export(
            Decoder(sam, return_single_mask=False), dummy, str(decoder_path),
            input_names=["image_embeddings", "point_coords", "point_labels",
                         "mask_input", "has_mask_input"],
            output_names=["iou_predictions", "low_res_masks"],
            dynamic_axes={
                "point_coords": {0: "batch", 1: "num_points"},
                "point_labels": {0: "batch", 1: "num_points"},
                "mask_input": {0: "batch"},
                "iou_predictions": {0: "batch"},
                "low_res_masks": {0: "batch"},
            },
            opset_version=args.opset, do_constant_folding=True, dynamo=False,
        )

    print("MODEL_FILES = {")
    for path, role in ((encoder_path, "encoder"), (decoder_path, "decoder")):
        print(f'    "{path.name}": {{"role": "{role}", '
              f'"sha256": "{_sha256(path)}", "bytes": {path.stat().st_size}}},')
    print("}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
