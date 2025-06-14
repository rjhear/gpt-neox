# Copyright (c) 2025, EleutherAI
# This file is based on code by the authors denoted below and has been modified from its original version.
#
# Copyright (c) 2025, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import torch
import torch.nn as nn
import enum
from ..fused_kernels import load_fused_kernels


class ScaledUpperTriangMaskedSoftmax(torch.autograd.Function):
    """
    Fused operation which performs following three operations in sequence
    1. Scale the tensor.
    2. Apply upper triangular mask (typically used in gpt models).
    3. Perform softmax.
    """

    @staticmethod
    def forward(ctx, inputs, scale):
        import scaled_upper_triang_masked_softmax_cuda

        scale_t = torch.tensor([scale])

        softmax_results = scaled_upper_triang_masked_softmax_cuda.forward(
            inputs, scale_t[0]
        )
        ctx.save_for_backward(softmax_results, scale_t)
        return softmax_results

    @staticmethod
    def backward(ctx, output_grads):
        import scaled_upper_triang_masked_softmax_cuda

        softmax_results, scale_t = ctx.saved_tensors

        input_grads = scaled_upper_triang_masked_softmax_cuda.backward(
            output_grads, softmax_results, scale_t[0]
        )
        return input_grads, None


class ScaledMaskedSoftmax(torch.autograd.Function):
    """
    Fused operation which performs following three operations in sequence
    1. Scale the tensor.
    2. Apply the mask.
    3. Perform softmax.
    """

    @staticmethod
    def forward(ctx, inputs, mask, scale):
        import scaled_masked_softmax_cuda

        scale_t = torch.tensor([scale])

        softmax_results = scaled_masked_softmax_cuda.forward(inputs, mask, scale_t[0])
        ctx.save_for_backward(softmax_results, scale_t)
        return softmax_results

    @staticmethod
    def backward(ctx, output_grads):
        import scaled_masked_softmax_cuda

        softmax_results, scale_t = ctx.saved_tensors

        input_grads = scaled_masked_softmax_cuda.backward(
            output_grads, softmax_results, scale_t[0]
        )
        return input_grads, None, None


class SoftmaxFusionTypes(enum.Enum):
    upper_triang = 1  # causal mask
    general = 2  # general mask
    none = 3  # no fusion


class FusedScaleMaskSoftmax(nn.Module):
    """
    fused operation: scaling + mask + softmax
    Arguments:
        input_in_fp16: flag to indicate if input in fp16 data format.
        input_in_bf16: flag to indicate if input in bf16 data format.
        fusion_type: type of fusion to perform, should be either upper_triang, general or none. None will perform a regular torch softmax.
        mask_func: mask function to be applied.
        softmax_in_fp32: if true, softmax in performed at fp32 precision.
        scale: scaling factor used in input tensor scaling.

    """

    def __init__(
        self,
        input_in_fp16,
        input_in_bf16,
        fusion_type,
        mask_func,
        softmax_in_fp32,
        scale,
    ):
        super().__init__()
        self.input_in_fp16 = input_in_fp16
        self.input_in_bf16 = input_in_bf16
        self.input_in_float16 = self.input_in_fp16 or self.input_in_bf16

        assert fusion_type in [
            SoftmaxFusionTypes.upper_triang,
            SoftmaxFusionTypes.general,
            SoftmaxFusionTypes.none,
        ], f"Invalid fusion type {fusion_type}"

        if fusion_type != SoftmaxFusionTypes.none:
            load_fused_kernels()  # check fused kernels are installed

        self.upper_triang_mask_fusion = fusion_type == SoftmaxFusionTypes.upper_triang
        self.general_mask_fusion = fusion_type == SoftmaxFusionTypes.general
        self.fusion = fusion_type != SoftmaxFusionTypes.none

        self.mask_func = mask_func
        self.softmax_in_fp32 = softmax_in_fp32
        self.scale = scale

        assert (
            self.scale is None or softmax_in_fp32
        ), "softmax should be in fp32 when scaled"

    def forward(self, input, mask):
        # [b, np, sq, sk]
        assert input.dim() == 4
        if self.is_kernel_available(mask, *input.size()):
            return self.forward_fused_softmax(input, mask)
        else:
            return self.forward_torch_softmax(input, mask)

    def is_kernel_available(self, mask, b, np, sq, sk):
        attn_batches = b * np

        if (
            self.fusion  # user wants to fuse
            and self.input_in_float16  # input must be fp16
            and mask is not None  # mask tensor must not be None
            and 16 < sk <= 2048  # sk must be 16 ~ 2048
            and sq % 4 == 0  # sq must be divisor of 4
            and attn_batches % 4 == 0  # np * b must be divisor of 4
        ):
            if 0 <= sk <= 2048:
                batch_per_block = self.get_batch_per_block(sq, sk, b, np)

                if self.upper_triang_mask_fusion:
                    if attn_batches % batch_per_block == 0:
                        return True
                else:
                    if sq % batch_per_block == 0:
                        return True
        return False

    def forward_fused_softmax(self, input, mask):
        b, np, sq, sk = input.size()
        scale = self.scale if self.scale is not None else 1.0
        if self.upper_triang_mask_fusion:
            assert sq == sk, "causal mask is only for self attention"

            # input is 3D tensor (attn_batches, sq, sk)
            input = input.view(-1, sq, sk)
            probs = ScaledUpperTriangMaskedSoftmax.apply(input, scale)
            return probs.view(b, np, sq, sk)
        else:
            # input is 4D tensor (b, np, sq, sk)
            return ScaledMaskedSoftmax.apply(input, mask, scale)

    def forward_torch_softmax(self, input, mask):
        if self.input_in_float16 and self.softmax_in_fp32:
            input = input.float()

        if self.scale is not None:
            input = input * self.scale
        mask_output = self.mask_func(input, mask) if mask is not None else input
        probs = torch.nn.Softmax(dim=-1)(mask_output)

        if self.input_in_float16 and self.softmax_in_fp32:
            if self.input_in_fp16:
                probs = probs.half()
            else:
                probs = probs.bfloat16()

        return probs

    @staticmethod
    def get_batch_per_block(sq, sk, b, np):
        import scaled_masked_softmax_cuda

        return scaled_masked_softmax_cuda.get_batch_per_block(sq, sk, b, np)
