"""The dual-branch contrast model [torch].

Patch branch: a small depthwise-separable CNN over each of the N patches
(log-scaled canonical intensities, never rescaled per patch), plus a learned
embedding of the patch's sampling category, pooled by masked attention and a
masked mean. Histogram branch: an MLP over the 256-bin canonical histogram,
the quantiles and the domain vector with its missing-mask.

Heads (canonical units):
    min_norm   = sigmoid(a)
    span       = softplus(b) + eps
    max_norm   = min(min_norm + span, 1 + margin)
    confidence = sigmoid(c)        -- P(window within tolerance)
    abstain    = sigmoid(d)        -- P(nothing worth showing)
`embedding()` is exposed for the Phase 2 pairwise-ranking head; unused now.
"""

from __future__ import annotations

import torch
from torch import nn
from torch.nn import functional as F

from biological_autocontrast.features import N_BINS, N_DOM, QUANTILES
from biological_autocontrast.sampling import CATEGORIES

MAX_PARAMS = 500_000
EPS = 1e-4


class DSBlock(nn.Module):
    def __init__(self, cin, cout, stride=2):
        super().__init__()
        self.dw = nn.Conv2d(cin, cin, 3, stride, 1, groups=cin, bias=False)
        self.bn1 = nn.BatchNorm2d(cin)
        self.pw = nn.Conv2d(cin, cout, 1, bias=False)
        self.bn2 = nn.BatchNorm2d(cout)

    def forward(self, x):
        x = F.gelu(self.bn1(self.dw(x)))
        return F.gelu(self.bn2(self.pw(x)))


class PatchEncoder(nn.Module):
    def __init__(self, widths=(16, 32, 64, 96)):
        super().__init__()
        self.stem = nn.Sequential(nn.Conv2d(1, widths[0], 3, 2, 1, bias=False),
                                  nn.BatchNorm2d(widths[0]), nn.GELU())
        blocks, cin = [], widths[0]
        for cout in widths[1:]:
            blocks += [DSBlock(cin, cout, 2), DSBlock(cout, cout, 1)]
            cin = cout
        blocks.append(DSBlock(cin, cin, 2))
        self.blocks = nn.Sequential(*blocks)
        self.out_dim = cin

    def forward(self, x):
        x = self.blocks(self.stem(x))
        mean = x.mean(dim=(2, 3))
        top = x.amax(dim=(2, 3))
        return mean + top


class ContrastNet(nn.Module):
    def __init__(self, *, use_patches=True, use_hist=True, hidden=128, margin=0.0):
        super().__init__()
        self.use_patches, self.use_hist, self.margin = use_patches, use_hist, margin
        self.encoder = PatchEncoder()
        d = self.encoder.out_dim
        self.category = nn.Embedding(len(CATEGORIES), d)
        self.attn = nn.Sequential(nn.Linear(d, 64), nn.Tanh(), nn.Linear(64, 1))
        side = N_BINS + len(QUANTILES) + 2 * N_DOM
        self.hist_mlp = nn.Sequential(nn.Linear(side, 128), nn.GELU(), nn.Dropout(0.1),
                                      nn.Linear(128, 64), nn.GELU())
        self.fuse = nn.Sequential(nn.Linear(2 * d + 64, hidden), nn.GELU(), nn.Dropout(0.1),
                                  nn.Linear(hidden, hidden), nn.GELU())
        self.heads = nn.Linear(hidden, 4)
        self.patch_dim = d

    def embedding(self, patches, patch_cat, patch_mask, hist, quant, dom, dom_mask):
        batch, n = patch_mask.shape
        if self.use_patches:
            x = patches.reshape(batch * n, 1, patches.shape[-2], patches.shape[-1])
            x = torch.log10(torch.clamp(x, min=0.0) + EPS) / 4.0 + 1.0
            h = self.encoder(x).reshape(batch, n, -1) + self.category(patch_cat)
            score = self.attn(h).squeeze(-1)
            score = score.masked_fill(patch_mask < 0.5, -1e4)
            weight = torch.softmax(score, dim=1) * patch_mask
            weight = weight / (weight.sum(dim=1, keepdim=True) + 1e-6)
            pooled = (weight.unsqueeze(-1) * h).sum(dim=1)
            mean = (patch_mask.unsqueeze(-1) * h).sum(dim=1) / (patch_mask.sum(dim=1, keepdim=True) + 1e-6)
            patch_vec = torch.cat([pooled, mean], dim=1)
        else:
            patch_vec = hist.new_zeros(batch, 2 * self.patch_dim)
        if self.use_hist:
            side = torch.cat([torch.sqrt(torch.clamp(hist, min=0.0)), quant, dom * dom_mask, dom_mask], dim=1)
            hist_vec = self.hist_mlp(side)
        else:
            hist_vec = hist.new_zeros(batch, 64)
        return self.fuse(torch.cat([patch_vec, hist_vec], dim=1))

    def forward(self, patches, patch_cat, patch_mask, hist, quant, dom, dom_mask):
        z = self.embedding(patches, patch_cat, patch_mask, hist, quant, dom, dom_mask)
        a, b, c, d = self.heads(z).unbind(dim=1)
        min_norm = torch.sigmoid(a)
        span = F.softplus(b) + EPS
        max_norm = torch.clamp(min_norm + span, max=1.0 + self.margin)
        return min_norm, max_norm, torch.sigmoid(c), torch.sigmoid(d), span


def param_count(model) -> int:
    return sum(p.numel() for p in model.parameters())


class _Export(nn.Module):
    def __init__(self, net):
        super().__init__()
        self.net = net

    def forward(self, patches, patch_cat, patch_mask, hist, quant, dom, dom_mask):
        min_norm, max_norm, confidence, abstain, _span = self.net(
            patches, patch_cat, patch_mask, hist, quant, dom, dom_mask)
        return min_norm, max_norm, confidence, abstain


def export_onnx(net, path, *, n_patches, patch_size, metadata=None, check=True):
    """ONNX opset 17 with dynamic batch and patch count; verified against torch."""
    import numpy as np

    from biological_autocontrast.model import INPUTS, OUTPUTS

    net = net.eval().cpu()
    wrapper = _Export(net).eval()
    g = torch.Generator().manual_seed(0)
    dummy = (torch.rand(2, n_patches, 1, patch_size, patch_size, generator=g) * 0.3,
             torch.randint(0, len(CATEGORIES), (2, n_patches), generator=g),
             torch.ones(2, n_patches), torch.softmax(torch.rand(2, N_BINS, generator=g), 1),
             torch.rand(2, len(QUANTILES), generator=g) - 1, torch.rand(2, N_DOM, generator=g),
             torch.ones(2, N_DOM))
    dynamic = {"patches": {0: "batch", 1: "n"}, "patch_cat": {0: "batch", 1: "n"},
               "patch_mask": {0: "batch", 1: "n"}, "hist": {0: "batch"}, "quant": {0: "batch"},
               "dom": {0: "batch"}, "dom_mask": {0: "batch"}}
    dynamic.update({name: {0: "batch"} for name in OUTPUTS})
    torch.onnx.export(wrapper, dummy, str(path), input_names=list(INPUTS),
                      output_names=list(OUTPUTS), dynamic_axes=dynamic, opset_version=17,
                      dynamo=False)
    import onnx

    model = onnx.load(str(path))
    for key, value in (metadata or {}).items():
        entry = model.metadata_props.add()
        entry.key, entry.value = str(key), str(value)
    onnx.save(model, str(path))
    if check:
        import onnxruntime as ort

        session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        feeds = {name: t.numpy() for name, t in zip(INPUTS, dummy)}
        got = session.run(list(OUTPUTS), feeds)
        with torch.no_grad():
            want = [t.numpy() for t in wrapper(*dummy)]
        for name, a, b in zip(OUTPUTS, got, want):
            if not np.allclose(a, b, atol=1e-4):
                raise AssertionError(f"ONNX output {name} differs from torch: {a} vs {b}")
    return path
